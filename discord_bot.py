import aiohttp
from asgiref.sync import sync_to_async
import os
import time


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "clubManager.settings")
from clubManager import settings

import django
import discord
from discord.ext import pages

django.setup()

from django.core.mail import send_mail

from core.emails import send_templated_email

from accounts.models import (
    AccountAlreadyExistsError,
    AccountLink,
    DiscordAccountAlreadyLinkedError,
    RegistrationAlreadySentError,
    RegistrationEmailError,
    UserStub,
)
from core.models import ServerSettings, User, UserProfile
from common.asyncutils import *
from common.utils import is_valid_net_id
from django.utils import timezone
from events.models import Attendance
from payments.models import Term
from datetime import datetime, time as dttime
import io
import subprocess
import socket
import random
from operator import itemgetter
import asyncio

from fastapi import FastAPI, HTTPException, Header
import uvicorn

import logging

logger = logging.getLogger("discord")
logger.setLevel(logging.DEBUG if settings.DEBUG else logging.INFO)
handler = logging.FileHandler(filename="discord.log", encoding="utf-8", mode="a")
handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s"))
logger.addHandler(handler)

LIST = [
    "Madina Halal Grill",
    "Zenna Thai & Japanese",
    "Zio Al's Pizza & Pasta",
    "Fat ni BBQ",
    "Cafe Brazil",
    "Biryaniify",
    "Bulldog Katsu",
    "LA Burger",
    "Torchy's Tacos",
    "Fuzzy's Tacos",
    "Velvet Taco",
    "Taco Bell",
    "Jimmy John's",
    "Chipotle",
    "Panera",
    "Potbelly",
    "Sky Rocket Burger",
    "Teriyaki Sensei",
    "Freebirds",
    "Unbelievabowl",
    "Pei Wei",
    "Masala Wok",
    "Little Greek",
    "Liberty Burger",
    "The Mango's Plano",
    "Bambu Thai",
    "Hawaiian Bros",
    "The String Bean",
    "Something new",
]

intents = discord.Intents.default()
intents.members = True
intents.presences = True
intents.message_content = True

bot = discord.Bot(intents=intents)

app = FastAPI()

PRIVILEGED_ROLE_IDS = {
    settings.DISCORD_OFFICER_ROLE_ID,
    settings.DISCORD_PROJECT_MANAGER_ROLE_ID,
    settings.DISCORD_TEAM_LEAD_ROLE_ID,
}

server_settings = ServerSettings.objects.first()
if server_settings is None:
    raise Exception("Server settings not found")
ORG_NAME = server_settings.organization_name

# -------- Utility methods --------


async def log_msg(msg: str, embed_color: discord.Color, func_name: str):
    guild = bot.get_guild(settings.DISCORD_SERVER_ID)
    if not guild:
        return
    log_channel = guild.get_channel(settings.DISCORD_BOT_LOG_CHANNEL_ID)
    if log_channel and hasattr(log_channel, "send"):
        await log_channel.send(
            embed=discord.Embed(
                description=msg,
                color=embed_color,
                footer=discord.EmbedFooter(text=func_name),
                timestamp=discord.utils.utcnow(),
            )
        )


async def get_user_attendances(user_profile: UserProfile):
    def get_attendances():
        return sorted(Attendance.objects.filter(user=user_profile.user), key=lambda a: a.event.event_date, reverse=True)

    return await sync_to_async(get_attendances)()


