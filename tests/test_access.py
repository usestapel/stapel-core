"""stapel_core.access — declarations, roles, ROLE_SOURCES, MandateBackend (AS-1).

Covers the mandate matrix (clearance × declared level), app scopes (Q7),
the undeclared-model default, the source chain degradation ladder, and the
negative invariants: downgrade takes effect immediately, secret category is
unreachable without superuser.
"""
import uuid

import pytest
from django.contrib.auth.models import Group
from django.test import override_settings

from stapel_core.access import (
    AccessConfigError,
    Level,
    MandateBackend,
    access,
    clearance_for,
    effective_access,
    effective_roles,
    user_roles,
)
from stapel_core.access.declaration import (
    DECLARATION_ATTR,
    OPS,
    SECRET,
    SENSITIVE,
    STANDARD,
    declared_access,
    is_declared,
)
from stapel_core.access.levels import parse_category
from stapel_core.access.sources import CLAIM_ATTR
from stapel_core.django.outbox.models import OutboxEvent
from stapel_core.django.taskstore.models import TaskRecord
from stapel_core.django.users.models import User


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

@pytest.fixture
def declare():
    """Apply an @access decorator to a real model, undo on teardown."""
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


def make_user(*, roles=None, staff=True, superuser=False, active=True, save=False):
    user = User(
        username=f"u_{uuid.uuid4().hex[:10]}",
        is_staff=staff,
        is_superuser=superuser,
        is_active=active,
    )
    if save:
        user.save()
    if roles is not None:
        user.staff_roles = list(roles)  # picked up by user_field_roles
    return user


backend = MandateBackend()


# --------------------------------------------------------------------------
# levels & categories
# --------------------------------------------------------------------------

def test_level_total_order():
    assert Level.LOW < Level.MID < Level.HIGH < Level.SUPERUSER < Level.FORBIDDEN


def test_level_parse_strings():
    assert Level.parse("low") is Level.LOW
    assert Level.parse(" HIGH ") is Level.HIGH
    assert Level.parse(Level.MID) is Level.MID
    assert Level.parse("forbidden") is Level.FORBIDDEN


def test_level_parse_rejects_garbage():
    with pytest.raises(AccessConfigError):
        Level.parse("ultra")
    with pytest.raises(AccessConfigError):
        Level.parse(42)


def test_clearance_only_rejects_sentinels():
    with pytest.raises(AccessConfigError):
        Level.parse("superuser", clearance_only=True)
    with pytest.raises(AccessConfigError):
        Level.parse("forbidden", clearance_only=True)


def test_parse_category_rejects_unknown():
    with pytest.raises(AccessConfigError):
        parse_category("topsecret")
    assert parse_category("ops") == "ops"


# --------------------------------------------------------------------------
# declarations: decorator, presets, defaults, overrides
# --------------------------------------------------------------------------

def test_standard_preset_matches_design(declare):
    declare(access.standard, TaskRecord)
    decl = declared_access(TaskRecord)
    assert (decl.category, decl.view, decl.add, decl.change, decl.delete) == (
        "business", Level.LOW, Level.MID, Level.MID, Level.HIGH,
    )


def test_sensitive_preset(declare):
    declare(access.sensitive, TaskRecord)
    decl = declared_access(TaskRecord)
    assert decl == SENSITIVE
    assert decl.view is Level.MID and decl.delete is Level.HIGH


def test_ops_preset_read_only_journal(declare):
    declare(access.ops, OutboxEvent)
    decl = declared_access(OutboxEvent)
    assert decl == OPS
    assert decl.category == "ops"
    assert decl.view is Level.HIGH
    assert decl.add is Level.FORBIDDEN
    assert decl.change is Level.FORBIDDEN
    assert decl.delete is Level.FORBIDDEN


def test_secret_preset_superuser_only(declare):
    declare(access.secret, TaskRecord)
    decl = declared_access(TaskRecord)
    assert decl == SECRET
    assert all(
        decl.required(a) is Level.SUPERUSER for a in ("view", "add", "change", "delete")
    )


