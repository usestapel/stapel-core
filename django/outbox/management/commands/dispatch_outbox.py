"""Outbox relay: deliver pending Action events.

Run continuously in a worker container (microservices) or periodically via
celery beat / cron (monolith):

    python manage.py dispatch_outbox            # loop forever
    python manage.py dispatch_outbox --once     # single pass (cron/beat)
"""
import time

from django.core.management.base import BaseCommand

from stapel_core.django.outbox.relay import dispatch_pending


class Command(BaseCommand):
    help = "Deliver pending outbox events over the configured Action transport."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Single pass, then exit")
        parser.add_argument("--interval", type=float, default=1.0, help="Seconds between passes")
        parser.add_argument("--batch", type=int, default=100, help="Rows per pass")

    def handle(self, *args, **options):
        while True:
            delivered, failed = dispatch_pending(limit=options["batch"])
            if delivered or failed:
                self.stdout.write(f"outbox: delivered={delivered} failed={failed}")
            if options["once"]:
                break
            time.sleep(options["interval"])
