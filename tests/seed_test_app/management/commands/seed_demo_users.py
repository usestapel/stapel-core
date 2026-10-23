from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Idempotent test seed: three demo users"

    def handle(self, *args, **options):
        User = get_user_model()
        for i in range(3):
            User.objects.get_or_create(username=f"seed-demo-{i}")
