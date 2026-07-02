"""Coverage tests for comm/http.py, comm/nats.py, serve_functions and the
remaining comm/actions|functions|registry branches."""
import asyncio
import json
import sys
import types
from io import StringIO

import pytest
from django.test import override_settings
from rest_framework.test import APIRequestFactory

from stapel_core.comm import (
    FunctionCallError,
    FunctionNotRegistered,
    action_registry,
    call,
    function_registry,
    register_function,
)
from stapel_core.comm.exceptions import FunctionRouteNotConfigured
from stapel_core.comm.http import FunctionCallView, get_function_urls


@pytest.fixture(autouse=True)
def clean_registries():
    function_registry.clear()
    action_registry.clear()
    yield
    function_registry.clear()
    action_registry.clear()


# ---------------------------------------------------------------------------
# comm/http.py — FunctionCallView
# ---------------------------------------------------------------------------


def _post(path="/api/_functions/x/", data=None, service=True):
    factory = APIRequestFactory()
    request = factory.post(path, data if data is not None else {}, format="json")
    if service:
        request.is_service_request = True
    return request


def test_http_view_unknown_function_404():
    resp = FunctionCallView.as_view()(_post(), name="nope.missing")
    assert resp.status_code == 404
    assert "unknown function" in resp.data["error"]


def test_http_view_success_with_payload():
    register_function("svc.echo", lambda p: {"echo": p})
    resp = FunctionCallView.as_view()(
        _post(data={"payload": {"a": 1}}), name="svc.echo"
    )
    assert resp.status_code == 200
    assert resp.data == {"result": {"echo": {"a": 1}}}


def test_http_view_non_dict_body_gives_empty_payload():
    register_function("svc.echo", lambda p: {"echo": p})
    resp = FunctionCallView.as_view()(_post(data=[1, 2, 3]), name="svc.echo")
    assert resp.status_code == 200
    assert resp.data == {"result": {"echo": {}}}


def test_http_view_handler_error_500():
    def boom(payload):
        raise ValueError("bad input")

    register_function("svc.boom", boom)
    resp = FunctionCallView.as_view()(_post(data={"payload": {}}), name="svc.boom")
    assert resp.status_code == 500
    assert "ValueError" in resp.data["error"]


def test_http_view_requires_service_request():
    register_function("svc.echo", lambda p: p)
    resp = FunctionCallView.as_view()(_post(service=False), name="svc.echo")
    assert resp.status_code in (401, 403)  # 401 when no credentials at all


def test_get_function_urls():
    patterns = get_function_urls("cdn/")
    assert len(patterns) == 1
    assert patterns[0].name == "stapel-function-call"
    assert str(patterns[0].pattern) == "cdn/api/_functions/<str:name>/"


# ---------------------------------------------------------------------------
# comm/nats.py — NatsBridge (fake nats module, real loop thread)
# ---------------------------------------------------------------------------


class _FakeNatsMsg:
    def __init__(self, data):
        self.data = data


class _FakeNC:
    is_closed = False

    def __init__(self):
        self.requests = []
        self.drained = False

    async def request(self, subject, data, timeout=None):
        self.requests.append((subject, data, timeout))
        return _FakeNatsMsg(b'{"result": 7}')

    async def drain(self):
        self.drained = True


def _install_fake_nats(monkeypatch, nc, connect_calls):
    fake = types.ModuleType("nats")

    async def connect(url, **kwargs):
        connect_calls.append(url)
        return nc

    fake.connect = connect
    monkeypatch.setitem(sys.modules, "nats", fake)
    return fake


def test_nats_bridge_lifecycle(monkeypatch):
    from stapel_core.comm.nats import NatsBridge

    nc = _FakeNC()
    connect_calls = []
    _install_fake_nats(monkeypatch, nc, connect_calls)

    bridge = NatsBridge("nats://test:4222")
    try:
        out = bridge.request("subj.a", b"x", 2.0)
        assert out == b'{"result": 7}'
        bridge.request("subj.b", b"y", 2.0)
        assert connect_calls == ["nats://test:4222"]  # connection reused
        assert [r[0] for r in nc.requests] == ["subj.a", "subj.b"]
        assert nc.requests[0][2] == 2.0
    finally:
        bridge.close()
    assert nc.drained is True


def test_nats_bridge_close_without_connection(monkeypatch):
    from stapel_core.comm.nats import NatsBridge

    bridge = NatsBridge("nats://never:4222")
    bridge.close()  # no connection: skips drain, just stops the loop


def test_get_bridge_singleton_and_reset(monkeypatch):
    from stapel_core.comm import nats as nats_mod

    closed = []

    class FakeBridge:
        def __init__(self, url):
            self.url = url

        def close(self):
            closed.append(self.url)

    monkeypatch.setattr(nats_mod, "NatsBridge", FakeBridge)
    nats_mod.reset_bridge()
    try:
        b1 = nats_mod.get_bridge()
        assert isinstance(b1, FakeBridge)
        assert b1.url == "nats://nats:4222"  # default from STAPEL_COMM
        assert nats_mod.get_bridge() is b1
    finally:
        nats_mod.reset_bridge()
    assert closed == ["nats://nats:4222"]


