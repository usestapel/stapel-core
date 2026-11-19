"""drf-spectacular postprocessing: flows + step-up verification metadata.

Wired automatically by get_spectacular_settings(); every operation gains
``x-stapel-flows`` (the business flows the endpoint participates in) and,
when @requires_verification protects it, an ``x-stapel-verification``
extension plus a documented 403 challenge response.
"""
from __future__ import annotations

import re

VERIFICATION_403 = {
    "description": (
        "Step-up verification required. Complete one of the listed factors "
        "via the auth service's verification endpoints, then retry (grant "
        "is server-side; stateless clients may resend X-Verification-Token). "
        "On strict endpoints a user with no usable factor instead receives "
        "the enrollment variant: verification.enroll=true, no challenge_id."
    ),
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "properties": {
                    "localizable_error": {
                        "type": "string",
                        "example": "error.403.verification_required",
                    },
                    "error": {"type": "string"},
                    "verification": {
                        "type": "object",
                        "properties": {
                            "challenge_id": {"type": "string"},
                            "scope": {"type": "string"},
                            "factors": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "expires_at": {"type": "integer"},
                            "enroll": {"type": "boolean"},
                        },
                        "required": ["scope", "factors"],
                    },
                },
                "required": ["localizable_error", "verification"],
            }
        }
    },
}

_PARAM_RE = re.compile(r"<(?:[^:<>]+:)?([^<>]+)>")
_REGEX_GROUP_RE = re.compile(r"\(\?P<([^<>]+)>[^)]*\)")
_TEMPLATE_RE = re.compile(r"\{[^{}]+\}")


def _openapi_path(django_path: str) -> str:
    """``a/<int:pk>/b`` → ``/a/{pk}/b`` (spectacular-style).

    Router (regex) patterns are converted too: ``^a/(?P<pk>[^/.]+)$`` →
    ``/a/{pk}``.
    """
    path = _REGEX_GROUP_RE.sub(r"{\1}", django_path)
    path = path.replace("^", "").replace("$", "")
    path = _PARAM_RE.sub(r"{\1}", path)
    trailing = path.endswith("/")
    return "/" + path.strip("/") + ("/" if trailing else "")


def _match_key(method: str, path: str) -> tuple[str, str]:
    """Parameter names differ between the URLConf and the schema (spectacular
    renames ``pk`` to ``id``); match on the path's shape."""
    return method.upper(), _TEMPLATE_RE.sub("{}", path)


def _registry_flow_ids() -> dict[tuple[str, str], set[str]]:
    """``(METHOD, path shape)`` → ids of the flows whose HTTP steps name it.

    Flows can bind an endpoint by ``@flow_step`` (an attribute on the
    handler) or by a step ``ref`` registered elsewhere (``Flow.http(ref)``);
    this reads the registry, the source ``flows.json`` is exported from.
    """
    from stapel_core.flows.docs import endpoint_index
    from stapel_core.flows.registry import STEP_HTTP, autodiscover_flows, flow_registry

    autodiscover_flows()
    index = endpoint_index()
    bound: dict[tuple[str, str], set[str]] = {}
    for flow in flow_registry.all():
        for step in flow.sorted_steps():
            if step.kind != STEP_HTTP:
                continue
            for ep in index.get(step.ref, []):
                bound.setdefault(_match_key(ep.method, _openapi_path(ep.path)), set()).add(
                    flow.id
                )
    return bound


def stapel_postprocessing_hook(result, generator, request, public):
    """Annotate operations with flow membership and verification contracts."""
    from stapel_core.flows.docs import iter_api_endpoints
    from stapel_core.flows.registry import FLOWS_ATTR
    from stapel_core.verification.decorators import VERIFICATION_ATTR

    # (METHOD, /openapi/path shape/) -> (view_cls, handler)
    lookup: dict[tuple[str, str], tuple] = {}
    for ep in iter_api_endpoints():
        if ep.view_cls is None:
            continue
        # handler_name is the action name for ViewSets — resolving by the
        # http verb would miss @flow_step/@requires_verification on actions.
        handler = getattr(ep.view_cls, ep.handler_name or ep.method.lower(), None)
        lookup[_match_key(ep.method, _openapi_path(ep.path))] = (ep.view_cls, handler)
    registry_flows = _registry_flow_ids()

    for path, methods in (result.get("paths") or {}).items():
        for method, operation in methods.items():
            if not isinstance(operation, dict):
                continue
            key = _match_key(method, path)
            view_cls, handler = lookup.get(key, (None, None))
            if view_cls is None:
                continue

            flow_ids = sorted({
                m["flow"]
                for target in (handler, view_cls)
                for m in (getattr(target, FLOWS_ATTR, None) or [])
            } | registry_flows.get(key, set()))
            if flow_ids:
                operation["x-stapel-flows"] = flow_ids

            contract = (
                getattr(handler, VERIFICATION_ATTR, None)
                or getattr(view_cls, VERIFICATION_ATTR, None)
            )
            if contract:
                operation["x-stapel-verification"] = {
                    "scope": contract["scope"],
                    "factors": contract["factors"],
                    "max_age": contract["max_age"],
                    "level": contract.get("level"),
                }
                operation.setdefault("responses", {}).setdefault(
                    "403", VERIFICATION_403
                )
    return result
