"""Admin visibility (admin-suite AS-3) — categories enforced end to end.

The matrix (category × role × operation) is driven through a real admin
client on **direct URLs** — the spec explicitly rejects app-list filtering,
so the tests must prove that ``/admin/app/model/`` itself answers 403/200
per the mandate, not that an index entry disappeared. On top of that:
secret-field masking, SHOW_OPS_MODELS dev mode, STAPEL_ADMIN["MODELS"]
overrides (category re-base / unregister / admin-class swap), and the Q9
django.contrib re-registration (auth.Group, sessions.Session).
"""
import uuid
from datetime import timedelta

import pytest
from django.contrib import admin
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import Group
from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone

from stapel_core.access import Level, effective_access
from stapel_core.access.declaration import CONTRIB_OPS_LABELS, OPS, STANDARD
from stapel_core.django.admin.base import (
    MASK_PLACEHOLDER,
    StapelModelAdmin,
)
from stapel_core.django.admin.checks import (
    E001_BAD_MODEL_ENTRY,
    E002_BAD_ADMIN_CLASS,
    W001_UNKNOWN_MODEL_LABEL,
    W002_SECRET_DOWNGRADED,
    check_admin_models,
    check_secret_downgrades,
)
from stapel_core.django.admin.conf import admin_settings, show_ops_models
from stapel_core.django.admin.registration import (
    StapelSessionAdmin,
    apply_admin_overrides,
    group_admin_class,
)
from stapel_core.django.eventstore.models import EventRecord, EventRollup
from stapel_core.django.gateway.models import PendingAction, ScopeToken
from stapel_core.django.outbox.models import OutboxEvent
from stapel_core.django.taskstore.models import TaskRecord
from stapel_core.django.users.models import User

# ---------------------------------------------------------------------------
# environment: a real admin site over the mandate backend chain
# ---------------------------------------------------------------------------

ADMIN_ENV = dict(
    INSTALLED_APPS=[
        "django.contrib.admin",
        "django.contrib.contenttypes",
        "django.contrib.auth",
        "django.contrib.sessions",
        "django.contrib.messages",
        "rest_framework",
        "stapel_core.django.apps.CommonDjangoConfig",
        "stapel_core.django.users",
        "stapel_core.django.outbox",
        "stapel_core.django.taskstore",
        "stapel_core.django.eventstore",
        "stapel_core.django.gateway",
    ],
    MIDDLEWARE=[
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.middleware.common.CommonMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "django.contrib.messages.middleware.MessageMiddleware",
    ],
    ROOT_URLCONF="tests.admin_urls",
    # No DB tables needed for sessions/messages:
    SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies",
    MESSAGE_STORAGE="django.contrib.messages.storage.cookie.CookieStorage",
    TEMPLATES=[{
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }],
    AUTHENTICATION_BACKENDS=[
        "stapel_core.access.backend.MandateBackend",
        "stapel_core.access.backend.AuditedModelBackend",
    ],
)


def _ensure_tables(*models):
    """Create tables of late-installed contrib apps (idempotent)."""
    existing = set(connection.introspection.table_names())
    for model in models:
        if model._meta.db_table not in existing:
            with connection.schema_editor() as editor:
                editor.create_model(model)


@pytest.fixture(scope="session")
def _admin_tables(django_db_setup, django_db_blocker):
    """LogEntry/Session tables — created once, outside any test transaction
    (the SQLite schema editor refuses to run inside atomic())."""
    with django_db_blocker.unblock():
        with override_settings(**ADMIN_ENV):
            # Late imports: these model classes only exist once their contrib
            # apps are installed (the override above).
            from django.contrib.admin.models import LogEntry
            from django.contrib.sessions.models import Session

            _ensure_tables(LogEntry, Session)


@pytest.fixture
def admin_env(db, _admin_tables):
    with override_settings(**ADMIN_ENV):
        snapshot = dict(admin.site._registry)
        try:
            yield
        finally:
            # Destructive override tests (None = unregister) must not leak
            # into the module-scoped global site.
            admin.site._registry.clear()
            admin.site._registry.update(snapshot)


