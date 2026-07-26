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

from stapel_core.django.peers import service_answered

logger = logging.getLogger(__name__)


WORKSPACES_SERVICE_URL = os.getenv(
    "WORKSPACES_SERVICE_URL", "http://stapel-workspaces:8000"
)
SERVICE_API_KEY = os.getenv("SERVICE_API_KEY", "")

# The internal API's mount point, newest first (owner-reported live incident,
# 2026-07-26). stapel-workspaces 0.4.2 moved its whole API under `v1/` (the
# §60 v1-canon sweep) and this client kept asking for the pre-v1 path. Django
# answered 404 — "no such ROUTE" — and `get_membership` below read that as
# "no such MEMBERSHIP", cached the non-answer for 30 seconds, and every
# service that asks about membership started telling the workspace's own
# OWNER "Forbidden: not a member of this workspace". A client that hardcodes
# a peer's URL and treats any 404 as a verdict has two bugs, and this fixes
# both: the path is discovered rather than assumed, and a routing 404 is
# never a verdict (see `_service_answered`).
INTERNAL_API_PREFIXES = (
    "/workspaces/api/workspaces/v1/internal",   # workspaces >= 0.4.2
    "/workspaces/api/workspaces/internal",      # legacy
)

#: Prefix that last answered, so the probe cost is paid once per process.
_resolved_prefix: Optional[str] = None

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


class WorkspaceLookupUnavailable(Exception):
    """The workspaces service did not render a verdict.

    Distinct from "not a member", which is a real answer. This is the
    membership equivalent of a 502: the peer was unreachable, returned 5xx,
    or answered 404 from Django's URL resolver rather than from the view (a
    version skew between this client and the service — see
    `INTERNAL_API_PREFIXES`). Callers that turn `None` into HTTP 403 should
    turn this into 503 instead: telling a user "you are not a member" on the
    strength of a routing mistake is worse than telling them the truth.
    """


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


#: Did the VIEW answer, or did the URL resolver? A 404 from stapel-workspaces'
#: internal API is a real answer ("that user is not a member") and carries a
#: JSON body, because DRF renders it; a 404 from Django's URL resolver carries
#: an HTML error page. Same-status/different-meaning is exactly how a
#: client/server path skew turned into "the owner is not a member of their own
#: workspace" for weeks. The rule now lives in ``stapel_core.django.peers`` so
#: every cross-service client shares it (translate's key collectors do);
#: re-exported under the historical private name for in-module callers.
_service_answered = service_answered


def _internal_get(path_suffix: str, *, method: str = "get", timeout: float = 3.0):
    """Call the workspaces internal API, discovering its mount point.

    Tries the known prefixes newest-first and remembers the one that answers,
    so a mixed-version fleet (this library ahead of, or behind, the service)
    keeps working instead of silently reading routing 404s as verdicts.
    Raises :class:`WorkspaceLookupUnavailable` when NO prefix produced an
    answer from the view.
    """
    global _resolved_prefix

    headers = {"Accept": "application/json"}
    if SERVICE_API_KEY:
        headers["X-API-KEY"] = SERVICE_API_KEY

    ordered = INTERNAL_API_PREFIXES
    if _resolved_prefix:
        ordered = (_resolved_prefix,) + tuple(
            p for p in INTERNAL_API_PREFIXES if p != _resolved_prefix
        )

    last_error: Optional[str] = None
    for prefix in ordered:
        url = f"{WORKSPACES_SERVICE_URL}{prefix}{path_suffix}"
        try:
            resp = requests.request(method, url, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            # Transport, not routing: another prefix cannot help.
            raise WorkspaceLookupUnavailable(f"{url}: {exc}") from exc
        if resp.status_code == 404 and not _service_answered(resp):
            last_error = f"{url}: 404 from the URL resolver (not the view)"
            continue  # wrong mount point — try the next one
        if prefix != _resolved_prefix:
            _resolved_prefix = prefix
            logger.info("workspaces internal API resolved at %s", prefix)
        return resp

    raise WorkspaceLookupUnavailable(
        "no known mount point for the workspaces internal API — this service "
        f"and stapel-workspaces disagree about its path ({last_error}). "
        f"Tried: {', '.join(INTERNAL_API_PREFIXES)}"
    )


def get_membership(workspace_id, user_id, *, strict: bool = False) -> Optional[Membership]:
    """Resolve the user's role in the workspace, or None if not a member.

    Cached briefly to avoid an HTTP roundtrip on every request.

    `strict=True` raises :class:`WorkspaceLookupUnavailable` when no verdict
    could be obtained (peer down, 5xx, path skew) instead of returning
    ``None``. Prefer it in any caller that renders `None` as HTTP 403 — see
    that exception's docstring for why. The default stays ``None`` so
    existing callers keep compiling; what changed regardless of the flag is
    that a NON-answer is never cached and never silently equated with "not a
    member".
    """
    key = _cache_key(workspace_id, user_id)
    cached = cache.get(key)
    if cached is not None:
        if cached == "__none__":
            return None
        return Membership(workspace_id=workspace_id, user_id=user_id, role=cached)

    try:
        resp = _internal_get(f"/{workspace_id}/members/{user_id}")
    except WorkspaceLookupUnavailable as exc:
        logger.warning("workspaces membership lookup failed: %s", exc)
        if strict:
            raise
        return None

    if resp.status_code == 404:
        # The VIEW answered (see `_service_answered`) — a genuine "not a
        # member", safe to remember.
        cache.set(key, "__none__", CACHE_TTL_SECONDS)
        return None
    if resp.status_code != 200:
        logger.warning(
            "workspaces membership lookup returned %s for %s/%s",
            resp.status_code,
            workspace_id,
            user_id,
        )
        if strict:
            raise WorkspaceLookupUnavailable(
                f"workspaces answered {resp.status_code}"
            )
        return None
    role = (resp.json() or {}).get("role")
    if not role:
        return None
    cache.set(key, role, CACHE_TTL_SECONDS)
    return Membership(workspace_id=workspace_id, user_id=user_id, role=role)


def require_role(
    workspace_id, user_id, minimum: str, *, strict: bool = False
) -> Optional[Membership]:
    """Return membership if the user has at least `minimum`, else None.

    `strict` forwards to :func:`get_membership` — with it, a lookup that
    reached no verdict raises instead of being reported as insufficient role.
    """
    membership = get_membership(workspace_id, user_id, strict=strict)
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
    try:
        resp = _internal_get(
            f"/users/{user_id}/personal", method="post", timeout=5.0
        )
    except WorkspaceLookupUnavailable as exc:
        # Same path-skew blast radius as membership: this used to 404 against
        # a v1-mounted service and return None, so registration quietly
        # produced accounts with no personal workspace and logged nothing
        # that named the cause.
        logger.warning(
            "get_or_create_personal_workspace failed for user %s: %s", user_id, exc
        )
        return None
    if resp.status_code == 200:
        return resp.json().get("workspace_id")
    logger.warning(
        "get_or_create_personal_workspace: unexpected %s for user %s",
        resp.status_code,
        user_id,
    )
    return None
