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

A handler that runs for longer than a healthcheck window USED TO BE
indistinguishable, from the outside, from a hung one — the advice here was
to pick ``--max-age`` above the longest handler and hope. A deployment that
did not, on 2026-09-20, had a supervisor restart a working consumer twice,
each time while a provider was publishing the answer it was waiting for;
both answers, both paid for, were dropped by a transport with no
persistence, and both tasks died ``deadline_exceeded``. The probe caused
the outage it exists to detect.

The consumer now says which state it is in (``stapel_core.bus.liveness``),
so this probe stops guessing:

* polling, file fresh within ``--max-age`` — alive, as before;
* inside a handler that has run less than its budget — alive, and the
  budget is ``--max-handler-age``, else ``STAPEL_BUS_HANDLER_BUDGET_SECONDS``
  as the consumer recorded it, else ``--max-age`` (today's behaviour);
* inside a handler that has outrun that budget — DEAD, and the message says
  so in those words, because that one is a hang;
* anything else stale — dead.

A failure while a handler is running names what the restart is about to
cost, so the next operator reads it in the log instead of reconstructing it
from a broker that kept nothing.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from stapel_core.bus.liveness import (
    HEARTBEAT_HANDLING,
    heartbeat_path,
    heartbeat_state,
)


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
            "--max-handler-age",
            type=float,
            default=0.0,
            help=(
                "Seconds one message may occupy the consumer before this "
                "probe calls it hung (default: the budget the consumer "
                "recorded, else --max-age)."
            ),
        )
        parser.add_argument(
            "--path",
            default=None,
            help="Heartbeat file (default: STAPEL_BUS_CONSUMER_HEARTBEAT_PATH)",
        )

    def handle(self, *args, **options):
        path = options["path"] or heartbeat_path()
        max_age = options["max_age"]
        state = heartbeat_state(path)

        if state is None:
            self.stderr.write(
                f"bus consumer heartbeat {path} does not exist — the consumer "
                f"has never had an assignment in this process"
            )
            raise SystemExit(1)

        age = state["age"]

        if state["state"] == HEARTBEAT_HANDLING:
            limit = options["max_handler_age"] or state["budget"] or max_age
            if age <= limit:
                self.stdout.write(
                    f"ok: handling one message for {age:.0f}s "
                    f"(budget {limit:.0f}s)"
                )
                return
            # Still a failure — a handler past its own budget is the hang
            # this probe is for. But say what restarting it costs, because
            # the process is demonstrably RUNNING something.
            self.stderr.write(
                f"bus consumer heartbeat {path}: the consumer has been inside "
                f"one handler for {age:.0f}s (budget {limit:.0f}s). If this "
                f"handler is merely slow rather than hung, restarting the "
                f"container now aborts it mid-message and any request it is "
                f"waiting on loses its reply — raise --max-handler-age or "
                f"STAPEL_BUS_HANDLER_BUDGET_SECONDS above this service's "
                f"longest handler."
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