def make_staff(*, roles=None, staff=True, superuser=False):
    user = User.objects.create(
        username=f"u_{uuid.uuid4().hex[:10]}",
        is_staff=staff,
        is_superuser=superuser,
    )
    if roles is not None:
        user.staff_roles = list(roles)
        user.save(update_fields=["staff_roles"])
    return user


def client_for(user):
    client = Client()
    # MandateBackend is not a session-auth path (BaseBackend.get_user is None);
    # pin the DAC half, exactly what a real password login would record.
    client.force_login(user, backend="stapel_core.access.backend.AuditedModelBackend")
    return client


def outbox_row():
    return OutboxEvent.objects.create(topic="orders.created", event_json="{}")


def token_row():
    return ScopeToken.objects.create(
        token_hash="a" * 64,
        project="proj-1",
        expires_at=timezone.now() + timedelta(hours=1),
    )


# ---------------------------------------------------------------------------
# matrix: business × role × operation — direct URLs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "role,view_ok,add_ok,delete_ok",
    [
        ("viewer", True, False, False),
        ("editor", True, True, False),
        ("admin", True, True, True),
    ],
)
def test_business_matrix_direct_urls(admin_env, role, view_ok, add_ok, delete_ok):
    # users.User is undecorated → implicit standard, registered via a *bare*
    # ModelAdmin (users/admin.py) — enforcement is pure backend, no cosmetics.
    target = make_staff()
    client = client_for(make_staff(roles=[role]))

    assert client.get("/admin/users/user/").status_code == (200 if view_ok else 403)
    assert client.get("/admin/users/user/add/").status_code == (200 if add_ok else 403)
    assert client.get(
        f"/admin/users/user/{target.pk}/delete/"
    ).status_code == (200 if delete_ok else 403)


def test_non_staff_redirected_to_login(admin_env):
    client = client_for(make_staff(roles=["admin"], staff=False))
    response = client.get("/admin/users/user/")
    assert response.status_code == 302
    assert "/admin/login/" in response.url


def test_staff_without_roles_denied(admin_env):
    # Mandate not engaged, no DAC grants — today's legacy behavior is "nothing".
    client = client_for(make_staff())
    assert client.get("/admin/users/user/").status_code == 403


# ---------------------------------------------------------------------------
# matrix: ops × role — hidden below HIGH, read-only for everyone
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role,visible", [("viewer", False), ("editor", False), ("admin", True)])
def test_ops_changelist_requires_high(admin_env, role, visible):
    outbox_row()
    client = client_for(make_staff(roles=[role]))
    assert client.get("/admin/stapel_outbox/outboxevent/").status_code == (
        200 if visible else 403
    )


def test_ops_direct_url_closed_not_just_index(admin_env):
    # The spec's anti-goal: get_app_list filtering would still leave this open.
    row = outbox_row()
    client = client_for(make_staff(roles=["editor"]))
    assert client.get(f"/admin/stapel_outbox/outboxevent/{row.pk}/change/").status_code == 403
    assert client.get("/admin/stapel_outbox/outboxevent/add/").status_code == 403


def test_ops_read_only_even_for_superuser(admin_env):
    row = outbox_row()
    client = client_for(make_staff(superuser=True))
    assert client.get("/admin/stapel_outbox/outboxevent/").status_code == 200
    assert client.get("/admin/stapel_outbox/outboxevent/add/").status_code == 403
    assert client.get(f"/admin/stapel_outbox/outboxevent/{row.pk}/delete/").status_code == 403
    response = client.get(f"/admin/stapel_outbox/outboxevent/{row.pk}/change/")
    assert response.status_code == 200  # view-only rendering
    assert b'name="topic"' not in response.content  # no editable form fields
    # and a write attempt is rejected outright
    assert client.post(
        f"/admin/stapel_outbox/outboxevent/{row.pk}/change/", {"topic": "x"}
    ).status_code == 403


def test_ops_high_clearance_views_readonly(admin_env):
    row = outbox_row()
    client = client_for(make_staff(roles=["admin"]))
    response = client.get(f"/admin/stapel_outbox/outboxevent/{row.pk}/change/")
    assert response.status_code == 200
    assert b'name="topic"' not in response.content
    assert client.post(
        f"/admin/stapel_outbox/outboxevent/{row.pk}/change/", {"topic": "x"}
    ).status_code == 403


