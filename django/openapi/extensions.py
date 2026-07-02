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
        "is server-side; stateless clients may resend X-Verification-Token)."
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
                        },
                        "required": ["challenge_id", "scope", "factors"],
                    },
                },
                "required": ["localizable_error", "verification"],
            }
        }
    },
}

_PARAM_RE = re.compile(r"<(?:[^:<>]+:)?([^<>]+)>")


def _openapi_path(django_path: str) -> str:
    """``a/<int:pk>/b`` → ``/a/{pk}/b`` (spectacular-style)."""
    path = _PARAM_RE.sub(r"{\1}", django_path)
    return "/" + path.strip("/") + ("/" if django_path.endswith("/") else "")


def stapel_postprocessing_hook(result, generator, request, public):
    """Annotate operations with flow membership and verification contracts."""
    from stapel_core.flows.docs import iter_api_endpoints
    from stapel_core.flows.registry import FLOWS_ATTR
    from stapel_core.verification.decorators import VERIFICATION_ATTR

    # (METHOD, /openapi/path/) -> (view_cls, handler)
    lookup: dict[tuple[str, str], tuple] = {}
    for ep in iter_api_endpoints():
        if ep.view_cls is None:
            continue
        handler = getattr(ep.view_cls, ep.method.lower(), None)
        lookup[(ep.method, _openapi_path(ep.path))] = (ep.view_cls, handler)

    for path, methods in (result.get("paths") or {}).items():
        for method, operation in methods.items():
            if not isinstance(operation, dict):
                continue
            view_cls, handler = lookup.get((method.upper(), path), (None, None))
            if view_cls is None:
                continue

            flow_ids = sorted({
                m["flow"]
                for target in (handler, view_cls)
                for m in (getattr(target, FLOWS_ATTR, None) or [])
            })
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
                }
                operation.setdefault("responses", {}).setdefault(
                    "403", VERIFICATION_403
                )
    return result
