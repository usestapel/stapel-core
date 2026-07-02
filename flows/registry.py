"""Flow objects and the registry — the documentation source of truth."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

STEP_HTTP = "http"
STEP_ACTION = "action"
STEP_FUNCTION = "function"
STEP_TASK = "task"
STEP_HUMAN = "human"

# Attribute set on view callables/classes by @flow_step; read by the doc
# engine, the OpenAPI hook and check_flows.
FLOWS_ATTR = "_stapel_flows"


@dataclass
class FlowStep:
    """One step of a flow.

    kind: http | action | function | task | human
    ref:  view "module.Class.method" for http; comm name for
          action/function/task; empty for human steps.
    """

    kind: str
    order: int
    note: str
    ref: str = ""
    extra: dict = field(default_factory=dict)

    def sort_key(self) -> tuple:
        return (self.order, self.kind, self.ref)


class Flow:
    """A named business scenario assembled from ordered steps."""

    def __init__(
        self,
        flow_id: str,
        *,
        title: str,
        description: str,
        actors: list[str] | None = None,
    ) -> None:
        self.id = flow_id
        self.title = title
        self.description = description
        self.actors = list(actors or [])
        self.steps: list[FlowStep] = []
        flow_registry.register(self)

    # ------------------------------------------------------------------
    # Non-HTTP steps
    # ------------------------------------------------------------------

    def action(self, name: str, *, order: int, note: str) -> "Flow":
        """Declare a comm Action emission as a step of this flow."""
        self.steps.append(FlowStep(STEP_ACTION, order, note, ref=name))
        return self

    def function(self, name: str, *, order: int, note: str) -> "Flow":
        """Declare a comm Function call as a step of this flow."""
        self.steps.append(FlowStep(STEP_FUNCTION, order, note, ref=name))
        return self

    def task(self, kind: str, *, order: int, note: str) -> "Flow":
        """Declare a comm Task as a step of this flow."""
        self.steps.append(FlowStep(STEP_TASK, order, note, ref=kind))
        return self

    def human(self, *, order: int, note: str) -> "Flow":
        """Declare a human action (UI step, manual check)."""
        self.steps.append(FlowStep(STEP_HUMAN, order, note))
        return self

    # Internal: registered by @flow_step
    def _http(self, ref: str, *, order: int, note: str, extra: dict) -> None:
        self.steps.append(FlowStep(STEP_HTTP, order, note, ref=ref, extra=extra))

    def sorted_steps(self) -> list[FlowStep]:
        return sorted(self.steps, key=FlowStep.sort_key)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Flow {self.id} ({len(self.steps)} steps)>"


class FlowRegistry:
    def __init__(self) -> None:
        self._flows: dict[str, Flow] = {}
        self._lock = threading.Lock()

    def register(self, flow: Flow) -> None:
        with self._lock:
            existing = self._flows.get(flow.id)
            if existing is not None and existing is not flow:
                raise ValueError(f"flow {flow.id!r} is already registered")
            self._flows[flow.id] = flow

    def get(self, flow_id: str) -> Flow:
        return self._flows[flow_id]

    def all(self) -> list[Flow]:
        return sorted(self._flows.values(), key=lambda f: f.id)

    def clear(self) -> None:
        """Tests only."""
        with self._lock:
            self._flows.clear()


flow_registry = FlowRegistry()


def flow_step(
    flow: Flow,
    *,
    order: int,
    note: str,
    **extra: Any,
) -> Callable:
    """Attach a view method/class to *flow* as an HTTP step.

    Stack multiple decorators to place one endpoint into several flows.
    The step ref is resolved to method+path later by the doc engine via
    the URLConf; here we only record identity and annotate the callable
    for the OpenAPI hook and check_flows.
    """

    def decorator(view: Callable) -> Callable:
        ref = f"{view.__module__}.{view.__qualname__}"
        flow._http(ref, order=order, note=note, extra=dict(extra))
        memberships = list(getattr(view, FLOWS_ATTR, []))
        memberships.append({"flow": flow.id, "order": order, "note": note})
        try:
            setattr(view, FLOWS_ATTR, memberships)
        except (AttributeError, TypeError):  # e.g. bound builtins
            logger.warning("flow_step: cannot annotate %r", view)
        return view

    return decorator


def autodiscover_flows() -> int:
    """Import ``flows`` from every installed app (django admin-style).

    Returns the number of apps that provided a flows module. Idempotent —
    repeated imports are no-ops thanks to sys.modules.
    """
    import importlib

    from django.apps import apps

    count = 0
    for app_config in apps.get_app_configs():
        module_name = f"{app_config.name}.flows"
        try:
            importlib.import_module(module_name)
            count += 1
        except ModuleNotFoundError as exc:
            if exc.name != module_name:  # real import error inside the module
                raise
    return count
