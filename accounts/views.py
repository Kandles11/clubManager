from django.contrib.auth import login
from django.db import transaction
from django.http import Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views import View
from .models import (
    AccountAlreadyExistsError,
    AccountLink,
    DiscordAccountAlreadyLinkedError,
    RegistrationAlreadySentError,
    RegistrationEmailError,
    UserStub,
)
from .forms import RegistrationCompletionForm, RegistrationRequestForm
from core.models import ServerSettings, UserProfile
from clubManager import settings
from core.utilities import get_layout_data
import requests
from typing import TypedDict


class LinkSocialView(View):
    template_name = "link_social.html"

    def get(self, request, uuid):
        layout_data = get_layout_data(request)
        account_link = get_object_or_404(AccountLink, uuid=uuid)
        user = account_link.user
        link_type = account_link.link_type
        social_id = account_link.social_id
        profile_image = None
        username = None
        if link_type == "discord":
            discord_id = int(account_link.social_id)
            discord_user = get_discord_user(discord_id, settings.DISCORD_TOKEN)
            if discord_user:
                username = discord_user["username"]
                profile_image = discord_user["profile_image"]

        return render(
            request,
            self.template_name,
            {
                **layout_data,
                "user": user,
                "link_type": link_type,
                "social_id": social_id,
                "profile_image": profile_image,
                "username": username,
            },
        )

    def post(self, request, uuid):
        account_link = get_object_or_404(AccountLink, uuid=uuid)
        user = account_link.user
        user_profile, created = UserProfile.objects.get_or_create(user=user)
        link_type = account_link.link_type
        if link_type == "discord":
            user_profile.discord_id = account_link.social_id
            user_profile.save()
        account_link.delete()

        return redirect("link_success")


class LinkSuccessView(View):
    template_name = "link_success.html"

    def get(self, request):
        layout_data = get_layout_data(request)
        return render(request, self.template_name, {**layout_data})


class RegistrationCompleteView(View):
    template_name = "registration_complete.html"

    @staticmethod
    def get_user_stub(user_registration_key, *, lock=False):
        user_stubs = UserStub.objects.filter(pk=user_registration_key, expires_at__gt=timezone.now())
        if lock:
            user_stubs = user_stubs.select_for_update()
        user_stub = user_stubs.first()
        if user_stub is None:
            raise Http404("This registration link is invalid or has expired.")
        return user_stub

    def get(self, request, user_registration_key):
        user_stub = self.get_user_stub(user_registration_key)
        return render(
            request,
            self.template_name,
            {"form": RegistrationCompletionForm(user_stub.build_user()), "settings": ServerSettings.objects.first()},
        )

    def post(self, request, user_registration_key):
        activated = False
        with transaction.atomic():
            user_stub = self.get_user_stub(user_registration_key, lock=True)
            # An unsaved User for the form to fill in. activate() is what actually creates
            # the account, so a submission that fails here leaves nothing behind.
            form = RegistrationCompletionForm(user_stub.build_user(), request.POST)
            if form.is_valid():
                user = form.save(commit=False)
                try:
                    redirect_destination = user_stub.activate(user)
                    activated = True
                except AccountAlreadyExistsError:
                    form.add_error(None, "An account for this Net ID already exists. Try signing in instead.")
                except DiscordAccountAlreadyLinkedError:
                    form.add_error(
                        None, "That Discord account is already linked to another account. Contact an officer."
                    )

        if not activated:
            return render(
                request,
                self.template_name,
                {"form": form, "settings": ServerSettings.objects.first()},
            )
        login(request, user)
        return redirect(redirect_destination or "profile")


class RegistrationRequestView(View):
    template_name = "registration_request.html"
    confirmation_message = (
        "If you are eligible to register, check your UTD email for a link to finish creating your account."
    )

    def render_form(self, request, form, **context):
        return render(
            request, self.template_name, {"form": form, "settings": ServerSettings.objects.first(), **context}
        )

    def get(self, request):
        return self.render_form(request, RegistrationRequestForm())

    def post(self, request):
        form = RegistrationRequestForm(request.POST)
        if not form.is_valid():
            return self.render_form(request, form)
        net_id = form.cleaned_data["net_id"]
        try:
            user_stub = UserStub.create(net_id, None)
            UserStub.notify(user_stub)
        except (RegistrationAlreadySentError, AccountAlreadyExistsError):
            pass
        except RegistrationEmailError:
            if "user_stub" in locals():
                user_stub.delete()
            return self.render_form(
                request, form, error="We could not send your registration email. Please try again later."
            )
        return self.render_form(request, RegistrationRequestForm(), success=self.confirmation_message)


class DiscordUser(TypedDict):
    username: str
    discord_id: int
    profile_image: str


def get_discord_user(user_id: int, bot_token: str) -> DiscordUser | None:
    url = f"https://discord.com/api/v10/users/{user_id}"
    headers = {"Authorization": f"Bot {bot_token}"}

    response = requests.get(url, headers=headers)

    if not response.status_code == 200:
        return None

    user_data = response.json()

    discord_id = int(user_data["id"])
    discriminator = int(user_data["discriminator"]) or 0

    if "avatar" in user_data:
        profile_image = f"https://cdn.discordapp.com/avatars/{discord_id}/{user_data['avatar']}.png"
    else:
        discord_profile_image_index = ((discord_id >> 22) % 6) if discriminator == 0 else discriminator % 5
        profile_image = f"https://cdn.discordapp.com/embed/avatars/{discord_id}.png"

    return {"username": user_data["username"], "discord_id": discord_id, "profile_image": profile_image}
