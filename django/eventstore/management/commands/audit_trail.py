"""Everything that happened to one person, across every audit stream.

The per-module HTTP endpoints each show their own slice under their own
mandate model (a workspace admin sees membership history, an account holder
sees their sign-in history). The question an OPERATOR asks — "show me every
audit line about this person, wherever it was written" — crosses those
mandates, so it does not belong on any product API. With every journal in
one event store it is just a filtered read per stream, and this command is
that read: streams from ``STAPEL_EVENTSTORE["AUDIT_STREAMS"]`` (or
discovered by the ``audit``/``*.audit`` naming convention), matched on the
canonical actor/subject payload keys, merged newest-first.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand

#: The payload keys an audit line names a person under. Gateway lines carry
#: ``subject`` (the CallerContext field); domain-fact journals carry
#: ``actor_id``/``subject_id`` (who did it / whom it happened to). Matching
#: all three is what makes one command serve both journal kinds.
IDENTITY_KEYS = ("subject", "subject_id", "actor_id")


def _streams():
    from stapel_core.eventstore.conf import eventstore_settings

    configured = list(eventstore_settings.AUDIT_STREAMS or [])
    if configured:
        return configured
    # Discovery reads the raw table directly (there is no facade call for
    # "which streams exist"), so buffered appends must land first.
    from stapel_core import eventstore

    eventstore.flush()
    from stapel_core.django.eventstore.models import EventRecord

    names = EventRecord.objects.values_list("stream", flat=True).distinct()
    return sorted(n for n in names if n == "audit" or n.endswith(".audit"))


class Command(BaseCommand):
    help = "Print every audit line naming a person, across all audit streams."

    def add_arguments(self, parser):
        parser.add_argument("person", help="The id the audit lines name (actor or subject).")
        parser.add_argument(
            "--streams",
            help="Comma-separated stream names (overrides AUDIT_STREAMS/discovery).",
        )
        parser.add_argument(
            "--limit", type=int, default=200, help="Max lines per (stream, key) read."
        )

    def handle(self, *args, **options):
        from stapel_core import eventstore

        person = options["person"]
        streams = (
            [s for s in options["streams"].split(",") if s]
            if options["streams"]
            else _streams()
        )
        # One line may name the person twice (actor and subject); ids are only
        # unique per backend, so the stream joins the key.
        seen: set[tuple[str, int]] = set()
        rows = []
        for stream in streams:
            for key in IDENTITY_KEYS:
                page = eventstore.query(
                    stream,
                    filters={key: person},
                    limit=int(options["limit"]),
                    reverse=True,
                )
                for event in page:
                    if (event.stream, event.id) in seen:
                        continue
                    seen.add((event.stream, event.id))
                    rows.append(event)
        rows.sort(key=lambda e: (e.ts, e.id), reverse=True)
        for event in rows:
            self.stdout.write(
                f"{event.ts.isoformat()}  {event.stream}  "
                f"{json.dumps(event.payload, sort_keys=True, default=str)}"
            )
        if not rows:
            self.stdout.write(f"no audit lines name {person!r} in {streams}")
