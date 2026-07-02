"""Tests for stapel_core.comm — Action/Function communication layer."""
import pytest
from django.db import transaction
from django.test import override_settings

from stapel_core.comm import (
    FunctionCallError,
    FunctionNotRegistered,
    action_registry,
    call,
    emit,
    function_registry,
    on_action,
    register_function,
    subscribe_action,
)
from stapel_core.comm.exceptions import FunctionRouteNotConfigured
from stapel_core.comm.functions import _route_for
from stapel_core.django.outbox.models import OutboxEvent
from stapel_core.django.outbox.relay import dispatch_pending


@pytest.fixture(autouse=True)
def clean_registries():
    function_registry.clear()
    action_registry.clear()
    yield
    function_registry.clear()
    action_registry.clear()


# ---------------------------------------------------------------------------
# Functions — in-process transport
# ---------------------------------------------------------------------------


def test_function_call_inprocess():
    register_function("cdn.media_exists", lambda p: {"exists": p["ref"] == "ok"})
    assert call("cdn.media_exists", {"ref": "ok"}) == {"exists": True}
    assert call("cdn.media_exists", {"ref": "no"}) == {"exists": False}


def test_function_not_registered():
    with pytest.raises(FunctionNotRegistered):
        call("nope.missing")


def test_function_provider_error_wrapped():
    @pytest.mark.filterwarnings("ignore")
    def boom(payload):
        raise ValueError("bad")

    register_function("svc.boom", boom)
    with pytest.raises(FunctionCallError):
        call("svc.boom")


def test_function_single_provider_enforced():
    register_function("svc.one", lambda p: 1)
    with pytest.raises(ValueError):
        register_function("svc.one", lambda p: 2)


def test_function_decorator():
    from stapel_core.comm import function as function_decorator

    @function_decorator("svc.decorated")
    def handler(payload):
        return payload.get("x", 0) * 2

    assert call("svc.decorated", {"x": 21}) == 42


def test_route_longest_prefix():
    routes = {"cdn.": "http://a", "cdn.media": "http://b"}
    with override_settings(STAPEL_COMM={"FUNCTION_ROUTES": routes}):
        assert _route_for("cdn.media_exists") == "http://b"
        assert _route_for("cdn.other") == "http://a"
        with pytest.raises(FunctionRouteNotConfigured):
            _route_for("billing.debit")


# ---------------------------------------------------------------------------
# Actions — in-process transport + outbox
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_emit_delivers_after_commit():
    seen = []
    subscribe_action("user.deleted", lambda e: seen.append(e))

    with transaction.atomic():
        emit("user.deleted", {"user_id": "u1"})
        # inside the transaction nothing is delivered yet
        assert seen == []

    assert len(seen) == 1
    assert seen[0].payload == {"user_id": "u1"}
    row = OutboxEvent.objects.get()
    assert row.dispatched_at is not None


@pytest.mark.django_db(transaction=True)
def test_emit_rolled_back_transaction_discards_event():
    seen = []
    subscribe_action("user.deleted", lambda e: seen.append(e))

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with transaction.atomic():
            emit("user.deleted", {"user_id": "u1"})
            raise Boom()

    assert seen == []
    assert OutboxEvent.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_failed_handler_retried_by_relay():
    calls = {"n": 0}

    def flaky(event):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")

    subscribe_action("payment.completed", flaky)

    with transaction.atomic():
        emit("payment.completed", {"tx": "t1"})

    row = OutboxEvent.objects.get()
    assert row.dispatched_at is None
    assert row.attempts == 1
    assert "transient" in row.last_error

    # relay retries (force due)
    OutboxEvent.objects.update(next_attempt_at=row.created_at)
    delivered, failed = dispatch_pending()
    assert (delivered, failed) == (1, 0)
    row.refresh_from_db()
    assert row.dispatched_at is not None
    assert calls["n"] == 2


@pytest.mark.django_db(transaction=True)
def test_multiple_subscribers_all_called():
    seen_a, seen_b = [], []
    subscribe_action("profile.changed", lambda e: seen_a.append(e.payload))
    subscribe_action("profile.changed", lambda e: seen_b.append(e.payload))

    with transaction.atomic():
        emit("profile.changed", {"user_id": "u2"})

    assert seen_a == [{"user_id": "u2"}]
    assert seen_b == [{"user_id": "u2"}]


