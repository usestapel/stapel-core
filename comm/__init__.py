"""
stapel_core.comm — Action/Function inter-module communication.

Two primitives, one loose-coupling rule: modules never import each other —
both sides only know a string name and a payload schema.

Action — fire-and-forget fact ("user.deleted"). At-least-once, async,
0..N subscribers. Emitted through the transactional outbox: the event
leaves iff the surrounding DB transaction commits.

    from stapel_core.comm import emit, mutate_and_emit, on_action

    @on_action("user.deleted")
    def erase(event):
        Profile.objects.filter(user_id=event.payload["user_id"]).delete()

    # mutation + emit in ONE transaction (the canonical outbox pattern):
    with mutate_and_emit() as emit_event:
        user.delete()
        emit_event("user.deleted", {"user_id": str(user.pk)})

Function — synchronous call with a result ("cdn.media_exists"). Exactly
one provider per name.

    from stapel_core.comm import call, function

    @function("cdn.media_exists")
    def media_exists(payload):
        return {"exists": ...}

    result = call("cdn.media_exists", {"ref": ref}, timeout=2.0)

Transports are deployment configuration (STAPEL_COMM setting), not code:
monolith runs both primitives in-process (no broker at all), microservices
run Actions over the bus (Kafka/NATS) and Functions over internal HTTP.
See docs/module-communication.md in the stapel workspace for the design.
"""

from .actions import deliver, emit, mutate_and_emit, on_action, subscribe_action
from .tasks import (
    TaskNotFound,
    TaskStatus,
    register_task,
    start,
    status,
    task_handler,
)
from .config import comm_setting
from .exceptions import (
    CommError,
    EmitOutsideAtomicError,
    FunctionCallError,
    FunctionNotRegistered,
    FunctionRouteNotConfigured,
    ProjectionConfigError,
    ProjectionError,
)
from .functions import call, function, register_function
from .projections import (
    DriftReport,
    Projection,
    ProjectionStatus,
    RebuildResult,
    drift_check,
    projection_registry,
    projection_status,
    rebuild,
)
from .registry import action_registry, function_registry

__all__ = [
    "emit",
    "mutate_and_emit",
    "start",
    "status",
    "task_handler",
    "register_task",
    "TaskStatus",
    "TaskNotFound",
    "on_action",
    "subscribe_action",
    "deliver",
    "call",
    "function",
    "register_function",
    "Projection",
    "projection_registry",
    "rebuild",
    "drift_check",
    "projection_status",
    "RebuildResult",
    "DriftReport",
    "ProjectionStatus",
    "action_registry",
    "function_registry",
    "comm_setting",
    "CommError",
    "EmitOutsideAtomicError",
    "FunctionCallError",
    "FunctionNotRegistered",
    "FunctionRouteNotConfigured",
    "ProjectionError",
    "ProjectionConfigError",
]