def test_ops_hidden_from_index_below_high(admin_env):
    client = client_for(make_staff(roles=["editor"]))
    response = client.get("/admin/")
    assert response.status_code == 200
    assert b"Stapel Outbox" not in response.content
    high = client_for(make_staff(roles=["admin"]))
    assert b"Stapel Outbox" in high.get("/admin/").content


# ---------------------------------------------------------------------------
# matrix: secret — superuser-only, masked
# ---------------------------------------------------------------------------

def test_secret_unreachable_for_highest_staff(admin_env):
    row = token_row()
    client = client_for(make_staff(roles=["admin"]))  # highest clearance
    assert client.get("/admin/stapel_gateway/scopetoken/").status_code == 403
    assert client.get(f"/admin/stapel_gateway/scopetoken/{row.pk}/change/").status_code == 403


def test_secret_superuser_sees_masked_value(admin_env):
    row = token_row()
    client = client_for(make_staff(superuser=True))
    assert client.get("/admin/stapel_gateway/scopetoken/").status_code == 200
    response = client.get(f"/admin/stapel_gateway/scopetoken/{row.pk}/change/")
    assert response.status_code == 200
    html = response.content.decode()
    assert "a" * 64 not in html  # the stored hash never reaches the response
    assert MASK_PLACEHOLDER in html


def test_secret_field_never_becomes_a_form_field(admin_env):
    client = client_for(make_staff(superuser=True))
    response = client.get("/admin/stapel_gateway/scopetoken/add/")
    assert response.status_code == 200
    assert b'name="token_hash"' not in response.content


# ---------------------------------------------------------------------------
# SHOW_OPS_MODELS: dev mode — visible to any staff, still read-only
# ---------------------------------------------------------------------------

def test_show_ops_models_reveals_to_staff_read_only(admin_env):
    row = outbox_row()
    client = client_for(make_staff(roles=["editor"]))
    with override_settings(STAPEL_ADMIN={"SHOW_OPS_MODELS": True}):
        assert client.get("/admin/stapel_outbox/outboxevent/").status_code == 200
        assert client.get("/admin/stapel_outbox/outboxevent/add/").status_code == 403
        assert client.post(
            f"/admin/stapel_outbox/outboxevent/{row.pk}/change/", {"topic": "x"}
        ).status_code == 403
        assert b"Stapel Outbox" in client.get("/admin/").content
    # flag off again — hidden
    assert client.get("/admin/stapel_outbox/outboxevent/").status_code == 403


def test_show_ops_models_does_not_open_secret(admin_env):
    client = client_for(make_staff(roles=["admin"]))
    with override_settings(STAPEL_ADMIN={"SHOW_OPS_MODELS": True}):
        assert client.get("/admin/stapel_gateway/scopetoken/").status_code == 403


def test_show_ops_models_env_coercion(monkeypatch):
    assert show_ops_models() is False
    with override_settings(STAPEL_ADMIN={"SHOW_OPS_MODELS": "1"}):
        assert show_ops_models() is True
    with override_settings(STAPEL_ADMIN={"SHOW_OPS_MODELS": "false"}):
        assert show_ops_models() is False
    monkeypatch.setenv("SHOW_OPS_MODELS", "true")
    admin_settings.reload()
    try:
        assert show_ops_models() is True  # 12-factor: env-readable default
    finally:
        monkeypatch.delenv("SHOW_OPS_MODELS")
        admin_settings.reload()


# ---------------------------------------------------------------------------
# STAPEL_ADMIN["MODELS"]: one resolution with the access overlay (§3.7)
# ---------------------------------------------------------------------------

def test_admin_category_override_rebasess_on_preset():
    with override_settings(
        STAPEL_ADMIN={"MODELS": {"stapel_outbox.OutboxEvent": {"category": "business"}}}
    ):
        # "показать всем стаффам": the category flip re-bases levels on the
        # business preset — view becomes LOW, not the ops HIGH.
        assert effective_access(OutboxEvent) == STANDARD
    assert effective_access(OutboxEvent) == OPS  # settings gone


