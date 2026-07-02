"""Outbox delivery: first-chance dispatch after commit + retrying relay."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import OutboxEvent

logger = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 300


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(2 ** attempts, MAX_BACKOFF_SECONDS))


def _deliver_row(row: OutboxEvent) -> bool:
    from stapel_core.bus.event import Event
    from stapel_core.comm.actions import deliver

    try:
        deliver(Event.from_json(row.event_json))
    except Exception as exc:
        row.attempts += 1
        row.next_attempt_at = timezone.now() + _backoff(row.attempts)
        row.last_error = repr(exc)[:2000]
        row.save(update_fields=["attempts", "next_attempt_at", "last_error"])
        logger.warning(
            "outbox delivery failed topic=%s attempts=%s: %r",
            row.topic, row.attempts, exc,
        )
        return False

    row.dispatched_at = timezone.now()
    row.last_error = ""
    row.save(update_fields=["dispatched_at", "last_error"])
    return True


def dispatch_one(pk) -> bool:
    """Deliver a single row (first-chance path from emit's on_commit)."""
    row = OutboxEvent.objects.filter(pk=pk, dispatched_at__isnull=True).first()
    if row is None:
        return True
    return _deliver_row(row)


def dispatch_pending(limit: int = 100) -> tuple[int, int]:
    """Deliver due undispatched rows. Returns (delivered, failed)."""
    from django.db import connection

    now = timezone.now()
    qs = OutboxEvent.objects.filter(
        dispatched_at__isnull=True, next_attempt_at__lte=now
    ).order_by("created_at")

    delivered = failed = 0
    with transaction.atomic():
        locked = qs.select_for_update(
            skip_locked=connection.features.has_select_for_update_skip_locked
        )[:limit]
        rows = list(locked)
        for row in rows:
            if _deliver_row(row):
                delivered += 1
            else:
                failed += 1
    return delivered, failed
