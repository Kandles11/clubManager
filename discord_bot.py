from asgiref.sync import sync_to_async
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'clubManager.settings')
from clubManager import settings

import django
import discord
django.setup()

from payments.models import Term
from django.core.mail import send_mail
from accounts.models import AccountLink
from core.models import User, UserProfile
from common.utils import is_valid_net_id
from django.utils import timezone
import subprocess
import socket
import random

LIST = ["Cafe Brazil", "Biryaniify", "Bulldog Katsu", "LA Burger", "Torchy's Tacos", "Velvet Taco"]

bot = discord.Bot()

@bot.event
async def on_ready():
    print(f"{bot.user} is ready and online!")

@bot.slash_command(description="Link your Discord account to your Comet Robotics account")
@discord.option(name="net_id", description="Your UT Dallas Net ID", required=True, min_length=9, max_length=9)
async def link(
    ctx: discord.ApplicationContext, 
    net_id: str
):
    await ctx.respond("Processing...", ephemeral=True, delete_after=3.0)

    author_id = ctx.author.id
    author_name = ctx.author.name

    net_id = net_id.lower()
    if not is_valid_net_id(net_id):
        await ctx.respond("Invalid Net ID format!", ephemeral=True, delete_after=3.0)
        return

    def get_user():
        try:
            return User.objects.get(username=net_id)
        except:
            return None
    user = await sync_to_async(get_user)()

    if user is None:
        # await ctx.respond("Your Net ID was not found in our database. If you're sure it's correct, use the `/create` command to create a new account with that Net ID.", ephemeral=True, delete_after=3.0)
        await ctx.respond("Your Net ID was not found in our database. Contact an officer to get an account set up.", ephemeral=True, delete_after=3.0)  # TODO
        return

    def get_profile():
        try:
            return UserProfile.objects.get_or_create(user=user)[0]
        except Exception as e:
            import traceback
            traceback.print_exc(e)
            return None
    profile = await sync_to_async(get_profile)()

    if not profile:
        # Hopefully never happens
        await ctx.respond("Unhandled error: ProfileNotFound. Ping an officer for assistance!")
        return

    if profile.discord_id:
        if profile.discord_id == str(author_id):
            await ctx.respond("That Net ID is already linked to your account!", ephemeral=True)
        else:
            await ctx.respond("That Net ID is already linked to a different Discord account. Ping an officer if this looks wrong.", ephemeral=True)
        return

    def is_discord_linked():
        try:
            return UserProfile.objects.get(discord_id=author_id) is not None
        except:
            return False
    discord_id_is_linked = await sync_to_async(is_discord_linked)()

    if discord_id_is_linked:
        await ctx.respond("Your Discord account is already linked to a Comet Robotics account. Ping an officer if this looks wrong.", ephemeral=True)
        return

    # NetID has been validated, generate the AccountLink and send the email!

    def create_account_link():
        return AccountLink.objects.create(user=user, link_type='discord', social_id=str(author_id))
    account_link = await sync_to_async(create_account_link)()

    email = f"{net_id}@utdallas.edu"
    await ctx.respond(f"Sending email to {email}...", ephemeral=True, delete_after=3.0)

    def send_the_email():
        send_mail(
            "Link your Discord to your Comet Robotics account",
            f"""
Hello {user.first_name},

You have requested to link your Discord account with your Comet Robotics account.

Full Name: {user.first_name} {user.last_name}
Discord Name: {author_name}
Net ID: {net_id}

If the above information is correct, click on the below link to connect your Discord account to your Comet Robotics account.

https://portal.cometrobotics.org/accounts/link/{account_link.uuid}

If the name is incorrect, reply to this email and we'll get back to you. If this was not you, you can safely ignore this email.

Thanks!
""",
            "cometrobotics@utdallas.edu",
            [email],
            fail_silently=False,
            html_message=f"""
<h2>Hello {user.first_name},</h2>

<p>You have requested to link your Discord account with your Comet Robotics account.</p>

<p>Full Name: {user.first_name} {user.last_name}<br>
Discord Name: {author_name}<br>
Net ID: {net_id}</p>


<p>If the above information is correct, click the button below or the link to connect your Discord account to your Comet Robotics account.</p>

<a href="https://portal.cometrobotics.org/accounts/link/{account_link.uuid}"><button style="border: solid #950000 3px;padding: 1em; border-radius: 10px; background-color:#bf1e2e; color: white;"><strong>Link Account</strong></button></a>

<br><br><a href="https://portal.cometrobotics.org/accounts/link/{account_link.uuid}">https://portal.cometrobotics.org/accounts/link/{account_link.uuid}</a>

<p>If the name is incorrect, reply to this email and we'll get back to you. If this was not you, you can safely ignore this email.</p>

<p>Thanks!</p>
""",
        )
    await sync_to_async(send_the_email)()

    embed = discord.Embed(
        title="Email sent!",
        description=f"Check your email (`{email}`) and click the link to connect your Discord account to your Comet Robotics account.",  
        color=discord.Color.red(),
    )

    await ctx.respond(":tada:", embed=embed, ephemeral=True)

