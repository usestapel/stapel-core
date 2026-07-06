"""Step-up on HIGH admin operations + access audit forwarding (admin-suite AS-6).

Q8a: step-up is part of the standard preset (delete=HIGH), enforced by default
— not opt-in. A HIGH-class admin mutation needs a fresh verification grant on
top of the mandate; the grant store is stapel_core.verification's (the same one
stapel-auth's step-up flow writes to — convergence, no auth hook here). With no
verification factor registered the feature self-disables (degradation).
"""
import uuid

import pytest
from django.contrib.admin.sites import AdminSite
from django.http import HttpResponseForbidden
from django.test import RequestFactory, override_settings

from stapel_core.access.audit import connect_access_audit
from stapel_core.access.checks import (
    E004_BAD_STEP_UP,
    W005_STEP_UP_DEGRADED,
    check_step_up,
)
from stapel_core.access.exceptions import AccessConfigError
from stapel_core.access.report import build_report, render_text
from stapel_core.access.signals import dac_escalation, step_up_denied
from stapel_core.access.stepup import (
    DEFAULT_STEP_UP,
    action_requires_step_up,
    step_up_active,
    step_up_config,
)
from stapel_core.django.admin.base import StapelModelAdmin
from stapel_core.django.users.models import User
from stapel_core.verification.factors import VerificationFactor, factor_registry
from stapel_core.verification.grants import grant_verification

pytestmark = pytest.mark.django_db

MANDATE_BACKENDS = [
    "stapel_core.access.backend.MandateBackend",
    "stapel_core.access.backend.AuditedModelBackend",
]


class _DummyFactor(VerificationFactor):
    id = "dummy"

    def verify(self, user, challenge, payload):  # pragma: no cover - unused
        return True


@pytest.fixture
def factor():
    """Register a verification factor so step-up is *capable* (not degraded)."""
    factor_registry.register(_DummyFactor())
    yield
    factor_registry.clear()


def make_staff(*, roles=None, superuser=False):
    user = User.objects.create(
        username=f"u_{uuid.uuid4().hex[:10]}",
        is_staff=True,
        is_superuser=superuser,
    )
    if roles is not None:
        user.staff_roles = list(roles)
        user.save(update_fields=["staff_roles"])
    return user


def grant(user, scope="sensitive"):
    grant_verification(user_id=str(user.pk), scope=scope, max_age=900)


def admin_for(model=User):
    return StapelModelAdmin(model, AdminSite())


def request_of(user):
    request = RequestFactory().get("/admin/")
    request.user = user
    return request


# ---------------------------------------------------------------------------
# policy parsing / degradation
# ---------------------------------------------------------------------------

def test_default_config_is_enforced_high_sensitive():
    cfg = step_up_config()
    assert cfg["ENFORCE"] is True             # Q8a: on by default
    assert cfg["LEVELS"] == frozenset({"high"})
    assert cfg["SCOPE"] == "sensitive"
    assert cfg["MAX_AGE"] == DEFAULT_STEP_UP["MAX_AGE"] == 900


def test_config_overrides_parse_and_normalize():
    with override_settings(STAPEL_ACCESS={"STEP_UP": {
        "ENFORCE": False, "LEVELS": ["mid", "high"], "SCOPE": "admin", "MAX_AGE": 60,
    }}):
        cfg = step_up_config()
        assert cfg["ENFORCE"] is False
        assert cfg["LEVELS"] == frozenset({"mid", "high"})
        assert cfg["SCOPE"] == "admin"
        assert cfg["MAX_AGE"] == 60


@pytest.mark.parametrize("bad", [
    {"LEVELS": "high"},              # not a list
    {"LEVELS": ["cosmic"]},          # unknown level
    {"MAX_AGE": 0},                  # non-positive
    {"MAX_AGE": True},               # bool is not an int here
    {"SCOPE": ""},                   # empty
    {"nope": 1},                     # unknown key
])
def test_config_rejects_malformed(bad):
    with override_settings(STAPEL_ACCESS={"STEP_UP": bad}):
        with pytest.raises(AccessConfigError):
            step_up_config()


def test_degraded_without_factor_then_active_with_one(factor):
    # `factor` fixture registers a factor → capable → active.
    assert step_up_active() is True


def test_not_active_without_factor():
    # no factor registered anywhere → grant unobtainable → self-disabled
    assert step_up_active() is False


def test_action_requires_step_up_only_high():
    # User is undecorated → implicit standard: delete=HIGH, add/change=MID, view=LOW
    assert action_requires_step_up(User, "delete") is True
    assert action_requires_step_up(User, "change") is False
    assert action_requires_step_up(User, "add") is False
    assert action_requires_step_up(User, "view") is False


