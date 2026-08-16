"""One-time code store: absence, wrongness, budget, outage."""
import time
from unittest.mock import patch

import pytest
from django.core.cache import cache

from stapel_core.verification.codes import (
    CodeOutcome,
    OneTimeCodeStore,
    StoreUnavailable,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def store():
    return OneTimeCodeStore("otp_email")


# ── the four things a user hits ──────────────────────────────────────────────


def test_right_code_passes_and_is_single_use(store):
    store.issue("a@example.com", "111111", ttl=600, max_attempts=5)
    assert store.check("a@example.com", "111111").outcome is CodeOutcome.OK
    # replay of a spent code is absence, not wrongness
    assert store.check("a@example.com", "111111").outcome is CodeOutcome.NOT_FOUND


def test_wrong_code_is_mismatch_with_a_shrinking_budget(store):
    store.issue("a@example.com", "111111", ttl=600, max_attempts=3)
    first = store.check("a@example.com", "222222")
    assert first.outcome is CodeOutcome.MISMATCH
    assert first.attempts_remaining == 2
    assert store.check("a@example.com", "222222").attempts_remaining == 1


def test_no_entry_is_absence_not_wrongness(store):
    """The day's canon: an expired wait is not a refusal."""
    assert store.check("nobody@example.com", "111111").outcome is CodeOutcome.NOT_FOUND


def test_expired_ttl_reads_as_absence(store):
    store.issue("a@example.com", "111111", ttl=1, max_attempts=5)
    cache.delete(store._code_key("a@example.com"))  # what the TTL does, without the wait
    assert store.check("a@example.com", "111111").outcome is CodeOutcome.NOT_FOUND


def test_a_restart_reads_as_absence_too(store):
    """Redis is not durable; a flush must be honest, never an admit."""
    store.issue("a@example.com", "111111", ttl=600, max_attempts=5)
    cache.clear()
    assert store.check("a@example.com", "111111").outcome is CodeOutcome.NOT_FOUND


def test_spent_budget_blocks_and_kills_the_code(store):
    store.issue("a@example.com", "111111", ttl=600, max_attempts=2)
    assert store.check("a@example.com", "222222").outcome is CodeOutcome.MISMATCH
    spent = store.check("a@example.com", "222222", block_seconds=600)
    assert spent.outcome is CodeOutcome.BLOCKED
    assert spent.retry_after > 0
    # the right code no longer helps while the block stands
    assert store.check("a@example.com", "111111").outcome is CodeOutcome.BLOCKED


def test_outage_fails_closed(store):
    store.issue("a@example.com", "111111", ttl=600, max_attempts=5)
    with patch.object(cache, "get", side_effect=ConnectionError("redis is gone")):
        result = store.check("a@example.com", "111111")
    assert result.outcome is CodeOutcome.UNAVAILABLE


def test_outage_on_issue_raises_rather_than_pretending(store):
    with patch.object(cache, "set", side_effect=ConnectionError("redis is gone")):
        with pytest.raises(StoreUnavailable):
            store.issue("a@example.com", "111111", ttl=600, max_attempts=5)


# ── the code never rests in the clear ────────────────────────────────────────


def test_the_stored_record_does_not_contain_the_code(store):
    store.issue("a@example.com", "111111", ttl=600, max_attempts=5)
    record = cache.get(store._code_key("a@example.com"))
    assert "111111" not in repr(record)
    assert record["digest"] != "111111"


def test_the_key_does_not_contain_the_identifier(store):
    assert "a@example.com" not in store._code_key("a@example.com")


def test_equal_codes_for_the_same_identifier_hash_differently(store):
    store.issue("a@example.com", "111111", ttl=600, max_attempts=5)
    first = cache.get(store._code_key("a@example.com"))["digest"]
    store.issue("a@example.com", "111111", ttl=600, max_attempts=5)
    assert cache.get(store._code_key("a@example.com"))["digest"] != first


def test_purposes_do_not_share_entries():
    email = OneTimeCodeStore("otp_email")
    phone = OneTimeCodeStore("otp_phone")
    email.issue("shared", "111111", ttl=600, max_attempts=5)
    assert phone.check("shared", "111111").outcome is CodeOutcome.NOT_FOUND


# ── attempts and lifetime are one record ─────────────────────────────────────


def test_a_wrong_attempt_does_not_extend_the_wait(store):
    store.issue("a@example.com", "111111", ttl=600, max_attempts=5)
    with patch("stapel_core.verification.codes.time.time", return_value=time.time() + 300):
        store.check("a@example.com", "222222")
        record = cache.get(store._code_key("a@example.com"))
    # expires_at is the issue-time deadline, untouched by the attempt
    assert record["expires_at"] <= int(time.time()) + 600


def test_attempts_die_with_the_code(store):
    store.issue("a@example.com", "111111", ttl=600, max_attempts=5)
    store.check("a@example.com", "222222")
    store.issue("a@example.com", "333333", ttl=600, max_attempts=5)
    assert store.check("a@example.com", "444444").attempts_remaining == 4


# ── send-side budget ─────────────────────────────────────────────────────────


def test_cooldown_starts_at_issue(store):
    assert store.send_wait("a@example.com", cooldown=60, hourly_limit=10) == 0
    store.issue("a@example.com", "111111", ttl=600, max_attempts=5)
    assert store.send_wait("a@example.com", cooldown=60, hourly_limit=10) > 0


def test_cooldown_also_binds_the_device(store):
    store.issue("a@example.com", "111111", ttl=600, max_attempts=5, device_id="dev-1")
    assert store.send_wait("b@example.com", cooldown=60, hourly_limit=10, device_id="dev-1") > 0


def test_hourly_cap_is_a_rolling_window(store):
    for n in range(3):
        store.issue(f"{n}@example.com", "111111", ttl=600, max_attempts=5)
        store._spend_send_slot("same@example.com")
    assert store.send_wait("same@example.com", cooldown=0, hourly_limit=3) > 0
    assert store.send_wait("same@example.com", cooldown=0, hourly_limit=0) == 0


def test_send_wait_fails_closed(store):
    with patch.object(cache, "get", side_effect=ConnectionError("redis is gone")):
        with pytest.raises(StoreUnavailable):
            store.send_wait("a@example.com", cooldown=60, hourly_limit=10)


# ── block and discard ────────────────────────────────────────────────────────


def test_blocked_for_reports_the_remaining_wait(store):
    assert store.blocked_for("a@example.com") == 0
    store.block("a@example.com", 300)
    assert 0 < store.blocked_for("a@example.com") <= 300


def test_discard_removes_a_pending_code(store):
    store.issue("a@example.com", "111111", ttl=600, max_attempts=5)
    store.discard("a@example.com")
    assert store.check("a@example.com", "111111").outcome is CodeOutcome.NOT_FOUND
