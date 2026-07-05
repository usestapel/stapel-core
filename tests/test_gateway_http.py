"""Container-facing HTTP door + control-plane comm Functions.

The HTTP surface enforces the three authorization factors (project id,
scope token, network identity), maps every gateway refusal to a status
code, keeps 404 free of capability hints, and audits token/network
failures that happen before the invoke pipeline. The comm surface routes
``gateway.invoke`` / ``gateway.confirm`` for internal callers — and
confirmation is *only* there, never on the container door.
"""
import pytest
from django.test import override_settings
from rest_framework.test import APIRequestFactory

from stapel_core.comm import call
from stapel_core.gateway import issue_token, register_verb, verb_registry
from stapel_core.gateway.functions import register as register_gateway_functions
from stapel_core.gateway.http import GatewayInvokeView, get_gateway_urls

pytestmark = pytest.mark.django_db

ECHO_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
    "additionalProperties": False,
}
NOARGS_SCHEMA = {"type": "object", "additionalProperties": False}

factory = APIRequestFactory()
view = GatewayInvokeView.as_view()


def echo_handler(args, caller):
    return {"echo": args["value"], "project": caller.project,
            "channel": caller.channel}


@pytest.fixture(autouse=True)
def clean_registry():
    verb_registry.clear()
    yield
    verb_registry.clear()


@pytest.fixture()
def audit_log():
    records = []

    def sink(stream, payload, *, project, container):
        records.append({"stream": stream, "payload": payload,
                        "project": project, "container": container})

    with override_settings(STAPEL_GATEWAY={"AUDIT_SINK": sink}):
        yield records