def test_full_form_decorator_accepts_strings_and_levels(declare):
    declare(
        access(view="mid", add=Level.HIGH, change="high", delete="high", category="business"),
        TaskRecord,
    )
    decl = declared_access(TaskRecord)
    assert decl.view is Level.MID and decl.add is Level.HIGH


def test_bare_access_decorator_is_standard(declare):
    declare(access, TaskRecord)
    assert declared_access(TaskRecord) == STANDARD
    assert is_declared(TaskRecord)


def test_undeclared_model_is_implicit_standard():
    assert declared_access(User) == STANDARD
    assert not is_declared(User)
    assert effective_access(User) == STANDARD


def test_declaration_inherited_by_subclass_fail_closed():
    @access.secret
    class Base:
        pass

    class Child(Base):
        pass

    assert declared_access(Child) == SECRET  # a child of a secret model stays secret


def test_models_override_patches_declaration(declare):
    declare(access.standard, TaskRecord)
    with override_settings(
        STAPEL_ACCESS={"MODELS": {"stapel_taskstore.TaskRecord": {"delete": "mid", "category": "ops"}}}
    ):
        decl = effective_access(TaskRecord)
        assert decl.delete is Level.MID
        assert decl.category == "ops"
        assert decl.view is Level.LOW  # untouched keys keep the decorator's values
    assert effective_access(TaskRecord).delete is Level.HIGH  # settings gone


def test_models_override_none_resets_to_standard(declare):
    declare(access.secret, TaskRecord)
    with override_settings(STAPEL_ACCESS={"MODELS": {"stapel_taskstore.TaskRecord": None}}):
        assert effective_access(TaskRecord) == STANDARD


def test_models_override_unknown_key_is_config_error(declare):
    declare(access.standard, TaskRecord)
    with override_settings(
        STAPEL_ACCESS={"MODELS": {"stapel_taskstore.TaskRecord": {"destroy": "low"}}}
    ):
        with pytest.raises(AccessConfigError):
            effective_access(TaskRecord)


# --------------------------------------------------------------------------
# role registry (merge over builtins)
# --------------------------------------------------------------------------

def test_builtin_roles():
    roles = effective_roles()
    assert roles["viewer"].clearance is Level.LOW
    assert roles["editor"].clearance is Level.MID
    assert roles["admin"].clearance is Level.HIGH


def test_settings_define_scoped_role():
    with override_settings(
        STAPEL_ACCESS={"ROLES": {"accountant": {"clearance": "low", "apps": {"stapel_outbox": "high"}}}}
    ):
        roles = effective_roles()
        accountant = roles["accountant"]
        assert accountant.clearance is Level.LOW
        assert accountant.clearance_for("stapel_outbox") is Level.HIGH
        assert accountant.clearance_for("users") is Level.LOW
        assert "viewer" in roles  # builtins survive the merge


def test_settings_patch_builtin_role():
    with override_settings(STAPEL_ACCESS={"ROLES": {"editor": {"clearance": "high"}}}):
        assert effective_roles()["editor"].clearance is Level.HIGH


def test_settings_disable_builtin_role():
    with override_settings(STAPEL_ACCESS={"ROLES": {"viewer": None}}):
        assert "viewer" not in effective_roles()


def test_new_role_requires_clearance():
    with override_settings(STAPEL_ACCESS={"ROLES": {"ghost": {"apps": {"users": "low"}}}}):
        with pytest.raises(AccessConfigError):
            effective_roles()


def test_role_unknown_keys_rejected():
    with override_settings(STAPEL_ACCESS={"ROLES": {"editor": {"level": "high"}}}):
        with pytest.raises(AccessConfigError):
            effective_roles()


def test_role_clearance_cannot_be_sentinel():
    with override_settings(STAPEL_ACCESS={"ROLES": {"deity": {"clearance": "superuser"}}}):
        with pytest.raises(AccessConfigError):
            effective_roles()


def test_clearance_for_takes_max_across_roles():
    assert clearance_for(["viewer", "admin"]) is Level.HIGH
    assert clearance_for(["viewer"]) is Level.LOW


def test_clearance_for_ignores_unknown_names():
    assert clearance_for(["nonexistent"]) is None
    assert clearance_for(["nonexistent", "editor"]) is Level.MID


