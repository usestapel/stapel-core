"""Liveness probe for a bus consumer, for a supervisor to run.

    HEALTHCHECK CMD python manage.py bus_consumer_alive --max-age 90

Exit 0 means the consumer touched its heartbeat file recently, and the
heartbeat is only touched while it actually owns partitions (Kafka) or holds
a live connection (NATS) — see :mod:`stapel_core.bus.liveness`. Exit 1 means
it did not, which is the state a container spent sixteen hours in while
Docker reported it ``Up``.

Deliberately a SEPARATE PROCESS. The watchdog this replaces ran inside the
consumer and read the same file the consumer refreshed on every poll, so it
could never notice the consumer failing; a probe that the judged process
cannot write is the whole point.

Pick ``--max-age`` above the longest handler in this service: while a handler
runs, the loop is not polling and the file is not refreshed. That is
intentional — a handler that runs for longer than a healthcheck window is
indistinguishable, from the outside, from a hung one.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from stapel_core.bus.liveness import heartbeat_age, heartbeat_path


class Command(BaseCommand):
    help = (
        "Exit 0 if this service's bus consumer touched its heartbeat file "
        "within --max-age seconds, 1 otherwise. Use as a Docker HEALTHCHECK."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--max-age",
            type=float,
            default=90.0,
            help="Seconds the heartbeat may be stale before this fails (default 90)",
        )
        parser.add_argument(
            "--path",
            default=None,
            help="Heartbeat file (default: STAPEL_BUS_CONSUMER_HEARTBEAT_PATH)",
        )

    def handle(self, *args, **options):
        path = options["path"] or heartbeat_path()
        max_age = options["max_age"]
        age = heartbeat_age(path)

        if age is None:
            self.stderr.write(
                f"bus consumer heartbeat {path} does not exist — the consumer "
                f"has never had an assignment in this process"
            )
            raise SystemExit(1)
        if age > max_age:
            self.stderr.write(
                f"bus consumer heartbeat {path} is {age:.0f}s old "
                f"(limit {max_age:.0f}s) — the consumer is not polling an "
                f"assignment"
            )
            raise SystemExit(1)
        self.stdout.write(f"ok: heartbeat {age:.0f}s old (limit {max_age:.0f}s)")
