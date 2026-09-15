from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core import mail
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import (
    AccountAlreadyExistsError,
    DiscordAccountAlreadyLinkedError,
    RegistrationAlreadySentError,
    UserStub,
    validate_after_registration_redirect_destination,
)
from core.models import ServerSettings, UserProfile
from payments.models import Payment, Product, Term


class RegistrationTestCase(TestCase):
    """
    Base for the UserStub tests.

    Saving a `UserProfile` that has a `discord_id` fires the member-role signal, which
    needs a current `Term` to resolve membership and would otherwise call out to the bot
    API. Give it a real Term and stub the outbound call so these tests exercise the
    actual signal path. Creating a `User` also kicks off a NetID -> major directory
    lookup, which has no business making a real request here.
    """

    def setUp(self):
        super().setUp()
        product = Product.objects.create(name="Dues", amount_cents=2000, max_purchases_per_user=1)
        today = timezone.now().date()
        Term.objects.create(
            name="Test Term",
            start_date=today - timedelta(days=30),
            end_date=today + timedelta(days=30),
            product=product,
        )
        for target, replacement in (
            ("core.signals.handlers.add_member_role", AsyncMock(return_value=True)),
            ("core.signals.handlers.get_major_from_netid", MagicMock(return_value=None)),
        ):
            patcher = patch(target, new=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def create_user(self, username):
        return User.objects.create(username=username)


class UserStubRedirectDestinationTests(RegistrationTestCase):
    def test_user_stub_allows_no_redirect_destination(self):
        user_stub = UserStub.create("noredirect", None)

        self.assertIsNone(user_stub.after_registration_redirect_destination)

    def test_allows_site_relative_destination(self):
        validate_after_registration_redirect_destination("/pay/whatever?invoice=123")

    def test_rejects_external_redirect_destinations(self):
        unsafe_destinations = [
            "https://attacker.example/pay/whatever",
            "//attacker.example/pay/whatever",
            "/\\attacker.example/pay/whatever",
            "javascript:alert(1)",
        ]

        for destination in unsafe_destinations:
            with self.subTest(destination=destination):
                with self.assertRaises(ValidationError):
                    validate_after_registration_redirect_destination(destination)


class UserStubCreateTests(RegistrationTestCase):
    def test_create_does_not_create_a_user(self):
        """The whole point of holding the Net ID on the stub - see UserStub's docstring."""
        user_stub = UserStub.create("abc123456", None)

        self.assertEqual(user_stub.net_id, "abc123456")
        self.assertFalse(User.objects.filter(username="abc123456").exists())

    def test_create_normalizes_net_id_case(self):
        UserStub.create("ABC123456", None)

        self.assertTrue(UserStub.objects.filter(net_id="abc123456").exists())
        with self.assertRaises(RegistrationAlreadySentError):
            UserStub.create("abc123456", None)

    def test_create_rejects_net_id_that_already_has_an_account(self):
        self.create_user("abc123456")

        with self.assertRaises(AccountAlreadyExistsError):
            UserStub.create("abc123456", None)

    def test_create_rejects_a_second_registration_for_the_same_net_id(self):
        UserStub.create("abc123456", None)

        with self.assertRaises(RegistrationAlreadySentError):
            UserStub.create("abc123456", None)

    def test_create_rejects_a_discord_account_that_is_already_linked(self):
        existing = self.create_user("xyz999999")
        profile = existing.userprofile
        profile.discord_id = "123456789012345678"
        profile.save()

        with self.assertRaises(DiscordAccountAlreadyLinkedError):
            UserStub.create("abc123456", None, discord_user_id="123456789012345678")

    def test_create_rejects_a_discord_account_with_a_pending_registration(self):
        UserStub.create("abc123456", None, discord_user_id="123456789012345678")

        with self.assertRaises(DiscordAccountAlreadyLinkedError):
            UserStub.create("def123456", None, discord_user_id="123456789012345678")

    def test_create_holds_the_discord_id_without_touching_any_profile(self):
        user_stub = UserStub.create("abc123456", None, discord_user_id="123456789012345678")

        self.assertEqual(user_stub.pending_discord_id, "123456789012345678")
        self.assertFalse(UserProfile.objects.filter(discord_id="123456789012345678").exists())

    def test_management_command_accepts_discord_user_id(self):
        call_command("create_user_stub", "commandstub", "--discord-user-id", "987654321098765432")

        user_stub = UserStub.objects.get(net_id="commandstub")
        self.assertEqual(user_stub.pending_discord_id, "987654321098765432")


class UserStubActivateTests(RegistrationTestCase):
    def activate(self, user_stub, *, password="a-very-good-password"):
        user = user_stub.build_user()
        user.first_name = "Comet"
        user.last_name = "Robotics"
        user.set_password(password)
        return user_stub.activate(user)

    def test_build_user_writes_nothing(self):
        user_stub = UserStub.create("abc123456", None)

        user_stub.build_user()

        self.assertFalse(User.objects.filter(username="abc123456").exists())

    def test_activate_creates_an_active_account_and_drops_the_stub(self):
        user_stub = UserStub.create("abc123456", "/payments/")

        redirect_destination = self.activate(user_stub)

        self.assertEqual(redirect_destination, "/payments/")
        user = User.objects.get(username="abc123456")
        self.assertTrue(user.is_active)
        self.assertTrue(user.check_password("a-very-good-password"))
        self.assertFalse(UserStub.objects.filter(pk=user_stub.pk).exists())

    def test_activate_moves_a_pending_discord_id_onto_the_profile(self):
        user_stub = UserStub.create("abc123456", None, discord_user_id="123456789012345678")

        self.activate(user_stub)

        user = User.objects.get(username="abc123456")
        user.userprofile.refresh_from_db()
        self.assertEqual(user.userprofile.discord_id, "123456789012345678")

    def test_activate_rejects_a_user_for_a_different_net_id(self):
        user_stub = UserStub.create("abc123456", None)

        with self.assertRaises(ValueError):
            user_stub.activate(User(username="def123456"))

    def test_activate_fails_if_the_net_id_was_claimed_in_the_meantime(self):
        user_stub = UserStub.create("abc123456", None)
        self.create_user("abc123456")

        with self.assertRaises(AccountAlreadyExistsError):
            self.activate(user_stub)

        self.assertTrue(UserStub.objects.filter(pk=user_stub.pk).exists())


class UserStubPurgeTests(RegistrationTestCase):
    def expire(self, user_stub):
        UserStub.objects.filter(pk=user_stub.pk).update(expires_at=timezone.now() - timedelta(minutes=1))

    def test_purge_deletes_expired_stubs(self):
        user_stub = UserStub.create("abc123456", None)
        self.expire(user_stub)

        UserStub.purge_deletion_candidates()

        self.assertFalse(UserStub.objects.filter(pk=user_stub.pk).exists())

    def test_purge_keeps_unexpired_stubs(self):
        user_stub = UserStub.create("abc123456", None)

        UserStub.purge_deletion_candidates()

        self.assertTrue(UserStub.objects.filter(pk=user_stub.pk).exists())

    def test_purge_never_deletes_real_accounts_or_their_payments(self):
        """
        An abandoned registration must not be able to take an account down with it.

        A stub used to own a disabled `User`, so purging it cascaded into that user's
        `Payment` and `Reservation` rows - deleting a settled dues payment while the
        charge stood. `UserStub` no longer references `User` at all, so a Net ID that
        also has a real account behind it is unaffected by a purge.
        """
        user = self.create_user("abc123456")
        payment = Payment.objects.create(user=user, amount_cents=2000)

        user_stub = UserStub.create("def123456", None)
        self.expire(user_stub)
        UserStub.purge_deletion_candidates()

        self.assertFalse(UserStub.objects.filter(pk=user_stub.pk).exists())
        self.assertTrue(User.objects.filter(pk=user.pk).exists())
        self.assertTrue(Payment.objects.filter(pk=payment.pk).exists())

    def test_purge_reports_only_user_stub_deletions(self):
        self.expire(UserStub.create("abc123456", None))

        deleted_count, deleted_objects = UserStub.purge_deletion_candidates()

        self.assertEqual(deleted_count, 1)
        self.assertEqual(deleted_objects, {"accounts.UserStub": 1})

    def test_create_purges_an_expired_stub_for_the_same_net_id(self):
        self.expire(UserStub.create("abc123456", None))

        user_stub = UserStub.create("abc123456", None)

        self.assertEqual(UserStub.objects.filter(net_id="abc123456").count(), 1)
        self.assertEqual(user_stub.net_id, "abc123456")


class UserStubManagementCommandTests(RegistrationTestCase):
    def test_purge_command_reports_expired_stubs(self):
        user_stub = UserStub.create("abc123456", None)
        UserStub.objects.filter(pk=user_stub.pk).update(expires_at=timezone.now() - timedelta(minutes=1))

        call_command("purge_expired_user_stubs")

        self.assertFalse(UserStub.objects.exists())


class UserStubNotifyTests(RegistrationTestCase):
    """
    Both sides of NEW_TRANSACTIONAL_EMAIL_TEMPLATES.

    `accounts.models` reads the flag off the settings *module* rather than
    `django.conf.settings`, so `override_settings` does not reach it - patch the dict.
    """

    def setUp(self):
        super().setUp()
        ServerSettings.objects.get_or_create(defaults={"organization_name": "Comet Robotics"})

    def notify(self, *, templated):
        user_stub = UserStub.create("abc123456", None)
        with patch.dict("clubManager.settings.FEATURE_FLAGS", {"NEW_TRANSACTIONAL_EMAIL_TEMPLATES": templated}):
            UserStub.notify(user_stub)
        self.assertEqual(len(mail.outbox), 1)
        return user_stub, mail.outbox[0]

    def test_templated_path_renders_the_shared_layout(self):
        user_stub, message = self.notify(templated=True)
        html = message.alternatives[0][0] if message.alternatives else ""

        self.assertEqual(message.subject, "Create your Comet Robotics account")
        self.assertEqual(message.to, ["abc123456@utdallas.edu"])
        self.assertIn(user_stub.get_registration_url(), html)
        # The shared base.html footer, which the hand-rolled path has no equivalent of.
        self.assertIn("You received this email because", html)
        # Plain text is generated from the HTML rather than maintained by hand.
        self.assertIn(user_stub.get_registration_url(), message.body)

    def test_legacy_path_is_unchanged_while_the_flag_is_off(self):
        user_stub, message = self.notify(templated=False)
        html = message.alternatives[0][0] if message.alternatives else ""

        self.assertEqual(message.subject, "Create your Comet Robotics account")
        self.assertEqual(message.to, ["abc123456@utdallas.edu"])
        self.assertIn(user_stub.get_registration_url(), html)
        self.assertIn("Hey there!", html)

    def test_email_address_derives_from_the_net_id(self):
        user_stub = UserStub.create("abc123456", None)

        self.assertEqual(user_stub.email_address(), "abc123456@utdallas.edu")


class RegistrationCompletionViewTests(RegistrationTestCase):
    def complete(self, user_stub, **overrides):
        payload = {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "new_password1": "correct-horse-battery-staple",
            "new_password2": "correct-horse-battery-staple",
        }
        payload.update(overrides)
        return self.client.post(reverse("registration_complete", args=[user_stub.user_registration_key]), payload)

    def test_valid_registration_creates_the_account_and_redirects(self):
        user_stub = UserStub.create("registrationuser", "/payments/")

        response = self.complete(user_stub)

        self.assertRedirects(response, "/payments/", fetch_redirect_response=False)
        user = User.objects.get(username="registrationuser")
        self.assertTrue(user.is_active)
        self.assertTrue(user.check_password("correct-horse-battery-staple"))
        self.assertEqual(user.first_name, "Ada")
        self.assertEqual(user.last_name, "Lovelace")
        self.assertFalse(UserStub.objects.filter(pk=user_stub.pk).exists())
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))

    def test_invalid_password_keeps_the_stub_and_creates_no_account(self):
        user_stub = UserStub.create("invalidregistration", "")

        response = self.complete(user_stub, new_password2="not-the-same-password")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="invalidregistration").exists())
        self.assertTrue(UserStub.objects.filter(pk=user_stub.pk).exists())
        self.assertContains(response, "The two password fields didn\u2019t match.")

    def test_missing_name_keeps_the_stub_and_creates_no_account(self):
        user_stub = UserStub.create("missingname", "")

        response = self.complete(user_stub, last_name="")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="missingname").exists())
        self.assertTrue(UserStub.objects.filter(pk=user_stub.pk).exists())
        self.assertContains(response, "This field is required.")

    def test_expired_registration_key_returns_not_found(self):
        user_stub = UserStub.create("expiredregistration", "")
        user_stub.expires_at = timezone.now()
        user_stub.save(update_fields=["expires_at"])

        response = self.client.get(reverse("registration_complete", args=[user_stub.user_registration_key]))

        self.assertEqual(response.status_code, 404)
        self.assertFalse(User.objects.filter(username="expiredregistration").exists())
        self.assertTrue(UserStub.objects.filter(pk=user_stub.pk).exists())

    def test_valid_registration_without_destination_redirects_to_profile(self):
        user_stub = UserStub.create("profiledestination", "")

        response = self.complete(user_stub)

        self.assertRedirects(response, reverse("profile"))

    def test_net_id_claimed_while_the_form_was_open_is_reported(self):
        """activate() raises AccountAlreadyExistsError here; the view must not 500."""
        user_stub = UserStub.create("racedregistration", "")
        self.create_user("racedregistration")

        response = self.complete(user_stub)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already")
