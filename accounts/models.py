from django.db import IntegrityError, models, transaction
from django.contrib.auth.models import User
import uuid

from datetime import datetime, timedelta

from django.core.exceptions import ValidationError
from django.core.mail import send_mail

from core.emails import send_templated_email
from django.utils.http import url_has_allowed_host_and_scheme

from clubManager import settings
from core.models import ServerSettings, UserProfile
from django.db.models.query import QuerySet
from django.utils import timezone


class RegistrationError(Exception):
    """Base exception for a self-service account registration failure."""


class RegistrationAlreadySentError(RegistrationError):
    """A valid registration email has already been sent for this Net ID."""


class AccountAlreadyExistsError(RegistrationError):
    """The Net ID already belongs to a completed account."""


class DiscordAccountAlreadyLinkedError(RegistrationError):
    """The Discord account is already associated with another user."""


class RegistrationEmailError(RegistrationError):
    """The registration email could not be delivered."""


def get_registration_expiration() -> datetime:
    return timezone.now() + timedelta(hours=24)


def validate_after_registration_redirect_destination(value: str | None) -> None:
    """Allow only paths that remain on this site after registration."""
    if not value:
        return

    if not (value.startswith("/") and url_has_allowed_host_and_scheme(value, allowed_hosts=set(), require_https=True)):
        raise ValidationError("After-registration redirect destinations must be site-relative paths.")


class AccountLink(models.Model):
    SOCIAL_CHOICES = (("discord", "Discord"), ("other", "Other"))

    uuid = models.UUIDField("UUID", default=uuid.uuid4, editable=False, unique=True, primary_key=True)
    date_created = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    link_type = models.CharField(max_length=200, choices=SOCIAL_CHOICES)
    social_id = models.CharField(max_length=200)

    def __str__(self):
        return f"{self.link_type} - {self.user.first_name} {self.user.last_name}"


