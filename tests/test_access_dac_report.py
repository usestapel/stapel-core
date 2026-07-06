"""stapel_core.access — DAC overlay (audit + STRICT), access_report, checks (AS-1).

A4 in action: a manual Permission grant above the mandate works by default
but is logged, signalled, and listed by the report; STRICT turns the mandate
into a ceiling.
"""
import json
import uuid
from io import StringIO

import pytest
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.test import override_settings

from stapel_core.access import AuditedModelBackend, Level, access
from stapel_core.access.checks import (
    E001_BAD_ROLES,
    E002_BAD_MODELS,
    E003_STRICT_UNENFORCEABLE,
    W001_BACKEND_NOT_INSTALLED,
    W002_UNAUDITED_DAC,
    W003_UNKNOWN_MODEL_LABEL,
    W004_RUNTIME_ROLES_RESERVED,
    check_access_backends,
    check_access_config,
)
from stapel_core.access.declaration import DECLARATION_ATTR
from stapel_core.access.report import build_report, render_text
from stapel_core.access.signals import dac_escalation
from stapel_core.django.management.commands.access_report import Command
from stapel_core.django.taskstore.models import TaskRecord
from stapel_core.django.users.models import User

pytestmark = pytest.mark.django_db

dac_backend = AuditedModelBackend()


@pytest.fixture
def declare():
    touched = []

    def _apply(decorator, model):
        touched.append((model, model.__dict__.get(DECLARATION_ATTR)))
        return decorator(model)

    yield _apply
    for model, previous in reversed(touched):
        if previous is None:
            if DECLARATION_ATTR in model.__dict__:
                delattr(model, DECLARATION_ATTR)
        else:
            setattr(model, DECLARATION_ATTR, previous)


@pytest.fixture
def escalations():
    received = []

    def handler(sender, **kwargs):
        received.append(kwargs)

    dac_escalation.connect(handler)
    yield received
    dac_escalation.disconnect(handler)


def make_staff(*, roles=None, superuser=False):
    user = User.objects.create(
        username=f"u_{uuid.uuid4().hex[:10]}",
        is_staff=True,
        is_superuser=superuser,
    )
    if roles is not None:
        user.staff_roles = list(roles)
        # AS-2: the staff_roles field is now the authoritative role source, so
        # persist it — build_report() reloads users from the DB.
        user.save(update_fields=["staff_roles"])
    return user


def perm(codename, app_label="stapel_taskstore"):
    return Permission.objects.get(
        content_type__app_label=app_label, codename=codename
    )


def grant(user, codename, app_label="stapel_taskstore"):
    user.user_permissions.add(perm(codename, app_label))
    # ModelBackend caches permissions per instance — return a fresh one.
    fresh = User.objects.get(pk=user.pk)
    if hasattr(user, "staff_roles"):
        fresh.staff_roles = user.staff_roles
    return fresh


# --------------------------------------------------------------------------
# AuditedModelBackend — DAC default: allowed + audited
# --------------------------------------------------------------------------

def test_dac_grant_within_mandate_is_silent(declare, escalations):
    declare(access.standard, TaskRecord)
    user = grant(make_staff(roles=["editor"]), "change_taskrecord")
    assert dac_backend.has_perm(user, "stapel_taskstore.change_taskrecord") is True
    assert escalations == []


def test_dac_escalation_granted_and_audited(declare, escalations, caplog):
    declare(access.standard, TaskRecord)
    user = grant(make_staff(roles=["viewer"]), "delete_taskrecord")
    with caplog.at_level("WARNING", logger="stapel_core.access"):
        assert dac_backend.has_perm(user, "stapel_taskstore.delete_taskrecord") is True
    assert len(escalations) == 1
    event = escalations[0]
    assert event["perm"] == "stapel_taskstore.delete_taskrecord"
    assert event["clearance"] is Level.LOW
    assert event["required"] is Level.HIGH
    assert event["user"].pk == user.pk
    assert "DAC escalation above mandate" in caplog.text


def test_dac_escalation_audited_once_per_instance(declare, escalations):
    declare(access.standard, TaskRecord)
    user = grant(make_staff(roles=["viewer"]), "delete_taskrecord")
    assert dac_backend.has_perm(user, "stapel_taskstore.delete_taskrecord") is True
    assert dac_backend.has_perm(user, "stapel_taskstore.delete_taskrecord") is True
    assert len(escalations) == 1  # request-scoped dedup


def test_dac_escalation_via_group_grant(declare, escalations):
    declare(access.standard, TaskRecord)
    user = make_staff(roles=["viewer"])
    group, _ = Group.objects.get_or_create(name="ops-fixers")
    group.permissions.add(perm("delete_taskrecord"))
    user.groups.add(group)
    fresh = User.objects.get(pk=user.pk)
    fresh.staff_roles = ["viewer"]
    assert dac_backend.has_perm(fresh, "stapel_taskstore.delete_taskrecord") is True
    assert len(escalations) == 1