async def respond_user_attendances(interaction: discord.Interaction, user_profile: UserProfile):
    attendances = await get_user_attendances(user_profile)

    def get_attended_events():
        return [a.event for a in attendances]

    events = await sync_to_async(get_attended_events)()
    ev_date_mapped = sorted(
        {
            date: list(filter(lambda e: date == e.event_date.date(), events))
            for date in set(map(lambda e: e.event_date.date(), events))
        }.items(),
        key=itemgetter(0),
        reverse=True,
    )
    attendance_strs = list(
        map(
            lambda de: (
                discord.utils.format_dt(datetime.combine(de[0], dttime.min), style="D")
                + "\n"
                + "\n".join(
                    map(lambda e: f"- **{e.event_name}**", sorted(de[1], key=lambda x: x.event_date, reverse=False))
                )
            ),
            ev_date_mapped,
        )
    )
    page_size = 8
    attendance_strs_paginated = [attendance_strs[i : i + page_size] for i in range(0, len(attendance_strs), page_size)]

    def make_embed(desc: str, **kwargs):
        return discord.Embed(
            title=f"{user_profile.user.get_short_name()}'s Attendances",
            color=discord.Color.blurple(),
            description=desc,
            **kwargs,
        )

    if not len(attendance_strs_paginated):
        await interaction.respond(embed=make_embed("No attendances found!"), ephemeral=True)
        return

    attendance_embeds = [
        make_embed(
            "\n\n".join(ls),
            footer=discord.EmbedFooter(
                text=f"Total: {len(events)}\nDays attended: {len(attendance_strs)}"
            ),  # TODO: weekly streak
        )
        for ls in attendance_strs_paginated
    ]
    paginator = pages.Paginator(attendance_embeds)
    await paginator.respond(interaction, ephemeral=True)


async def get_current_member_discord_ids():
    def run() -> list[int]:
        current_term = Term.get_current_term()
        profiles = UserProfile.objects.exclude(discord_id__isnull=True)
        valid_ids: list[int] = []
        for profile in profiles:
            if profile.is_member(current_term)[1]:
                if profile.discord_id is not None:
                    valid_ids.append(int(profile.discord_id))
        return valid_ids

    return await sync_to_async(run)()


# -------- Bot commands --------