def test_admin_category_override_with_explicit_level():
    with override_settings(
        STAPEL_ADMIN={"MODELS": {"stapel_outbox.OutboxEvent": {"category": "business", "view": "mid"}}}
    ):
        decl = effective_access(OutboxEvent)
        assert decl.category == "business"
        assert decl.view is Level.MID
        assert decl.delete is Level.HIGH  # rest of the business preset


def test_admin_class_key_ignored_by_access_resolution():
    with override_settings(
        STAPEL_ADMIN={"MODELS": {"stapel_outbox.OutboxEvent": {"admin_class": "x.y.Z"}}}
    ):
        assert effective_access(OutboxEvent) == OPS


def test_both_overlays_combine():
    with override_settings(
        STAPEL_ACCESS={"MODELS": {"stapel_taskstore.TaskRecord": {"view": "mid"}}},
        STAPEL_ADMIN={"MODELS": {"stapel_taskstore.TaskRecord": {"delete": "high"}}},
    ):
        decl = effective_access(TaskRecord)
        assert decl.view is Level.MID     # from STAPEL_ACCESS
        assert decl.delete is Level.HIGH  # from STAPEL_ADMIN
        assert decl.category == "ops"     # declaration untouched otherwise


def test_admin_none_leaves_access_levels_alone():
    # None = unregister from the admin; API-level permissions unchanged.
    with override_settings(
        STAPEL_ADMIN={"MODELS": {"stapel_outbox.OutboxEvent": None}}
    ):
        assert effective_access(OutboxEvent) == OPS


def test_category_override_makes_ops_visible_to_low_staff(admin_env):
    outbox_row()
    client = client_for(make_staff(roles=["viewer"]))
    assert client.get("/admin/stapel_outbox/outboxevent/").status_code == 403
    with override_settings(
        STAPEL_ADMIN={"MODELS": {"stapel_outbox.OutboxEvent": {"category": "business"}}}
    ):
        # Real visibility through the backend (enforcement), not cosmetics.
        assert client.get("/admin/stapel_outbox/outboxevent/").status_code == 200


def test_none_override_unregisters_direct_url_404(admin_env):
    import sys
    import types

    from django.urls import path

    client = client_for(make_staff(superuser=True))
    assert client.get("/admin/stapel_outbox/outboxevent/").status_code == 200
    with override_settings(
        STAPEL_ADMIN={"MODELS": {"stapel_outbox.OutboxEvent": None}}
    ):
        apply_admin_overrides()
        assert not admin.site.is_registered(OutboxEvent)
        # In a deployment the hook runs in ready(), before the urlconf is
        # built; the test resolver was built earlier, so rebuild it the same
        # way to observe the direct URL going 404.
        urlconf = types.ModuleType("tests._as3_none_urls")
        urlconf.urlpatterns = [path("admin/", admin.site.urls)]
        sys.modules["tests._as3_none_urls"] = urlconf
        try:
            with override_settings(ROOT_URLCONF="tests._as3_none_urls"):
                assert client.get("/admin/stapel_outbox/outboxevent/").status_code == 404
                # sibling ops admin is untouched
                assert client.get("/admin/stapel_taskstore/taskrecord/").status_code == 200
        finally:
            del sys.modules["tests._as3_none_urls"]


def test_admin_class_override_swaps_registration(admin_env):
    # admin_env: importing an admin module (the dotted path below) needs
    # django.contrib.admin installed.
    from stapel_core.django.outbox.admin import OutboxEventAdmin

    site = AdminSite(name="swap-test")
    site.register(OutboxEvent, StapelModelAdmin)
    with override_settings(
        STAPEL_ADMIN={"MODELS": {"stapel_outbox.OutboxEvent": {
            "admin_class": "stapel_core.django.outbox.admin.OutboxEventAdmin",
        }}}
    ):
        apply_admin_overrides(site)
    assert type(site._registry[OutboxEvent]) is OutboxEventAdmin


