from django.core.management.base import BaseCommand

from accounts.models import UserStub


class Command(BaseCommand):
    help = "Create a user stub for an incomplete registration."

    def add_arguments(self, parser):
        parser.add_argument("net_id", help="NetID to reserve for the registration.")
        parser.add_argument(
            "--after-registration-redirect-destination",
            default="",
            help="Optional URL to redirect to after registration completes.",
        )
        parser.add_argument(
            "--discord-user-id",
            help="Optional Discord user ID to associate with the new user profile.",
        )
        parser.add_argument(
            "--notify",
            action="store_true",
            help="Send the user-stub notification after creation.",
        )

    def handle(self, *args, **options):
        user_stub = UserStub.create(
            net_id=options["net_id"],
            after_registration_redirect_destination=options["after_registration_redirect_destination"],
            discord_user_id=options["discord_user_id"],
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Created user stub: {str(user_stub)} for Net ID {user_stub.net_id}\n"
                f"Registration URL: {user_stub.get_registration_url()}"
            )
        )

        if options["notify"]:
            UserStub.notify(user_stub)

            self.stdout.write(self.style.SUCCESS("sent notification!"))
