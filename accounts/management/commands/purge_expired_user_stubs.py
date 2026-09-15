from django.core.management.base import BaseCommand

from accounts.models import UserStub


class Command(BaseCommand):
    help = "Delete expired user stubs. Never touches real user accounts."

    def handle(self, *args, **options):
        deleted_count, deleted_objects = UserStub.purge_deletion_candidates()
        user_stub_count = deleted_objects.get("accounts.UserStub", 0)

        self.stdout.write(
            self.style.SUCCESS(
                f"Purged {user_stub_count} expired user stub(s) ({deleted_count} object(s) deleted in total)."
            )
        )