# ---------------------------------------------------------------------------
# StapelModelAdmin enforcement (permission layer + educational 403)
# ---------------------------------------------------------------------------

def test_high_delete_denied_without_grant(factor):
    admin = admin_for()
    user = make_staff(roles=["admin"])  # HIGH clearance — mandate allows delete
    with override_settings(AUTHENTICATION_BACKENDS=MANDATE_BACKENDS):
        request = request_of(user)
        # mandate grants the base perm, step-up withholds it
        assert user.has_perm("users.delete_user") is True
        assert admin.has_delete_permission(request) is False


def test_high_delete_allowed_with_fresh_grant(factor):
    admin = admin_for()
    user = make_staff(roles=["admin"])
    grant(user)
    with override_settings(AUTHENTICATION_BACKENDS=MANDATE_BACKENDS):
        assert admin.has_delete_permission(request_of(user)) is True


def test_mid_and_low_operations_untouched(factor):
    admin = admin_for()
    user = make_staff(roles=["admin"])
    with override_settings(AUTHENTICATION_BACKENDS=MANDATE_BACKENDS):
        request = request_of(user)
        # change=MID, view=LOW — never step-up gated, no grant needed
        assert admin.has_change_permission(request) is True
        assert admin.has_add_permission(request) is True
        assert admin.has_view_permission(request) is True


def test_enforce_false_disables_gate(factor):
    admin = admin_for()
    user = make_staff(roles=["admin"])
    with override_settings(
        AUTHENTICATION_BACKENDS=MANDATE_BACKENDS,
        STAPEL_ACCESS={"STEP_UP": {"ENFORCE": False}},
    ):
        # without a grant, delete is allowed because ENFORCE is off
        assert admin.has_delete_permission(request_of(user)) is True


def test_degradation_no_factor_delete_allowed():
    # No factor registered → step-up self-disabled → prior behavior (mandate only)
    admin = admin_for()
    user = make_staff(roles=["admin"])
    with override_settings(AUTHENTICATION_BACKENDS=MANDATE_BACKENDS):
        assert admin.has_delete_permission(request_of(user)) is True


def test_superuser_also_under_step_up(factor):
    # A5: superusers are few and under step-up.
    admin = admin_for()
    su = make_staff(superuser=True)
    with override_settings(AUTHENTICATION_BACKENDS=MANDATE_BACKENDS):
        assert admin.has_delete_permission(request_of(su)) is False
        grant(su)
        assert admin.has_delete_permission(request_of(su)) is True


def test_delete_view_returns_educational_403(factor):
    admin = admin_for()
    target = make_staff(roles=["admin"])
    user = make_staff(roles=["admin"])
    with override_settings(AUTHENTICATION_BACKENDS=MANDATE_BACKENDS):
        response = admin.delete_view(request_of(user), str(target.pk))
    assert isinstance(response, HttpResponseForbidden)
    assert response.status_code == 403
    assert b"Step-up verification required" in response.content
    assert b"users.User" in response.content
    assert b"sensitive" in response.content


def test_view_passes_through_when_grant_fresh(factor):
    # With a grant, _step_up_response yields None → the stock view would run.
    admin = admin_for()
    user = make_staff(roles=["admin"])
    grant(user)
    with override_settings(AUTHENTICATION_BACKENDS=MANDATE_BACKENDS):
        assert admin._step_up_response(request_of(user), "delete") is None


def test_delete_view_fires_step_up_denied_signal(factor):
    received = []

    def handler(sender, **kw):
        received.append(kw)

    step_up_denied.connect(handler)
    admin = admin_for()
    user = make_staff(roles=["admin"])
    target = make_staff(roles=["admin"])
    try:
        with override_settings(AUTHENTICATION_BACKENDS=MANDATE_BACKENDS):
            admin.delete_view(request_of(user), str(target.pk))
    finally:
        step_up_denied.disconnect(handler)
    assert len(received) == 1
    assert received[0]["label"] == "users.User"
    assert received[0]["action"] == "delete"
    assert received[0]["scope"] == "sensitive"


# ---------------------------------------------------------------------------
# audit forwarding (dac_escalation / step_up_denied → sink + NOTIFY)
# ---------------------------------------------------------------------------

@pytest.fixture
def audit_wired():
    connect_access_audit()
    yield
    dac_escalation.disconnect(dispatch_uid="stapel_access.audit.dac_escalation")
    step_up_denied.disconnect(dispatch_uid="stapel_access.audit.step_up_denied")


