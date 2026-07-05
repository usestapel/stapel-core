"""stapel_core.gateway — registry, policy engine, invoke pipeline, audit.

Security invariants under test: deny-by-default (an undeclared verb does
not exist — S5 surface minimization), mandatory schema validation of
untrusted args (S5), safe policy defaults (unknown tier on a restricted
verb denies; malformed rate limit is a config error, not "unlimited"),
two-phase confirmation out of the caller's reach, and one audit line for
*every* outcome including refusals (S6).
"""
import sys
import types

import pytest
from django.test import override_settings

from stapel_core import gateway
from stapel_core.gateway import (
    ArgsInvalid,
    AuditFailure,
    CallerContext,
    ConfirmationInvalid,
    GatewayConfigError,
    HandlerError,
    PendingConfirmation,
    PolicyDenied,
    RateLimited,
    VerbDeclaration,
    VerbNotDeclared,
    VerbPolicy,
    register_verb,
    verb_registry,
)
from stapel_core.gateway.policy import DefaultPolicyEngine
from stapel_core.gateway.ratelimit import parse_rate
from stapel_core.gateway.registry import verb as verb_decorator

ECHO_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
    "additionalProperties": False,
}
NOARGS_SCHEMA = {"type": "object", "additionalProperties": False}


def echo_handler(args, caller):
    return {"echo": args["value"], "project": caller.project}


def boom_handler(args, caller):
    raise RuntimeError("kaput")


CALLER = CallerContext(channel="internal", project="p1", container="c1")


@pytest.fixture(autouse=True)
def clean_registry():
    verb_registry.clear()
    yield
    verb_registry.clear()


@pytest.fixture()
def audit_log():
    """Recording audit sink + restore; every test asserts lines, not logs."""
    records = []

    def sink(stream, payload, *, project, container):
        records.append({
            "stream": stream, "payload": payload,
            "project": project, "container": container,
        })

    with override_settings(STAPEL_GATEWAY={"AUDIT_SINK": sink}):
        yield records


def gw_settings(audit_records, **extra):
    def sink(stream, payload, *, project, container):
        audit_records.append({
            "stream": stream, "payload": payload,
            "project": project, "container": container,
        })

    return override_settings(STAPEL_GATEWAY={"AUDIT_SINK": sink, **extra})


# --------------------------------------------------------------------------
# declarations & merge-registry
# --------------------------------------------------------------------------

def test_declaration_requires_schema():
    with pytest.raises(ValueError, match="JSON schema"):
        VerbDeclaration(name="x", schema={}, handler=echo_handler)
    with pytest.raises(ValueError, match="JSON schema"):
        VerbDeclaration(name="x", schema=None, handler=echo_handler)


def test_declaration_requires_name_and_handler():
    with pytest.raises(ValueError, match="name"):
        VerbDeclaration(name="", schema=ECHO_SCHEMA, handler=echo_handler)
    with pytest.raises(ValueError, match="handler"):
        VerbDeclaration(name="x", schema=ECHO_SCHEMA, handler=None)


def test_policy_rejects_unknown_keys():
    with pytest.raises(ValueError, match="unknown policy keys"):
        VerbPolicy.from_mapping({"tires": ["pro"]})


def test_duplicate_registration_is_an_error():
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
    with pytest.raises(ValueError, match="exactly one declaration"):
        register_verb("echo", schema=ECHO_SCHEMA, handler=boom_handler)


def test_reregistering_same_declaration_is_idempotent():
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
    assert verb_registry.resolve("echo").handler is echo_handler


def test_decorator_registers():
    @verb_decorator("echo", schema=ECHO_SCHEMA, policy={"rate_limit": "5/m"})
    def handler(args, caller):
        return args

    decl = verb_registry.resolve("echo")
    assert decl.handler is handler
    assert decl.policy.rate_limit == "5/m"


def test_settings_declare_new_verb():
    with override_settings(STAPEL_GATEWAY={"VERBS": {
        "echo": {"schema": ECHO_SCHEMA, "handler": "gwtest_mod.echo",
                 "policy": {"tiers": ["pro"]}},
    }}):
        decl = verb_registry.resolve("echo")
        assert decl.handler == "gwtest_mod.echo"
        assert decl.policy.tiers == ("pro",)


