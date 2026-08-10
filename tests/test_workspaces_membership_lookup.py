"""Membership lookup: a routing 404 is not a verdict.

Owner-reported live incident, 2026-07-26, app.ironmemo.com. Opening "My
meetings" showed a toast reading `Forbidden: not a member of this workspace`
— to the account that OWNS the workspace, with the membership row sitting
right there in the workspaces database (`role=owner`, accepted, not
suspended).

What actually happened: stapel-workspaces 0.4.2 moved its whole API under
`v1/` (the §60 v1-canon sweep), this client kept requesting the pre-v1 path,
and Django's URL resolver answered 404. `get_membership` read that as "no
such membership", cached the non-answer for 30 seconds, and every caller
that renders `None` as HTTP 403 confidently told the user they were not a
member. Nothing logged the cause: the 404 branch was the "normal" path.

Two separate defects, two separate guards below — the path is discovered
rather than assumed, and a 404 that came from the resolver instead of the
view is never a verdict.
"""
import uuid

import pytest
from django.core.cache import cache

from stapel_core.django import workspaces as ws
from stapel_core.django.workspaces import (
    INTERNAL_API_PREFIXES,
    WorkspaceLookupUnavailable,
    get_membership,
    get_or_create_personal_workspace,
    require_role,
)

WS_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")

# The canonical address (api-versioning.md §2): stapel_workspaces.urls
# contributes 'v1/', so a correctly-mounted host serves this.
CANON = "/workspaces/api/v1/internal"
# The doubled-segment address a mis-mounted host serves. Named V1 here for
# historical reasons — it is NOT canonical; it is what a host that mounted
# the module at 'workspaces/api/workspaces/' produces, and it was for a
# while the only path this client knew, which made a CORRECTLY mounted
# workspaces service the one configuration the client could not talk to.
V1 = "/workspaces/api/workspaces/v1/internal"
LEGACY = "/workspaces/api/workspaces/internal"


class FakeResponse:
    def __init__(self, status_code, *, json_body=None, html=False):
        self.status_code = status_code
        self._json = json_body
        # Django's own 404 page is HTML; DRF's 404 is JSON. That difference
        # is the whole signal.
        self.headers = {
            "Content-Type": "text/html; charset=utf-8" if html else "application/json"
        }

    def json(self):
        return self._json


def route_404():
    """What Django's URL resolver returns for an unknown path."""
    return FakeResponse(404, html=True)


@pytest.fixture(autouse=True)
def _clean():
    cache.clear()
    ws._resolved_prefix = None
    yield
    cache.clear()
    ws._resolved_prefix = None


@pytest.fixture
def transport(monkeypatch):
    """Records requests and replays a per-prefix routing table."""
    calls = []
    table = {}

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        for prefix, response in table.items():
            if prefix in url:
                return response() if callable(response) else response
        return route_404()

    monkeypatch.setattr(ws.requests, "request", fake_request)
    return type("T", (), {"calls": calls, "table": table})()


class TestPathDiscovery:
    def test_finds_the_v1_mount_after_the_legacy_path_404s(self, transport):
        transport.table[V1] = FakeResponse(200, json_body={"role": "owner"})

        membership = get_membership(WS_ID, USER_ID)

        assert membership is not None
        assert membership.role == "owner"

    def test_still_works_against_a_pre_v1_service(self, transport):
        transport.table[LEGACY] = FakeResponse(200, json_body={"role": "admin"})

        assert get_membership(WS_ID, USER_ID).role == "admin"

    def test_the_resolved_prefix_is_tried_first_next_time(self, transport):
        transport.table[LEGACY] = FakeResponse(200, json_body={"role": "member"})
        get_membership(WS_ID, USER_ID)
        cache.clear()
        transport.calls.clear()

        get_membership(WS_ID, USER_ID)

        # One call, straight to the prefix that answered — not the full walk.
        assert len(transport.calls) == 1
        assert LEGACY in transport.calls[0][1]

    def test_v1_is_tried_before_legacy_on_a_cold_process(self, transport):
        transport.table[V1] = FakeResponse(200, json_body={"role": "owner"})
        get_membership(WS_ID, USER_ID)
        assert V1 in transport.calls[1][1]
        assert INTERNAL_API_PREFIXES[0].endswith("/v1/internal")

    def test_the_canonical_path_is_tried_first_of_all(self, transport):
        """A correctly-mounted workspaces service was, for six weeks, the one
        configuration this client could NOT talk to: the canonical address
        was missing from the probe list entirely, because the 2026-07-26 fix
        copied the path from a deployment whose host mount was itself wrong.
        Fixing that deployment's mount then broke every membership check on
        it — caught by preflight E004 on app.ironmemo.com, 2026-08-10."""
        transport.table[CANON] = FakeResponse(200, json_body={"role": "owner"})

        assert get_membership(WS_ID, USER_ID).role == "owner"
        assert CANON in transport.calls[0][1]
        assert INTERNAL_API_PREFIXES[0] == CANON

    def test_a_correctly_mounted_service_is_reachable_at_all(self, transport):
        """The regression this ordering exists to prevent, stated as the
        property rather than the order: a host serving ONLY the canon must be
        usable. Before the fix this returned None and callers rendered it as
        'not a member of this workspace' to the workspace's own owner."""
        transport.table[CANON] = FakeResponse(200, json_body={"role": "admin"})
        transport.table[V1] = FakeResponse(404, html=True)
        transport.table[LEGACY] = FakeResponse(404, html=True)

        assert get_membership(WS_ID, USER_ID).role == "admin"


