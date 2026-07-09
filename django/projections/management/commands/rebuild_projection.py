"""Rebuild (or drift-check) a comm Projection from its owner's snapshot.

    python manage.py rebuild_projection catalog.listing_likes
    python manage.py rebuild_projection catalog.listing_likes --check
    python manage.py rebuild_projection catalog.listing_likes --batch-size 1000

Rebuild re-derives the whole read-model table from the projection's
``source_of_truth`` Function (batched, all-or-nothing) — the first-class
replacement for a hand-written backfill script (module-communication §10).
``--check`` compares row counts without writing.
"""
from django.core.management.base import BaseCommand, CommandError

from stapel_core.comm.exceptions import ProjectionConfigError
from stapel_core.comm.projections import drift_check, projection_status, rebuild


class Command(BaseCommand):
    help = "Rebuild a comm Projection read-model from its owner's snapshot Function."

    def add_arguments(self, parser):
        parser.add_argument("name", help="Projection name (e.g. catalog.listing_likes)")
        parser.add_argument(
            "--batch-size", type=int, default=500,
            help="Rows requested per source_of_truth call (default 500).",
        )
        parser.add_argument(
            "--check", action="store_true",
            help="Only compare local vs source row counts; write nothing.",
        )

    def handle(self, *args, **options):
        name = options["name"]
        batch_size = options["batch_size"]
        try:
            if options["check"]:
                report = drift_check(name, batch_size=batch_size)
                verdict = "in sync" if report.in_sync else "DRIFT"
                self.stdout.write(
                    f"{name}: local={report.local} source={report.source} [{verdict}]"
                )
                return

            def progress(done, total):
                suffix = f"/{total}" if total is not None else ""
                self.stdout.write(f"  {name}: {done}{suffix} row(s) rebuilt")

            result = rebuild(name, batch_size=batch_size, on_progress=progress)
            status = projection_status(name)
            self.stdout.write(self.style.SUCCESS(
                f"rebuilt {result.name}: {result.rows} row(s) in "
                f"{result.batches} batch(es); table now holds {status.rows} row(s)"
            ))
        except ProjectionConfigError as exc:
            raise CommandError(str(exc)) from exc