def test_settings_patch_merges_policy_per_key():
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler,
                  policy={"rate_limit": "5/m", "require_confirmation": True})
    with override_settings(STAPEL_GATEWAY={"VERBS": {
        "echo": {"policy": {"rate_limit": "1/h"}},
    }}):
        decl = verb_registry.resolve("echo")
        assert decl.policy.rate_limit == "1/h"          # patched
        assert decl.policy.require_confirmation is True  # kept
        assert decl.handler is echo_handler              # kept


def test_settings_entry_none_disables_verb():
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
    with override_settings(STAPEL_GATEWAY={"VERBS": {"echo": None}}):
        with pytest.raises(VerbNotDeclared):
            verb_registry.resolve("echo")
        assert "echo" not in verb_registry.names()


def test_settings_only_verb_must_be_complete():
    with override_settings(STAPEL_GATEWAY={"VERBS": {"echo": {"handler": "x.y"}}}):
        with pytest.raises(GatewayConfigError, match="not a valid declaration"):
            verb_registry.resolve("echo")


def test_settings_entry_unknown_keys_fail_closed():
    with override_settings(STAPEL_GATEWAY={"VERBS": {"echo": {"handlr": "x.y"}}}):
        with pytest.raises(GatewayConfigError, match="unknown keys"):
            verb_registry.resolve("echo")


def test_names_merges_both_sources():
    register_verb("a", schema=NOARGS_SCHEMA, handler=echo_handler)
    register_verb("b", schema=NOARGS_SCHEMA, handler=echo_handler)
    with override_settings(STAPEL_GATEWAY={"VERBS": {
        "b": None,
        "c": {"schema": NOARGS_SCHEMA, "handler": "x.y"},
    }}):
        assert verb_registry.names() == ["a", "c"]


# --------------------------------------------------------------------------
# deny-by-default + schema validation (S5)
# --------------------------------------------------------------------------

def test_undeclared_verb_denied_and_audited(audit_log):
    with pytest.raises(VerbNotDeclared):
        gateway.invoke("nope", {"value": "x"}, caller=CALLER)
    assert len(audit_log) == 1
    line = audit_log[0]["payload"]
    assert line["decision"] == "denied"
    assert line["reason"] == "verb_not_declared"
    assert line["verb"] == "nope"
    assert audit_log[0]["project"] == "p1"


def test_args_violating_schema_denied_and_audited(audit_log):
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
    with pytest.raises(ArgsInvalid):
        gateway.invoke("echo", {"value": 42}, caller=CALLER)
    assert audit_log[-1]["payload"]["reason"] == "args_invalid"

    with pytest.raises(ArgsInvalid):
        gateway.invoke("echo", {"value": "ok", "extra": "smuggled"}, caller=CALLER)
    with pytest.raises(ArgsInvalid):
        gateway.invoke("echo", None, caller=CALLER)  # required arg missing
    assert len(audit_log) == 3
    assert all(r["payload"]["decision"] == "denied" for r in audit_log)


def test_missing_validator_fails_closed(audit_log, monkeypatch):
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
    monkeypatch.setattr("builtins.__import__", _import_blocking("jsonschema"))
    with pytest.raises(GatewayConfigError):
        gateway.invoke("echo", {"value": "x"}, caller=CALLER)
    assert audit_log[-1]["payload"]["reason"] == "gateway_misconfigured"


def _import_blocking(blocked):
    real_import = __import__

    def guarded(name, *args, **kwargs):
        if name == blocked:
            raise ImportError(f"{blocked} blocked for test")
        return real_import(name, *args, **kwargs)

    return guarded


def test_valid_call_executes_and_audits(audit_log):
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
    result = gateway.invoke("echo", {"value": "hi"}, caller=CALLER)
    assert result == {"echo": "hi", "project": "p1"}
    assert len(audit_log) == 1
    line = audit_log[0]["payload"]
    assert line["decision"] == "executed"
    assert line["ok"] is True
    assert line["verb"] == "echo"
    assert line["channel"] == "internal"
    assert line["args"] == {"value": "hi"}
    assert isinstance(line["duration_ms"], int)
    assert audit_log[0]["stream"] == "audit"
    assert audit_log[0]["container"] == "c1"


