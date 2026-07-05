"""Apply event-store retention (run via cron / celery beat).

    python manage.py sweep_eventstore

Deletes raw events older than ``STAPEL_EVENTSTORE["RETENTION"][stream]`` days
and rollup buckets older than ``STAPEL_EVENTSTORE["RETENTION_ROLLUP"][stream]``
days (raw retention ≠ rollup retention). Streams absent from a map are kept
forever. Deletion goes through the resolved backend, so a routed stream is
purged in whatever engine holds it.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from stapel_core import eventstore
from stapel_core.eventstore.conf import eventstore_settings


class Command(BaseCommand):
    help = "Delete event-store raw/rollup rows past their per-stream retention."

    def handle(self, *args, **options):
        now = timezone.now()
        eventstore.flush()

        raw = eventstore_settings.RETENTION or {}
        rollup = eventstore_settings.RETENTION_ROLLUP or {}

        total_raw = 0
        for stream, days in raw.items():
            cutoff = now - timedelta(days=float(days))
            backend = eventstore.resolve_backend(stream)
            count = backend.purge(stream, older_than=cutoff)
            total_raw += count
            self.stdout.write(f"sweep_eventstore: purged {count} raw {stream!r} (<{days}d)")

        total_rollup = 0
        for stream, days in rollup.items():
            cutoff = now - timedelta(days=float(days))
            backend = eventstore.resolve_backend(stream)
            count = backend.purge_rollup(stream, older_than=cutoff)
            total_rollup += count
            self.stdout.write(
                f"sweep_eventstore: purged {count} rollup {stream!r} (<{days}d)"
            )

        self.stdout.write(
            f"sweep_eventstore: done (raw={total_raw}, rollup={total_rollup})"
        )
