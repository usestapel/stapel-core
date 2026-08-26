"""require_capability consumer helper: comm path, cache, degrade fallback."""
import uuid

import pytest

from stapel_core.comm import register_function
from stapel_core.comm.registry import function_registry as comm_functions
from stapel_core.django import workspaces as ws
from stapel_core.django.workspaces import (
    BUILTIN_ROLES,
    CAPABILITY_FUNCTION,
    Membership,
    WorkspaceLookupUnavailable,
    _capability_matches,
    require_capability,
)

WS_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture(autouse=True)
def _isolated_comm():
    comm_functions.clear()
    yield
    comm_functions.clear()


# --- comm happy path ---------------------------------------------------------


def test_allowed_via_comm_returns_membership():
    calls = []

    def check(payload):
        calls.append(payload)
        return {"allowed": True, "role": "secretary"}

    register_function(CAPABILITY_FUNCTION, check)

    membership = require_capability(WS_ID, USER_ID, "meetings.spotlight")
    assert membership == Membership(
        workspace_id=WS_ID, user_id=USER_ID, role="secretary"
    )
    assert calls == [
        {
            "workspace_id": str(WS_ID),
            "user_id": str(USER_ID),
            "capability": "meetings.spotlight",
        }
    ]


def test_denied_via_comm_returns_none():
    register_function(CAPABILITY_FUNCTION, lambda p: {"allowed": False, "role": "viewer"})
    assert require_capability(WS_ID, USER_ID, "members.remove") is None


def test_verdict_is_cached_for_burst():
    # One comm call per (ws, user, capability) within the TTL.
    calls = []

    def check(payload):
        calls.append(payload)
        return {"allowed": True, "role": "admin"}

    register_function(CAPABILITY_FUNCTION, check)

    for _ in range(3):
        assert require_capability(WS_ID, USER_ID, "members.invite") is not None
    assert len(calls) == 1


def test_deny_is_cached_too():
    calls = []

    def check(payload):
        calls.append(payload)
        return {"allowed": False, "role": None}

    register_function(CAPABILITY_FUNCTION, check)

    for _ in range(3):
        assert require_capability(WS_ID, USER_ID, "members.invite") is None
    assert len(calls) == 1


def test_cache_is_per_capability():
    calls = []

    def check(payload):
        calls.append(payload["capability"])
        return {"allowed": True, "role": "admin"}

    register_function(CAPABILITY_FUNCTION, check)

    require_capability(WS_ID, USER_ID, "members.invite")
    require_capability(WS_ID, USER_ID, "members.remove")
    assert calls == ["members.invite", "members.remove"]


def test_a_provider_that_raised_is_not_a_denial(monkeypatch):
    """The no-verdict case. Was ``is None`` — a fabricated deny — until 0.47.0.

    A provider that raised rendered NO verdict. Reporting that as "you do not
    hold the capability" is how a workspaces outage reaches a user as 403,
    indistinguishable from a real refusal, with the caller's `unavailable ->
    503` branch unable to fire.
    """
    boom = [True]
    calls = []

    def check(payload):
        calls.append(payload)
        if boom[0]:
            raise RuntimeError("db down")
        return {"allowed": True, "role": "owner"}

    register_function(CAPABILITY_FUNCTION, check)

    with pytest.raises(WorkspaceLookupUnavailable):
        require_capability(WS_ID, USER_ID, "workspace.update")

    boom[0] = False  # next call retries (the non-answer was not cached)
    assert require_capability(WS_ID, USER_ID, "workspace.update") is not None
    assert len(calls) == 2


def test_the_old_shape_is_still_reachable_but_must_be_asked_for():
    """``strict=False`` for a soft, non-authorization caller — never default."""
    register_function(CAPABILITY_FUNCTION, _raise("db down"))
    assert require_capability(WS_ID, USER_ID, "workspace.update", strict=False) is None


def test_a_membership_lookup_with_no_verdict_is_not_a_denial(monkeypatch):
    """Same defect one layer down: the degrade path swallowed it too."""

    def unavailable(workspace_id, user_id, *, strict=False):
        if strict:
            raise WorkspaceLookupUnavailable("workspaces is down")
        return None

    monkeypatch.setattr(ws, "get_membership", unavailable)
    with pytest.raises(WorkspaceLookupUnavailable):
        require_capability(WS_ID, USER_ID, "workspace.view")
    assert require_capability(WS_ID, USER_ID, "workspace.view", strict=False) is None


# --- degrade path (old workspaces without check_capability) ------------------


def _raise(message):
    def check(payload):
        raise RuntimeError(message)

    return check


def _fake_membership(role):
    def fake(workspace_id, user_id, *, strict=False):
        return Membership(workspace_id=workspace_id, user_id=user_id, role=role)

    return fake


def test_degrade_owner_wildcard_allows_anything(monkeypatch):
    # CAPABILITY_FUNCTION is not registered -> FunctionRouteNotConfigured
    monkeypatch.setattr(ws, "get_membership", _fake_membership("owner"))
    m = require_capability(WS_ID, USER_ID, "anything.at.all")
    assert m is not None and m.role == "owner"


def test_degrade_admin_has_builtin_capabilities(monkeypatch):
    monkeypatch.setattr(ws, "get_membership", _fake_membership("admin"))
    assert require_capability(WS_ID, USER_ID, "members.provision") is not None
    assert require_capability(WS_ID, USER_ID, "meetings.spotlight") is None


def test_degrade_member_viewer_view_only(monkeypatch):
    for role in ("member", "viewer"):
        monkeypatch.setattr(ws, "get_membership", _fake_membership(role))
        assert require_capability(WS_ID, USER_ID, "workspace.view") is not None
        assert require_capability(WS_ID, USER_ID, "members.view") is not None
        assert require_capability(WS_ID, USER_ID, "workspace.update") is None


def test_degrade_non_member_denied(monkeypatch):
    monkeypatch.setattr(ws, "get_membership", lambda w, u, *, strict=False: None)
    assert require_capability(WS_ID, USER_ID, "workspace.view") is None


def test_degrade_unknown_custom_role_denied(monkeypatch):
    # A client-registered role can't be resolved without the authoritative
    # registry in workspaces -> deny is the only safe answer.
    monkeypatch.setattr(ws, "get_membership", _fake_membership("secretary"))
    assert require_capability(WS_ID, USER_ID, "workspace.view") is None


# --- matcher / fallback table ------------------------------------------------


def test_capability_matcher_wildcards():
    assert _capability_matches("members.invite", "*")
    assert _capability_matches("members.invite", "members.*")
    assert _capability_matches("members.invite", "members.invite")
    assert not _capability_matches("members.invite", "workspace.*")
    assert not _capability_matches("members.invite", "members.remove")


def test_builtin_roles_table_shape():
    assert set(BUILTIN_ROLES) == {"owner", "admin", "member", "viewer"}
    ranks = [BUILTIN_ROLES[r]["rank"] for r in ("viewer", "member", "admin", "owner")]
    assert ranks == sorted(ranks)  # same order as ROLE_HIERARCHY
    assert BUILTIN_ROLES["owner"]["capabilities"] == ["*"]
