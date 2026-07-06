"""Coverage tests for stapel_core.django.admin.* and stapel_core.django.groups."""
import json
from types import SimpleNamespace
from unittest import mock

import pytest
from django.conf import settings as dj_settings
from django.test import RequestFactory

from stapel_core.django.admin.context import stapel_services
from stapel_core.django.admin.redirect import AdminLoginRedirectMiddleware, _login_url
from stapel_core.django.groups import (
    STAFF_GROUP_NAME,
    add_user_to_staff_group,
    ensure_staff_group_permissions,
    export_staff_group_fixture,
    get_or_create_staff_group,
    load_staff_group_if_empty,
    setup_staff_group_from_fixture,
)

rf = RequestFactory()

# Minimal urlconf so django.shortcuts.redirect() can attempt reverse() and
# fall back to treating the target as a literal URL (test ROOT_URLCONF is '').
urlpatterns = []


@pytest.fixture
def urlconf(settings):
    settings.ROOT_URLCONF = __name__


@pytest.fixture
def admin_mixins(settings):
    """Import admin mixins with django.contrib.admin temporarily installed."""
    settings.INSTALLED_APPS = list(dj_settings.INSTALLED_APPS) + [
        "django.contrib.admin"
    ]
    from stapel_core.django.admin import mixins

    return mixins


# ---------------------------------------------------------------------------
# admin/mixins.py
# ---------------------------------------------------------------------------


class TestSuperuserOnlyMixin:
    def _request(self, is_superuser):
        return SimpleNamespace(user=SimpleNamespace(is_superuser=is_superuser))

    def test_superuser_allowed(self, admin_mixins):
        m = admin_mixins.SuperuserOnlyMixin()
        request = self._request(True)
        assert m.has_module_permission(request) is True
        assert m.has_view_permission(request) is True
        assert m.has_add_permission(request) is True
        assert m.has_change_permission(request, obj=None) is True
        assert m.has_delete_permission(request, obj=None) is True

    def test_regular_user_denied(self, admin_mixins):
        m = admin_mixins.SuperuserOnlyMixin()
        request = self._request(False)
        assert m.has_module_permission(request) is False
        assert m.has_view_permission(request) is False
        assert m.has_add_permission(request) is False
        assert m.has_change_permission(request) is False
        assert m.has_delete_permission(request) is False


class TestRevisionAdmin:
    def _admin(self, admin_mixins):
        from django.contrib.admin.sites import AdminSite
        from django.contrib.auth import get_user_model

        return admin_mixins.RevisionAdmin(get_user_model(), AdminSite())

    def test_list_display_gets_revision_fields(self, admin_mixins):
        ra = self._admin(admin_mixins)
        assert ra.get_list_display(None) == ["__str__", "revision", "deleted"]
        # idempotent — calling twice does not duplicate
        assert ra.get_list_display(None).count("revision") == 1

    def test_list_filter_gets_deleted(self, admin_mixins):
        assert self._admin(admin_mixins).get_list_filter(None) == ["deleted"]

    def test_readonly_fields_include_revision(self, admin_mixins):
        assert self._admin(admin_mixins).get_readonly_fields(None) == ["revision"]

    def test_mark_deleted_action(self, admin_mixins):
        ra = self._admin(admin_mixins)
        ra.message_user = mock.Mock()
        active = mock.Mock(deleted=False)
        already = mock.Mock(deleted=True)
        ra.mark_deleted(request=None, queryset=[active, already])
        active.soft_delete.assert_called_once_with()
        already.soft_delete.assert_not_called()
        assert "1 item(s)" in str(ra.message_user.call_args[0][1])

    def test_restore_deleted_action(self, admin_mixins):
        ra = self._admin(admin_mixins)
        ra.message_user = mock.Mock()
        active = mock.Mock(deleted=False)
        removed = mock.Mock(deleted=True)
        ra.restore_deleted(request=None, queryset=[active, removed])
        removed.restore.assert_called_once_with()
        active.restore.assert_not_called()