class TestRoutingFourOhFourIsNotAVerdict:
    def test_no_mount_point_answers_at_all_returns_none_not_a_cached_denial(
        self, transport
    ):
        """The incident, reproduced: every prefix 404s from the resolver."""
        assert get_membership(WS_ID, USER_ID) is None
        # …and crucially, NOTHING was written to the cache, so the next call
        # retries instead of repeating a fabricated denial for 30 seconds.
        assert cache.get(ws._cache_key(WS_ID, USER_ID)) is None

    def test_strict_callers_can_tell_a_denial_from_an_outage(self, transport):
        with pytest.raises(WorkspaceLookupUnavailable) as excinfo:
            get_membership(WS_ID, USER_ID, strict=True)
        # The message has to name the actual problem: an operator staring at
        # "Forbidden: not a member" had nothing to go on.
        assert "path" in str(excinfo.value) or "mount point" in str(excinfo.value)

    def test_a_view_rendered_404_IS_a_verdict_and_is_cached(self, transport):
        """DRF answering "that user is not a member" — a real answer."""
        transport.table[V1] = FakeResponse(404, json_body={"detail": "Not found."})

        assert get_membership(WS_ID, USER_ID) is None
        assert cache.get(ws._cache_key(WS_ID, USER_ID)) == "__none__"

    def test_a_view_rendered_404_does_not_raise_even_in_strict_mode(self, transport):
        transport.table[V1] = FakeResponse(404, json_body={"detail": "Not found."})
        assert get_membership(WS_ID, USER_ID, strict=True) is None

    def test_a_transport_failure_is_unavailable_not_a_denial(self, transport, monkeypatch):
        def boom(method, url, **kwargs):
            raise ws.requests.RequestException("connection refused")

        monkeypatch.setattr(ws.requests, "request", boom)

        assert get_membership(WS_ID, USER_ID) is None
        assert cache.get(ws._cache_key(WS_ID, USER_ID)) is None
        with pytest.raises(WorkspaceLookupUnavailable):
            get_membership(WS_ID, USER_ID, strict=True)

    def test_a_5xx_is_unavailable_not_a_denial(self, transport):
        transport.table[V1] = FakeResponse(503, json_body={"detail": "down"})

        assert get_membership(WS_ID, USER_ID) is None
        assert cache.get(ws._cache_key(WS_ID, USER_ID)) is None
        with pytest.raises(WorkspaceLookupUnavailable):
            get_membership(WS_ID, USER_ID, strict=True)

    def test_require_role_forwards_strict(self, transport):
        with pytest.raises(WorkspaceLookupUnavailable):
            require_role(WS_ID, USER_ID, "member", strict=True)

    def test_require_role_still_answers_normally(self, transport):
        transport.table[V1] = FakeResponse(200, json_body={"role": "admin"})
        assert require_role(WS_ID, USER_ID, "member") is not None
        assert require_role(WS_ID, USER_ID, "owner") is None


class TestPersonalWorkspace:
    """Same blast radius, quieter symptom: registration produced accounts
    with no personal workspace and logged nothing that named the cause."""

    def test_finds_the_v1_mount(self, transport):
        transport.table[V1] = FakeResponse(200, json_body={"workspace_id": "ws-1"})
        assert get_or_create_personal_workspace(USER_ID) == "ws-1"

    def test_returns_none_when_no_mount_point_answers(self, transport):
        assert get_or_create_personal_workspace(USER_ID) is None

    def test_posts_rather_than_gets(self, transport):
        transport.table[V1] = FakeResponse(200, json_body={"workspace_id": "ws-1"})
        get_or_create_personal_workspace(USER_ID)
        assert transport.calls[0][0] == "post"
