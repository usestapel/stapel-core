"""A reply that was produced must not be lost because nobody heard it.

The incident, on a client host, 2026-09-20, twice inside half an hour.

The caller runs its task executor inside a bus consumer whose HEALTHCHECK
budget (90s) is shorter than its own longest handler (an STT call over a
148-minute meeting). The probe went red on a process that was merely busy,
the supervisor restarted it, and the provider published the finished answer
21 seconds later into an inbox that had died with the process. NATS core
request-reply keeps nothing: the broker dropped both replies. Both tasks sat
RUNNING until their deadline and were buried ``deadline_exceeded`` — one
paid transcription and one six-call summary, produced, charged, and thrown
away.

Three properties, one per hop:

1. the provider writes the finished frame to the shared store BEFORE it
   publishes, under a key the caller can compute on its own;
2. a caller that heard nothing — this attempt or the next process — reads
   it back instead of buying the work again;
3. a deadline never buries a task while a finished answer is waiting.

Plus the trigger: a consumer that is inside a handler now says so, so a
probe can stop treating "busy" and "hung" as the same word.

Nothing here sleeps and nothing here needs a broker.
"""
from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from stapel_core.bus import liveness as liveness_mod
from stapel_core.bus.liveness import ConsumerLiveness
from stapel_core.comm import overflow
from stapel_core.comm import tasks as task_primitive
from stapel_core.comm.exceptions import FunctionCallError
from stapel_core.comm.functions import call
from stapel_core.comm.registry import action_registry, function_registry
from stapel_core.comm.tasks import (
    TASK_REQUESTED,
    clear_handlers,
    execute,
    handle_task_requested,
    recoverable_replies,
    register_task,
    start,
)
from stapel_core.django.management.commands.serve_functions import (
    decode_envelope,
    fit_reply,
    persist_reply,
)
from stapel_core.django.taskstore.models import TaskRecord

CAP = 8 * 1024 * 1024


class MemoryStore(overflow.OverflowStore):
    """The bucket both services share, in a dict."""

    name = "django"

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def put(self, key, data, *, ttl_seconds):
        self.objects[key] = data
        return key

    def get(self, key):
        return self.objects[key]

    def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)


class FakeBroker:
    """One NATS, with a switch for the failure that actually happened.

    ``drop_publishes`` is the caller's inbox dying mid-request: the provider
    runs to completion and answers, and the answer reaches nobody. That is
    not a provider failure and must not look like one.
    """

    def __init__(self, provider):
        self.provider = provider
        self.drop_publishes = 0
        self.provider_calls = 0
        self.max_payload = CAP

    def request(self, subject, data, timeout):
        # The full function name, exactly as serve_functions recovers it
        # from the subject prefix. Both ends MUST agree on it: it is half
        # of the key the caller comes back for.
        name = subject[len("stapel.fn."):]
        payload, reply_key = decode_envelope(data, name)

        self.provider_calls += 1
        try:
            frame = json.dumps({"result": self.provider(payload)}).encode()
        except Exception as exc:
            frame = json.dumps({"error": repr(exc)}).encode()

        # THE SHELF COPY GOES DOWN FIRST — the ordering is the mechanism.
        persist_reply(frame, name, reply_key)
        frame = fit_reply(frame, self.max_payload, name)

        if self.drop_publishes > 0:
            self.drop_publishes -= 1
            raise TimeoutError("nats: request timed out")
        return frame


class FakeBridge:
    def __init__(self, broker):
        self.broker = broker

    def max_payload(self, timeout=5.0):
        return self.broker.max_payload

    def request(self, subject, data, timeout):
        return self.broker.request(subject, data, timeout)


@pytest.fixture
def store(settings):
    st = MemoryStore()
    settings.STAPEL_COMM = {
        **(getattr(settings, "STAPEL_COMM", {}) or {}),
        "FUNCTION_TRANSPORT": "nats",
        "FUNCTION_TIMEOUT": 30.0,
        "LARGE_REPLY": {"STORE": st},
    }
    overflow.reset_store()
    yield st
    overflow.reset_store()