class UserStub(models.Model):
    """
    Tracks a self-service registration that has been started but not finished.

    Deliberately does *not* create a `User` row. A `User` existing means "this is a
    real account", and the rest of the app relies on that: every query that lists or
    looks up users would otherwise need to learn to skip half-registered placeholders.
    The Net ID is held here instead, and the unique constraint on it is what reserves
    the name until the registration is either completed or expires.
    """

    user_registration_key = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, primary_key=True)
    net_id = models.CharField(max_length=150, unique=True)
    pending_discord_id = models.CharField(max_length=200, null=True, blank=True, unique=True)
    after_registration_redirect_destination = models.TextField(
        blank=True, null=True, validators=[validate_after_registration_redirect_destination]
    )

    expires_at = models.DateTimeField(default=get_registration_expiration)

    def __str__(self):
        return f"registration for {self.net_id}"

    @staticmethod
    def _get_deletion_candidates() -> QuerySet["UserStub"]:
        return UserStub.objects.filter(expires_at__lte=timezone.now())

    @staticmethod
    def purge_deletion_candidates():
        """
        Drop expired registrations.

        This only ever deletes `UserStub` rows. `UserStub` holds no reference to
        `User` in either direction, so an abandoned registration cannot take an
        account - or the payments and reservations hanging off it - down with it.
        """
        with transaction.atomic():
            return UserStub._get_deletion_candidates().delete()

    @staticmethod
    def create(
        net_id: str,
        after_registration_redirect_destination: str | None,
        discord_user_id: str | None = None,
    ):
        validate_after_registration_redirect_destination(after_registration_redirect_destination)
        net_id = net_id.lower()
        _ = UserStub.purge_deletion_candidates()

        with transaction.atomic():
            if User.objects.filter(username=net_id).exists():
                raise AccountAlreadyExistsError

            if UserStub.objects.filter(net_id=net_id).exists():
                raise RegistrationAlreadySentError

            if discord_user_id is not None and UserProfile.objects.filter(discord_id=discord_user_id).exists():
                raise DiscordAccountAlreadyLinkedError

            try:
                with transaction.atomic():
                    return UserStub.objects.create(
                        net_id=net_id,
                        pending_discord_id=discord_user_id,
                        after_registration_redirect_destination=after_registration_redirect_destination,
                    )
            except IntegrityError as error:
                # Lost a race against a concurrent registration for the same Net ID or
                # Discord account. Work out which unique constraint gave way.
                if UserStub.objects.filter(net_id=net_id).exists():
                    raise RegistrationAlreadySentError from error
                raise DiscordAccountAlreadyLinkedError from error

    def build_user(self) -> User:
        """
        An unsaved `User` for this registration, for a form to fill in before `activate`.

        Nothing is written to the database until `activate` is called, so abandoning the
        form here leaves no trace.
        """
        return User(username=self.net_id, is_active=True)

    def activate(self, user: User) -> str | None:
        """
        Turn this registration into a real account and return where to send the user next.

        `user` is the instance from `build_user` with names and password already set.
        Creating the `User` and dropping this stub happen in one transaction, so a Net ID
        is never held by both a stub and an account.
        """
        if user.username != self.net_id:
            raise ValueError(f"Cannot activate the registration for {self.net_id} with a User for {user.username}.")

        user.is_active = True

        with transaction.atomic():
            try:
                with transaction.atomic():
                    user.save()
            except IntegrityError as error:
                # Someone else claimed this Net ID while the form was open - the real
                # account wins, and this registration is now moot.
                raise AccountAlreadyExistsError from error

            if self.pending_discord_id is not None:
                profile, _ = UserProfile.objects.get_or_create(user=user)
                profile.discord_id = self.pending_discord_id
                try:
                    with transaction.atomic():
                        profile.save()
                except IntegrityError as error:
                    raise DiscordAccountAlreadyLinkedError from error

            UserStub.objects.filter(pk=self.pk).delete()

        return self.after_registration_redirect_destination

    def email_address(self) -> str:
        return f"{self.net_id}@utdallas.edu"

    def get_registration_url(self):
        return f"{settings.PUBLIC_URL}/accounts/register/continue/{self.user_registration_key}"

    @staticmethod
    def notify(user_stub: "UserStub"):
        try:
            server_settings = ServerSettings.objects.first()
            if server_settings is None:
                raise Exception("Server settings not found")

            if settings.FEATURE_FLAGS["NEW_TRANSACTIONAL_EMAIL_TEMPLATES"]:
                send_templated_email(
                    "email/messages/account_registration.html",
                    {
                        "registration_url": user_stub.get_registration_url(),
                        "expires_at": user_stub.expires_at.strftime("%m-%d-%Y %H:%M:%S"),
                        "request_url": f"{settings.PUBLIC_URL}/accounts/register",
                    },
                    [user_stub.email_address()],
                )
                return

            send_mail(
                f"Create your {server_settings.organization_name} account",
                f"""
Hey there!

Let's finish creating your {server_settings.organization_name} account! Just click this link to finish up: {user_stub.get_registration_url()}

This link expires at {user_stub.expires_at.strftime("%m-%d-%Y %H:%M:%S")}. To request a new one, click this link: {f"{settings.PUBLIC_URL}/accounts/register"}

If this was not you, you can safely ignore this email.

Thanks!
""",
                settings.EMAIL_FROM,
                [user_stub.email_address()],
                fail_silently=False,
                html_message=f"""
<h2>Hey there!</h2>

<p>Let's finish creating your {server_settings.organization_name} account! Click the button below or use the link to finish up.</p>

<a href="{user_stub.get_registration_url()}"><button style="border: solid #950000 3px;padding: 1em; border-radius: 10px; background-color:#bf1e2e; color: white;"><strong>Create Account</strong></button></a>

<br><br><a href="{user_stub.get_registration_url()}">{user_stub.get_registration_url()}</a>

<p>This link expires at {user_stub.expires_at.strftime("%m-%d-%Y %H:%M:%S")}. To request a new one, <a href="{settings.PUBLIC_URL}/accounts/register">click here</a>.</p>

<p>If this was not you, you can safely ignore this email.</p>

<p>Thanks!</p>
""",
            )
        except Exception as error:
            raise RegistrationEmailError from error