class AccountCreationView(discord.ui.View):
    def __init__(self, net_id: str, discord_user_id: int) -> None:
        super().__init__(timeout=300)
        self.net_id = net_id
        self.discord_user_id = str(discord_user_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user is not None and str(interaction.user.id) == self.discord_user_id:
            return True

        await interaction.response.send_message(
            "Only the person who requested this account can confirm its creation.", ephemeral=True
        )
        return False

    @discord.ui.button(label="Yes, create my account", style=discord.ButtonStyle.primary)
    async def confirm_account_creation(self, button, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="Creating your account and sending the registration email...", view=None
        )

        try:
            user_stub = await sync_to_async(UserStub.create)(
                net_id=self.net_id,
                after_registration_redirect_destination="",
                discord_user_id=self.discord_user_id,
            )
        except RegistrationAlreadySentError:
            await interaction.edit_original_response(
                content=(
                    "Oh, looks like we've already sent you a registration email recently. Please check your email."
                ),
                view=None,
            )
            return
        except AccountAlreadyExistsError:
            await interaction.edit_original_response(
                content="There's already an account with this Net ID in the system.", view=None
            )
            return
        except DiscordAccountAlreadyLinkedError:
            await interaction.edit_original_response(
                content=(
                    f"Your Discord account is already linked to a {ORG_NAME} account. "
                    "Ping an officer if this looks wrong."
                ),
                view=None,
            )
            return
        except Exception:
            logger.exception("Unable to create account registration for Net ID %s", self.net_id)
            await interaction.edit_original_response(
                content="We couldn't start your account registration. Please try again or contact an officer.",
                view=None,
            )
            return

        try:
            await sync_to_async(UserStub.notify)(user_stub)
        except RegistrationEmailError:
            logger.exception("Unable to send registration email for Net ID %s", self.net_id)
            try:
                await sync_to_async(user_stub.delete)()
            except Exception:
                logger.exception("Unable to clean up failed registration for Net ID %s", self.net_id)
            await interaction.edit_original_response(
                content="We failed to send your registration email. Please try again in a few minutes.", view=None
            )
            return

        email = f"{self.net_id}@utdallas.edu"
        await interaction.edit_original_response(
            content=(
                f"Registration email sent to `{email}`. Check your inbox and follow the link to finish creating "
                f"your {ORG_NAME} account."
            ),
            view=None,
        )


@bot.event
async def on_ready():
    print(f"{bot.user} is ready and online!")
    logger.info(f"{bot.user} is ready and online!")


@bot.slash_command(description=f"Link your Discord account to your {ORG_NAME} account")
@discord.option(name="net_id", description="Your UT Dallas Net ID", required=True, min_length=9, max_length=9)
async def link(ctx: discord.ApplicationContext, net_id):
    await ctx.respond("Processing...", ephemeral=True, delete_after=3.0)

    author_id = ctx.author.id
    author_name = ctx.author.name

    net_id = net_id.lower()
    if not is_valid_net_id(net_id):
        await ctx.respond("Invalid Net ID format!", ephemeral=True, delete_after=3.0)
        return

    user = await get_user_async(username=net_id)

    if user is None:
        if not settings.FEATURE_FLAGS["DISCORD_ACCOUNT_REGISTRATION"]:
            await ctx.respond(
                "Your Net ID was not found in our database. Contact an officer to get an account set up.",
                ephemeral=True,
                delete_after=3.0,
            )
            return

        await ctx.respond(
            f"We couldn't find an account for `{net_id}`. Is this your correct Net ID? "
            "Confirm below and we'll email you a link to create your account.",
            view=AccountCreationView(net_id, author_id),
            ephemeral=True,
        )
        return

    profile = await get_or_create_profile_async(user=user)

    if not profile:
        # Hopefully never happens
        await ctx.respond("Unhandled error: ProfileNotFound. Ping an officer for assistance!")
        return

    if profile.discord_id:
        if profile.discord_id == str(author_id):
            await ctx.respond("That Net ID is already linked to your account!", ephemeral=True)
        else:
            await ctx.respond(
                "That Net ID is already linked to a different Discord account. Ping an officer if this looks wrong.",
                ephemeral=True,
            )
        return

    discord_id_is_linked = (await get_profile_async(discord_id=author_id)) is not None

    if discord_id_is_linked:
        await ctx.respond(
            f"Your Discord account is already linked to a {ORG_NAME} account. Ping an officer if this looks wrong.",
            ephemeral=True,
        )
        return

    # NetID has been validated, generate the AccountLink and send the email!

    def create_account_link():
        return AccountLink.objects.create(user=user, link_type="discord", social_id=str(author_id))

    account_link = await sync_to_async(create_account_link)()

    email = f"{net_id}@utdallas.edu"
    await ctx.respond(f"Sending email to {email}...", ephemeral=True, delete_after=3.0)

    link_url = f"{settings.PUBLIC_URL}/accounts/link/{account_link.uuid}"
    account_details = {
        "Full Name": f"{user.first_name} {user.last_name}",
        "Discord Name": author_name,
        "Net ID": net_id,
    }

    def send_the_email():
        if settings.FEATURE_FLAGS["NEW_TRANSACTIONAL_EMAIL_TEMPLATES"]:
            send_templated_email(
                "email/messages/discord_account_link.html",
                {
                    "first_name": user.first_name,
                    "account_details": account_details,
                    "link_url": link_url,
                },
                [email],
            )
            return

        send_mail(
            f"Link your Discord to your {ORG_NAME} account",
            f"""
Hello {user.first_name},

You have requested to link your Discord account with your {ORG_NAME} account.

Full Name: {user.first_name} {user.last_name}
Discord Name: {author_name}
Net ID: {net_id}

If the above information is correct, click on the below link to connect your Discord account to your {ORG_NAME} account.

{link_url}

If the name is incorrect, reply to this email and we'll get back to you. If this was not you, you can safely ignore this email.

Thanks!
""",
            settings.EMAIL_FROM,
            [email],
            fail_silently=False,
            html_message=f"""
<h2>Hello {user.first_name},</h2>

<p>You have requested to link your Discord account with your {ORG_NAME} account.</p>

<p>Full Name: {user.first_name} {user.last_name}<br>
Discord Name: {author_name}<br>
Net ID: {net_id}</p>


<p>If the above information is correct, click the button below or the link to connect your Discord account to your {ORG_NAME} account.</p>

<a href="{link_url}"><button style="border: solid #950000 3px;padding: 1em; border-radius: 10px; background-color:#bf1e2e; color: white;"><strong>Link Account</strong></button></a>

<br><br><a href="{link_url}">{link_url}</a>

<p>If the name is incorrect, reply to this email and we'll get back to you. If this was not you, you can safely ignore this email.</p>

<p>Thanks!</p>
""",
        )

    await sync_to_async(send_the_email)()

    embed = discord.Embed(
        title="Email sent!",
        description=f"Check your email (`{email}`) and click the link to connect your Discord account to your {ORG_NAME} account.",
        color=discord.Color.red(),
    )

    await ctx.respond(":tada:", embed=embed, ephemeral=True)


@bot.slash_command(description=f"View your {ORG_NAME} profile")
@discord.option(
    name="net_id",
    description="Net ID of the account to view. Defaults to your own.",
    required=False,
    input_type=str,
    min_length=9,
    max_length=9,
)
@discord.option(
    name="user",
    parameter_name="discord_user",
    description="Discord user of the account to view. Defaults to your own.",
    required=False,
    input_type=discord.User,
)
async def profile(ctx: discord.ApplicationContext, net_id: str | None, discord_user: discord.User | None):
    async def get_embed(user_profile: UserProfile, yours: bool):
        def get_basic_info(user_profile: UserProfile):
            info_string = f"""
**Full Name:** {user_profile.user.first_name} {user_profile.user.last_name}
**Net ID:** {user_profile.user.username}
**Gender:** {UserProfile.GenderChoice(user_profile.gender).label if user_profile.gender else "Not specified"}
"""
            if not yours:
                info_string += (
                    f"**Email:** {user_profile.user.email if user_profile.user.email else 'No email provided'}\n"
                )
                info_string += f"**Discord:** {'<@' + user_profile.discord_id + '>' if user_profile.discord_id else 'Not linked'}\n"

            return info_string

        basic_info = await sync_to_async(get_basic_info)(user_profile)

        # Membership Status
        def get_membership_status(user_profile: UserProfile):
            current_term, current_payment = user_profile.is_member()
            body = "**Current Membership:** " + (
                "Not a member" if not current_payment else f"Active for {current_term.name}"
            )

            past_terms = Term.objects.filter(end_date__lte=timezone.now())
            future_terms = Term.objects.filter(start_date__gte=timezone.now()).exclude(pk=current_term.pk)

            paid_future_terms = [term for term in future_terms if user_profile.is_member(term)[1]]
            if len(paid_future_terms) > 0:
                body += f"\n**Dues paid for future term(s)**: {', '.join([t.name for t in paid_future_terms])}"

            paid_past_terms = [term.name for term in past_terms if user_profile.is_member(term)[1]]
            past_terms_info = ", ".join(paid_past_terms) if len(paid_past_terms) > 0 else "No past memberships"

            body += f"\n**Past Memberships:** {past_terms_info}"

            return (
                body,
                f"{settings.PUBLIC_URL}/payments/{current_term.product.id}/pay" if not current_payment else None,
                current_term.name,
            )

        membership_status, due_paying_url, term_name = await sync_to_async(get_membership_status)(user_profile)

        embed = discord.Embed(
            title=("Your" if yours else (user_profile.user.username + "'s")) + " Profile", color=discord.Color.red()
        )
        embed.add_field(name="Basic Info", value=basic_info, inline=False)
        embed.add_field(name="Membership Status", value=membership_status, inline=False)

        actions_view = ProfileActionsView(term_name, due_paying_url, user_profile)

        return embed, actions_view

    if net_id is None and discord_user is None:
        discord_user_id = ctx.author.id
        user_profile = await get_profile_async(discord_id=str(discord_user_id))
        if user_profile is None:
            await ctx.respond(
                f"You don't have a linked {ORG_NAME} account. Use the `/link` command to connect your {ORG_NAME} account to your Discord account.",
                ephemeral=True,
            )
            return
        embed, actions_view = await get_embed(user_profile, True)
        await ctx.respond(
            embed=embed,
            ephemeral=True,
            view=actions_view,
        )
    else:
        if not hasattr(ctx.author, "roles"):
            await ctx.respond("You can only view other user profiles in a server!")
            return
        user_role_ids = set([role.id for role in ctx.author.roles])
        if not user_role_ids.intersection(PRIVILEGED_ROLE_IDS):
            await ctx.respond(
                "You don't have the required privileges to view this user's profile. Only officers, team leads, and project managers can view other user profiles.",
                ephemeral=True,
            )
            return
        if net_id is not None:
            user_profile = await get_profile_async(user__username=str(net_id).lower())
            if user_profile is None:
                await ctx.respond(
                    "User with NetID not found!",
                    ephemeral=True,
                )
                return
        else:
            assert discord_user is not None
            user_profile = await get_profile_async(discord_id=str(discord_user.id))
            if user_profile is None:
                await ctx.respond(
                    "User with Discord ID not found or not linked!",
                    ephemeral=True,
                )
                return
        embed, _ = await get_embed(user_profile, False)
        await ctx.respond(
            embed=embed,
            ephemeral=True,
        )


class ProfileActionsView(discord.ui.View):
    def __init__(self, term_name: str, due_paying_url: str | None, user_profile: UserProfile) -> None:
        super().__init__()
        self.user_profile = user_profile
        self.add_item(
            discord.ui.Button(
                label=f"Pay Dues for {term_name}" if due_paying_url else f"Dues paid for {term_name} :)",
                url=due_paying_url or settings.PUBLIC_URL,
                disabled=not due_paying_url,
            )
        )

        # # TODO: Implement interaction for gender
        # discord_gender = [discord.SelectOption(label=str(obj._name_), value=str(obj)) for obj in UserProfile.GenderChoice]
        # self.add_item(discord.ui.Select(placeholder="Edit Gender...", options=discord_gender))

    @discord.ui.button(label="View Attendances", style=discord.ButtonStyle.primary)
    async def view_attendances(self, button, interaction: discord.Interaction):
        await respond_user_attendances(interaction, self.user_profile)

    @discord.ui.button(label="Edit Profile (WIP)", style=discord.ButtonStyle.primary, disabled=True)
    async def edit_profile(self, button, interaction: discord.Interaction):
        await interaction.response.send_modal(ProfileEditView(user_profile=self.user_profile))


class ProfileEditView(discord.ui.Modal):
    def __init__(self, user_profile: UserProfile, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs, title="Edit Profile")

        self.user_profile = user_profile

        self.first_name = discord.ui.InputText(
            label="First Name", value=user_profile.user.first_name, required=True, style=discord.InputTextStyle.short
        )
        self.last_name = discord.ui.InputText(
            label="Last Name", value=user_profile.user.last_name, required=True, style=discord.InputTextStyle.short
        )

        # People should not be able to change name without talking to us - Colin
        # self.add_item(self.first_name)
        # self.add_item(self.last_name)

    async def callback(self, interaction: discord.Interaction):
        making_new_profile = self.user_profile is None

        def run():
            if making_new_profile:
                user, created_user = User.objects.get_or_create(username=self.net_id.value)
                self.user_profile = UserProfile.objects.create(user=user)

            if self.user_profile:
                self.user_profile.user.first_name = self.first_name.value or ""
                self.user_profile.user.last_name = self.last_name.value or ""
                self.user_profile.user.save()

        await interaction.response.defer(ephemeral=True)
        await sync_to_async(run)()


@bot.slash_command(description="View your meeting attendances")
async def attendances(ctx: discord.ApplicationContext):
    user = ctx.author
    user_profile: UserProfile | None = await get_profile_async(discord_id=str(user.id))
    if user_profile is None:
        await ctx.respond(
            f"You don't have a linked {ORG_NAME} account. Use the `/link` command to connect your {ORG_NAME} account to your Discord account.",
            ephemeral=True,
        )
        return

    await respond_user_attendances(ctx.interaction, user_profile)


# TODO: /create - should send a confirmation email before creating account (extra impl for /accounts/link endpoint?)


@bot.slash_command(description="Get the current version of the bot")
async def version(ctx: discord.ApplicationContext):
    """
    Returns the current version of the bot.

    NOTE: This command should only be accessible to org leaders (people with the Team Lead, Project Manager, or Officer roles). This needs to be configured manually in Discord server settings > Integrations.
    """
    try:
        commit_hash = subprocess.check_output(["git", "rev-parse", "HEAD"]).strip().decode("utf-8")
        commit_message = subprocess.check_output(["git", "log", "-1", "--pretty=%B"]).strip().decode("utf-8")
        hostname = socket.gethostname()

        embed = discord.Embed(title="Bot Version Information", color=discord.Color.red())
        embed.add_field(name="Commit Hash", value=commit_hash, inline=False)
        embed.add_field(name="Commit Message", value=commit_message, inline=False)
        embed.add_field(name="Hostname", value=hostname, inline=False)

        await ctx.respond(embed=embed, ephemeral=True)
    except Exception as e:
        await ctx.respond(f"An error occurred while fetching version information: {str(e)}", ephemeral=True)


@bot.slash_command(description="Pay your member dues to become a member")
async def pay(ctx: discord.ApplicationContext):
    def get_payment_links():
        try:
            user = UserProfile.objects.get(discord_id=str(ctx.author.id))
        except:
            return None
        active_terms = Term.objects.filter(end_date__gte=timezone.now())
        if not active_terms:
            # ctx.respond("No active terms available for payment.",  ephemeral=True)  # TODO: move to separate func to await
            pass

        payment_links = [
            (
                f"[Pay for {term.name}]({settings.PUBLIC_URL}/payments/{term.product.id}/pay/)"
                + (" (you've already paid this term's dues)" if user.is_member(term)[1] else "")
            )
            for term in active_terms
        ]
        return "\n".join(payment_links)

    payment_links = await sync_to_async(get_payment_links)()

    if payment_links is None:
        await ctx.respond(
            f"You're not linked yet! Use `/link` with your NetID to link your Discord account to your {ORG_NAME} account, then try again.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(title="Become a Member", description=payment_links, color=discord.Color.red())
    embed.set_footer(text="Click on the links to pay for the respective terms.")

    await ctx.respond(embed=embed, ephemeral=True)


@app.post("/give-member-role")
async def give_member_role(data: dict, authorization: str = Header(None)):
    if authorization != f"Bearer {settings.API_SECRET}":
        raise HTTPException(status_code=403, detail="Forbidden")

    member_id = int(data.get("member_id", "0"))
    do_log = bool(data.get("log", True))
    guild = bot.get_guild(settings.DISCORD_SERVER_ID)
    if not guild:
        raise HTTPException(status_code=500, detail="Discord server not found")
    member_role = guild.get_role(settings.DISCORD_MEMBER_ROLE_ID)
    if not member_role:
        if do_log:
            await log_msg(
                f"Could not find member role with id {settings.DISCORD_MEMBER_ROLE_ID}",
                discord.Color.red(),
                "give_member_role",
            )
        raise HTTPException(status_code=500, detail="Member role not found")
    member = guild.get_member(member_id)
    if not member:
        if do_log:
            await log_msg(f"Could not find member with id {member_id}", discord.Color.red(), "give_member_role")
        raise HTTPException(status_code=422, detail="Member not found")
    if member_role not in member.roles:
        await member.add_roles(member_role)
        await log_msg(
            f"Added member role to {member.name} ({member.id}) (<@{member.id}>)",
            discord.Color.green(),
            "give_member_role",
        )


@bot.event
async def on_member_join(member: discord.Member):
    def is_profile_valid():
        profile = UserProfile.objects.filter(discord_id=str(member.id)).first()
        return profile and profile.is_member()[1]

    profile_valid = await sync_to_async(is_profile_valid)()
    if profile_valid:
        await give_member_role({"member_id": member.id}, authorization=f"Bearer {settings.API_SECRET}")


@bot.slash_command(description="Give member roles to paid members")
async def givememberroles(ctx: discord.ApplicationContext):
    guild = bot.get_guild(settings.DISCORD_SERVER_ID)
    if not guild:
        return
    member_role = guild.get_role(settings.DISCORD_MEMBER_ROLE_ID)
    if not member_role:
        return

    message = await ctx.respond("Processing...")
    logger.debug("Starting the role addition...")
    ids_to_add = await get_current_member_discord_ids()

    start_time = time.time()

    async def add_role_to_member(discord_id: int):
        member = guild.get_member(discord_id)
        if not member:
            logger.warning(f"Could not find member with id {discord_id}")
            return False
        try:
            if member_role not in member.roles:
                await member.add_roles(member_role)
            return True
        except Exception as e:
            logger.warning(f"Failed to add role to member {discord_id}: {e}")
            return False

    tasks = [add_role_to_member(discord_id) for discord_id in ids_to_add]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    removed_count = sum(1 for result in results if result is True)

    end_time = time.time()

    logger.info(f"Added role to {len(ids_to_add)} users.")
    success_message = f"Member role addition success! Added member role to {removed_count} users. (Time taken: {end_time - start_time:.2f} seconds.)"
    if isinstance(message, discord.Interaction):
        await message.edit_original_response(content=success_message)
    else:
        await message.edit(content=success_message)


@bot.slash_command(description="Purge member roles from non-paying members")
async def purgememberroles(ctx: discord.ApplicationContext):
    guild = bot.get_guild(settings.DISCORD_SERVER_ID)
    if not guild:
        return
    member_role = guild.get_role(settings.DISCORD_MEMBER_ROLE_ID)
    if not member_role:
        return

    message = await ctx.respond("Processing...")
    logger.debug("Starting the role removal...")

    removed_count = 0
    discord_members = guild.members
    valid_members = await get_current_member_discord_ids()

    start_time = time.time()
    members_to_remove = [member for member in discord_members if member.id not in valid_members]

    async def remove_role_from_member(discord_member: discord.Member):
        try:
            if member_role in discord_member.roles:
                await discord_member.remove_roles(member_role)
            return True
        except Exception as e:
            logger.warning(f"Failed to remove role from member {discord_member.id}: {e}")
            return False

    tasks = [remove_role_from_member(member) for member in members_to_remove]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    removed_count = sum(1 for result in results if result is True)
    end_time = time.time()

    logger.info(f"Purged role for {removed_count} users.")
    success_message = f"Member role purge success! Removed member role for {removed_count} users. (Time taken: {end_time - start_time:.2f} seconds.)"
    if isinstance(message, discord.Interaction):
        await message.edit_original_response(content=success_message)
    else:
        await message.edit(content=success_message)


class ListView(discord.ui.View):
    @discord.ui.button(label="I'm Feeling Lucky!", style=discord.ButtonStyle.primary, emoji="🎲")
    async def button_callback(self, button, interaction):
        randomChoice = random.choice(LIST)
        await interaction.response.send_message(randomChoice, ephemeral=True, delete_after=10.0)


@bot.slash_command(description="View THE LIST")
async def thelist(ctx: discord.ApplicationContext):
    embed = discord.Embed(title="The List", description="\n".join(LIST), color=discord.Color.red())

    await ctx.respond(embed=embed, view=ListView(), ephemeral=True, delete_after=60.0)


@bot.slash_command(description="View the printers")  # TODO: change to the Makerspace
async def camera(ctx: discord.ApplicationContext):
    message = await ctx.respond("Processing...", ephemeral=True)
    assert isinstance(message, discord.Interaction)

    valid_members = await get_current_member_discord_ids()
    if ctx.author.id not in valid_members:
        await message.edit_original_response(content=f"You are not registered as a member of {ORG_NAME}!")
    else:
        # url = "http://eric1:8080/stream" # TODO: probably /snapshot instead of /stream
        url = "http://192.168.1.64:8080/snapshot"
        try:
            async with aiohttp.request("GET", url) as resp:
                if resp.status != 200:
                    await message.edit_original_response(content="Camera is currently offline.")
                    return
            img = discord.File(io.BytesIO(await resp.read()), "stream.jpg")
            await message.edit_original_response(content="", file=img)
        except Exception as e:
            await message.edit_original_response(content="An error occurred.")
            logger.error("Error fetching camera image:")
            logger.exception(e)


async def main():
    # Launch FastAPI server
    config = uvicorn.Config(app, host="0.0.0.0", port=settings.DISCORD_API_PORT, loop="asyncio", lifespan="on")
    server = uvicorn.Server(config)

    # Run bot and API concurrently
    bot_task = asyncio.create_task(bot.start(settings.DISCORD_TOKEN))
    api_task = asyncio.create_task(server.serve())

    await asyncio.gather(bot_task, api_task)


if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Bot shutting down...")