class TestOtherAdminClasses:
    def test_superuser_only_admin(self, admin_mixins):
        from django.contrib.admin.sites import AdminSite
        from django.contrib.auth import get_user_model

        adm = admin_mixins.SuperuserOnlyAdmin(get_user_model(), AdminSite())
        request = SimpleNamespace(user=SimpleNamespace(is_superuser=False))
        assert adm.has_view_permission(request) is False

    def test_user_admin_instantiates(self, admin_mixins):
        from django.contrib.admin.sites import AdminSite
        from django.contrib.auth import get_user_model

        ua = admin_mixins.UserAdmin(get_user_model(), AdminSite())
        assert "email" in ua.list_display
        assert ua.ordering == ["-created_at"]


# ---------------------------------------------------------------------------
# admin/context.py
# ---------------------------------------------------------------------------


class TestStapelServicesContext:
    def test_registry_driven_services(self, settings):
        # Services now come from the STAPEL_SERVICES deploy-config (AS-4),
        # not a framework hardcode.
        settings.STAPEL_SERVICES = [
            {"name": "Auth", "prefix": "auth"},
            {"name": "Translate", "prefix": "translate"},
        ]
        ctx = stapel_services(None)
        assert len(ctx["stapel_services"]) == 2
        assert ctx["stapel_services_multi"] is True
        assert ctx["current_service_prefix"] == ""
        # Swagger not mounted in the package harness (no ROOT_URLCONF) — the
        # introspection env-gate hides the Swagger links.
        assert ctx["current_swagger_url"] is None
        auth = ctx["stapel_services"][0]
        assert auth["admin_url"] == "/auth/admin/"
        assert auth["swagger_url"] is None
        assert auth["is_active"] is False

    def test_monolith_fallback_single_service(self, settings):
        # No STAPEL_SERVICES → one implicit service derived from URL_PREFIX;
        # "All Services" collapses (stapel_services_multi False).
        settings.STAPEL_SERVICES = None
        settings.URL_PREFIX = "translate/"
        ctx = stapel_services(None)
        assert len(ctx["stapel_services"]) == 1
        assert ctx["stapel_services_multi"] is False
        assert ctx["current_service_prefix"] == "translate"
        active = [s for s in ctx["stapel_services"] if s["is_active"]]
        assert len(active) == 1 and active[0]["prefix"] == "translate"


# ---------------------------------------------------------------------------
# admin/redirect.py
# ---------------------------------------------------------------------------


class TestAdminLoginRedirectMiddleware:
    def _mw(self):
        return AdminLoginRedirectMiddleware(lambda request: None)

    def test_login_url_default(self):
        assert _login_url() == "/auth/admin/login/"

    def test_login_url_empty_prefix(self, settings):
        settings.STAPEL_AUTH_SERVICE_PREFIX = ""
        assert _login_url() == "/admin/login/"

    def test_non_admin_path_ignored(self):
        request = rf.get("/api/items/")
        assert self._mw().process_request(request) is None

    def test_authenticated_user_passes(self):
        request = rf.get("/profiles/admin/")
        request.user = SimpleNamespace(is_authenticated=True)
        assert self._mw().process_request(request) is None

    def test_login_page_not_redirected(self):
        request = rf.get("/auth/admin/login/")
        request.user = SimpleNamespace(is_authenticated=False)
        assert self._mw().process_request(request) is None

    def test_unauthenticated_redirects_with_next(self, urlconf):
        request = rf.get("/profiles/admin/?tab=1")
        request.user = SimpleNamespace(is_authenticated=False)
        resp = self._mw().process_request(request)
        assert resp.status_code == 302
        assert resp.url == "/auth/admin/login/?next=%2Fprofiles%2Fadmin%2F%3Ftab%3D1"

    def test_request_without_user_redirects(self, urlconf):
        request = rf.get("/profiles/admin/")
        resp = self._mw().process_request(request)
        assert resp.status_code == 302

    def test_custom_auth_prefix(self, settings, urlconf):
        settings.STAPEL_AUTH_SERVICE_PREFIX = "sso"
        request = rf.get("/profiles/admin/")
        request.user = SimpleNamespace(is_authenticated=False)
        resp = self._mw().process_request(request)
        assert resp.url.startswith("/sso/admin/login/?next=")