def test_handler_exception_wrapped_and_audited(audit_log):
    register_verb("boom", schema=NOARGS_SCHEMA, handler=boom_handler)
    with pytest.raises(HandlerError):
        gateway.invoke("boom", {}, caller=CALLER)
    line = audit_log[-1]["payload"]
    assert line["decision"] == "executed"
    assert line["ok"] is False
    assert "kaput" in line["error"]


def test_dotted_path_handler_resolves(audit_log):
    mod = types.ModuleType("gwtest_mod")
    mod.echo = echo_handler
    sys.modules["gwtest_mod"] = mod
    try:
        register_verb("echo", schema=ECHO_SCHEMA, handler="gwtest_mod.echo")
        result = gateway.invoke("echo", {"value": "hi"}, caller=CALLER)
        assert result["echo"] == "hi"
    finally:
        del sys.modules["gwtest_mod"]


def test_unimportable_handler_fails_closed_and_audited(audit_log):
    register_verb("echo", schema=ECHO_SCHEMA, handler="no.such.module.fn")
    with pytest.raises(GatewayConfigError):
        gateway.invoke("echo", {"value": "hi"}, caller=CALLER)
    assert audit_log[-1]["payload"]["reason"] == "gateway_misconfigured"


def test_per_verb_audit_stream_override(audit_log):
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler,
                  policy={"audit_stream": "audit.email"})
    gateway.invoke("echo", {"value": "hi"}, caller=CALLER)
    assert audit_log[0]["stream"] == "audit.email"


def test_oversized_args_fingerprinted_on_audit_line():
    records = []
    with gw_settings(records, AUDIT_ARGS_MAXLEN=64):
        register_verb("echo", schema={"type": "object"}, handler=echo_handler)
        with pytest.raises(HandlerError):  # echo_handler needs "value"
            gateway.invoke("echo", {"blob": "x" * 500}, caller=CALLER)
    line = records[-1]["payload"]
    assert "args" not in line
    assert len(line["args_sha256"]) == 64
    assert line["args_size"] > 500


def test_audit_sink_failure_fails_noisy_on_execution():
    def broken(stream, payload, *, project, container):
        raise OSError("disk full")

    with override_settings(STAPEL_GATEWAY={"AUDIT_SINK": broken}):
        register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
        with pytest.raises(AuditFailure):
            gateway.invoke("echo", {"value": "hi"}, caller=CALLER)


def test_audit_sink_failure_on_denial_still_denies():
    def broken(stream, payload, *, project, container):
        raise OSError("disk full")

    with override_settings(STAPEL_GATEWAY={"AUDIT_SINK": broken}):
        with pytest.raises(AuditFailure):
            gateway.invoke("nope", {}, caller=CALLER)


def test_default_sink_appends_to_eventstore(db):
    from stapel_core import eventstore

    with override_settings(STAPEL_EVENTSTORE={"BUFFER_SYNC": True}):
        register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
        gateway.invoke("echo", {"value": "hi"}, caller=CALLER)
        page = eventstore.query("audit", filters={"project": "p1"})
    assert len(page) == 1
    assert page.events[0].payload["verb"] == "echo"
    assert page.events[0].container == "c1"


# --------------------------------------------------------------------------
# policy: tiers
# --------------------------------------------------------------------------

def test_tier_restricted_verb_with_unknown_tier_denies(audit_log):
    register_verb("deploy", schema=NOARGS_SCHEMA, handler=echo_handler,
                  policy={"tiers": ["business"]})
    with pytest.raises(PolicyDenied):
        gateway.invoke("deploy", {}, caller=CallerContext(channel="internal", project="p1"))
    assert audit_log[-1]["payload"]["reason"] == "tier_unresolved"


def test_tier_denied_and_allowed(audit_log):
    register_verb("deploy", schema=NOARGS_SCHEMA, handler=lambda a, c: "ok",
                  policy={"tiers": ["business", "pro"]})
    free = CallerContext(channel="internal", project="p1", tier="free")
    with pytest.raises(PolicyDenied):
        gateway.invoke("deploy", {}, caller=free)
    assert audit_log[-1]["payload"]["reason"] == "tier_denied"

    pro = CallerContext(channel="internal", project="p1", tier="pro")
    assert gateway.invoke("deploy", {}, caller=pro) == "ok"


