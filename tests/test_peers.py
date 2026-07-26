"""Cross-service path discovery: a routing 404 is never an answer.

The membership incident (2026-07-26) and the stapel-translate key collectors
are the same bug twice: a client hardcodes a peer's path, the peer moves it
(the §60 ``v1/`` canon sweep), Django's URL resolver answers 404, and the
client reads that non-answer as a business verdict ("not a member", "no
notification keys"). ``stapel_core.django.peers`` is the shared rule; this
file pins it.
"""
import pytest

from stapel_core.django import peers
from stapel_core.django.peers import (
    PathResolver,
    PeerRouteUnavailable,
    get_with_path_discovery,
    service_answered,
)

V1 = "/notifications/api/v1/notification-keys/"
LEGACY = "/notifications/api/notification-keys/"
BASE = "http://stapel-notifications:8000"


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


@pytest.fixture
def transport(monkeypatch):
    """Replays a path -> response routing table and records the calls."""
    calls = []
    table = {}

    def fake_get(url, **kwargs):
        calls.append(url)
        for path, response in table.items():
            if url.endswith(path):
                return response() if callable(response) else response
        return route_404()

    monkeypatch.setattr(peers.requests, "get", fake_get)
    return calls, table


# --- service_answered --------------------------------------------------------


def test_view_404_is_an_answer():
    assert service_answered(FakeResponse(404)) is True


def test_resolver_404_is_not_an_answer():
    assert service_answered(route_404()) is False


def test_missing_content_type_is_not_an_answer():
    class NoHeaders:
        headers = {}

    assert service_answered(NoHeaders()) is False


# --- get_with_path_discovery -------------------------------------------------


def test_uses_the_first_candidate_that_reaches_a_view(transport):
    calls, table = transport
    table[V1] = FakeResponse(200, json_body={"k": "v"})

    resp, url = get_with_path_discovery(BASE, [V1, LEGACY])

    assert resp.status_code == 200
    assert url == f"{BASE}{V1}"
    assert calls == [f"{BASE}{V1}"]


def test_falls_back_to_the_legacy_mount(transport):
    """This client ahead of the peer: v1 not there yet, legacy still is."""
    calls, table = transport
    table[LEGACY] = FakeResponse(200, json_body={"k": "v"})

    resp, url = get_with_path_discovery(BASE, [V1, LEGACY])

    assert resp.status_code == 200
    assert url == f"{BASE}{LEGACY}"
    assert calls == [f"{BASE}{V1}", f"{BASE}{LEGACY}"]


def test_no_mount_point_raises_instead_of_returning_a_404(transport):
    """The defect: nothing routes, so the caller must not get a "result"."""
    _calls, _table = transport

    with pytest.raises(PeerRouteUnavailable) as exc:
        get_with_path_discovery(BASE, [V1, LEGACY])

    message = str(exc.value)
    assert V1 in message and LEGACY in message


def test_a_views_own_404_is_returned_not_retried(transport):
    """A DRF 404 is a real answer — discovery must stop, not walk the list."""
    calls, table = transport
    table[V1] = FakeResponse(404, json_body={"detail": "Not found."})

    resp, url = get_with_path_discovery(BASE, [V1, LEGACY])

    assert resp.status_code == 404
    assert url == f"{BASE}{V1}"
    assert calls == [f"{BASE}{V1}"]


def test_transport_errors_propagate(monkeypatch):
    """Connection refused is not a routing problem — another path can't help."""

    def boom(url, **kwargs):
        raise peers.requests.RequestException("connection refused")

    monkeypatch.setattr(peers.requests, "get", boom)

    with pytest.raises(peers.requests.RequestException):
        get_with_path_discovery(BASE, [V1, LEGACY])


def test_accept_json_header_and_caller_headers_are_both_sent(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured.update(headers or {})
        return FakeResponse(200, json_body={})

    monkeypatch.setattr(peers.requests, "get", fake_get)
    get_with_path_discovery(BASE, [V1], headers={"X-API-Key": "secret"})

    assert captured["Accept"] == "application/json"
    assert captured["X-API-Key"] == "secret"


def test_empty_candidate_list_is_a_configuration_error():
    with pytest.raises(ValueError):
        get_with_path_discovery(BASE, [])


# --- PathResolver ------------------------------------------------------------


def test_resolver_remembers_the_answering_path(transport):
    calls, table = transport
    table[LEGACY] = FakeResponse(200, json_body={})
    resolver = PathResolver([V1, LEGACY])

    get_with_path_discovery(BASE, [V1, LEGACY], resolver=resolver)
    assert resolver.resolved == LEGACY
    assert calls == [f"{BASE}{V1}", f"{BASE}{LEGACY}"]

    # Second run pays no probe: the remembered path is tried first.
    calls.clear()
    get_with_path_discovery(BASE, [V1, LEGACY], resolver=resolver)
    assert calls == [f"{BASE}{LEGACY}"]


def test_resolver_needs_candidates():
    with pytest.raises(ValueError):
        PathResolver([])
