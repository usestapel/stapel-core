"""Coverage tests for django/monitoring/health.py and django/monitoring/telegram.py."""
import json
import logging
from io import StringIO
from unittest import mock

import pytest
from django.test import RequestFactory, override_settings

from stapel_core.django.monitoring import health as health_mod
from stapel_core.django.monitoring import telegram as telegram_mod


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


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


class _BrokenConnection:
    def cursor(self):
        raise RuntimeError("db down")


@pytest.fixture
def rf():
    return RequestFactory()


@pytest.fixture(autouse=True)
def _isolate_exporters():
    saved = list(health_mod._custom_metrics_exporters)
    yield
    health_mod._custom_metrics_exporters[:] = saved


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


def test_health_check_healthy(rf, monkeypatch):
    monkeypatch.setattr(health_mod, "connection", _OkConnection())
    with override_settings(SERVICE_NAME="My Service", APP_VERSION_NUMBER="1.2.3"):
        resp = health_mod.health_check(rf.get("/api/health/"))
    assert resp.status_code == 200
    body = json.loads(resp.content)
    assert body["status"] == "healthy"
    assert body["service"] == "My Service"
    assert body["version"] == "1.2.3"
    assert body["checks"]["database"] == "ok"
    assert body["uptime_seconds"] >= 0


def test_health_check_degraded_on_db_error(rf, monkeypatch):
    monkeypatch.setattr(health_mod, "connection", _BrokenConnection())
    resp = health_mod.health_check(rf.get("/api/health/"))
    assert resp.status_code == 503
    body = json.loads(resp.content)
    assert body["status"] == "degraded"
    assert body["checks"]["database"] == "error"
    # unknown defaults when settings absent
    assert body["service"] == "unknown"
    assert body["version"] == "unknown"


# ---------------------------------------------------------------------------
# readiness / liveness
# ---------------------------------------------------------------------------


def test_readiness_probe_ok(rf, monkeypatch):
    monkeypatch.setattr(health_mod, "connection", _OkConnection())
    resp = health_mod.readiness_probe(rf.get("/api/health/ready/"))
    assert resp.status_code == 200
    assert resp.content == b"OK"


def test_readiness_probe_not_ready(rf, monkeypatch):
    monkeypatch.setattr(health_mod, "connection", _BrokenConnection())
    resp = health_mod.readiness_probe(rf.get("/api/health/ready/"))
    assert resp.status_code == 503
    assert b"Not Ready" in resp.content


def test_liveness_probe(rf):
    resp = health_mod.liveness_probe(rf.get("/api/health/live/"))
    assert resp.status_code == 200
    assert resp.content == b"OK"


# ---------------------------------------------------------------------------
# prometheus_metrics
# ---------------------------------------------------------------------------


def test_prometheus_metrics_basic(rf, monkeypatch):
    monkeypatch.setattr(health_mod, "connection", _OkConnection())
    with override_settings(SERVICE_NAME="My Service", APP_VERSION_NUMBER="9.9"):
        resp = health_mod.prometheus_metrics(rf.get("/api/metrics/"))
    assert resp.status_code == 200
    text = resp.content.decode()
    assert 'stapel_service_info{service="my_service",version="9.9"} 1' in text
    assert 'stapel_database_up{service="my_service"} 1' in text
    assert 'stapel_up{service="my_service"} 1' in text
    assert "stapel_uptime_seconds" in text
    assert resp["Content-Type"].startswith("text/plain")


def test_prometheus_metrics_db_down_and_custom_prefix(rf, monkeypatch):
    monkeypatch.setattr(health_mod, "connection", _BrokenConnection())
    with override_settings(STAPEL_METRICS_PREFIX="iron_"):
        resp = health_mod.prometheus_metrics(rf.get("/api/metrics/"))
    text = resp.content.decode()
    assert 'iron_database_up{service="unknown"} 0' in text
    assert "stapel_database_up" not in text


def test_prometheus_metrics_custom_exporters(rf, monkeypatch):
    monkeypatch.setattr(health_mod, "connection", _OkConnection())

    health_mod.register_metrics_exporter(lambda: "custom_metric 42")
    health_mod.register_metrics_exporter(lambda: "")  # falsy -> skipped

    def broken():
        raise RuntimeError("exporter boom")

    health_mod.register_metrics_exporter(broken)

    resp = health_mod.prometheus_metrics(rf.get("/api/metrics/"))
    text = resp.content.decode()
    assert "custom_metric 42" in text
    # broken exporter is swallowed, endpoint still 200
    assert resp.status_code == 200


def test_get_health_urls():
    patterns = health_mod.get_health_urls("svc/")
    names = [p.name for p in patterns]
    assert names == [
        "health-check",
        "readiness-probe",
        "liveness-probe",
        "prometheus-metrics",
    ]
    assert str(patterns[0].pattern) == "svc/api/health/"
    assert str(patterns[3].pattern) == "svc/api/metrics/"