def test_step_up_denied_forwarded_to_sink_and_notify(audit_wired):
    lines, alerts = [], []

    def sink(stream, payload, *, project=None, container=None):
        lines.append((stream, payload))

    def notify(event, payload):
        alerts.append((event, payload))

    with override_settings(STAPEL_ACCESS={"AUDIT_SINK": sink, "NOTIFY": notify}):
        step_up_denied.send(
            sender=User, user=make_staff(), label="users.User",
            action="delete", scope="sensitive",
        )
    assert len(lines) == 1
    stream, payload = lines[0]
    assert stream == "audit"
    assert payload["event"] == "access.step_up_denied"
    assert payload["model"] == "users.User"
    assert payload["action"] == "delete"
    assert alerts == [("access.step_up_denied", lines[0][1])]


def test_dac_escalation_forwarded_to_sink(audit_wired):
    from stapel_core.access.levels import Level

    lines = []
    with override_settings(STAPEL_ACCESS={
        "AUDIT_SINK": lambda s, p, **k: lines.append((s, p)),
    }):
        dac_escalation.send(
            sender=object(), user=make_staff(), perm="stapel_taskstore.delete_taskrecord",
            clearance=Level.LOW, required=Level.HIGH,
        )
    assert len(lines) == 1
    assert lines[0][1]["event"] == "access.dac_escalation"
    assert lines[0][1]["perm"] == "stapel_taskstore.delete_taskrecord"
    assert lines[0][1]["clearance"] == "low"
    assert lines[0][1]["required"] == "high"


def test_audit_sink_failure_is_swallowed(audit_wired, caplog):
    def broken(stream, payload, **kwargs):
        raise RuntimeError("eventstore down")

    with override_settings(STAPEL_ACCESS={"AUDIT_SINK": broken}):
        with caplog.at_level("ERROR", logger="stapel_core.access"):
            # Must NOT raise — best-effort telemetry, never breaks the caller.
            step_up_denied.send(
                sender=User, user=make_staff(), label="users.User",
                action="delete", scope="sensitive",
            )
    assert "access audit sink failed" in caplog.text


def test_connect_access_audit_is_idempotent():
    step_up_denied.disconnect(dispatch_uid="stapel_access.audit.step_up_denied")
    before = len(step_up_denied.receivers)
    connect_access_audit()
    connect_access_audit()
    try:
        assert len(step_up_denied.receivers) == before + 1
    finally:
        dac_escalation.disconnect(dispatch_uid="stapel_access.audit.dac_escalation")
        step_up_denied.disconnect(dispatch_uid="stapel_access.audit.step_up_denied")


# ---------------------------------------------------------------------------
# access_report step-up section
# ---------------------------------------------------------------------------

def test_report_step_up_section_degraded():
    report = build_report()
    step = report["step_up"]
    assert step["enforce"] is True
    assert step["capable"] is False        # no factor in a core-only report
    assert step["active"] is False
    assert step["scope"] == "sensitive"
    assert step["levels"] == ["high"]
    # users.User (implicit standard, delete=HIGH) is a gated model
    gated = {e["label"]: e["actions"] for e in step["gated_models"]}
    assert "delete" in gated.get("users.User", [])


def test_report_step_up_fresh_grant_aggregate(factor):
    user = make_staff(roles=["admin"])
    grant(user)
    make_staff(roles=["viewer"])  # no grant
    report = build_report()
    grants = report["step_up"]["fresh_grants"]
    assert grants["staff_total"] >= 2
    assert grants["with_fresh_grant"] == 1
    assert report["step_up"]["active"] is True  # factor registered


def test_report_render_text_has_step_up_section():
    text = render_text(build_report())
    assert "Step-up on HIGH operations" in text


# ---------------------------------------------------------------------------
# system checks
# ---------------------------------------------------------------------------

def _ids(findings):
    return [f.id for f in findings]


def test_check_bad_step_up_is_error():
    with override_settings(STAPEL_ACCESS={"STEP_UP": {"MAX_AGE": -1}}):
        assert E004_BAD_STEP_UP in _ids(check_step_up())


def test_check_step_up_degraded_warns():
    # enforced (default) but no factor registered → W005
    assert W005_STEP_UP_DEGRADED in _ids(check_step_up())


def test_check_step_up_clean_when_capable(factor):
    assert check_step_up() == []


def test_check_step_up_clean_when_disabled():
    with override_settings(STAPEL_ACCESS={"STEP_UP": {"ENFORCE": False}}):
        assert check_step_up() == []