def test_staff_without_roles_keeps_legacy_dac(escalations):
    # mandate not engaged — plain ModelBackend behavior, no audit noise
    user = grant(make_staff(), "delete_taskrecord")
    assert dac_backend.has_perm(user, "stapel_taskstore.delete_taskrecord") is True
    assert escalations == []


def test_dac_without_grant_still_denied(declare):
    declare(access.standard, TaskRecord)
    user = make_staff(roles=["viewer"])
    assert dac_backend.has_perm(user, "stapel_taskstore.delete_taskrecord") is False


# --------------------------------------------------------------------------
# STRICT: mandate is a ceiling
# --------------------------------------------------------------------------

def test_strict_denies_dac_escalation(declare, escalations):
    declare(access.standard, TaskRecord)
    user = grant(make_staff(roles=["viewer"]), "delete_taskrecord")
    with override_settings(STAPEL_ACCESS={"STRICT": True}):
        assert dac_backend.has_perm(user, "stapel_taskstore.delete_taskrecord") is False
    assert escalations == []
    # and back to allowed-with-audit once STRICT is off
    fresh = User.objects.get(pk=user.pk)
    fresh.staff_roles = ["viewer"]
    assert dac_backend.has_perm(fresh, "stapel_taskstore.delete_taskrecord") is True
    assert len(escalations) == 1


def test_strict_keeps_grants_within_mandate(declare):
    declare(access.standard, TaskRecord)
    user = grant(make_staff(roles=["editor"]), "change_taskrecord")
    with override_settings(STAPEL_ACCESS={"STRICT": True}):
        assert dac_backend.has_perm(user, "stapel_taskstore.change_taskrecord") is True


def test_strict_superuser_outside_mandate(declare):
    declare(access.secret, TaskRecord)
    superuser = make_staff(superuser=True)
    with override_settings(STAPEL_ACCESS={"STRICT": True}):
        assert dac_backend.has_perm(superuser, "stapel_taskstore.delete_taskrecord") is True


def test_strict_custom_codename_unaffected(declare):
    declare(access.standard, TaskRecord)
    # custom (non-CRUD) codenames stay pure DAC even in STRICT
    ct = perm("view_taskrecord").content_type
    custom = Permission.objects.create(
        codename="requeue_taskrecord2", name="Can requeue", content_type=ct
    )
    user = make_staff(roles=["viewer"])
    user.user_permissions.add(custom)
    fresh = User.objects.get(pk=user.pk)
    fresh.staff_roles = ["viewer"]
    with override_settings(STAPEL_ACCESS={"STRICT": True}):
        assert dac_backend.has_perm(fresh, "stapel_taskstore.requeue_taskrecord2") is True


# --------------------------------------------------------------------------
# full chain through user.has_perm
# --------------------------------------------------------------------------

def test_user_has_perm_through_backend_chain(declare):
    declare(access.standard, TaskRecord)
    with override_settings(
        AUTHENTICATION_BACKENDS=[
            "stapel_core.access.backend.MandateBackend",
            "stapel_core.access.backend.AuditedModelBackend",
        ]
    ):
        user = make_staff(roles=["editor"])
        assert user.has_perm("stapel_taskstore.change_taskrecord") is True  # mandate
        assert user.has_perm("stapel_taskstore.delete_taskrecord") is False
        assert user.has_module_perms("stapel_taskstore") is True

        granted = grant(make_staff(roles=["viewer"]), "delete_taskrecord")
        assert granted.has_perm("stapel_taskstore.delete_taskrecord") is True  # DAC


# --------------------------------------------------------------------------
# access_report
# --------------------------------------------------------------------------

def test_report_matrix_and_roles(declare):
    declare(access.ops, TaskRecord)
    report = build_report()
    assert report["roles"]["editor"]["clearance"] == "mid"
    entry = next(m for m in report["models"] if m["label"] == "stapel_taskstore.TaskRecord")
    assert entry["category"] == "ops"
    assert entry["declared"] is True
    assert entry["requirements"] == {
        "view": "high", "add": "forbidden", "change": "forbidden", "delete": "forbidden",
    }
    assert entry["roles"]["admin"] == "v---"
    assert entry["roles"]["viewer"] == "----"


def test_report_lists_dac_escalations(declare):
    declare(access.standard, TaskRecord)
    # AS-2: role via the authoritative staff_roles field (default User now has
    # it, so the field source wins over the role:* group fallback).
    user = make_staff(roles=["viewer"])
    user.user_permissions.add(perm("delete_taskrecord"))
    report = build_report()
    rows = [r for r in report["dac_escalations"] if r["user_id"] == str(user.pk)]
    assert len(rows) == 1
    assert rows[0]["perm"] == "stapel_taskstore.delete_taskrecord"
    assert rows[0]["required"] == "high"
    assert rows[0]["clearance"] == "low"
    assert rows[0]["roles"] == ["viewer"]