# ---------------------------------------------------------------------------
# telegram: _cfg / is_configured
# ---------------------------------------------------------------------------


@pytest.fixture
def tg_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "-100555")
    monkeypatch.delenv("TELEGRAM_ALERT_THREAD_ID", raising=False)


@pytest.fixture
def tg_no_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALERT_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_ALERT_THREAD_ID", raising=False)


def test_cfg_reads_env(tg_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALERT_THREAD_ID", "1717")
    assert telegram_mod._cfg() == ("tok123", "-100555", "1717")


def test_cfg_thread_id_defaults_to_none(tg_env):
    assert telegram_mod._cfg() == ("tok123", "-100555", None)


def test_is_configured(tg_env):
    assert telegram_mod.is_configured() is True


def test_is_not_configured(tg_no_env):
    assert telegram_mod.is_configured() is False


# ---------------------------------------------------------------------------
# telegram: send_message / send_alert
# ---------------------------------------------------------------------------


def test_send_message_unconfigured_returns_false(tg_no_env):
    with mock.patch("requests.post") as post:
        assert telegram_mod.send_message("hi") is False
    post.assert_not_called()


def test_send_message_success(tg_env):
    with mock.patch("requests.post") as post:
        post.return_value.raise_for_status.return_value = None
        assert telegram_mod.send_message("hello", parse_mode="HTML") is True
    args, kwargs = post.call_args
    assert args[0] == "https://api.telegram.org/bottok123/sendMessage"
    payload = kwargs["json"]
    assert payload["chat_id"] == "-100555"
    assert payload["text"] == "hello"
    assert payload["parse_mode"] == "HTML"
    assert "message_thread_id" not in payload
    assert kwargs["timeout"] == 10


def test_send_message_includes_thread_id(tg_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALERT_THREAD_ID", "1717")
    with mock.patch("requests.post") as post:
        assert telegram_mod.send_message("hello") is True
    assert post.call_args[1]["json"]["message_thread_id"] == "1717"


def test_send_message_http_error_returns_false(tg_env):
    with mock.patch("requests.post") as post:
        post.return_value.raise_for_status.side_effect = RuntimeError("HTTP 400")
        assert telegram_mod.send_message("hello") is False


def test_send_message_network_error_returns_false(tg_env):
    with mock.patch("requests.post", side_effect=ConnectionError("no net")):
        assert telegram_mod.send_message("hello") is False


def test_send_alert_with_service(tg_env, monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_mod, "send_message", lambda text: sent.append(text) or True)
    assert telegram_mod.send_alert("boom", service="stapel-auth") is True
    assert sent[0].startswith("\U0001f6a8 <b>stapel-auth</b>\n")
    assert sent[0].endswith("boom")


def test_send_alert_without_service(tg_env, monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_mod, "send_message", lambda text: sent.append(text) or True)
    telegram_mod.send_alert("boom")
    assert sent[0].startswith("\U0001f6a8 <b>Alert</b>\n")


# ---------------------------------------------------------------------------
# telegram: TelegramHandler
# ---------------------------------------------------------------------------


def _record(msg="boom", name="app.module", level=logging.ERROR):
    return logging.LogRecord(
        name=name, level=level, pathname=__file__, lineno=1, msg=msg,
        args=(), exc_info=None,
    )


def test_handler_skips_when_unconfigured(tg_no_env, monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_mod, "send_message", lambda text: sent.append(text) or True)
    telegram_mod.TelegramHandler(service="svc").emit(_record())
    assert sent == []


def test_handler_sends_escaped_message(tg_env, monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_mod, "send_message", lambda text: sent.append(text) or True)
    handler = telegram_mod.TelegramHandler(service="svc")
    handler.emit(_record(msg="a <tag> & b"))
    assert len(sent) == 1
    assert sent[0].startswith("\U0001f6a8 <b>svc</b> [ERROR]\n<pre>")
    assert "a &lt;tag&gt; &amp; b" in sent[0]


def test_handler_uses_record_name_and_truncates(tg_env, monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_mod, "send_message", lambda text: sent.append(text) or True)
    handler = telegram_mod.TelegramHandler()  # no service -> record.name
    handler.emit(_record(msg="x" * 5000, name="my.logger"))
    assert "<b>my.logger</b>" in sent[0]
    assert "(truncated)" in sent[0]
    assert len(sent[0]) < 4096


def test_handler_emit_error_calls_handle_error(tg_env, monkeypatch):
    handler = telegram_mod.TelegramHandler(service="svc")
    errors = []
    monkeypatch.setattr(handler, "format", mock.Mock(side_effect=RuntimeError("fmt")))
    monkeypatch.setattr(handler, "handleError", lambda record: errors.append(record))
    record = _record()
    handler.emit(record)
    assert errors == [record]


def test_handler_default_level_is_error():
    buf = StringIO()  # noqa: F841 - just ensure constructor path
    handler = telegram_mod.TelegramHandler(service="s")
    assert handler.level == logging.ERROR
    assert handler.service == "s"