def test_nats_transport_reraises_function_call_error(monkeypatch):
    from stapel_core.comm import nats as nats_mod

    class FakeBridge:
        def request(self, subject, data, timeout):
            raise FunctionCallError("already wrapped")

    monkeypatch.setattr(nats_mod, "get_bridge", lambda: FakeBridge())
    with override_settings(STAPEL_COMM={"FUNCTION_TRANSPORT": "nats"}):
        with pytest.raises(FunctionCallError, match="already wrapped"):
            call("svc.x", {})


def test_nats_transport_wraps_generic_error(monkeypatch):
    from stapel_core.comm import nats as nats_mod

    class FakeBridge:
        def request(self, subject, data, timeout):
            raise RuntimeError("socket exploded")

    monkeypatch.setattr(nats_mod, "get_bridge", lambda: FakeBridge())
    with override_settings(STAPEL_COMM={"FUNCTION_TRANSPORT": "nats"}):
        with pytest.raises(FunctionCallError, match="failed over NATS"):
            call("svc.x", {})


def test_nats_transport_non_dict_reply(monkeypatch):
    from stapel_core.comm import nats as nats_mod

    class FakeBridge:
        def request(self, subject, data, timeout):
            return b"[1, 2]"

    monkeypatch.setattr(nats_mod, "get_bridge", lambda: FakeBridge())
    with override_settings(STAPEL_COMM={"FUNCTION_TRANSPORT": "nats"}):
        assert call("svc.x", {}) == [1, 2]


def test_nats_transport_empty_reply(monkeypatch):
    from stapel_core.comm import nats as nats_mod

    class FakeBridge:
        def request(self, subject, data, timeout):
            return b""

    monkeypatch.setattr(nats_mod, "get_bridge", lambda: FakeBridge())
    with override_settings(STAPEL_COMM={"FUNCTION_TRANSPORT": "nats"}):
        assert call("svc.x", {}) is None


# ---------------------------------------------------------------------------
# serve_functions management command
# ---------------------------------------------------------------------------


def _make_command():
    from stapel_core.django.management.commands.serve_functions import Command

    buf = StringIO()
    return Command(stdout=buf), buf


def test_serve_functions_no_functions_registered():
    cmd, buf = _make_command()
    cmd.handle()
    assert "nothing to serve" in buf.getvalue()


def test_serve_functions_handle_starts_server(monkeypatch):
    from stapel_core.django.management.commands import serve_functions as sf_mod

    register_function("svc.echo", lambda p: p)
    served = {}

    async def fake_serve(self, url, names):
        served.update(url=url, names=names)

    monkeypatch.setattr(sf_mod.Command, "_serve", fake_serve)
    cmd, buf = _make_command()
    with override_settings(STAPEL_COMM={"NATS_URL": "nats://custom:4222"}):
        cmd.handle()
    assert served == {"url": "nats://custom:4222", "names": ["svc.echo"]}
    assert "serving 1 function(s) on nats://custom:4222" in buf.getvalue()


def test_serve_functions_serve_loop(monkeypatch):
    register_function("svc.echo", lambda p: {"echo": p})

    def boom(payload):
        raise ValueError("provider blew up")

    register_function("svc.boom", boom)

    subs = {}

    class FakeNC:
        def __init__(self):
            self.drained = False

        async def subscribe(self, subject, queue=None, cb=None):
            subs[subject] = (queue, cb)

        async def drain(self):
            self.drained = True

    fake_nc = FakeNC()
    fake_nats = types.ModuleType("nats")

    async def connect(url, **kwargs):
        return fake_nc

    fake_nats.connect = connect
    monkeypatch.setitem(sys.modules, "nats", fake_nats)

    cmd, _ = _make_command()
    replies = []

    class FakeMsg:
        def __init__(self, subject, data):
            self.subject = subject
            self.data = data

        async def respond(self, data):
            replies.append(json.loads(data.decode()))

    async def scenario():
        task = asyncio.ensure_future(cmd._serve("nats://x", ["svc.boom", "svc.echo"]))
        for _ in range(200):
            if len(subs) == 2:
                break
            await asyncio.sleep(0)
        assert set(subs) == {"stapel.fn.svc.echo", "stapel.fn.svc.boom"}

        queue, cb = subs["stapel.fn.svc.echo"]
        assert queue == "stapel"  # no SERVICE_NAME -> default queue group
        await cb(FakeMsg("stapel.fn.svc.echo", json.dumps({"payload": {"x": 1}}).encode()))
        await cb(FakeMsg("stapel.fn.svc.echo", b"\xff\xfe"))  # undecodable body

        _, boom_cb = subs["stapel.fn.svc.boom"]
        await boom_cb(FakeMsg("stapel.fn.svc.boom", b""))  # empty body -> {} payload

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert replies[0] == {"result": {"echo": {"x": 1}}}
    assert replies[1] == {"error": "invalid request body"}
    assert "provider blew up" in replies[2]["error"]
    assert fake_nc.drained is True