def test_report_flags_grants_of_unmandated_staff():
    user = make_staff()  # no roles at all
    user.user_permissions.add(perm("delete_taskrecord"))
    report = build_report()
    rows = [r for r in report["dac_escalations"] if r["user_id"] == str(user.pk)]
    assert len(rows) == 1
    assert rows[0]["clearance"] is None


def test_report_skips_grants_within_mandate(declare):
    declare(access.standard, TaskRecord)
    # AS-2: role via the authoritative staff_roles field (see above).
    user = make_staff(roles=["admin"])
    user.user_permissions.add(perm("delete_taskrecord"))  # admin may delete anyway
    report = build_report()
    assert [r for r in report["dac_escalations"] if r["user_id"] == str(user.pk)] == []


def test_report_undeclared_models(declare):
    declare(access.standard, TaskRecord)
    report = build_report()
    assert "users.User" in report["undeclared"]
    assert "stapel_taskstore.TaskRecord" not in report["undeclared"]


def test_report_render_text_smoke(declare):
    declare(access.secret, TaskRecord)
    text = render_text(build_report())
    assert "STAPEL ACCESS REPORT" in text
    assert "stapel_taskstore.TaskRecord" in text
    assert "DAC grants above mandate" in text


def test_report_command_text_and_json(declare):
    declare(access.ops, TaskRecord)
    out = StringIO()
    call_command(Command(), stdout=out)
    assert "STAPEL ACCESS REPORT" in out.getvalue()

    out = StringIO()
    call_command(Command(), "--json", stdout=out)
    payload = json.loads(out.getvalue())
    assert payload["strict"] is False
    assert "stapel_taskstore.TaskRecord" in payload["undeclared"] or any(
        m["label"] == "stapel_taskstore.TaskRecord" for m in payload["models"]
    )


# --------------------------------------------------------------------------
# system checks
# --------------------------------------------------------------------------

def _ids(findings):
    return [f.id for f in findings]


def test_checks_clean_config_no_findings():
    with override_settings(
        STAPEL_ACCESS={"ROLES": {"accountant": {"clearance": "low"}}},
        AUTHENTICATION_BACKENDS=[
            "stapel_core.access.backend.MandateBackend",
            "stapel_core.access.backend.AuditedModelBackend",
        ],
    ):
        assert check_access_config() == []
        assert check_access_backends() == []


def test_check_bad_roles_is_error():
    with override_settings(STAPEL_ACCESS={"ROLES": {"x": {"clearance": "cosmic"}}}):
        assert E001_BAD_ROLES in _ids(check_access_config())


def test_check_bad_model_entry_is_error():
    with override_settings(
        STAPEL_ACCESS={"MODELS": {"stapel_taskstore.TaskRecord": {"nuke": "low"}}}
    ):
        assert E002_BAD_MODELS in _ids(check_access_config())


def test_check_unknown_model_label_is_warning():
    # legal: shared deploy config may target models of other services
    with override_settings(STAPEL_ACCESS={"MODELS": {"stapel_billing.Invoice": {"view": "mid"}}}):
        findings = check_access_config()
        assert _ids(findings) == [W003_UNKNOWN_MODEL_LABEL]
        assert all(f.is_serious() is False for f in findings)


def test_check_backend_not_installed_warns():
    with override_settings(STAPEL_ACCESS={"ROLES": {"moderator": {"clearance": "mid"}}}):
        assert W001_BACKEND_NOT_INSTALLED in _ids(check_access_backends())


def test_check_strict_with_plain_modelbackend_is_error():
    with override_settings(
        STAPEL_ACCESS={"STRICT": True},
        AUTHENTICATION_BACKENDS=[
            "stapel_core.access.backend.MandateBackend",
            "django.contrib.auth.backends.ModelBackend",
        ],
    ):
        assert E003_STRICT_UNENFORCEABLE in _ids(check_access_backends())


def test_check_unaudited_dac_warns():
    with override_settings(
        STAPEL_ACCESS={"ROLES": {}},
        AUTHENTICATION_BACKENDS=[
            "stapel_core.access.backend.MandateBackend",
            "django.contrib.auth.backends.ModelBackend",
        ],
    ):
        assert W002_UNAUDITED_DAC in _ids(check_access_backends())


def test_check_runtime_role_definitions_reserved():
    with override_settings(STAPEL_ACCESS={"RUNTIME_ROLE_DEFINITIONS": True}):
        assert W004_RUNTIME_ROLES_RESERVED in _ids(check_access_backends())