def test_tier_resolver_seam():
    records = []
    with gw_settings(records, TIER_RESOLVER=lambda project: {"p1": "business"}.get(project)):
        register_verb("deploy", schema=NOARGS_SCHEMA, handler=lambda a, c: "ok",
                      policy={"tiers": ["business"]})
        caller = CallerContext(channel="internal", project="p1")  # no tier carried
        assert gateway.invoke("deploy", {}, caller=caller) == "ok"
        with pytest.raises(PolicyDenied):
            gateway.invoke("deploy", {}, caller=CallerContext(channel="internal", project="p2"))


def test_unrestricted_verb_needs_no_tier(audit_log):
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
    assert gateway.invoke("echo", {"value": "x"},
                          caller=CallerContext(channel="internal"))["echo"] == "x"


# --------------------------------------------------------------------------
# policy: rate limit
# --------------------------------------------------------------------------

def test_parse_rate_forms():
    assert parse_rate("30/m") == (30, 60)
    assert parse_rate("5/s") == (5, 1)
    assert parse_rate("100/h") == (100, 3600)
    assert parse_rate("500/d") == (500, 86400)
    assert parse_rate("10/900") == (10, 900)


@pytest.mark.parametrize("bad", ["", "fast", "0/m", "10/0", "10/y", "-1/m"])
def test_malformed_rate_is_config_error_not_unlimited(bad):
    with pytest.raises(GatewayConfigError):
        parse_rate(bad)


def test_rate_limit_enforced_and_denial_audited(audit_log):
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler,
                  policy={"rate_limit": "2/h"})
    gateway.invoke("echo", {"value": "1"}, caller=CALLER)
    gateway.invoke("echo", {"value": "2"}, caller=CALLER)
    with pytest.raises(RateLimited):
        gateway.invoke("echo", {"value": "3"}, caller=CALLER)
    line = audit_log[-1]["payload"]
    assert line["decision"] == "denied"
    assert line["reason"] == "rate_limited"


def test_rate_limit_counted_per_project(audit_log):
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler,
                  policy={"rate_limit": "1/h"})
    gateway.invoke("echo", {"value": "1"}, caller=CALLER)
    other = CallerContext(channel="internal", project="p2")
    assert gateway.invoke("echo", {"value": "1"}, caller=other)["project"] == "p2"
    with pytest.raises(RateLimited):
        gateway.invoke("echo", {"value": "2"}, caller=CALLER)


def test_custom_policy_engine_seam():
    class NightFreeze(DefaultPolicyEngine):
        def check(self, declaration, args, caller):
            super().check(declaration, args, caller)
            raise PolicyDenied("freeze window", reason="freeze_window")

    records = []
    with gw_settings(records, POLICY_ENGINE=NightFreeze):
        register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
        with pytest.raises(PolicyDenied):
            gateway.invoke("echo", {"value": "x"}, caller=CALLER)
    assert records[-1]["payload"]["reason"] == "freeze_window"


# --------------------------------------------------------------------------
# require_confirmation: two-phase execution
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_confirmation_parks_call_without_executing(audit_log):
    executed = []
    register_verb("db_restore", schema=NOARGS_SCHEMA,
                  handler=lambda a, c: executed.append(1) or "done",
                  policy={"require_confirmation": True})
    result = gateway.invoke("db_restore", {}, caller=CALLER)
    assert isinstance(result, PendingConfirmation)
    assert executed == []
    line = audit_log[-1]["payload"]
    assert line["decision"] == "pending"
    assert line["confirmation_id"] == result.confirmation_id