@pytest.fixture
def broker(store, monkeypatch):
    """A provider that answers a transcript, wired to a droppable broker."""

    def transcribe(payload):
        return {"segments": payload.get("segments", 3), "text": "…"}

    bus = FakeBroker(transcribe)
    monkeypatch.setattr(
        "stapel_core.comm.nats.get_bridge", lambda: FakeBridge(bus)
    )
    return bus


@contextmanager
def _caller_dies_before_reading(monkeypatch):
    """The window the incident happened in.

    The provider has answered and the frame is on the shelf; this process
    never gets to look, because it is being restarted. Blanking the read is
    how a test says "killed here" without killing the test runner.
    """
    monkeypatch.setattr(overflow, "take_durable_reply", lambda *a, **k: None)
    try:
        yield
    finally:
        monkeypatch.undo()


@pytest.fixture(autouse=True)
def clean():
    from stapel_core.comm.actions import subscribe_action

    clear_handlers()
    action_registry.clear()
    function_registry.clear()
    subscribe_action(TASK_REQUESTED, handle_task_requested)
    overflow.reset_store()
    yield
    clear_handlers()
    action_registry.clear()
    function_registry.clear()
    overflow.reset_store()


# ─────────────────────────────────────────────────────────────────────
# 1. The provider persists before it publishes
# ─────────────────────────────────────────────────────────────────────


class TestTheAnswerOutlivesThePublish:
    def test_an_unnamed_call_leaves_nothing_behind(self, broker, store):
        """No reply_key, no copy. A caller with no retry ladder has nothing
        to come back with, and a spare copy of a private payload in the
        bucket is a copy no erasure sweep would find."""
        assert call("llm.transcribe", {"id": "a"}, reply_key="") == {
            "segments": 3, "text": "…",
        }
        assert store.objects == {}

    def test_a_named_call_is_on_the_shelf_before_it_is_on_the_wire(
        self, broker, store
    ):
        call("llm.transcribe", {"id": "a"}, reply_key="rec-a")
        keys = list(store.objects)
        assert len(keys) == 1
        assert overflow.DURABLE_SEGMENT in keys[0]
        assert json.loads(store.objects[keys[0]])["result"]["segments"] == 3

    def test_an_error_is_never_persisted(self, store, monkeypatch):
        """An error is an answer worth retrying. A stored one would make a
        transient provider failure permanent until the TTL ran out."""

        def broken(payload):
            raise RuntimeError("provider is down")

        bus = FakeBroker(broken)
        monkeypatch.setattr(
            "stapel_core.comm.nats.get_bridge", lambda: FakeBridge(bus)
        )
        with pytest.raises(FunctionCallError):
            call("llm.transcribe", {"id": "a"}, reply_key="rec-a")
        assert store.objects == {}


# ─────────────────────────────────────────────────────────────────────
# 2. A caller that heard nothing collects it
# ─────────────────────────────────────────────────────────────────────


class TestALostPublishCostsAReadNotABill:
    def test_without_a_reply_key_a_dropped_publish_loses_the_work(
        self, broker, store
    ):
        """THE REGRESSION TEST. This is precisely what happened on the host:
        the provider ran, the answer went nowhere, and the caller learned
        only that it had timed out."""
        broker.drop_publishes = 1
        with pytest.raises(FunctionCallError):
            call("llm.transcribe", {"id": "a"}, reply_key="")
        assert broker.provider_calls == 1
        assert store.objects == {}, "nothing was kept — the work is gone"

    def test_a_dropped_publish_is_recovered_inside_the_same_call(
        self, broker, store
    ):
        broker.drop_publishes = 1
        result = call("llm.transcribe", {"id": "a"}, reply_key="rec-a")
        assert result == {"segments": 3, "text": "…"}
        assert broker.provider_calls == 1, "the provider was not asked twice"
        assert store.objects == {}, "collected mail does not stay in the box"

    def test_a_later_process_collects_what_this_one_never_heard(
        self, broker, store, monkeypatch
    ):
        """The restart case. The first caller never gets to read the shelf —
        on the host it was killed by its own healthcheck — and a second
        process, with the same key, finds the answer waiting."""
        broker.drop_publishes = 1
        with _caller_dies_before_reading(monkeypatch):
            with pytest.raises(FunctionCallError):
                call("llm.transcribe", {"id": "a"}, reply_key="rec-a")

        assert broker.provider_calls == 1
        assert len(store.objects) == 1, "the answer survived the caller"

        # A fresh process, same call, same key.
        result = call("llm.transcribe", {"id": "a"}, reply_key="rec-a")
        assert result == {"segments": 3, "text": "…"}
        assert broker.provider_calls == 1, "no second bill"

    def test_a_reply_by_reference_survives_the_same_way(self, broker, store):
        """An oversized answer takes two roads at once: the wire carries a
        $ref, the shelf carries the whole frame. Losing the wire must not
        cost the work either."""
        broker.max_payload = 512
        broker.drop_publishes = 1
        result = call("llm.transcribe", {"id": "a", "segments": 41}, reply_key="big")
        assert result["segments"] == 41
        assert broker.provider_calls == 1