def test_emit_without_outbox_synchronous():
    seen = []
    subscribe_action("x.y", lambda e: seen.append(e))
    with override_settings(STAPEL_COMM={"OUTBOX_ENABLED": False}):
        event = emit("x.y", {"a": 1})
    assert seen == [event]


def test_on_action_decorator():
    seen = []

    @on_action("a.b")
    def handler(event):
        seen.append(event.event_type)

    with override_settings(STAPEL_COMM={"OUTBOX_ENABLED": False}):
        emit("a.b")
    assert seen == ["a.b"]


def test_bus_transport_publishes_to_bus():
    from stapel_core.bus import get_bus

    with override_settings(
        STAPEL_COMM={"OUTBOX_ENABLED": False, "ACTION_TRANSPORT": "bus"}
    ):
        emit("workspace.created", {"id": "w1"})
    bus = get_bus()
    assert bus.events[-1].event_type == "workspace.created"
    assert bus.events[-1].payload == {"id": "w1"}


# ---------------------------------------------------------------------------
# HTTP function transport (mocked)
# ---------------------------------------------------------------------------


def test_http_transport_calls_route(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"result": {"ok": True}}

    class FakeSession:
        def post(self, url, json=None, headers=None, timeout=None):
            captured.update(url=url, json=json, headers=headers, timeout=timeout)
            return FakeResponse()

    from stapel_core.comm import functions as functions_mod

    monkeypatch.setattr(functions_mod, "_http_session", lambda: FakeSession())

    with override_settings(
        SERVICE_API_KEY="s3cr3t",
        STAPEL_COMM={
            "FUNCTION_TRANSPORT": "http",
            "FUNCTION_ROUTES": {"cdn.": "http://svc-cdn:8000/cdn"},
        },
    ):
        result = call("cdn.media_exists", {"ref": "r"}, timeout=1.5)

    assert result == {"ok": True}
    assert captured["url"] == "http://svc-cdn:8000/cdn/api/_functions/cdn.media_exists/"
    assert captured["json"] == {"payload": {"ref": "r"}}
    assert captured["headers"]["X-API-KEY"] == "s3cr3t"
    assert captured["timeout"] == 1.5


def test_http_transport_remote_error(monkeypatch):
    class FakeResponse:
        status_code = 500
        text = "boom"

    class FakeSession:
        def post(self, *a, **k):
            return FakeResponse()

    from stapel_core.comm import functions as functions_mod

    monkeypatch.setattr(functions_mod, "_http_session", lambda: FakeSession())

    with override_settings(
        STAPEL_COMM={
            "FUNCTION_TRANSPORT": "http",
            "FUNCTION_ROUTES": {"cdn.": "http://svc-cdn:8000/cdn"},
        }
    ):
        with pytest.raises(FunctionCallError):
            call("cdn.media_exists", {"ref": "r"})


def test_http_session_is_pooled_and_reused():
    from stapel_core.comm.functions import _http_session, reset_http_session

    reset_http_session()
    try:
        s1 = _http_session()
        s2 = _http_session()
        assert s1 is s2
        adapter = s1.get_adapter("http://svc-cdn:8000")
        assert adapter._pool_maxsize == 50
    finally:
        reset_http_session()


def test_custom_dotted_transport(monkeypatch):
    """FUNCTION_TRANSPORT accepts a dotted path — gRPC/NATS slot in without
    touching module code."""
    import sys
    import types

    mod = types.ModuleType("fake_rpc")
    mod.echo = lambda name, payload, timeout=None: {
        "via": "custom",
        "name": name,
        "payload": payload,
    }
    monkeypatch.setitem(sys.modules, "fake_rpc", mod)

    with override_settings(STAPEL_COMM={"FUNCTION_TRANSPORT": "fake_rpc.echo"}):
        result = call("billing.debit", {"n": 1})
    assert result == {"via": "custom", "name": "billing.debit", "payload": {"n": 1}}


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_schema_validation_when_enabled():
    pytest.importorskip("jsonschema")
    from stapel_core.comm.exceptions import SchemaValidationError

    register_function(
        "svc.strict",
        lambda p: p,
        schema={
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
            "additionalProperties": False,
        },
    )
    with override_settings(STAPEL_COMM={"VALIDATE_SCHEMAS": True}):
        assert call("svc.strict", {"n": 1}) == {"n": 1}
        with pytest.raises(SchemaValidationError):
            call("svc.strict", {"n": "not-int"})