def test_app_scope_can_lower_clearance():
    with override_settings(
        STAPEL_ACCESS={"ROLES": {"restricted": {"clearance": "high", "apps": {"users": "low"}}}}
    ):
        assert clearance_for(["restricted"], "users") is Level.LOW
        assert clearance_for(["restricted"], "stapel_outbox") is Level.HIGH


# --------------------------------------------------------------------------
# ROLE_SOURCES chain
# --------------------------------------------------------------------------

def test_claim_source_wins_over_field():
    user = make_user(roles=["admin"])
    setattr(user, CLAIM_ATTR, ["viewer"])
    assert user_roles(user) == {"viewer"}


def test_field_source_authoritative_even_when_empty(db):
    user = make_user(roles=[], save=True)
    group, _ = Group.objects.get_or_create(name="role:editor")
    user.groups.add(group)
    # staff_roles=[] terminates the chain: stale role:* groups must not
    # resurrect a revoked role (sync-down replace semantics, в.3/A3).
    assert user_roles(user) == frozenset()


def test_groups_fallback_without_field(db):
    user = make_user(save=True)
    # AS-2 adds staff_roles to the default User, so simulate a user model that
    # genuinely lacks the field (None => user_field_roles abstains) to exercise
    # the group_roles fallback rung of the ladder.
    user.staff_roles = None
    group, _ = Group.objects.get_or_create(name="role:editor")
    other, _ = Group.objects.get_or_create(name="Staff")
    user.groups.add(group, other)
    assert user_roles(user) == {"editor"}


def test_no_sources_yield_no_roles():
    assert user_roles(make_user()) == frozenset()


def test_unknown_role_names_silently_ignored():
    user = make_user(roles=["editor", "warlord"])
    assert user_roles(user) == {"editor"}


def test_custom_source_callable_in_settings():
    with override_settings(STAPEL_ACCESS={"ROLE_SOURCES": [lambda user: ["admin"]]}):
        assert user_roles(make_user()) == {"admin"}


def test_roles_cached_per_instance_fresh_instance_reevaluates(db):
    user = make_user(roles=["admin"], save=True)
    assert user_roles(user) == {"admin"}
    user.staff_roles = ["viewer"]
    assert user_roles(user) == {"admin"}  # instance cache (request-scoped)
    fresh = User.objects.get(pk=user.pk)
    fresh.staff_roles = ["viewer"]
    assert user_roles(fresh) == {"viewer"}  # next request sees the downgrade


# --------------------------------------------------------------------------
# MandateBackend: clearance × declaration matrix
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "role,expected",
    [
        ("viewer", {"view": True, "add": False, "change": False, "delete": False}),
        ("editor", {"view": True, "add": True, "change": True, "delete": False}),
        ("admin", {"view": True, "add": True, "change": True, "delete": True}),
    ],
)
def test_matrix_standard_declaration(declare, role, expected):
    declare(access.standard, TaskRecord)
    user = make_user(roles=[role])
    for action, allowed in expected.items():
        assert backend.has_perm(user, f"stapel_taskstore.{action}_taskrecord") is allowed


@pytest.mark.parametrize(
    "role,expected",
    [
        ("viewer", {"view": False, "add": False, "change": False, "delete": False}),
        ("editor", {"view": True, "add": False, "change": False, "delete": False}),
        ("admin", {"view": True, "add": True, "change": True, "delete": True}),
    ],
)
def test_matrix_sensitive_declaration(declare, role, expected):
    declare(access.sensitive, TaskRecord)
    user = make_user(roles=[role])
    for action, allowed in expected.items():
        assert backend.has_perm(user, f"stapel_taskstore.{action}_taskrecord") is allowed


def test_matrix_ops_read_only_even_for_high(declare):
    declare(access.ops, OutboxEvent)
    admin_user = make_user(roles=["admin"])
    assert backend.has_perm(admin_user, "stapel_outbox.view_outboxevent") is True
    for action in ("add", "change", "delete"):
        assert backend.has_perm(admin_user, f"stapel_outbox.{action}_outboxevent") is False
    # ops is invisible below HIGH
    editor = make_user(roles=["editor"])
    assert backend.has_perm(editor, "stapel_outbox.view_outboxevent") is False