@pytest.mark.django_db
def test_confirm_executes_with_original_identity(audit_log):
    register_verb("db_restore", schema=NOARGS_SCHEMA,
                  handler=lambda a, c: f"restored for {c.project} by {c.confirmed_by}",
                  policy={"require_confirmation": True})
    pending = gateway.invoke("db_restore", {}, caller=CALLER)
    out = gateway.confirm(pending.confirmation_id, approved_by="owner@p1")
    assert out == "restored for p1 by owner@p1"
    line = audit_log[-1]["payload"]
    assert line["decision"] == "executed"
    assert line["ok"] is True
    assert line["confirmed_by"] == "owner@p1"
    assert line["confirmation_id"] == pending.confirmation_id

    from stapel_core.django.gateway.models import PendingAction
    row = PendingAction.objects.get(pk=pending.confirmation_id)
    assert row.status == PendingAction.STATUS_EXECUTED
    assert row.resolved_by == "owner@p1"


@pytest.mark.django_db
def test_confirm_twice_refused(audit_log):
    register_verb("db_restore", schema=NOARGS_SCHEMA, handler=lambda a, c: "ok",
                  policy={"require_confirmation": True})
    pending = gateway.invoke("db_restore", {}, caller=CALLER)
    gateway.confirm(pending.confirmation_id, approved_by="owner")
    with pytest.raises(ConfirmationInvalid, match="already"):
        gateway.confirm(pending.confirmation_id, approved_by="owner")
    assert audit_log[-1]["payload"]["reason"] == "confirmation_resolved"


@pytest.mark.django_db
def test_reject_never_executes(audit_log):
    executed = []
    register_verb("db_restore", schema=NOARGS_SCHEMA,
                  handler=lambda a, c: executed.append(1),
                  policy={"require_confirmation": True})
    pending = gateway.invoke("db_restore", {}, caller=CALLER)
    assert gateway.confirm(pending.confirmation_id, approved_by="owner", approve=False) is None
    assert executed == []
    assert audit_log[-1]["payload"]["decision"] == "rejected"

    from stapel_core.django.gateway.models import PendingAction
    assert PendingAction.objects.get(pk=pending.confirmation_id).status == "rejected"


@pytest.mark.django_db
def test_expired_confirmation_refused(audit_log):
    from django.utils import timezone

    from stapel_core.django.gateway.models import PendingAction

    register_verb("db_restore", schema=NOARGS_SCHEMA, handler=lambda a, c: "ok",
                  policy={"require_confirmation": True})
    pending = gateway.invoke("db_restore", {}, caller=CALLER)
    PendingAction.objects.filter(pk=pending.confirmation_id).update(
        expires_at=timezone.now())
    with pytest.raises(ConfirmationInvalid, match="expired"):
        gateway.confirm(pending.confirmation_id, approved_by="owner")
    assert audit_log[-1]["payload"]["decision"] == "expired"
    assert PendingAction.objects.get(pk=pending.confirmation_id).status == "expired"


@pytest.mark.django_db
def test_unknown_confirmation_audited(audit_log):
    import uuid

    with pytest.raises(ConfirmationInvalid):
        gateway.confirm(str(uuid.uuid4()), approved_by="owner")
    assert audit_log[-1]["payload"]["reason"] == "confirmation_unknown"


@pytest.mark.django_db
def test_confirm_requires_identity():
    with pytest.raises(ValueError, match="approved_by"):
        gateway.confirm("whatever", approved_by="")


@pytest.mark.django_db
def test_confirmed_execution_reruns_policy(audit_log):
    """Tier revoked between park and confirm → the confirmed leg denies."""
    register_verb("db_restore", schema=NOARGS_SCHEMA, handler=lambda a, c: "ok",
                  policy={"require_confirmation": True, "tiers": ["business"]})
    caller = CallerContext(channel="internal", project="p1", tier="business")
    pending = gateway.invoke("db_restore", {}, caller=caller)

    from stapel_core.django.gateway.models import PendingAction
    PendingAction.objects.filter(pk=pending.confirmation_id).update(tier="free")
    with pytest.raises(PolicyDenied):
        gateway.confirm(pending.confirmation_id, approved_by="owner")
    assert audit_log[-1]["payload"]["reason"] == "tier_denied"
    assert PendingAction.objects.get(pk=pending.confirmation_id).status == "failed"


