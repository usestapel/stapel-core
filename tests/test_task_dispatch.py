"""TASK_DISPATCH — routing task.* events to a broker of their own.

"action" (default) keeps today's behavior: task.requested rides
ACTION_TRANSPORT. "bus" publishes task.* directly via stapel_core.bus even
when Actions stay in-process. "inline" makes start() run synchronously.
"""
import pytest
from django.db import transaction
from django.test import override_settings

from stapel_core.bus import get_bus
from stapel_core.bus.event import Event
from stapel_core.comm.actions import deliver, subscribe_action
from stapel_core.comm.registry import action_registry
from stapel_core.comm.tasks import (
    TASK_COMPLETED,
    TASK_REQUESTED,
    clear_handlers,
    handle_task_requested,
    register_task,
    start,
    status,
)


@pytest.fixture(autouse=True)
def clean():
    clear_handlers()
    action_registry.clear()
    yield
    clear_handlers()
    action_registry.clear()


def _event(name: str, payload: dict | None = None) -> Event:
    return Event(event_type=name, service="tests", payload=payload or {})


class TestDeliverRouting:
    def test_bus_dispatch_routes_task_events_to_bus(self):
        seen = []
        subscribe_action(TASK_REQUESTED, seen.append)
        with override_settings(STAPEL_COMM={"TASK_DISPATCH": "bus"}):
            deliver(_event(TASK_REQUESTED, {"task_id": "1", "kind": "k"}))
        assert [e.event_type for e in get_bus().events] == [TASK_REQUESTED]
        assert seen == []  # in-process subscribers are bypassed

    def test_bus_dispatch_routes_every_task_prefixed_event(self):
        with override_settings(STAPEL_COMM={"TASK_DISPATCH": "bus"}):
            deliver(_event(TASK_COMPLETED, {"task_id": "1"}))
        assert [e.event_type for e in get_bus().events] == [TASK_COMPLETED]

    def test_bus_dispatch_leaves_other_actions_inprocess(self):
        seen = []
        subscribe_action("user.deleted", seen.append)
        with override_settings(STAPEL_COMM={"TASK_DISPATCH": "bus"}):
            deliver(_event("user.deleted", {"user_id": "1"}))
        assert len(seen) == 1
        assert get_bus().events == []

    def test_action_dispatch_default_unchanged(self):
        seen = []
        subscribe_action(TASK_REQUESTED, seen.append)
        deliver(_event(TASK_REQUESTED, {"task_id": "1", "kind": "k"}))
        assert len(seen) == 1
        assert get_bus().events == []

    def test_explicit_action_dispatch_unchanged(self):
        seen = []
        subscribe_action(TASK_REQUESTED, seen.append)
        with override_settings(STAPEL_COMM={"TASK_DISPATCH": "action"}):
            deliver(_event(TASK_REQUESTED, {"task_id": "1", "kind": "k"}))
        assert len(seen) == 1
        assert get_bus().events == []


@pytest.mark.django_db(transaction=True)
class TestOutboxPath:
    def test_start_publishes_to_bus_after_commit_without_local_run(self):
        from stapel_core.django.outbox.models import OutboxEvent
        from stapel_core.django.taskstore.models import TaskRecord

        ran = []
        register_task("noop", lambda p: ran.append(p) or {})
        subscribe_action(TASK_REQUESTED, handle_task_requested)

        with override_settings(STAPEL_COMM={"TASK_DISPATCH": "bus"}):
            with transaction.atomic():
                task_id = start("noop", {"n": 1})
                assert get_bus().events == []  # nothing until commit

        events = get_bus().events
        assert [e.event_type for e in events] == [TASK_REQUESTED]
        assert events[0].payload["task_id"] == task_id
        # the task waits for a bus worker — no in-process execution
        assert ran == []
        assert status(task_id).state == TaskRecord.PENDING
        # transactional guarantee: the outbox row was written and dispatched
        row = OutboxEvent.objects.get()
        assert row.dispatched_at is not None

    def test_outbox_relay_routes_task_events_to_bus(self):
        from stapel_core.django.outbox.models import OutboxEvent
        from stapel_core.django.outbox.relay import dispatch_pending

        event = _event(TASK_REQUESTED, {"task_id": "x", "kind": "k"})
        OutboxEvent.objects.create(topic=event.event_type, event_json=event.to_json())
        with override_settings(STAPEL_COMM={"TASK_DISPATCH": "bus"}):
            delivered, failed = dispatch_pending()
        assert (delivered, failed) == (1, 0)
        assert [e.event_type for e in get_bus().events] == [TASK_REQUESTED]

    def test_bus_worker_side_executes_via_handle_task_requested(self):
        from stapel_core.django.taskstore.models import TaskRecord

        register_task("math.triple", lambda p: {"value": p["n"] * 3})
        with override_settings(STAPEL_COMM={"TASK_DISPATCH": "bus"}):
            task_id = start("math.triple", {"n": 2})
            assert status(task_id).state == TaskRecord.PENDING
            # ...what the dedicated worker's bus consumer does:
            handle_task_requested(get_bus().events[-1])
            st = status(task_id)
        assert st.state == TaskRecord.DONE
        assert st.result == {"value": 6}
        # completion is a task.* event too — it also travels via the bus
        assert [e.event_type for e in get_bus().events] == [
            TASK_REQUESTED,
            TASK_COMPLETED,
        ]

    def test_action_dispatch_executes_inprocess_after_commit(self):
        from stapel_core.django.taskstore.models import TaskRecord

        register_task("math.double", lambda p: {"value": p["n"] * 2})
        subscribe_action(TASK_REQUESTED, handle_task_requested)
        with transaction.atomic():
            task_id = start("math.double", {"n": 21})
            assert status(task_id).state == TaskRecord.PENDING
        st = status(task_id)
        assert st.state == TaskRecord.DONE
        assert st.result == {"value": 42}
        assert get_bus().events == []

    def test_inline_dispatch_runs_synchronously_in_start(self):
        from stapel_core.django.taskstore.models import TaskRecord

        register_task("math.double", lambda p: {"value": p["n"] * 2})
        with override_settings(STAPEL_COMM={"TASK_DISPATCH": "inline"}):
            with transaction.atomic():
                task_id = start("math.double", {"n": 4})
                st = status(task_id)  # done before the commit
                assert st.state == TaskRecord.DONE
                assert st.result == {"value": 8}
        assert st.attempts == 1