# ---------------------------------------------------------------------------
# comm/functions.py — remaining branches
# ---------------------------------------------------------------------------


def test_unknown_transport_raises():
    with override_settings(STAPEL_COMM={"FUNCTION_TRANSPORT": "carrierpigeon"}):
        with pytest.raises(FunctionRouteNotConfigured, match="unknown FUNCTION_TRANSPORT"):
            call("svc.x", {})


def test_http_transport_unreachable(monkeypatch):
    import requests as requests_lib

    from stapel_core.comm import functions as functions_mod

    class FakeSession:
        def post(self, *a, **k):
            raise requests_lib.ConnectionError("refused")

    monkeypatch.setattr(functions_mod, "_http_session", lambda: FakeSession())
    with override_settings(
        STAPEL_COMM={
            "FUNCTION_TRANSPORT": "http",
            "FUNCTION_ROUTES": {"svc.": "http://svc:8000"},
        }
    ):
        with pytest.raises(FunctionCallError, match="unreachable"):
            call("svc.x", {})


def test_http_transport_404_maps_to_not_registered(monkeypatch):
    from stapel_core.comm import functions as functions_mod

    class FakeResponse:
        status_code = 404

    class FakeSession:
        def post(self, *a, **k):
            return FakeResponse()

    monkeypatch.setattr(functions_mod, "_http_session", lambda: FakeSession())
    with override_settings(
        STAPEL_COMM={
            "FUNCTION_TRANSPORT": "http",
            "FUNCTION_ROUTES": {"svc.": "http://svc:8000"},
        }
    ):
        with pytest.raises(FunctionNotRegistered):
            call("svc.x", {})


def test_http_transport_remote_error_key(monkeypatch):
    from stapel_core.comm import functions as functions_mod

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"error": "KeyError('x')"}

    class FakeSession:
        def post(self, *a, **k):
            return FakeResponse()

    monkeypatch.setattr(functions_mod, "_http_session", lambda: FakeSession())
    with override_settings(
        STAPEL_COMM={
            "FUNCTION_TRANSPORT": "http",
            "FUNCTION_ROUTES": {"svc.": "http://svc:8000"},
        }
    ):
        with pytest.raises(FunctionCallError, match="failed remotely"):
            call("svc.x", {})


# ---------------------------------------------------------------------------
# comm/actions.py — remaining branches
# ---------------------------------------------------------------------------


def test_dispatch_row_swallows_exceptions(monkeypatch):
    import stapel_core.django.outbox.relay as relay_mod
    from stapel_core.comm.actions import _dispatch_row

    def boom(pk):
        raise RuntimeError("relay down")

    monkeypatch.setattr(relay_mod, "dispatch_one", boom)
    assert _dispatch_row(123) is None  # never raises into the request cycle


def test_deliver_unknown_transport_raises():
    from stapel_core.bus.event import Event
    from stapel_core.comm.actions import deliver
    from stapel_core.comm.exceptions import ActionDeliveryError

    event = Event(event_type="a.b", service="svc", payload={})
    with override_settings(STAPEL_COMM={"ACTION_TRANSPORT": "smoke-signals"}):
        with pytest.raises(ActionDeliveryError):
            deliver(event)


# ---------------------------------------------------------------------------
# comm/registry.py — remaining branches
# ---------------------------------------------------------------------------


def test_validate_skips_when_jsonschema_missing(monkeypatch):
    from stapel_core.comm.registry import _validate

    monkeypatch.setitem(sys.modules, "jsonschema", None)  # forces ImportError
    with override_settings(STAPEL_COMM={"VALIDATE_SCHEMAS": True}):
        assert _validate("svc.x", {"n": "wrong"}, {"type": "object"}) is None


def test_function_registry_register_schema_and_names():
    function_registry.register("svc.b", lambda p: p)
    function_registry.register("svc.a", lambda p: p)
    function_registry.register_schema("svc.a", {"type": "object"})
    function_registry.register_schema("svc.a", None)  # None is ignored
    assert function_registry._schemas["svc.a"] == {"type": "object"}
    assert function_registry.names() == ["svc.a", "svc.b"]


def test_function_registry_same_handler_reregister_ok():
    handler = lambda p: p  # noqa: E731
    function_registry.register("svc.same", handler)
    function_registry.register("svc.same", handler)  # idempotent
    assert function_registry.get("svc.same") is handler


def test_action_registry_register_schema_and_names():
    action_registry.subscribe("b.a", lambda e: None)
    action_registry.subscribe("a.b", lambda e: None)
    action_registry.register_schema("a.b", {"type": "object"})
    action_registry.register_schema("a.b", None)
    assert action_registry._schemas["a.b"] == {"type": "object"}
    assert action_registry.names() == ["a.b", "b.a"]


def test_action_registry_duplicate_subscribe_ignored():
    handler = lambda e: None  # noqa: E731
    action_registry.subscribe("x.y", handler)
    action_registry.subscribe("x.y", handler)
    assert action_registry.handlers("x.y") == [handler]
