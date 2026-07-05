"""CI gate: documentation completeness over flows.

Rules:
- every API endpoint (past the allowlist) belongs to at least one flow;
- every flow has a non-empty title and a description of >= MIN_DESCRIPTION
  characters;
- every step carries a non-empty note;
- steps within a flow have distinct i18n note keys (colliding implicit
  keys — same order twice — would silently share one catalog entry);
- action/function/task steps reference names that exist in the comm
  registries or committed schemas (best-effort when registries are empty).
"""
from __future__ import annotations

from dataclasses import dataclass

from .docs import _flow_refs, iter_api_endpoints
from .registry import STEP_ACTION, STEP_FUNCTION, STEP_HTTP, STEP_TASK, flow_registry

MIN_DESCRIPTION = 40

# Endpoints that never need a business flow.
DEFAULT_ALLOWLIST_SUBSTRINGS = (
    "/health", "/ready", "/live", "/metrics",
    "/schema", "/docs", "/swagger", "/redoc",
    "/error-keys", "/_functions/",
    "/admin/", "/__debug__/",
)


@dataclass
class FlowIssue:
    level: str  # "error" | "warning"
    message: str


def check_flows(extra_allowlist: tuple[str, ...] = ()) -> list[FlowIssue]:
    issues: list[FlowIssue] = []
    allow = DEFAULT_ALLOWLIST_SUBSTRINGS + tuple(extra_allowlist)

    flows = flow_registry.all()
    if not flows:
        issues.append(FlowIssue("warning", "no flows registered at all"))

    # 1. Flow completeness
    for f in flows:
        if not f.title.strip():
            issues.append(FlowIssue("error", f"{f.id}: empty title"))
        if len(f.description.strip()) < MIN_DESCRIPTION:
            issues.append(FlowIssue(
                "error",
                f"{f.id}: description shorter than {MIN_DESCRIPTION} chars — "
                "write the actual scenario, not a stub",
            ))
        if not f.steps:
            issues.append(FlowIssue("error", f"{f.id}: flow has no steps"))
        seen_keys: dict[str, int] = {}
        for s in f.steps:
            if not s.note.strip():
                issues.append(FlowIssue(
                    "error", f"{f.id}: step {s.kind}:{s.ref or s.order} has an empty note"
                ))
            seen_keys[s.note_key] = seen_keys.get(s.note_key, 0) + 1
        for key, n in seen_keys.items():
            if n > 1:
                issues.append(FlowIssue(
                    "error",
                    f"{f.id}: {n} steps share the i18n key {key!r} — give the "
                    "steps distinct orders or explicit note_key values",
                ))

    # 2. Endpoint coverage
    for ep in iter_api_endpoints():
        if any(sub in ep.path for sub in allow):
            continue
        handler = None
        if ep.view_cls is not None:
            handler = getattr(ep.view_cls, ep.method.lower(), None)
            if handler is None:
                # ViewSet action handlers were resolved into the ref already
                handler = ep.view_cls
        if not _flow_refs(ep.view_cls, handler) and not _ref_known(ep.view_ref, flows):
            issues.append(FlowIssue(
                "error",
                f"endpoint {ep.method} {ep.path} ({ep.view_ref}) belongs to no flow — "
                "attach @flow_step or add the path to the allowlist",
            ))

    # 3. comm-name references
    known_actions, known_functions, known_tasks = _known_comm_names()
    for f in flows:
        for s in f.steps:
            if s.kind == STEP_ACTION and known_actions and s.ref not in known_actions:
                issues.append(FlowIssue(
                    "warning", f"{f.id}: action {s.ref!r} is not registered/known"
                ))
            if s.kind == STEP_FUNCTION and known_functions and s.ref not in known_functions:
                issues.append(FlowIssue(
                    "warning", f"{f.id}: function {s.ref!r} is not registered/known"
                ))
            if s.kind == STEP_TASK and known_tasks and s.ref not in known_tasks:
                issues.append(FlowIssue(
                    "warning", f"{f.id}: task {s.ref!r} is not registered/known"
                ))
    return issues


def _ref_known(view_ref: str, flows) -> bool:
    for f in flows:
        for s in f.steps:
            if s.kind == STEP_HTTP and s.ref == view_ref:
                return True
    return False


def _known_comm_names():
    try:
        from stapel_core.comm import action_registry, function_registry
        from stapel_core.comm.tasks import registered_kinds

        return (
            set(action_registry.names()),
            set(function_registry.names()),
            set(registered_kinds()),
        )
    except Exception:  # pragma: no cover
        return set(), set(), set()