def test_secret_unreachable_without_superuser(declare):
    declare(access.secret, TaskRecord)
    admin_user = make_user(roles=["admin"])  # highest staff clearance
    for action in ("view", "add", "change", "delete"):
        assert backend.has_perm(admin_user, f"stapel_taskstore.{action}_taskrecord") is False
    superuser = make_user(superuser=True)
    assert backend.has_perm(superuser, "stapel_taskstore.view_taskrecord") is True  # A5


def test_scoped_role_grants_only_in_its_app(declare):
    declare(access.standard, OutboxEvent)
    with override_settings(
        STAPEL_ACCESS={"ROLES": {"accountant": {"clearance": "low", "apps": {"stapel_outbox": "high"}}}}
    ):
        user = make_user(roles=["accountant"])
        assert backend.has_perm(user, "stapel_outbox.delete_outboxevent") is True
        assert backend.has_perm(user, "stapel_outbox.view_outboxevent") is True
        # outside the scoped app the role is plain LOW
        assert backend.has_perm(user, "users.view_user") is True
        assert backend.has_perm(user, "users.change_user") is False


def test_undeclared_model_gets_standard_behavior():
    editor = make_user(roles=["editor"])
    assert backend.has_perm(editor, "users.view_user") is True
    assert backend.has_perm(editor, "users.change_user") is True
    assert backend.has_perm(editor, "users.delete_user") is False


def test_non_staff_denied_even_with_roles():
    user = make_user(roles=["admin"], staff=False)
    assert backend.has_perm(user, "users.view_user") is False


def test_inactive_user_denied():
    user = make_user(roles=["admin"], active=False)
    assert backend.has_perm(user, "users.view_user") is False


def test_staff_without_roles_gets_nothing():
    # opt-in degradation: no roles anywhere → today's behavior (DAC only)
    assert backend.has_perm(make_user(), "users.view_user") is False


def test_custom_codename_not_mandate_governed():
    user = make_user(roles=["admin"])
    assert backend.has_perm(user, "users.frobnicate_user") is False
    assert backend.has_perm(user, "users.view_nosuchmodel") is False
    assert backend.has_perm(user, "malformed") is False


def test_role_disabled_in_registry_stops_granting():
    with override_settings(STAPEL_ACCESS={"ROLES": {"editor": None}}):
        user = make_user(roles=["editor"])
        assert backend.has_perm(user, "users.view_user") is False


def test_downgrade_effective_immediately_on_fresh_instance(db, declare):
    declare(access.standard, TaskRecord)
    user = make_user(roles=["admin"], save=True)
    assert backend.has_perm(user, "stapel_taskstore.delete_taskrecord") is True
    fresh = User.objects.get(pk=user.pk)
    fresh.staff_roles = ["viewer"]  # role revoked upstream, synced down
    assert backend.has_perm(fresh, "stapel_taskstore.delete_taskrecord") is False
    assert backend.has_perm(fresh, "stapel_taskstore.view_taskrecord") is True


def test_object_level_check_follows_class_level(declare):
    declare(access.standard, TaskRecord)
    user = make_user(roles=["editor"])
    sentinel = object()
    assert backend.has_perm(user, "stapel_taskstore.change_taskrecord", obj=sentinel) is True
    assert backend.has_perm(user, "stapel_taskstore.delete_taskrecord", obj=sentinel) is False


def test_has_module_perms(declare):
    declare(access.secret, OutboxEvent)
    viewer = make_user(roles=["viewer"])
    assert backend.has_module_perms(viewer, "users") is True
    assert backend.has_module_perms(viewer, "stapel_outbox") is False  # all secret
    assert backend.has_module_perms(viewer, "nonexistent_app") is False
    assert backend.has_module_perms(make_user(), "users") is False  # no roles
    assert backend.has_module_perms(make_user(staff=False, roles=["admin"]), "users") is False
    assert backend.has_module_perms(make_user(superuser=True), "users") is True


def test_authenticate_is_not_an_auth_path():
    assert backend.authenticate(None, username="x", password="y") is None