# ─────────────────────────────────────────────────────────────────────
# 3. A task's calls are named for it, and its deadline respects them
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.django_db(transaction=True)
class TestATaskNamesItsOwnCalls:
    def test_the_key_is_stable_across_attempts(self, broker, store):
        seen = []

        def handler(payload):
            seen.append(task_primitive.reply_key_for("llm.transcribe", payload))
            return call("llm.transcribe", payload)

        register_task("llm.transcribe", handler)
        task_id = start("llm.transcribe", {"id": "a"})
        execute(task_id)
        record = TaskRecord.objects.get(pk=task_id)
        record.state = TaskRecord.PENDING
        record.save(update_fields=["state"])
        execute(task_id)

        assert len(seen) == 2
        assert seen[0] == seen[1], "attempt two must be able to find attempt one's answer"
        assert seen[0].startswith(task_id)

    def test_a_successful_task_leaves_no_copy_in_the_bucket(self, broker, store):
        register_task(
            "llm.transcribe", lambda payload: call("llm.transcribe", payload)
        )
        task_id = start("llm.transcribe", {"id": "a"})
        execute(task_id)
        assert TaskRecord.objects.get(pk=task_id).state == TaskRecord.DONE
        assert store.objects == {}


@pytest.mark.django_db(transaction=True)
class TestADeadlineDoesNotBuryPaidWork:
    def _stalled_task(self, broker, monkeypatch):
        """The incident, reproduced: one task, RUNNING past its deadline,
        whose answer is on the shelf and whose caller never heard it."""
        from django.utils import timezone

        register_task(
            "llm.transcribe", lambda payload: call("llm.transcribe", payload)
        )
        broker.drop_publishes = 1
        with _caller_dies_before_reading(monkeypatch):
            task_id = start("llm.transcribe", {"id": "a"})

        assert broker.provider_calls == 1, "the provider ran and was charged"
        TaskRecord.objects.filter(pk=task_id).update(
            state=TaskRecord.RUNNING, attempts=1, deadline=timezone.now()
        )
        return task_id

    def test_the_sweep_sees_the_waiting_reply(self, broker, store, monkeypatch):
        task_id = self._stalled_task(broker, monkeypatch)
        record = TaskRecord.objects.get(pk=task_id)
        waiting = recoverable_replies(record)
        assert [fn for fn, _ in waiting] == ["llm.transcribe"]

    def test_the_sweep_re_announces_instead_of_failing(
        self, broker, store, monkeypatch
    ):
        from django.core.management import call_command

        task_id = self._stalled_task(broker, monkeypatch)
        bought = broker.provider_calls

        call_command("sweep_tasks")

        record = TaskRecord.objects.get(pk=task_id)
        assert record.state != TaskRecord.FAILED, (
            "a paid result that exists must never expire unread"
        )
        assert record.state == TaskRecord.DONE
        assert record.result == {"segments": 3, "text": "…"}
        assert broker.provider_calls == bought, (
            "the recovery cost a read, not a bill"
        )

    def test_a_task_with_nothing_waiting_still_fails(self, broker, store):
        from django.core.management import call_command
        from django.utils import timezone

        register_task("llm.transcribe", lambda p: call("llm.transcribe", p))
        task_id = start("llm.transcribe", {"id": "a"})
        TaskRecord.objects.filter(pk=task_id).update(
            state=TaskRecord.RUNNING, deadline=timezone.now(), attempts=1
        )
        call_command("sweep_tasks")
        assert TaskRecord.objects.get(pk=task_id).state == TaskRecord.FAILED

    def test_an_exhausted_ladder_is_not_retried_forever(
        self, broker, store, monkeypatch
    ):
        from django.core.management import call_command

        task_id = self._stalled_task(broker, monkeypatch)
        TaskRecord.objects.filter(pk=task_id).update(attempts=3, max_attempts=3)
        call_command("sweep_tasks")
        assert TaskRecord.objects.get(pk=task_id).state == TaskRecord.FAILED