def test_overrides_skip_unknown_labels_and_plain_patches():
    site = AdminSite(name="skip-test")
    site.register(OutboxEvent, StapelModelAdmin)
    with override_settings(
        STAPEL_ADMIN={"MODELS": {
            "stapel_billing.StripeWebhookEvent": None,       # other service
            "stapel_outbox.OutboxEvent": {"category": "business"},  # no admin key
        }}
    ):
        apply_admin_overrides(site)
    assert type(site._registry[OutboxEvent]) is StapelModelAdmin


# ---------------------------------------------------------------------------
# Q9: django.contrib service tables
# ---------------------------------------------------------------------------

def test_contrib_labels_default_to_ops(admin_env):
    from django.contrib.sessions.models import Session

    assert "auth.Group" in CONTRIB_OPS_LABELS
    assert "sessions.Session" in CONTRIB_OPS_LABELS
    assert effective_access(Group) == OPS
    assert effective_access(Session) == OPS


def test_contrib_group_reregistered_declaration_aware(admin_env):
    from django.contrib.sessions.models import Session

    assert isinstance(admin.site._registry[Group], group_admin_class())
    assert isinstance(admin.site._registry[Group], StapelModelAdmin)
    assert isinstance(admin.site._registry[Session], StapelSessionAdmin)


def test_contrib_group_hidden_and_read_only(admin_env):
    Group.objects.create(name="ops-fixers")
    editor = client_for(make_staff(roles=["editor"]))
    assert editor.get("/admin/auth/group/").status_code == 403  # direct URL
    root = client_for(make_staff(superuser=True))
    assert root.get("/admin/auth/group/").status_code == 200
    assert root.get("/admin/auth/group/add/").status_code == 403  # ops read-only


def test_contrib_group_show_ops_models(admin_env):
    editor = client_for(make_staff(roles=["editor"]))
    with override_settings(STAPEL_ADMIN={"SHOW_OPS_MODELS": True}):
        assert editor.get("/admin/auth/group/").status_code == 200
        assert editor.get("/admin/auth/group/add/").status_code == 403


def test_contrib_group_recategorized_business_is_editable(admin_env):
    # The documented escape hatch back to the classic editable Group.
    editor = client_for(make_staff(roles=["editor"]))
    with override_settings(
        STAPEL_ADMIN={"MODELS": {"auth.Group": {"category": "business"}}}
    ):
        assert editor.get("/admin/auth/group/").status_code == 200
        assert editor.get("/admin/auth/group/add/").status_code == 200


def test_contrib_session_admin_masks_key_material(admin_env):
    from django.contrib.sessions.models import Session

    session_admin = admin.site._registry[Session]
    assert session_admin.secret_fields == ("session_key", "session_data")
    display = session_admin.get_list_display(None)
    assert "stapel_masked_session_key" in display
    assert "session_key" not in display


# ---------------------------------------------------------------------------
# StapelModelAdmin unit behavior (no client needed)
# ---------------------------------------------------------------------------

def _plain_admin(model, **attrs):
    cls = type(f"{model.__name__}TestAdmin", (StapelModelAdmin,), attrs)
    return cls(model, AdminSite())


def test_secret_pattern_autodetection():
    instance = _plain_admin(ScopeToken)
    assert instance._masked_field_names() == ("token_hash",)


def test_explicit_secret_fields_win_over_patterns():
    instance = _plain_admin(ScopeToken, secret_fields=("network",))
    assert instance._masked_field_names() == ("network",)


def test_no_masking_on_business_without_explicit_fields():
    instance = _plain_admin(User)
    assert instance._masked_field_names() == ()


def test_masked_field_excluded_from_forms():
    instance = _plain_admin(ScopeToken)
    assert "token_hash" in instance.get_exclude(None)


def test_masked_field_stripped_from_search():
    instance = _plain_admin(ScopeToken, search_fields=("token_hash", "project"))
    assert instance.get_search_fields(None) == ["project"]


def test_masked_field_replaced_in_list_display():
    instance = _plain_admin(ScopeToken, list_display=("id", "token_hash", "project"))
    display = instance.get_list_display(None)
    assert "token_hash" not in display
    assert "stapel_masked_token_hash" in display