@pytest.mark.django_db
def test_confirmed_handler_failure_marks_failed(audit_log):
    register_verb("db_restore", schema=NOARGS_SCHEMA, handler=boom_handler,
                  policy={"require_confirmation": True})
    pending = gateway.invoke("db_restore", {}, caller=CALLER)
    with pytest.raises(HandlerError):
        gateway.confirm(pending.confirmation_id, approved_by="owner")

    from stapel_core.django.gateway.models import PendingAction
    assert PendingAction.objects.get(pk=pending.confirmation_id).status == "failed"
    line = audit_log[-1]["payload"]
    assert line["decision"] == "executed" and line["ok"] is False


# --------------------------------------------------------------------------
# configuration hardening & small contracts
# --------------------------------------------------------------------------

def test_policy_from_mapping_accepts_policy_instance():
    policy = VerbPolicy(rate_limit="5/m")
    assert VerbPolicy.from_mapping(policy) is policy


def test_settings_entry_must_be_dict_or_none():
    with override_settings(STAPEL_GATEWAY={"VERBS": {"echo": "yes please"}}):
        with pytest.raises(GatewayConfigError, match="dict or None"):
            verb_registry.resolve("echo")


def test_settings_policy_patch_with_bad_keys_fails_closed():
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
    with override_settings(STAPEL_GATEWAY={"VERBS": {
        "echo": {"policy": {"rate_limits": "5/m"}},
    }}):
        with pytest.raises(GatewayConfigError, match="policy.*invalid"):
            verb_registry.resolve("echo")


def test_settings_patch_with_broken_schema_fails_closed():
    register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
    with override_settings(STAPEL_GATEWAY={"VERBS": {"echo": {"schema": {}}}}):
        with pytest.raises(GatewayConfigError, match="patch is invalid"):
            verb_registry.resolve("echo")


def test_non_callable_handler_fails_closed(audit_log):
    mod = types.ModuleType("gwtest_mod2")
    mod.not_callable = "just a string"
    sys.modules["gwtest_mod2"] = mod
    try:
        register_verb("echo", schema=ECHO_SCHEMA, handler="gwtest_mod2.not_callable")
        with pytest.raises(GatewayConfigError, match="not callable"):
            gateway.invoke("echo", {"value": "x"}, caller=CALLER)
    finally:
        del sys.modules["gwtest_mod2"]
    assert audit_log[-1]["payload"]["reason"] == "gateway_misconfigured"


def test_bad_rate_limiter_setting_is_config_error():
    records = []
    with gw_settings(records, RATE_LIMITER="stapel_core.gateway.policy.PolicyEngine"):
        register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler,
                      policy={"rate_limit": "5/m"})
        with pytest.raises(GatewayConfigError, match="not a RateLimiter"):
            gateway.invoke("echo", {"value": "x"}, caller=CALLER)


def test_bad_policy_engine_setting_is_config_error():
    records = []
    with gw_settings(records, POLICY_ENGINE="stapel_core.gateway.ratelimit.CacheRateLimiter"):
        register_verb("echo", schema=ECHO_SCHEMA, handler=echo_handler)
        with pytest.raises(GatewayConfigError, match="not a PolicyEngine"):
            gateway.invoke("echo", {"value": "x"}, caller=CALLER)


def test_rate_limiter_recovers_from_expiry_race(monkeypatch):
    from stapel_core.gateway.ratelimit import CacheRateLimiter

    calls = {"n": 0}
    from django.core import cache as cache_mod
    real_incr = cache_mod.cache.incr

    def flaky_incr(key):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("key expired between add and incr")
        return real_incr(key)

    monkeypatch.setattr(cache_mod.cache, "incr", flaky_incr)
    limiter = CacheRateLimiter()
    assert limiter.allow("echo", CALLER, limit=5, window=60) is True
    assert calls["n"] == 2


@pytest.mark.django_db
def test_model_reprs():
    from django.utils import timezone

    from stapel_core.django.gateway.models import PendingAction, ScopeToken

    token = ScopeToken.objects.create(token_hash="a" * 64, project="p1",
                                      expires_at=timezone.now())
    assert "p1" in str(token)
    action = PendingAction.objects.create(verb="deploy", channel="http",
                                          expires_at=timezone.now())
    assert "deploy" in str(action) and "pending" in str(action)