@pytest.mark.django_db(transaction=True)
def test_a_task_id_with_no_row_is_logged_not_dropped(caplog):
    """A reply for a task nobody has is the quietest failure in the seam.
    Before this it was a bare ``return``."""
    import logging
    import uuid

    with caplog.at_level(logging.ERROR, logger="stapel_core.comm.tasks"):
        execute(str(uuid.uuid4()))
    assert any("no such row exists" in r.getMessage() for r in caplog.records)


# ─────────────────────────────────────────────────────────────────────
# 4. The trigger: busy is not hung
# ─────────────────────────────────────────────────────────────────────


def _probe(*, max_age: float, path: str, max_handler_age: float = 0.0):
    """Run the healthcheck the way a HEALTHCHECK line does."""
    from stapel_core.django.management.commands.bus_consumer_alive import Command

    return Command().handle(
        max_age=max_age, max_handler_age=max_handler_age, path=path
    )


class TestAProbeCanTellBusyFromHung:
    def _liveness(self, tmp_path):
        alive = ConsumerLiveness(
            group="recordings.actions", heartbeat=str(tmp_path / "hb")
        )
        alive.on_assign([1, 2, 3])
        alive.touch_heartbeat()
        return alive

    def test_a_polling_consumer_says_poll(self, tmp_path):
        alive = self._liveness(tmp_path)
        state = liveness_mod.heartbeat_state(alive.heartbeat)
        assert state["state"] == liveness_mod.HEARTBEAT_POLL

    def test_a_handler_in_flight_says_so_and_carries_its_budget(self, tmp_path):
        alive = self._liveness(tmp_path)
        with alive.handling(budget=3600):
            state = liveness_mod.heartbeat_state(alive.heartbeat)
            assert state["state"] == liveness_mod.HEARTBEAT_HANDLING
            assert state["budget"] == 3600
        assert (
            liveness_mod.heartbeat_state(alive.heartbeat)["state"]
            == liveness_mod.HEARTBEAT_POLL
        ), "the marker is cleared the moment the handler returns"

    def test_the_probe_passes_a_long_handler_within_its_budget(self, tmp_path):
        alive = self._liveness(tmp_path)
        with alive.handling(budget=3600):
            # 90s is the budget the incident's container was configured
            # with, and this is the call that used to fail under it.
            _probe(max_age=90, path=alive.heartbeat)

    def test_the_probe_still_fails_a_handler_past_its_budget(self, tmp_path):
        import time

        alive = self._liveness(tmp_path)
        with alive.handling(budget=0.02):
            time.sleep(0.05)
            with pytest.raises(SystemExit) as exc:
                _probe(max_age=600, path=alive.heartbeat)
            assert exc.value.code == 1

    def test_an_empty_legacy_heartbeat_still_reads_as_polling(self, tmp_path):
        path = tmp_path / "hb"
        path.write_bytes(b"")
        state = liveness_mod.heartbeat_state(str(path))
        assert state["state"] == liveness_mod.HEARTBEAT_POLL
        assert state["age"] < 5