def post(name, body=None, token=None, ip="10.0.7.4", header=None):
    request = factory.post(f"/api/_gateway/{name}/", body or {}, format="json",
                           REMOTE_ADDR=ip,
                           **({"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}),
                           **(header or {}))
    return view(request, name=name)


# --------------------------------------------------------------------------
# token factor
# --------------------------------------------------------------------------

def test_missing_token_401_and_audited(audit_log):
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
    response = post("echo", {"args": {"value": "x"}})
    assert response.status_code == 401
    assert audit_log[-1]["payload"]["reason"] == "token_missing"
    assert audit_log[-1]["payload"]["decision"] == "denied"


def test_forged_token_401(audit_log):
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
    response = post("echo", {"args": {"value": "x"}}, token="sgw_forged")
    assert response.status_code == 401
    assert audit_log[-1]["payload"]["reason"] == "token_unknown"


def test_expired_token_401(audit_log):
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
    issued = issue_token("p1", ttl=-1)
    response = post("echo", {"args": {"value": "x"}}, token=issued.token)
    assert response.status_code == 401
    assert audit_log[-1]["payload"]["reason"] == "token_expired"


def test_project_crosscheck_against_token(audit_log):
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
    issued = issue_token("p1")
    response = post("echo", {"args": {"value": "x"}, "project": "p2"},
                    token=issued.token)
    assert response.status_code == 401
    assert audit_log[-1]["payload"]["reason"] == "token_project_mismatch"


def test_x_gateway_token_header_works():
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
    issued = issue_token("p1")
    response = post("echo", {"args": {"value": "x"}},
                    header={"HTTP_X_GATEWAY_TOKEN": issued.token})
    assert response.status_code == 200


# --------------------------------------------------------------------------
# network factor
# --------------------------------------------------------------------------

def test_wrong_source_network_403(audit_log):
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
    issued = issue_token("p1", container="c1", network="10.0.7.4")
    response = post("echo", {"args": {"value": "x"}}, token=issued.token,
                    ip="192.168.1.50")
    assert response.status_code == 403
    line = audit_log[-1]["payload"]
    assert line["reason"] == "network_mismatch"
    assert line["ip"] == "192.168.1.50"
    assert audit_log[-1]["project"] == "p1"


def test_bound_network_passes():
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
    issued = issue_token("p1", network="10.0.7.0/24")
    response = post("echo", {"args": {"value": "x"}}, token=issued.token,
                    ip="10.0.7.4")
    assert response.status_code == 200


def test_require_network_binding_blocks_unbound_tokens(audit_log):
    with override_settings(STAPEL_GATEWAY={"REQUIRE_NETWORK_BINDING": True}):
        register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
        issued = issue_token("p1")  # no binding
        response = post("echo", {"args": {"value": "x"}}, token=issued.token)
    assert response.status_code == 403


# --------------------------------------------------------------------------
# status mapping + audit of the pipeline
# --------------------------------------------------------------------------

def test_success_200_with_caller_identity(audit_log):
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
    issued = issue_token("p1", container="c1", network="10.0.7.4")
    response = post("echo", {"args": {"value": "hi"}}, token=issued.token)
    assert response.status_code == 200
    assert response.data == {"result": {"echo": "hi", "project": "p1",
                                        "channel": "http"}}
    line = audit_log[-1]["payload"]
    assert line["decision"] == "executed"
    assert line["channel"] == "http"
    assert line["token_id"] == issued.token_id
    assert line["ip"] == "10.0.7.4"
    assert audit_log[-1]["container"] == "c1"


def test_unknown_verb_404_without_enumeration(audit_log):
    issued = issue_token("p1")
    response = post("secret_verb", {"args": {}}, token=issued.token)
    assert response.status_code == 404
    assert response.data == {"error": "unknown verb"}
    assert audit_log[-1]["payload"]["reason"] == "verb_not_declared"


def test_schema_violation_400(audit_log):
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
    issued = issue_token("p1")
    response = post("echo", {"args": {"value": 42}}, token=issued.token)
    assert response.status_code == 400
    assert audit_log[-1]["payload"]["reason"] == "args_invalid"


def test_tier_denial_403():
    register_verb("deploy", schema=NOARGS_SCHEMA, handler=echo_handler,
                  policy={"tiers": ["business"]})
    issued = issue_token("p1")
    response = post("deploy", {"args": {}}, token=issued.token)
    assert response.status_code == 403


def test_rate_limit_429(audit_log):
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler,
                  policy={"rate_limit": "1/h"})
    issued = issue_token("p1")
    assert post("echo", {"args": {"value": "a"}}, token=issued.token).status_code == 200
    response = post("echo", {"args": {"value": "b"}}, token=issued.token)
    assert response.status_code == 429
    assert audit_log[-1]["payload"]["reason"] == "rate_limited"


def test_handler_failure_502(audit_log):
    def bad(args, caller):
        raise RuntimeError("nope")

    register_verb("echo", schema=ECHO_SCHEMA, handler=bad)
    issued = issue_token("p1")
    response = post("echo", {"args": {"value": "x"}}, token=issued.token)
    assert response.status_code == 502
    assert audit_log[-1]["payload"]["ok"] is False


def test_pending_confirmation_202():
    register_verb("db_restore", schema=NOARGS_SCHEMA, handler=echo_handler,
                  policy={"require_confirmation": True})
    issued = issue_token("p1")
    response = post("db_restore", {"args": {}}, token=issued.token)
    assert response.status_code == 202
    assert response.data["status"] == "pending"
    assert response.data["confirmation_id"]


def test_no_confirmation_route_on_container_surface():
    """The container door exposes exactly one route: verb invocation."""
    patterns = get_gateway_urls()
    assert len(patterns) == 1
    assert "confirm" not in str(patterns[0].pattern)


def test_misconfigured_gateway_500(audit_log):
    register_verb("echo", schema=ECHO_SCHEMA, handler="no.such.module.fn")
    issued = issue_token("p1")
    response = post("echo", {"args": {"value": "x"}}, token=issued.token)
    assert response.status_code == 500
    assert audit_log[-1]["payload"]["reason"] == "gateway_misconfigured"


# --------------------------------------------------------------------------
# comm surface (control plane)
# --------------------------------------------------------------------------

@pytest.fixture()
def comm_functions():
    register_gateway_functions()  # idempotent; app.ready() did it once already
    yield


def test_comm_invoke(comm_functions, audit_log):
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
    out = call("gateway.invoke", {"verb": "echo", "args": {"value": "hi"},
                                  "project": "p1", "subject": "svc:mailer"})
    assert out == {"status": "ok",
                   "result": {"echo": "hi", "project": "p1", "channel": "comm"}}
    line = audit_log[-1]["payload"]
    assert line["channel"] == "comm"
    assert line["subject"] == "svc:mailer"


def test_comm_invoke_pending_and_confirm(comm_functions, audit_log):
    register_verb("db_restore", schema=NOARGS_SCHEMA,
                  handler=lambda a, c: f"by {c.confirmed_by}",
                  policy={"require_confirmation": True})
    parked = call("gateway.invoke", {"verb": "db_restore", "args": {},
                                     "project": "p1"})
    assert parked["status"] == "pending"
    confirmed = call("gateway.confirm", {
        "confirmation_id": parked["confirmation_id"],
        "approved_by": "owner@p1",
    })
    assert confirmed == {"status": "ok", "result": "by owner@p1"}


def test_comm_invoke_denials_propagate(comm_functions, audit_log):
    from stapel_core.comm.exceptions import FunctionCallError

    with pytest.raises(FunctionCallError):
        call("gateway.invoke", {"verb": "ghost", "args": {}})
    assert audit_log[-1]["payload"]["reason"] == "verb_not_declared"


def test_unexpected_error_500_without_detail(audit_log, monkeypatch):
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
    issued = issue_token("p1")

    def explode(*args, **kwargs):
        raise MemoryError("boom")

    monkeypatch.setattr("stapel_core.gateway.http.service.invoke", explode)
    response = post("echo", {"args": {"value": "x"}}, token=issued.token)
    assert response.status_code == 500
    assert response.data == {"error": "gateway failure"}
