from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Test seed that always fails"

    def handle(self, *args, **options):
        raise RuntimeError("the seed source is unreachable")
