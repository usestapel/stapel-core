"""Create upcoming PostgreSQL time-partitions for the event table.

    python manage.py eventstore_partition --periods-ahead 3

Idempotent: emits ``CREATE TABLE IF NOT EXISTS … PARTITION OF …`` for the
current period plus N future ones, so writes always land in an existing
partition. No-op unless the database is PostgreSQL *and* the base table has
been converted to a partitioned parent (see ``partitions.parent_ddl`` — a
one-time ops/RunSQL step). On the SQLite minimal profile the table is a plain
table and this command reports that it skipped, changing nothing.

``--dry-run`` prints the SQL without executing it (useful in CI / review).
"""
from django.core.management.base import BaseCommand
from django.db import connection

from stapel_core.django.eventstore import partitions
from stapel_core.eventstore.conf import eventstore_settings


class Command(BaseCommand):
    help = "Create current + upcoming time-partitions of the event table (PostgreSQL)."

    def add_arguments(self, parser):
        parser.add_argument("--periods-ahead", type=int, default=3)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        period = str(eventstore_settings.PARTITION_PERIOD)
        statements = partitions.ensure_partitions_sql(
            periods_ahead=options["periods_ahead"], period=period
        )

        if options["dry_run"]:
            for sql in statements:
                self.stdout.write(sql)
            return

        if connection.vendor != "postgresql":
            self.stdout.write(
                f"eventstore_partition: backend is {connection.vendor!r}, not "
                "postgresql — partitions skipped (plain-table degradation)."
            )
            return

        with connection.cursor() as cursor:
            for sql in statements:
                cursor.execute(sql)
        self.stdout.write(
            f"eventstore_partition: ensured {len(statements)} {period} partition(s)."
        )
