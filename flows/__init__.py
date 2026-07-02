"""stapel_core.flows — business scenarios as first-class objects.

Endpoints, comm actions/tasks and human actions attach to named Flows; the
doc engine (``manage.py generate_flow_docs``) assembles system-analysis
documentation from them, and ``manage.py check_flows`` gates CI on
completeness. One endpoint may participate in several flows.

    # <app>/flows.py — autodiscovered via INSTALLED_APPS
    from stapel_core.flows import Flow

    LOGIN = Flow("auth.passwordless_login", title="...", description="...")

    # on a view method or class:
    @flow_step(LOGIN, order=1, note="Request the code; 429 on rate limit")
    def post(self, request): ...

    # non-HTTP steps:
    LOGIN.action("user.registered", order=3, note="Emitted on first login")
    LOGIN.human(order=0, note="User enters their email")

See docs: flows-and-verification.md in the stapel workspace.
"""

from .registry import (
    Flow,
    FlowStep,
    autodiscover_flows,
    flow_registry,
    flow_step,
)

__all__ = [
    "Flow",
    "FlowStep",
    "autodiscover_flows",
    "flow_registry",
    "flow_step",
]
