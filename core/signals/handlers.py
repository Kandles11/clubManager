from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver
from clubManager import settings

from common.major import get_major_from_netid
from core.models import UserProfile
from payments.models import PurchasedProduct, Term

import aiohttp
from asgiref.sync import sync_to_async


async def add_member_role(discord_id: int):
    try:
        async with aiohttp.request(
            "POST",
            f"{settings.BOT_API_HOST}/give-member-role",
            json={"member_id": discord_id},
            headers={"Authorization": f"Bearer {settings.API_SECRET}"},
        ) as resp:
            if resp.status != 200:
                print(f"Failed to add member role to {discord_id}: status code {resp.status} {resp.reason}")
                return False
            return True
    except Exception as e:
        print(f"Failed to connect to bot api: {e}")
        return False


# Add member role when discord id is added (user linked or id manually added)
@receiver(post_save, sender=UserProfile)
async def update_roles_profile_signal(sender, instance: UserProfile, created, **kwargs):
    def is_member():
        if not (discord_id := instance.discord_id):
            return False, None
        current_term = Term.get_current_term()
        if current_term is None:
            return False, None
        return instance.is_member(current_term)[1] is not None, discord_id

    valid, discord_id = await sync_to_async(is_member)()
    if valid:
        await add_member_role(int(discord_id))


# Add member role when member due is purchased
@receiver(post_save, sender=PurchasedProduct)
async def update_roles_purchasedproduct_signal(sender, instance: PurchasedProduct, created, **kwargs):
    def is_member():
        if not (discord_id := instance.payment.user.userprofile.discord_id):
            return False, None
        return instance.payment.user.userprofile.is_member()[1] is not None, discord_id

    valid, discord_id = await sync_to_async(is_member)()
    if valid:
        await add_member_role(int(discord_id))


# Add UserProfile when User is created
@receiver(post_save, sender=User)
def update_user_signal(sender, instance, created, **kwargs):
    if created or not hasattr(instance, "userprofile"):
        UserProfile.objects.create(user=instance)
    instance.userprofile.save()


# Auto-fill major when UserProfile is created or when major is None for users with a valid NetID
@receiver(post_save, sender=UserProfile)
def update_profile_signal(sender, instance, created, **kwargs):
    if not instance.is_utd_affiliate:
        return
    if created or instance.major is None:
        instance.major = get_major_from_netid(instance.user.username) or "unknown"
        instance.save()
