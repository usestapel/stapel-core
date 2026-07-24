"""
Cross-service workspace membership helpers.

Other services (recordings, billing, ...) ask stapel-workspaces over HTTP
whether a given user has the required role in a given workspace.  The
result is cached briefly in Redis (or the local cache) so a single
request burst doesn't fan out into N HTTP calls.

Service-to-service authentication uses the SERVICE_API_KEY header.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)


WORKSPACES_SERVICE_URL = os.getenv(
    "WORKSPACES_SERVICE_URL", "http://stapel-workspaces:8000"
)
SERVICE_API_KEY = os.getenv("SERVICE_API_KEY", "")

# Role hierarchy mirrored from stapel-workspaces.workspaces.permissions
ROLE_HIERARCHY = ["viewer", "member", "admin", "owner"]
CACHE_TTL_SECONDS = 30

#: Comm Function answering capability checks (workspaces 0.6+).
CAPABILITY_FUNCTION = "workspaces.check_capability"

# FALLBACK table only — stapel-workspaces owns the authoritative registry
# (builtin roles + STAPEL_WORKSPACES["ROLES"] overlay). This mirror of the
# builtin four (org-program spec §A1) exists so require_capability() can
# degrade gracefully against an old workspaces service that does not yet
# expose workspaces.check_capability: membership lookup + this map. Client
# custom roles / overlays are invisible here by design.
BUILTIN_ROLES = {
    "owner": {"rank": 400, "capabilities": ["*"]},
    "admin": {"rank": 300, "capabilities": [
        "workspace.view", "workspace.update",
        "members.view", "members.invite", "members.remove",
        "members.role.change", "members.provision",
        "workspace.security.manage",
    ]},
    "member": {"rank": 200, "capabilities": ["workspace.view", "members.view"]},
    "viewer": {"rank": 100, "capabilities": ["workspace.view", "members.view"]},
}


@dataclass
class Membership:
    workspace_id: UUID
    user_id: UUID
    role: str


def _cache_key(workspace_id, user_id) -> str:
    return f"workspaces:membership:{workspace_id}:{user_id}"


def _role_at_least(role: str, minimum: str) -> bool:
    try:
        return ROLE_HIERARCHY.index(role) >= ROLE_HIERARCHY.index(minimum)
    except ValueError:
        return False


def get_membership(workspace_id, user_id) -> Optional[Membership]:
    """Resolve the user's role in the workspace, or None if not a member.

    Cached briefly to avoid an HTTP roundtrip on every request.
    """
    key = _cache_key(workspace_id, user_id)
    cached = cache.get(key)
    if cached is not None:
        if cached == "__none__":
            return None
        return Membership(workspace_id=workspace_id, user_id=user_id, role=cached)

    headers = {"Accept": "application/json"}
    if SERVICE_API_KEY:
        headers["X-API-KEY"] = SERVICE_API_KEY
    url = (
        f"{WORKSPACES_SERVICE_URL}/workspaces/api/workspaces/internal/"
        f"{workspace_id}/members/{user_id}"
    )
    try:
        resp = requests.get(url, headers=headers, timeout=3)
    except requests.RequestException as exc:
        logger.warning("workspaces membership lookup failed: %s", exc)
        return None
    if resp.status_code == 404:
        cache.set(key, "__none__", CACHE_TTL_SECONDS)
        return None
    if resp.status_code != 200:
        logger.warning(
            "workspaces membership lookup returned %s for %s/%s",
            resp.status_code,
            workspace_id,
            user_id,
        )
        return None
    role = (resp.json() or {}).get("role")
    if not role:
        return None
    cache.set(key, role, CACHE_TTL_SECONDS)
    return Membership(workspace_id=workspace_id, user_id=user_id, role=role)


def require_role(workspace_id, user_id, minimum: str) -> Optional[Membership]:
    """Return membership if the user has at least `minimum`, else None."""
    membership = get_membership(workspace_id, user_id)
    if membership and _role_at_least(membership.role, minimum):
        return membership
    return None


def _capability_cache_key(workspace_id, user_id, capability) -> str:
    return f"workspaces:capability:{workspace_id}:{user_id}:{capability}"


def _capability_matches(capability: str, granted: str) -> bool:
    """Match one granted capability string against the requested one.

    Supports the registry wildcards: ``"*"`` (everything) and prefix
    wildcards like ``"members.*"``.
    """
    if granted == "*" or granted == capability:
        return True
    if granted.endswith(".*"):
        return capability.startswith(granted[:-1])
    return False


def _require_capability_fallback(workspace_id, user_id, capability: str) -> Optional[Membership]:
    """Degrade path for pre-capability workspaces: membership + builtin map."""
    membership = get_membership(workspace_id, user_id)
    if membership is None:
        return None
    granted = BUILTIN_ROLES.get(membership.role, {}).get("capabilities", [])
    if any(_capability_matches(capability, g) for g in granted):
        return membership
    return None


def require_capability(workspace_id, user_id, capability: str) -> Optional[Membership]:
    """Return membership if the user holds *capability* in the workspace.

    Asks the ``workspaces.check_capability`` comm Function (workspaces 0.6+),
    caching the verdict briefly like :func:`get_membership`. Against an old
    workspaces deployment where the Function is not registered/routed, it
    degrades to :func:`get_membership` plus the builtin role→capability
    fallback table (custom roles are unknowable there and deny). A remote
    call failure is fail-closed: ``None``, not cached, next call retries.
    """
    key = _capability_cache_key(workspace_id, user_id, capability)
    cached = cache.get(key)
    if cached is not None:
        if cached == "__deny__":
            return None
        return Membership(workspace_id=workspace_id, user_id=user_id, role=cached)

    from stapel_core.comm import call
    from stapel_core.comm.exceptions import (
        FunctionCallError,
        FunctionNotRegistered,
        FunctionRouteNotConfigured,
    )

    try:
        result = call(
            CAPABILITY_FUNCTION,
            {
                "workspace_id": str(workspace_id),
                "user_id": str(user_id),
                "capability": capability,
            },
            timeout=3.0,
        )
    except (FunctionNotRegistered, FunctionRouteNotConfigured) as exc:
        logger.debug(
            "workspaces.check_capability unavailable (%s) — degrading to "
            "membership + builtin role map", exc,
        )
        return _require_capability_fallback(workspace_id, user_id, capability)
    except FunctionCallError as exc:
        logger.warning("workspaces capability check failed: %s", exc)
        return None

    allowed = bool((result or {}).get("allowed"))
    role = (result or {}).get("role")
    if allowed and role:
        cache.set(key, role, CACHE_TTL_SECONDS)
        return Membership(workspace_id=workspace_id, user_id=user_id, role=role)
    cache.set(key, "__deny__", CACHE_TTL_SECONDS)
    return None


def invalidate_membership_cache(workspace_id, user_id) -> None:
    cache.delete(_cache_key(workspace_id, user_id))


def get_or_create_personal_workspace(user_id) -> Optional[str]:
    """Call workspaces service to get-or-create the user's personal workspace.

    Returns the workspace_id string, or None on failure (non-fatal: caller
    should log and continue — missing workspace is not a hard error at
    registration time).
    """
    headers = {"Accept": "application/json"}
    if SERVICE_API_KEY:
        headers["X-API-KEY"] = SERVICE_API_KEY
    url = f"{WORKSPACES_SERVICE_URL}/workspaces/api/workspaces/internal/users/{user_id}/personal"
    try:
        resp = requests.post(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            return resp.json().get("workspace_id")
        logger.warning(
            "get_or_create_personal_workspace: unexpected %s for user %s",
            resp.status_code,
            user_id,
        )
    except requests.RequestException as exc:
        logger.warning(
            "get_or_create_personal_workspace failed for user %s: %s", user_id, exc
        )
    return None