def test_mask_renderer_never_returns_the_value():
    instance = _plain_admin(ScopeToken)
    render = getattr(instance, instance._mask_display_name("token_hash"))
    row = ScopeToken(token_hash="b" * 64)
    assert "b" * 64 not in str(render(row))
    assert MASK_PLACEHOLDER in str(render(row))
    assert str(render(ScopeToken(token_hash=""))) == "—"


def test_ops_readonly_covers_every_concrete_field():
    instance = _plain_admin(OutboxEvent)
    readonly = instance.get_readonly_fields(None)
    concrete = {f.name for f in OutboxEvent._meta.concrete_fields}
    assert concrete <= set(readonly)


def test_ops_masked_field_readonly_via_placeholder():
    # ops + explicit secret_fields (the Session shape): read-only list carries
    # the masked callable, never the raw attribute.
    instance = _plain_admin(OutboxEvent, secret_fields=("event_json",))
    readonly = instance.get_readonly_fields(None)
    assert "stapel_masked_event_json" in readonly
    assert "event_json" not in readonly


def test_business_admin_keeps_default_behavior():
    with override_settings(
        STAPEL_ADMIN={"MODELS": {"stapel_outbox.OutboxEvent": {"category": "business"}}}
    ):
        instance = _plain_admin(OutboxEvent)
        assert instance.get_readonly_fields(None) == []
        assert instance.get_exclude(None) is None


def test_declared_ops_models_cover_core_tables():
    for model in (OutboxEvent, TaskRecord, EventRecord, EventRollup, PendingAction):
        assert effective_access(model) == OPS, model
    decl = effective_access(ScopeToken)
    assert decl.category == "secret"


# ---------------------------------------------------------------------------
# system checks (tag stapel_admin)
# ---------------------------------------------------------------------------

def _ids(findings):
    return [f.id for f in findings]


def test_checks_clean_config():
    with override_settings(
        STAPEL_ADMIN={"MODELS": {"stapel_outbox.OutboxEvent": {"category": "business"}},
                      "SHOW_OPS_MODELS": True}
    ):
        assert check_admin_models() == []
        assert check_secret_downgrades() == []


def test_check_unknown_entry_key_is_error():
    with override_settings(
        STAPEL_ADMIN={"MODELS": {"stapel_outbox.OutboxEvent": {"bogus": "low"}}}
    ):
        assert E001_BAD_MODEL_ENTRY in _ids(check_admin_models())


def test_check_non_dict_entry_is_error():
    with override_settings(
        STAPEL_ADMIN={"MODELS": {"stapel_outbox.OutboxEvent": "hide"}}
    ):
        assert E001_BAD_MODEL_ENTRY in _ids(check_admin_models())


def test_check_unknown_label_is_warning():
    with override_settings(
        STAPEL_ADMIN={"MODELS": {"stapel_billing.Invoice": {"category": "ops"}}}
    ):
        findings = check_admin_models()
        assert _ids(findings) == [W001_UNKNOWN_MODEL_LABEL]
        assert all(not f.is_serious() for f in findings)


def test_check_bad_admin_class_is_error():
    with override_settings(
        STAPEL_ADMIN={"MODELS": {"stapel_outbox.OutboxEvent": {"admin_class": "no.such.Thing"}}}
    ):
        assert E002_BAD_ADMIN_CLASS in _ids(check_admin_models())


def test_check_secret_downgrade_is_warning():
    with override_settings(
        STAPEL_ADMIN={"MODELS": {"stapel_gateway.ScopeToken": {"category": "business"}}}
    ):
        findings = check_secret_downgrades()
        assert _ids(findings) == [W002_SECRET_DOWNGRADED]
        assert "stapel_gateway.ScopeToken" in findings[0].msg


def test_check_secret_downgrade_via_access_overlay_too():
    with override_settings(
        STAPEL_ACCESS={"MODELS": {"stapel_gateway.ScopeToken": {"category": "business"}}}
    ):
        assert W002_SECRET_DOWNGRADED in _ids(check_secret_downgrades())
