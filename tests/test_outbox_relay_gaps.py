"""Outbox relay: backoff growth/cap and failure accounting."""
from datetime import timedelta

import pytest
from django.utils import timezone

from stapel_core.django.outbox import relay
from stapel_core.django.outbox.models import OutboxEvent


def test_backoff_grows_exponentially():
    assert relay._backoff(1) == timedelta(seconds=2)
    assert relay._backoff(3) == timedelta(seconds=8)
    assert relay._backoff(8) == timedelta(seconds=256)


def test_backoff_is_capped():
    assert relay._backoff(9) == timedelta(seconds=relay.MAX_BACKOFF_SECONDS)
    assert relay._backoff(64) == timedelta(seconds=relay.MAX_BACKOFF_SECONDS)
    assert relay.MAX_BACKOFF_SECONDS == 300


@pytest.mark.django_db(transaction=True)
def test_dispatch_one_missing_row_is_success():
    assert relay.dispatch_one(987654321) is True


@pytest.mark.django_db(transaction=True)
def test_dispatch_one_already_dispatched_row_is_success():
    row = OutboxEvent.objects.create(
        topic="t", event_json="{}", dispatched_at=timezone.now()
    )
    assert relay.dispatch_one(row.pk) is True


@pytest.mark.django_db(transaction=True)
def test_dispatch_pending_counts_failure_and_schedules_retry():
    row = OutboxEvent.objects.create(topic="t", event_json="not-json")
    before = timezone.now()
    delivered, failed = relay.dispatch_pending()
    assert (delivered, failed) == (0, 1)
    row.refresh_from_db()
    assert row.attempts == 1
    assert row.dispatched_at is None
    assert row.last_error
    assert row.next_attempt_at >= before + relay._backoff(1)


@pytest.mark.django_db(transaction=True)
def test_dispatch_pending_skips_rows_not_yet_due():
    OutboxEvent.objects.create(topic="t", event_json="not-json")
    relay.dispatch_pending()  # first failure pushes next_attempt_at forward
    delivered, failed = relay.dispatch_pending()  # row no longer due
    assert (delivered, failed) == (0, 0)
