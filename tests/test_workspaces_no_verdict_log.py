"""A workspaces outage is one WARNING per (peer, reason) per window, not one per request."""
import logging
import uuid

import pytest
import requests
from django.core.cache import cache

from stapel_core.django import workspaces as ws
from stapel_core.django.workspaces import (
    WorkspaceLookupUnavailable,
    _NoVerdictLog,
    get_membership,
)

LOGGER = "stapel_core.django.workspaces"
WS_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeResponse:
    def __init__(self, status_code, json_body=None):
        self.status_code = status_code
        self._json = json_body
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        return self._json


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    log = _NoVerdictLog(window=60, max_keys=4, clock=fake)
    monkeypatch.setattr(ws, "_no_verdict_log", log)
    cache.clear()
    ws._resolved_prefix = None
    yield fake
    cache.clear()
    ws._resolved_prefix = None


def _warnings(caplog):
    return [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


def _peer_down(monkeypatch):
    def refuse(method, url, **kwargs):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(ws.requests, "request", refuse)


def test_a_peer_outage_warns_once_per_window_then_summarises(clock, monkeypatch, caplog):
    _peer_down(monkeypatch)
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    for _ in range(50):
        assert get_membership(WS_ID, uuid.uuid4()) is None

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    first = warnings[0].getMessage()
    assert "reason=transport" in first
    assert "connection refused" in first  # the first line keeps its context
    assert "suppressed" not in first

    clock.now += 61
    get_membership(WS_ID, uuid.uuid4())

    warnings = _warnings(caplog)
    assert len(warnings) == 2
    assert "49 more like it suppressed" in warnings[1].getMessage()


def test_strict_callers_still_get_the_exception_every_time(clock, monkeypatch, caplog):
    _peer_down(monkeypatch)
    for _ in range(3):
        with pytest.raises(WorkspaceLookupUnavailable) as excinfo:
            get_membership(WS_ID, uuid.uuid4(), strict=True)
        assert excinfo.value.reason == "transport"
    assert len(_warnings(caplog)) == 1


def test_reasons_are_throttled_separately(clock, monkeypatch, caplog):
    _peer_down(monkeypatch)
    get_membership(WS_ID, uuid.uuid4())
    monkeypatch.setattr(ws.requests, "request", lambda *a, **k: FakeResponse(503))
    get_membership(WS_ID, uuid.uuid4())
    get_membership(WS_ID, uuid.uuid4())

    reasons = [r.getMessage() for r in _warnings(caplog)]
    assert len(reasons) == 2
    assert "reason=transport" in reasons[0]
    assert "reason=status_503" in reasons[1]


def test_recovery_reports_the_hidden_count_and_reopens_the_window(clock, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    _peer_down(monkeypatch)
    for _ in range(5):
        get_membership(WS_ID, uuid.uuid4())

    monkeypatch.setattr(
        ws.requests, "request", lambda *a, **k: FakeResponse(200, {"role": "member"})
    )
    assert get_membership(WS_ID, uuid.uuid4()).role == "member"
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO and "answers again" in r.getMessage()]
    assert infos and "4 no-verdict lookup(s)" in infos[0] and "transport=4" in infos[0]

    # A new outage after a recovery is news: loud again inside the old window.
    _peer_down(monkeypatch)
    get_membership(WS_ID, uuid.uuid4())
    assert len(_warnings(caplog)) == 2


def test_the_key_space_is_bounded():
    clock = FakeClock()
    log = _NoVerdictLog(window=60, max_keys=3, clock=clock)
    for i in range(10):
        log.report("peer", f"status_{500 + i}", "x")
    assert len(log._entries) == 3
    assert ("peer", "status_509") in log._entries


def test_window_zero_logs_every_occurrence():
    log = _NoVerdictLog(window=0, max_keys=8, clock=FakeClock())
    assert all(log.report("peer", "transport", "x") for _ in range(3))
