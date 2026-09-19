"""The emit gate is LIVE — proven, not asserted.

``EMIT_OUTSIDE_ATOMIC`` was a switch that could not fire under tests:
pytest-django wraps every database test in a transaction, so the guard's
question (``connection.in_atomic_block``) had the answer True from the first
line of every test body. A suite could set the switch to "error", stay green
forever, and have proven nothing.

These tests are the gate's own self-test. The first one is the important
one: an ``emit()`` at the depth the test began at MUST raise here — if this
file goes green with the mechanism removed, the gate is inert again.
"""
from __future__ import annotations

import pytest
from django.db import connection, transaction

from stapel_core.comm import EmitOutsideAtomicError, emit, mutate_and_emit
from stapel_core.comm.atomic import atomic_depth, in_own_atomic_block, is_armed
from stapel_core.django.outbox.models import OutboxEvent


# ---------------------------------------------------------------------------
# The gate fires
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_emit_at_baseline_depth_raises_although_a_transaction_is_open():
    """THE self-test. Inside pytest-django's transaction — and still caught."""
    assert connection.in_atomic_block, "pytest-django's transaction is open"
    assert is_armed(connection.alias), "the gate fixture armed this connection"

    with pytest.raises(EmitOutsideAtomicError, match="outside transaction.atomic"):
        emit("user.created", {"user_id": "u1"})

    assert OutboxEvent.objects.count() == 0  # raised before the outbox write


@pytest.mark.django_db
def test_emit_one_level_deeper_than_the_baseline_is_fine():
    with transaction.atomic():
        emit("user.created", {"user_id": "u1"})
    assert OutboxEvent.objects.count() == 1


@pytest.mark.django_db
def test_mutate_and_emit_passes_the_gate():
    with mutate_and_emit() as emit_event:
        emit_event("user.created", {"user_id": "u1"})
    assert OutboxEvent.objects.count() == 1


@pytest.mark.django_db
def test_emit_after_its_own_atomic_block_closed_is_caught():
    """Back at baseline depth: the block that would have carried it is gone."""
    with transaction.atomic():
        pass
    with pytest.raises(EmitOutsideAtomicError):
        emit("user.created", {"user_id": "u1"})


@pytest.mark.django_db
def test_emit_in_an_on_commit_callback_is_caught_under_the_gate():
    """on_commit runs after the commit — outside the block, at baseline."""
    failures = []

    def _emit_late():
        try:
            emit("user.created", {"user_id": "u1"})
        except EmitOutsideAtomicError as exc:
            failures.append(exc)

    with transaction.atomic():
        transaction.on_commit(_emit_late)
        captured = connection.run_on_commit[-1]
    captured[1]()  # run it where on_commit would: at baseline depth

    assert failures, "an emit from on_commit must not pass the gate"


# ---------------------------------------------------------------------------
# The gate is what makes it fire — and it is removable
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_without_the_gate_the_guard_is_inert(emit_outside_atomic_allowed, settings):
    """The defect, preserved: error mode under pytest-django never fires.

    With the baseline disarmed the guard is back to ``in_atomic_block``,
    which the test transaction has already made True — so the strictest
    production setting there is lets this through without a sound. That is
    what every library's "we gate on it" suite was really running.
    """
    settings.STAPEL_COMM = {"EMIT_OUTSIDE_ATOMIC": "error"}
    assert not is_armed(connection.alias)

    emit("user.created", {"user_id": "u1"})  # no raise: the inert gate

    assert OutboxEvent.objects.count() == 1


@pytest.mark.django_db
def test_the_opt_out_is_restored_for_the_next_test():
    """The disarm above must not leak — this test is armed again."""
    assert is_armed(connection.alias)


# ---------------------------------------------------------------------------
# The depth measure itself
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_atomic_depth_counts_nesting():
    start = atomic_depth(connection)
    with transaction.atomic():
        assert atomic_depth(connection) == start + 1
        with transaction.atomic():
            assert atomic_depth(connection) == start + 2
        assert atomic_depth(connection) == start + 1
    assert atomic_depth(connection) == start


@pytest.mark.django_db
def test_atomic_depth_without_django_s_own_list():
    """The fallback measure agrees with the Django-native one.

    A connection wrapper that keeps no ``atomic_blocks`` (a third-party
    backend, an older Django) is still measurable: outermost block sets
    ``in_atomic_block``, each nested one pushes one savepoint id.
    """

    class _NoBlocks:
        atomic_blocks = None

        def __init__(self, in_atomic, savepoints):
            self.in_atomic_block = in_atomic
            self.savepoint_ids = savepoints

    assert atomic_depth(_NoBlocks(False, [])) == 0
    assert atomic_depth(_NoBlocks(True, [])) == 1
    assert atomic_depth(_NoBlocks(True, ["s1", "s2"])) == 3


def test_in_own_atomic_block_is_plain_in_atomic_block_in_production():
    """Nothing armed — production must behave exactly as it did."""
    from stapel_core.comm import atomic as atomic_mod

    class _Conn:
        alias = "no-such-alias"
        atomic_blocks = []
        in_atomic_block = False
        savepoint_ids = []

    conn = _Conn()
    assert not atomic_mod.is_armed(conn.alias)
    assert in_own_atomic_block(conn) is False
    conn.in_atomic_block = True
    assert in_own_atomic_block(conn) is True


@pytest.mark.django_db(transaction=True)
def test_gate_arms_at_zero_for_a_transactional_test():
    """No wrapping transaction here — the baseline is simply 0."""
    from stapel_core.comm import atomic as atomic_mod

    assert atomic_mod.baseline(connection.alias) == 0
    with pytest.raises(EmitOutsideAtomicError):
        emit("user.created", {"user_id": "u1"})
