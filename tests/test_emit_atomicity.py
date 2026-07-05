"""Outbox-atomicity seam: mutate_and_emit() + runtime guards on emit().

The guarantee under test: "the event leaves iff the surrounding transaction
commits" — including the two failure classes that independently broke it in
module repos (categories C1: swallowed emit failure; listings L2: save and
emit in separate transactions).
"""
import logging

import pytest
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import override_settings

from stapel_core.comm import (
    EmitOutsideAtomicError,
    action_registry,
    emit,
    function_registry,
    mutate_and_emit,
    subscribe_action,
)
from stapel_core.django.outbox.models import OutboxEvent

User = get_user_model()


@pytest.fixture(autouse=True)
def clean_registries():
    function_registry.clear()
    action_registry.clear()
    yield
    function_registry.clear()
    action_registry.clear()


def _make_user(**kwargs):
    return User.objects.create(username=kwargs.pop("username", "u1"), **kwargs)


# ---------------------------------------------------------------------------
# mutate_and_emit — happy path
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_mutate_and_emit_commits_mutation_and_event_together():
    seen = []
    subscribe_action("user.created", lambda e: seen.append(e.payload))

    with mutate_and_emit() as emit_event:
        user = _make_user()
        event = emit_event("user.created", {"user_id": str(user.pk)}, key=str(user.pk))
        assert seen == []  # nothing delivered before commit

    assert seen == [{"user_id": str(user.pk)}]
    assert event.key == str(user.pk)
    assert User.objects.count() == 1
    assert OutboxEvent.objects.get().dispatched_at is not None


@pytest.mark.django_db(transaction=True)
def test_mutate_and_emit_multiple_emits_supported():
    seen = []
    subscribe_action("thing.changed", lambda e: seen.append(e.payload["n"]))

    with mutate_and_emit() as emit_event:
        _make_user()
        emit_event("thing.changed", {"n": 1})
        emit_event("thing.changed", {"n": 2})
        assert len(emit_event.events) == 2

    assert seen == [1, 2]
    assert OutboxEvent.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_mutate_and_emit_zero_emits_is_valid():
    # Idempotent early-return paths (recordings finalize) emit nothing.
    with mutate_and_emit() as emit_event:
        _make_user()

    assert emit_event.events == []
    assert User.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_mutate_and_emit_nested_in_outer_atomic_waits_for_outer_commit():
    seen = []
    subscribe_action("user.created", lambda e: seen.append(e.payload))

    with transaction.atomic():
        with mutate_and_emit() as emit_event:
            user = _make_user()
            emit_event("user.created", {"user_id": str(user.pk)})
        # inner block exited, but the outer transaction is still open
        assert seen == []

    assert len(seen) == 1


# ---------------------------------------------------------------------------
# mutate_and_emit — rollback semantics (the whole point)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_body_failure_rolls_back_mutation_and_event():
    seen = []
    subscribe_action("user.created", lambda e: seen.append(e))

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with mutate_and_emit() as emit_event:
            user = _make_user()
            emit_event("user.created", {"user_id": str(user.pk)})
            raise Boom()

    assert seen == []
    assert User.objects.count() == 0
    assert OutboxEvent.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_failing_emit_rolls_back_mutation(monkeypatch):
    """L2/C1 canonical test: emit fails -> the mutation must not commit."""
    from stapel_core.comm import actions

    def explode(event):
        raise RuntimeError("outbox write failed")

    monkeypatch.setattr(actions, "_emit_via_outbox", explode)

    with pytest.raises(RuntimeError):
        with mutate_and_emit() as emit_event:
            _make_user()
            emit_event("user.created", {"user_id": "u1"})

    assert User.objects.count() == 0
    assert OutboxEvent.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_swallowed_emit_failure_still_rolls_back(monkeypatch):
    """C1 adversarial: even a caller that swallows the emit failure cannot
    commit the mutation — emit marks the transaction rollback-only."""
    from stapel_core.comm import actions

    def explode(event):
        raise RuntimeError("outbox write failed")

    monkeypatch.setattr(actions, "_emit_via_outbox", explode)

    with mutate_and_emit() as emit_event:
        _make_user()
        try:
            emit_event("user.created", {"user_id": "u1"})
        except RuntimeError:
            pass  # the C1 anti-pattern — swallow and hope

    assert User.objects.count() == 0
    assert OutboxEvent.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_swallowed_plain_emit_failure_inside_atomic_rolls_back(monkeypatch):
    """Same guarantee without the helper: plain emit() inside atomic."""
    from stapel_core.comm import actions

    def explode(event):
        raise RuntimeError("outbox write failed")

    monkeypatch.setattr(actions, "_emit_via_outbox", explode)

    with transaction.atomic():
        _make_user()
        try:
            emit("user.created", {"user_id": "u1"})
        except RuntimeError:
            pass

    assert User.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_emitter_leaked_out_of_block_refuses_to_emit():
    with mutate_and_emit() as emit_event:
        _make_user()

    with pytest.raises(RuntimeError, match="after its block exited"):
        emit_event("user.created", {"user_id": "u1"})
    assert OutboxEvent.objects.count() == 0


# ---------------------------------------------------------------------------
# emit() outside transaction.atomic() — runtime guard
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_emit_outside_atomic_warns_by_default(caplog):
    with caplog.at_level(logging.WARNING, logger="stapel_core.comm.actions"):
        emit("user.created", {"user_id": "u1"})
    assert "outside transaction.atomic()" in caplog.text
    # still emitted (warn, not error) and delivered via autocommit on_commit
    assert OutboxEvent.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_emit_outside_atomic_error_mode_raises():
    with override_settings(STAPEL_COMM={"EMIT_OUTSIDE_ATOMIC": "error"}):
        with pytest.raises(EmitOutsideAtomicError):
            emit("user.created", {"user_id": "u1"})
    assert OutboxEvent.objects.count() == 0  # raised before the outbox write


@pytest.mark.django_db(transaction=True)
def test_emit_outside_atomic_allow_mode_is_silent(caplog):
    with override_settings(STAPEL_COMM={"EMIT_OUTSIDE_ATOMIC": "allow"}):
        with caplog.at_level(logging.WARNING, logger="stapel_core.comm.actions"):
            emit("user.created", {"user_id": "u1"})
    assert "outside transaction.atomic()" not in caplog.text
    assert OutboxEvent.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_emit_inside_atomic_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING, logger="stapel_core.comm.actions"):
        with transaction.atomic():
            emit("user.created", {"user_id": "u1"})
    assert "outside transaction.atomic()" not in caplog.text


@pytest.mark.django_db(transaction=True)
def test_emit_in_on_commit_callback_is_flagged(caplog):
    """Adversarial: emitting from on_commit runs after commit — outside any
    transaction — so a crash in between silently loses the event. The
    outside-atomic guard fires for it."""
    with caplog.at_level(logging.WARNING, logger="stapel_core.comm.actions"):
        with transaction.atomic():
            _make_user()
            transaction.on_commit(lambda: emit("user.created", {"user_id": "u1"}))
    assert "outside transaction.atomic()" in caplog.text


def test_emit_without_outbox_skips_the_guard():
    # Synchronous mode has no outbox row to keep atomic with the mutation.
    seen = []
    subscribe_action("x.y", lambda e: seen.append(e))
    with override_settings(
        STAPEL_COMM={"OUTBOX_ENABLED": False, "EMIT_OUTSIDE_ATOMIC": "error"}
    ):
        emit("x.y", {"a": 1})
    assert len(seen) == 1