# ---------------------------------------------------------------------------
# groups.py
# ---------------------------------------------------------------------------


def _make_user(**kwargs):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(**kwargs)


def _view_user_permission():
    from django.contrib.auth.models import Permission
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get(app_label="users", model="user")
    return Permission.objects.get(content_type=ct, codename="view_user")


@pytest.mark.django_db
class TestStaffGroup:
    def test_get_or_create_is_idempotent(self):
        g1 = get_or_create_staff_group()
        g2 = get_or_create_staff_group()
        assert g1.pk == g2.pk
        assert g1.name == STAFF_GROUP_NAME

    def test_non_staff_user_not_added(self):
        user = _make_user(username="plain")
        assert add_user_to_staff_group(user) is False

    def test_superuser_not_added(self):
        user = _make_user(username="root", is_staff=True, is_superuser=True)
        assert add_user_to_staff_group(user) is False

    def test_staff_user_added_once(self):
        user = _make_user(username="staff1", is_staff=True, email="s@example.com")
        assert add_user_to_staff_group(user) is True
        assert add_user_to_staff_group(user) is False
        assert user.groups.filter(name=STAFF_GROUP_NAME).exists()


@pytest.mark.django_db
class TestEnsurePermissions:
    def test_all_permissions_for_app(self):
        ensure_staff_group_permissions("users")
        group = get_or_create_staff_group()
        count = group.permissions.count()
        assert count > 0
        # Idempotent
        ensure_staff_group_permissions("users")
        assert group.permissions.count() == count

    def test_specific_permissions_with_missing_entries(self):
        ensure_staff_group_permissions(
            "users",
            {
                "user": ["view_user", "bogus_permission"],
                "missingmodel": ["view_missing"],
            },
        )
        group = get_or_create_staff_group()
        assert group.permissions.filter(codename="view_user").exists()
        assert not group.permissions.filter(codename="bogus_permission").exists()
        # Idempotent for the existing permission
        ensure_staff_group_permissions("users", {"user": ["view_user"]})
        assert group.permissions.filter(codename="view_user").count() == 1


@pytest.mark.django_db
class TestFixtures:
    def _fixture(self, tmp_path, extra=()):
        data = {
            "group_name": STAFF_GROUP_NAME,
            "permissions": [
                {"app_label": "users", "model": "user", "codename": "view_user"},
                *extra,
            ],
        }
        path = tmp_path / "staff.json"
        path.write_text(json.dumps(data))
        return str(path)

    def test_setup_from_fixture(self, tmp_path):
        bad = {"app_label": "users", "model": "user", "codename": "nope"}
        setup_staff_group_from_fixture(self._fixture(tmp_path, extra=[bad]))
        group = get_or_create_staff_group()
        assert group.permissions.filter(codename="view_user").exists()
        assert group.permissions.count() == 1

    def test_setup_missing_file_is_noop(self, tmp_path):
        setup_staff_group_from_fixture(str(tmp_path / "missing.json"))

    def test_export_without_group_writes_nothing(self, tmp_path):
        out = tmp_path / "out.json"
        export_staff_group_fixture(str(out))
        assert not out.exists()

    def test_export_roundtrip(self, tmp_path):
        group = get_or_create_staff_group()
        group.permissions.add(_view_user_permission())
        out = tmp_path / "out.json"
        export_staff_group_fixture(str(out))
        data = json.loads(out.read_text())
        assert data["group_name"] == STAFF_GROUP_NAME
        assert data["permissions"] == [
            {"app_label": "users", "model": "user", "codename": "view_user"}
        ]

    def test_load_if_empty(self, tmp_path):
        fixture = self._fixture(tmp_path)
        assert load_staff_group_if_empty(fixture) is True
        group = get_or_create_staff_group()
        assert group.permissions.count() == 1
        # Second call: group already has permissions
        assert load_staff_group_if_empty(fixture) is False