@bot.slash_command(name="profile", description="View your Comet Robotics profile")
async def profile(ctx):
    user = ctx.author
    try:
        user_profile = await sync_to_async(UserProfile.objects.get)(discord_id=str(user.id))
    except UserProfile.DoesNotExist:
        await ctx.respond("You don't have a linked Comet Robotics account. Use the `/link` command to connect your Comet Robotics account to your Discord account.", ephemeral=True)
        return

    def get_basic_info(user_profile: UserProfile):
        return f"""
        **Full Name:** {user_profile.user.first_name} {user_profile.user.last_name}
        **Net ID:** {user_profile.user.username}
        **Gender:** {user_profile.gender if user_profile.gender else 'Not specified'}
        """

    basic_info = await sync_to_async(get_basic_info)(user_profile)

    # Membership Status
    def get_membership_status(user_profile: UserProfile):
        current_term, current_payment = user_profile.is_member() 
        body = "**Current Membership:** " + ("Not a member" if not current_payment else f"Active for {current_term.name}")
        
        past_terms: list[Term] = Term.objects.filter(end_date__lte=timezone.now())
        future_terms: list[Term] = Term.objects.filter(start_date__gte=timezone.now()).exclude(pk=current_term.pk)

        paid_future_terms = [term for term in future_terms if user_profile.is_member(term)[1]]
        if len(paid_future_terms) > 0:
            body += f"\n**Dues paid for future term(s)**: {', '.join([t.name for t in paid_future_terms])}"

        past_terms_info = ", ".join([term.name for term in past_terms if user_profile.is_member(term)[1]]) if len(past_terms) > 0 else "No past memberships"

        body += f"\n**Past Memberships:** {past_terms_info}"

        return body
    membership_status = await sync_to_async(get_membership_status)(user_profile)


    embed = discord.Embed(
        title="Your Comet Robotics Profile",
        color=discord.Color.red()
    )
    embed.add_field(name="Basic Info", value=basic_info, inline=False)
    embed.add_field(name="Membership Status", value=membership_status, inline=False)
    embed.set_footer(text="To pay member dues, use the /pay command :)")

    await ctx.respond(embed=embed, ephemeral=True)


@bot.slash_command(description="Get the current version of the bot")
async def version(ctx: discord.ApplicationContext):
    """
    Returns the current version of the bot. 

    NOTE: This command should only be accessible to org leaders (people with the Team Lead, Project Manager, or Officer roles). This needs to be configured manually in Discord server settings > Integrations.
    """
    try:
        commit_hash = subprocess.check_output(["git", "rev-parse", "HEAD"]).strip().decode('utf-8')
        commit_message = subprocess.check_output(["git", "log", "-1", "--pretty=%B"]).strip().decode('utf-8')
        hostname = socket.gethostname()

        embed = discord.Embed(
            title="Bot Version Information",
            color=discord.Color.red()
        )
        embed.add_field(name="Commit Hash", value=commit_hash, inline=False)
        embed.add_field(name="Commit Message", value=commit_message, inline=False)
        embed.add_field(name="Hostname", value=hostname, inline=False)

        await ctx.respond(embed=embed, ephemeral=True)
    except Exception as e:
        await ctx.respond(f"An error occurred while fetching version information: {str(e)}", ephemeral=True)


@bot.slash_command(description="Pay your member dues to become a Comet Robotics member")
async def pay(ctx: discord.ApplicationContext):

    def get_payment_links():
        user = UserProfile.objects.get(discord_id=str(ctx.author.id))
        active_terms = Term.objects.filter(end_date__gte=timezone.now())
        if not active_terms:
            ctx.respond("No active terms available for payment.",  ephemeral=True)

        payment_links = [(f"[Pay for {term.name}](https://portal.cometrobotics.org/payments/{term.product.id}/pay/)" + ('(you\'ve already paid this term\'s dues)' if user.is_member(term)[1] else '')) for term in active_terms]
        return "\n".join(payment_links)

    payment_links = await sync_to_async(get_payment_links)()

    embed = discord.Embed(
        title="Become a Comet Robotics Member",
        description=payment_links,
        color=discord.Color.red()
    )
    embed.set_footer(text="Click on the links to pay for the respective terms.")

    await ctx.respond(embed=embed, ephemeral=True)


# TODO: /create
# TODO: /edit


class ListView(discord.ui.View):
    @discord.ui.button(label="I'm Feeling Lucky!", style=discord.ButtonStyle.primary, emoji="🎲") 
    async def button_callback(self, button, interaction):
        randomChoice = random.choice(LIST)
        await interaction.response.send_message(randomChoice, delete_after=10.0)

@bot.slash_command(description="View THE LIST")
async def thelist(ctx: discord.ApplicationContext):
    embed = discord.Embed(
        title="The List",
        description="\n".join(LIST),
        color=discord.Color.red()
    )

    await ctx.respond(embed=embed, view=ListView(), ephemeral=False)
    

bot.run(settings.DISCORD_TOKEN)
