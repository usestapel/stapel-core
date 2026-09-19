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


class DeliveryNotConfirmed(RuntimeError):
    """The transport still had the message when the flush timeout expired."""


def _mark_failed(row: OutboxEvent, exc: Exception) -> None:
    row.attempts += 1
    row.next_attempt_at = timezone.now() + _backoff(row.attempts)
    row.last_error = repr(exc)[:2000]
    row.save(update_fields=["attempts", "next_attempt_at", "last_error"])
    logger.warning(
        "outbox delivery failed topic=%s attempts=%s: %r",
        row.topic, row.attempts, exc,
    )


def _mark_dispatched(row: OutboxEvent) -> None:
    row.dispatched_at = timezone.now()
    row.last_error = ""
    row.save(update_fields=["dispatched_at", "last_error"])


def _publish_row(row: OutboxEvent) -> bool:
    """Hand the row's event to the transport. Marks only FAILURE.

    Success is not known yet at this point — see :func:`_confirm_delivery`.
    """
    from stapel_core.bus.event import Event
    from stapel_core.comm.actions import deliver

    try:
        deliver(Event.from_json(row.event_json))
    except Exception as exc:
        _mark_failed(row, exc)
        return False
    return True


def _confirm_delivery(rows: list[OutboxEvent]) -> bool:
    """Wait for the transport's delivery reports before believing any of it.

    ``dispatched_at`` is the outbox's one claim about the outside world, and
    for an asynchronous producer ``deliver()`` returning is not evidence for
    it: librdkafka has accepted the message into a local queue and nothing
    has acknowledged anything. Marking the row there is how a message gets
    LOST rather than retried — the row says sent, so no later sweep touches
    it, and a process that exits before the queue drains takes it with it
    ("Producer terminating with 1 message still in queue", observed on a
    client host).

    So the mark waits for ``bus.flush()``, which returns once the delivery
    reports are in. Anything still pending leaves its rows unsent: the relay
    will send them again, and at-least-once with an idempotent subscriber is
    the contract everywhere else in this file too.
    """
    from stapel_core.bus import flush

    try:
        remaining = flush()
    except Exception as exc:  # a transport that cannot even be asked
        for row in rows:
            _mark_failed(row, exc)
        return False
    if remaining:
        exc = DeliveryNotConfirmed(
            f"{remaining} message(s) not confirmed by the transport before "
            f"the flush timeout; {len(rows)} outbox row(s) stay unsent"
        )
        for row in rows:
            _mark_failed(row, exc)
        return False
    return True


def _deliver_row(row: OutboxEvent) -> bool:
    """Publish one row and mark it only once delivery is confirmed."""
    if not _publish_row(row):
        return False
    if not _confirm_delivery([row]):
        return False
    _mark_dispatched(row)
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
        # Publish the whole batch, then wait ONCE. A flush per row would put
        # a broker round-trip between every two messages of a sweep; a flush
        # per batch keeps the relay's throughput and still marks nothing
        # before the transport has reported on all of it.
        published = [row for row in rows if _publish_row(row)]
        failed = len(rows) - len(published)
        if published and _confirm_delivery(published):
            for row in published:
                _mark_dispatched(row)
            delivered = len(published)
        else:
            failed += len(published)
    return delivered, failed
