"""The dependency-check contract has THREE states, and the third one is not
merely unsupported — before this, it was silently wrong.

``_run_dependency_checks`` did ``ok = bool(probe())``. A probe that could not
ask returned a sentinel meaning "unknown", the sentinel coerced to ``True``,
and ``/api/health/`` rendered ``"ok"``. That is the incident class this file
locks shut: a live stand reporting healthy because every layer was allowed to
stay silent.

Four properties, each of which was violated by the two-valued contract:

1. ``None`` from a probe is UNDETERMINED, never healthy;
2. the health body says ``"unknown"``, distinct from ``"error"``;
3. ``stapel_dependency_up`` is OMITTED for an undetermined dependency (a
   series dropping to 0 because nobody could ask is a false verdict an alert
   would fire on), while ``stapel_dependency_probe_ok`` is emitted always;
4. ``readiness_probe`` does NOT 503 on an undetermined CRITICAL dependency —
   an inability to ask is not proof the dependency is down, and every replica
   loses the same probe at the same instant, so taking the service out of
   rotation turns a blip into an outage.
"""
import json

import pytest
from django.test import RequestFactory, override_settings

from stapel_core.django.monitoring import health as health_mod


class _OkCursor:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql):
        assert sql == "SELECT 1"


class _OkConnection:
    def cursor(self):
        return _OkCursor()


@pytest.fixture
def rf():
    return RequestFactory()


@pytest.fixture(autouse=True)
def _isolated_registries(monkeypatch):
    monkeypatch.setattr(health_mod, "_dependency_checks", [])
    monkeypatch.setattr(health_mod, "_custom_metrics_exporters", [])
    monkeypatch.setattr(health_mod, "connection", _OkConnection())


# ---------------------------------------------------------------------------
# 1 + 2 — the body distinguishes the three answers
# ---------------------------------------------------------------------------


def test_probe_returning_none_is_unknown_not_healthy(rf):
    """``ok = bool(probe())`` had no room for this answer: a truthy sentinel
    meaning 'unknown' read as 'ok', and ``None`` read as 'error'. Both are
    verdicts the probe never gave."""
    health_mod.register_dependency_check("schema", lambda: None, critical=False)
    body = json.loads(health_mod.health_check(rf.get("/api/health/")).content)
    assert body["checks"]["schema"] == "unknown"


def test_unknown_is_distinct_from_error(rf):
    health_mod.register_dependency_check("undetermined", lambda: None)
    health_mod.register_dependency_check("down", lambda: False)
    health_mod.register_dependency_check("up", lambda: True)
    body = json.loads(health_mod.health_check(rf.get("/api/health/")).content)
    assert body["checks"] == {
        "database": "ok",
        "undetermined": "unknown",
        "down": "error",
        "up": "ok",
    }


def test_undetermined_alone_is_not_reported_as_degraded(rf):
    """'degraded' is a verdict. Saying it because a probe timed out makes
    every database restart look like a product outage."""
    health_mod.register_dependency_check("schema", lambda: None)
    resp = health_mod.health_check(rf.get("/api/health/"))
    assert resp.status_code == 200
    assert json.loads(resp.content)["status"] == "healthy"


def test_a_raising_probe_is_still_an_error_not_an_unknown(rf):
    """A broken probe is a bug, not an inability to ask. Saying 'I could not
    ask' is done deliberately, by returning None."""
    def boom():
        raise RuntimeError("twirp: connection refused")

    health_mod.register_dependency_check("livekit", boom)
    body = json.loads(health_mod.health_check(rf.get("/api/health/")).content)
    assert body["checks"]["livekit"] == "error"


# ---------------------------------------------------------------------------
# 3 — the metric series is omitted, not zeroed
# ---------------------------------------------------------------------------


def test_dependency_up_is_omitted_when_undetermined(rf):
    health_mod.register_dependency_check("schema", lambda: None)
    with override_settings(SERVICE_NAME="My Service"):
        text = health_mod.prometheus_metrics(rf.get("/api/metrics/")).content.decode()
    assert 'stapel_dependency_up{service="my_service",dependency="schema"}' not in text
    assert 'stapel_dependency_probe_ok{service="my_service",dependency="schema"} 0' in text


def test_determined_dependencies_still_carry_up_and_probe_ok(rf):
    health_mod.register_dependency_check("up", lambda: True)
    health_mod.register_dependency_check("down", lambda: False)
    with override_settings(SERVICE_NAME="svc"):
        text = health_mod.prometheus_metrics(rf.get("/api/metrics/")).content.decode()
    assert 'stapel_dependency_up{service="svc",dependency="up"} 1' in text
    assert 'stapel_dependency_up{service="svc",dependency="down"} 0' in text
    assert 'stapel_dependency_probe_ok{service="svc",dependency="up"} 1' in text
    assert 'stapel_dependency_probe_ok{service="svc",dependency="down"} 1' in text


def test_the_up_series_header_disappears_with_its_only_sample(rf):
    """No HELP/TYPE for a metric with no samples — a bare header is what a
    scraper reads as 'the exporter emits this and it is empty'."""
    health_mod.register_dependency_check("schema", lambda: None)
    text = health_mod.prometheus_metrics(rf.get("/api/metrics/")).content.decode()
    assert "dependency_up " not in text.replace("dependency_probe_ok", "")


# ---------------------------------------------------------------------------
# 4 — an undetermined critical dependency must not empty the load balancer
# ---------------------------------------------------------------------------


def test_readiness_does_not_503_on_an_undetermined_critical_dependency(rf):
    health_mod.register_dependency_check("primary_store", lambda: None, critical=True)
    assert health_mod.readiness_probe(rf.get("/api/health/ready/")).status_code == 200


def test_readiness_still_503s_on_a_determined_critical_failure(rf):
    """The relaxation is scoped to UNKNOWN; a probe that positively says 'down'
    must still pull the process."""
    health_mod.register_dependency_check("primary_store", lambda: False, critical=True)
    resp = health_mod.readiness_probe(rf.get("/api/health/ready/"))
    assert resp.status_code == 503
    assert b"primary_store" in resp.content


def test_health_check_does_not_503_on_an_undetermined_critical_dependency(rf):
    health_mod.register_dependency_check("primary_store", lambda: None, critical=True)
    resp = health_mod.health_check(rf.get("/api/health/"))
    assert resp.status_code == 200
    assert json.loads(resp.content)["checks"]["primary_store"] == "unknown"


def test_an_undetermined_dependency_does_not_mask_a_determined_one(rf):
    health_mod.register_dependency_check("undetermined", lambda: None, critical=True)
    health_mod.register_dependency_check("store", lambda: False, critical=True)
    resp = health_mod.readiness_probe(rf.get("/api/health/ready/"))
    assert resp.status_code == 503
    assert b"store" in resp.content
    assert b"undetermined" not in resp.content
