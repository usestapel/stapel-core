"""Mount registry + derived URLs + system checks (stapel_core.django.mounts)."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from django.test import RequestFactory
from django.urls import get_script_prefix, set_script_prefix

import tests.mounts_address_urls as fixture
from stapel_core.django.checks import (
    E001_LOGIN_URL_UNRESOLVABLE,
    E002_REDIRECT_URL_UNRESOLVABLE,
    E003_BAD_MOUNTS,
    E004_MODULE_OUTSIDE_CANON,
    E005_MODULE_API_OFF_CANON,
    E006_MODULE_SURFACE_UNMOUNTED,
    W001_STOCK_LOGIN_REDIRECT,
    check_auth_redirect_settings,
    check_module_api_address,
    check_mounts_config,
    check_module_surface_containment,
)
from stapel_core.django.mounts import (
    MODULE_RESERVED_SUFFIXES,
    Mount,
    MountConfigError,
    admin_index_url,
    admin_login_url,
    get_mount,
    get_mounts,
    lazy_admin_login_url,
    mount_path,
    mount_reverse,
    reserved_paths,
)

rf = RequestFactory()

URLS = "tests.mounts_urls"
URLS_PREFIXED = "tests.mounts_urls_prefixed"
URLS_SURFACE = "tests.mounts_surface_urls"
URLS_ADDRESS = "tests.mounts_address_urls"


def _app(name, label=None, stapel_module=None):
    """A minimal stand-in for a Django AppConfig — mirrors the one in
    test_nav_modules.py (only the attributes discover_modules()/is_stapel_app()
    read)."""
    kwargs = dict(name=name, label=label or name.rsplit(".", 1)[-1])
    if stapel_module is not None:
        kwargs["stapel_module"] = stapel_module
    return SimpleNamespace(**kwargs)


@pytest.fixture
def mock_apps(monkeypatch):
    """Patch django.apps.apps.get_app_configs() to return *configs."""

    def _set(*configs):
        import django.apps

        monkeypatch.setattr(django.apps.apps, "get_app_configs", lambda: list(configs))

    return _set


@contextmanager
def script_prefix(prefix):
    old = get_script_prefix()
    set_script_prefix(prefix)
    try:
        yield
    finally:
        set_script_prefix(old)


# ---------------------------------------------------------------------------
# Registry: builtins + merge-over-builtins
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_builtin_defaults(self):
        mounts = get_mounts()
        assert mounts["admin"] == Mount(
            key="admin", prefix="admin/", namespace="admin", name="Admin"
        )
        # historical microservices default: dedicated auth service at auth/
        assert mounts["auth"].external is True
        assert mounts["auth"].prefix == "auth/"

    def test_empty_auth_prefix_removes_auth_mount(self, settings):
        settings.STAPEL_AUTH_SERVICE_PREFIX = ""
        assert get_mount("auth") is None

    def test_custom_auth_prefix(self, settings):
        settings.STAPEL_AUTH_SERVICE_PREFIX = "sso"
        assert get_mount("auth").prefix == "sso/"

    def test_overlay_merges_over_builtins(self, settings):
        settings.STAPEL_MOUNTS = {
            "auth": {"prefix": "sso/", "external": True},
            "billing": {"prefix": "billing/", "external": True, "name": "Billing"},
            "studio": "studio",  # string shorthand = local prefix
        }
        mounts = get_mounts()
        assert mounts["auth"].prefix == "sso/"
        assert mounts["billing"].external is True
        assert mounts["studio"] == Mount(key="studio", prefix="studio/")
        assert "admin" in mounts  # builtins survive

    def test_overlay_none_removes_builtin(self, settings):
        settings.STAPEL_MOUNTS = {"auth": None}
        assert get_mount("auth") is None
        assert get_mount("admin") is not None

    def test_prefix_normalization(self, settings):
        settings.STAPEL_MOUNTS = {"x": {"prefix": "/a/b/"}}
        assert get_mount("x").prefix == "a/b/"

    def test_bad_entry_type_raises(self, settings):
        settings.STAPEL_MOUNTS = {"x": 42}
        with pytest.raises(MountConfigError):
            get_mounts()

    def test_unknown_keys_raise(self, settings):
        settings.STAPEL_MOUNTS = {"x": {"prefix": "x/", "url": "/x/"}}
        with pytest.raises(MountConfigError):
            get_mounts()

    def test_non_dict_overlay_raises(self, settings):
        settings.STAPEL_MOUNTS = ["auth"]
        with pytest.raises(MountConfigError):
            get_mounts()


# ---------------------------------------------------------------------------
# Path building
# ---------------------------------------------------------------------------


class TestMountPath:
    def test_external_mount_path(self):
        assert mount_path("auth", "admin/login/") == "/auth/admin/login/"

    def test_missing_mount_is_none(self, settings):
        settings.STAPEL_MOUNTS = {"auth": None}
        assert mount_path("auth", "admin/login/") is None

    def test_script_prefix_prepended(self):
        with script_prefix("/studio/"):
            assert mount_path("auth", "admin/login/") == "/studio/auth/admin/login/"

    def test_mount_reverse_local(self, settings):
        settings.ROOT_URLCONF = URLS
        assert mount_reverse("admin", "login") == "/admin/login/"

    def test_mount_reverse_external_is_none(self):
        assert mount_reverse("auth", "login") is None

    def test_mount_reverse_no_urlconf_is_none(self):
        assert mount_reverse("admin", "login") is None


# ---------------------------------------------------------------------------
# Derived URLs — the LOGIN_URL mechanism
# ---------------------------------------------------------------------------


class TestAdminLoginUrl:
    def test_default_preserves_historical_value(self):
        # microservices layout (degenerate case): external auth service
        assert admin_login_url() == "/auth/admin/login/"

    def test_monolith_reverses_local_admin(self, settings):
        settings.STAPEL_AUTH_SERVICE_PREFIX = ""
        settings.ROOT_URLCONF = URLS
        assert admin_login_url() == "/admin/login/"

    def test_monolith_under_include_prefix(self, settings):
        # the stapel-studio shape: the whole project mounted under myproj/
        settings.STAPEL_AUTH_SERVICE_PREFIX = ""
        settings.ROOT_URLCONF = URLS_PREFIXED
        assert admin_login_url() == "/myproj/admin/login/"

    def test_external_auth_under_script_prefix(self):
        with script_prefix("/studio/"):
            assert admin_login_url() == "/studio/auth/admin/login/"

    def test_fallback_without_urlconf(self, settings):
        settings.STAPEL_AUTH_SERVICE_PREFIX = ""
        assert admin_login_url() == "/admin/login/"

    def test_fallback_respects_moved_admin_mount(self, settings):
        settings.STAPEL_AUTH_SERVICE_PREFIX = ""
        settings.STAPEL_MOUNTS = {"admin": {"prefix": "backoffice/admin/"}}
        assert admin_login_url() == "/backoffice/admin/login/"

    def test_lazy_variant_tracks_settings(self, settings):
        lazy_url = lazy_admin_login_url()
        assert str(lazy_url) == "/auth/admin/login/"
        settings.STAPEL_AUTH_SERVICE_PREFIX = "sso"
        assert str(lazy_url) == "/sso/admin/login/"


class TestAdminIndexUrl:
    def test_prefers_local_admin(self, settings):
        settings.ROOT_URLCONF = URLS_PREFIXED
        assert admin_index_url() == "/myproj/admin/"

    def test_external_auth_fallback(self):
        assert admin_index_url() == "/auth/admin/"

    def test_root_fallback(self, settings):
        settings.STAPEL_AUTH_SERVICE_PREFIX = ""
        assert admin_index_url() == "/admin/"


# ---------------------------------------------------------------------------
# Middleware integration: the anonymous→login chain under a prefix
# ---------------------------------------------------------------------------


class TestAdminLoginRedirectUnderPrefix:
    def _mw(self):
        from stapel_core.django.admin.redirect import AdminLoginRedirectMiddleware

        return AdminLoginRedirectMiddleware(lambda request: None)

    def test_redirect_stays_inside_mount_prefix(self, settings):
        settings.STAPEL_AUTH_SERVICE_PREFIX = ""
        settings.ROOT_URLCONF = URLS_PREFIXED
        request = rf.get("/myproj/admin/")
        request.user = SimpleNamespace(is_authenticated=False)
        resp = self._mw().process_request(request)
        assert resp.status_code == 302
        assert resp.url == "/myproj/admin/login/?next=%2Fmyproj%2Fadmin%2F"

    def test_login_page_under_prefix_not_redirected(self, settings):
        settings.STAPEL_AUTH_SERVICE_PREFIX = ""
        settings.ROOT_URLCONF = URLS_PREFIXED
        request = rf.get("/myproj/admin/login/")
        request.user = SimpleNamespace(is_authenticated=False)
        assert self._mw().process_request(request) is None


# ---------------------------------------------------------------------------
# System checks
# ---------------------------------------------------------------------------


def _ids(findings):
    return [f.id for f in findings]


class TestChecks:
    def test_no_urlconf_skips(self):
        assert check_auth_redirect_settings() == []

    def test_derived_defaults_pass_for_external_auth(self, settings):
        settings.ROOT_URLCONF = URLS
        settings.LOGIN_URL = lazy_admin_login_url()   # → /auth/admin/login/
        settings.LOGOUT_REDIRECT_URL = lazy_admin_login_url()
        settings.LOGIN_REDIRECT_URL = "admin:index"
        assert check_auth_redirect_settings() == []

    def test_derived_defaults_pass_for_monolith(self, settings):
        settings.STAPEL_AUTH_SERVICE_PREFIX = ""
        settings.ROOT_URLCONF = URLS_PREFIXED
        settings.LOGIN_URL = lazy_admin_login_url()   # → /myproj/admin/login/
        settings.LOGOUT_REDIRECT_URL = lazy_admin_login_url()
        settings.LOGIN_REDIRECT_URL = "admin:index"
        assert check_auth_redirect_settings() == []

    def test_unresolvable_login_url_is_error(self, settings):
        settings.ROOT_URLCONF = URLS
        settings.STAPEL_AUTH_SERVICE_PREFIX = ""
        settings.LOGIN_URL = "/admin/login/"          # fine — resolves
        settings.LOGOUT_REDIRECT_URL = "/admin/login/"
        settings.LOGIN_REDIRECT_URL = "admin:index"
        assert check_auth_redirect_settings() == []

        # the live-stack bug: whole project mounted under a prefix, LOGIN_URL
        # still points at the root — silently 404s for every user
        settings.ROOT_URLCONF = URLS_PREFIXED
        ids = _ids(check_auth_redirect_settings())
        assert E001_LOGIN_URL_UNRESOLVABLE in ids
        assert E002_REDIRECT_URL_UNRESOLVABLE in ids

    def test_external_mount_paths_are_skipped(self, settings):
        settings.ROOT_URLCONF = URLS
        settings.LOGIN_URL = "/auth/admin/login/"     # external auth service
        settings.LOGOUT_REDIRECT_URL = "/auth/admin/login/"
        settings.LOGIN_REDIRECT_URL = "admin:index"
        assert check_auth_redirect_settings() == []

    def test_bad_view_name_is_error(self, settings):
        settings.ROOT_URLCONF = URLS
        settings.LOGIN_URL = "nosuch:login"
        settings.LOGOUT_REDIRECT_URL = "admin:login"
        settings.LOGIN_REDIRECT_URL = "admin:index"
        assert _ids(check_auth_redirect_settings()) == [E001_LOGIN_URL_UNRESOLVABLE]

    def test_stock_django_defaults_warn_not_block(self, settings):
        settings.ROOT_URLCONF = URLS
        settings.STAPEL_AUTH_SERVICE_PREFIX = ""
        settings.LOGIN_URL = "/accounts/login/"       # Django's untouched default
        settings.LOGOUT_REDIRECT_URL = "admin:login"
        settings.LOGIN_REDIRECT_URL = "/accounts/profile/"
        findings = check_auth_redirect_settings()
        assert _ids(findings) == [W001_STOCK_LOGIN_REDIRECT, W001_STOCK_LOGIN_REDIRECT]

    def test_absolute_urls_are_skipped(self, settings):
        settings.ROOT_URLCONF = URLS
        settings.LOGIN_URL = "https://sso.example.com/login/"
        settings.LOGOUT_REDIRECT_URL = "admin:login"
        settings.LOGIN_REDIRECT_URL = "admin:index"
        assert check_auth_redirect_settings() == []

    def test_script_prefix_is_stripped_before_resolve(self, settings):
        settings.STAPEL_AUTH_SERVICE_PREFIX = ""
        settings.ROOT_URLCONF = URLS
        settings.LOGIN_URL = "/studio/admin/login/"
        settings.LOGOUT_REDIRECT_URL = "admin:login"
        settings.LOGIN_REDIRECT_URL = "admin:index"
        with script_prefix("/studio/"):
            assert check_auth_redirect_settings() == []

    def test_malformed_mounts_is_error(self, settings):
        settings.STAPEL_MOUNTS = {"x": 42}
        assert _ids(check_mounts_config()) == [E003_BAD_MOUNTS]
        settings.ROOT_URLCONF = URLS
        # the redirect check must not explode on the same misconfig
        settings.LOGIN_URL = "/whatever/"
        settings.LOGOUT_REDIRECT_URL = "/whatever/"
        settings.LOGIN_REDIRECT_URL = "/whatever/"
        assert check_auth_redirect_settings() == []

    def test_mounts_config_ok(self, settings):
        settings.STAPEL_MOUNTS = {"auth": {"prefix": "sso/", "external": True}}
        assert check_mounts_config() == []


# ---------------------------------------------------------------------------
# reserved_paths() — §37 machine-readable reservation
# ---------------------------------------------------------------------------


class TestReservedPaths:
    def test_empty_when_no_stapel_modules(self, mock_apps):
        mock_apps(_app("myproject.blog", label="blog"))
        assert reserved_paths() == {}

    def test_lists_installed_modules_only(self, mock_apps):
        mock_apps(
            _app("stapel_billing", label="billing"),
            _app("stapel_core", label="stapel_core"),  # framework — excluded
            _app("myproject.blog", label="blog"),  # unmarked — excluded
        )
        assert reserved_paths() == {"billing": list(MODULE_RESERVED_SUFFIXES)}

    def test_marked_local_app_included(self, mock_apps):
        mock_apps(_app("myproject.apps.tools", label="tools", stapel_module=True))
        assert reserved_paths() == {"tools": list(MODULE_RESERVED_SUFFIXES)}

    def test_reservation_shape_matches_canon(self, mock_apps):
        mock_apps(_app("stapel_calendar", label="calendar"))
        assert reserved_paths()["calendar"] == ["api/", "swagger/", "schema.json", "admin/"]


# ---------------------------------------------------------------------------
# check_module_surface_containment — E004, BACKLOG §37 surface topology
# ---------------------------------------------------------------------------


class TestModuleSurfaceContainment:
    def test_no_urlconf_skips(self):
        assert check_module_surface_containment() == []

    def test_compliant_module_passes(self, settings, mock_apps):
        settings.ROOT_URLCONF = URLS_SURFACE
        mock_apps(_app("stapel_billing", label="billing"))
        assert check_module_surface_containment() == []

    def test_regex_anchored_router_is_not_a_violation(self, settings, mock_apps):
        """`currencies/^api/v1/rates` is canonical — the `^` is a regex
        anchor from the module's own re_path router, not a path segment.

        Stripping anchors once across the whole path (rather than per
        segment) left this reading as "^api" and reported E004 against a
        perfectly correct mount, so every generated project that included
        currencies failed its own `manage.py check`."""
        settings.ROOT_URLCONF = URLS_SURFACE
        mock_apps(_app("stapel_currencies", label="currencies"))
        assert check_module_surface_containment() == []

    def test_admin_segment_nested_inside_api_is_fine(self, settings, mock_apps):
        # auth's admin_api gate lives at auth/api/v1/admin/audit/ — "api" is
        # present in the path, so this is compliant even though "admin" also
        # appears deeper in.
        settings.ROOT_URLCONF = URLS_SURFACE
        mock_apps(_app("stapel_auth", label="auth"))
        assert check_module_surface_containment() == []

    def test_dashboard_route_outside_canon_is_error(self, settings, mock_apps):
        # the real fleet finding: stapel-translate's translate/dashboard/
        # carries no api/swagger/schema/admin segment anywhere.
        settings.ROOT_URLCONF = URLS_SURFACE
        mock_apps(_app("stapel_translate", label="translate"))
        findings = check_module_surface_containment()
        assert len(findings) == 1
        assert findings[0].id == E004_MODULE_OUTSIDE_CANON
        assert "translate/dashboard/" in findings[0].msg

    def test_bare_module_root_is_error(self, settings, mock_apps):
        # the /calendar incident: a bare module-root pattern, no canonical
        # segment at all.
        settings.ROOT_URLCONF = URLS_SURFACE
        mock_apps(_app("stapel_calendar", label="calendar"))
        findings = check_module_surface_containment()
        assert [f.id for f in findings] == [E004_MODULE_OUTSIDE_CANON]

    def test_host_urls_are_never_flagged(self, settings, mock_apps):
        # "whatever/" belongs to no installed Stapel app — a project is free
        # in its own paths, this check only polices installed modules.
        settings.ROOT_URLCONF = URLS_SURFACE
        mock_apps()  # no Stapel modules installed at all
        assert check_module_surface_containment() == []

    def test_nested_include_is_walked(self, settings, mock_apps):
        # billing/api/v1/extra sits behind a nested include() — the DFS walk
        # must recurse into resolvers, not just scan top-level patterns.
        settings.ROOT_URLCONF = URLS_SURFACE
        mock_apps(_app("stapel_billing", label="billing"))
        assert check_module_surface_containment() == []

    def test_multiple_installed_modules_report_only_violators(self, settings, mock_apps):
        settings.ROOT_URLCONF = URLS_SURFACE
        mock_apps(
            _app("stapel_billing", label="billing"),
            _app("stapel_auth", label="auth"),
            _app("stapel_translate", label="translate"),
            _app("stapel_calendar", label="calendar"),
        )
        findings = check_module_surface_containment()
        assert {f.id for f in findings} == {E004_MODULE_OUTSIDE_CANON}
        assert len(findings) == 2  # translate/dashboard + calendar bare root


# ---------------------------------------------------------------------------
# check_module_api_address — E005/E006, canonical /<mod>/api/v<N>/ address
# ---------------------------------------------------------------------------


class TestModuleApiAddress:
    """The address half of §37, written against the live app.ironmemo.com
    mounts (see ``tests/mounts_address_urls.py`` for the transcription).

    E004 was green through the whole life of that deployment because it
    tests for the *presence* of an ``api`` segment and the segment was
    present — one position too early.
    """

    @pytest.fixture
    def surface(self, settings, monkeypatch, mock_apps):
        """Install *patterns* as the deployment's URLconf and *apps* as its
        installed Stapel modules, then run the check."""
        from django.urls import clear_url_caches

        import tests.mounts_address_urls as fixture_urls
        from stapel_core.django import checks as checks_module

        # Every module in these fixtures is treated as shipping a urls
        # module: the real signal is importlib.find_spec("<pkg>.urls"), and
        # stapel_agent/stapel_workspaces/... are not installed in this
        # harness. E006's own tests override this back to False.
        monkeypatch.setattr(checks_module, "_declares_http_surface", lambda app: True)

        def _run(patterns, *apps):
            monkeypatch.setattr(fixture_urls, "urlpatterns", patterns)
            settings.ROOT_URLCONF = URLS_ADDRESS
            clear_url_caches()
            mock_apps(*apps)
            return check_module_api_address()

        yield _run
        clear_url_caches()

    # --- the two shapes that are correct, and differ from each other -------

    def test_library_contributing_api_v1_mounted_at_bare_prefix(self, surface):
        """stapel_agent adds ``api/v1/`` itself, so ``path('agent/', ...)``
        is right — the literal is shorter than the canonical prefix."""
        assert surface(fixture.CORRECT_AGENT, _app("stapel_agent", label="agent")) == []

    def test_library_contributing_v1_mounted_through_api(self, surface):
        """stapel_workspaces adds only ``v1/``, so the host mount must carry
        ``api/`` — a different literal reaching the same canonical address."""
        assert surface(
            fixture.CORRECT_WORKSPACES, _app("stapel_workspaces", label="workspaces")
        ) == []

    # --- the live incident --------------------------------------------------

    def test_extra_segment_before_version_is_error(self, surface):
        """The defect: /workspaces/api/workspaces/v1/ instead of
        /workspaces/api/v1/. Every caller got a 404 from a healthy service."""
        findings = surface(
            fixture.BROKEN_WORKSPACES, _app("stapel_workspaces", label="workspaces")
        )
        assert [f.id for f in findings] == [E005_MODULE_API_OFF_CANON]

    def test_finding_reports_found_and_expected_address(self, surface):
        """A check that says only "wrong" repeats the failure it closes."""
        finding = surface(
            fixture.BROKEN_WORKSPACES, _app("stapel_workspaces", label="workspaces")
        )[0]
        assert "/workspaces/api/workspaces/v1" in finding.msg
        assert "/workspaces/api/v1" in finding.msg
        assert "Found:    /workspaces/api/workspaces/v1" in finding.hint
        assert "Expected: /workspaces/api/v1" in finding.hint

    def test_canon_plus_legacy_shim_is_green(self, surface):
        """The obligation is that the canon ANSWERS, not that nothing else
        does. ironmemo had to serve the legacy doubled-segment path as well,
        because stapel-core's own workspaces client probes only that one and
        sibling services call the pod directly (nginx cannot rewrite it for
        them). Every caller built against the canon is served, so this is not
        the defect — reporting it would push people to delete the shim and
        re-break their peers."""
        assert surface(
            fixture.CANON_PLUS_LEGACY_SHIM, _app("stapel_workspaces", label="workspaces")
        ) == []

    def test_two_wrong_addresses_still_error_and_list_both(self, surface):
        """...but two non-canonical mounts and no canonical one is still the
        incident, and the finding names every address actually served."""
        findings = surface(
            fixture.BROKEN_WORKSPACES + fixture.BROKEN_MISSING_API_WORKSPACES,
            _app("stapel_workspaces", label="workspaces"),
        )
        assert [f.id for f in findings] == [E005_MODULE_API_OFF_CANON]
        assert "/workspaces/api/workspaces/v1/" in findings[0].msg
        assert "/workspaces/v1/" in findings[0].msg

    def test_module_mounted_at_site_root_is_error(self, surface):
        """iron-translate's ``path('', include(...))`` — the module prefix is
        missing entirely, so the surface answers at /api/v1/."""
        findings = surface(
            fixture.BROKEN_TRANSLATE_AT_ROOT, _app("stapel_translate", label="translate")
        )
        assert [f.id for f in findings] == [E005_MODULE_API_OFF_CANON]
        assert "/translate/api/v1" in findings[0].msg

    def test_missing_api_segment_is_error(self, surface):
        """The symmetric mistake to the live one: a segment too few."""
        findings = surface(fixture.BROKEN_MISSING_API, _app("stapel_agent", label="agent"))
        assert [f.id for f in findings] == [E005_MODULE_API_OFF_CANON]
        assert "/agent/v1" in findings[0].msg

    # --- what must NOT be flagged ------------------------------------------

    def test_unversioned_routes_are_not_addressing_subjects(self, surface):
        """``/<mod>/api/error-keys/`` and ``/<mod>/admin/`` are owned by the
        module but carry no version segment — E004's business, not this
        check's. Flagging them would make the check unusable."""
        assert surface(
            fixture.UNVERSIONED_ONLY, _app("stapel_billing", label="billing")
        ) == []

    def test_host_own_routes_are_never_flagged(self, surface):
        """A project is free in its own URL space: ``/whatever/api/v1/thing``
        is shaped exactly like a mis-mounted module API, and is not one —
        ownership follows the view's package, never the path shape."""
        assert surface(
            fixture.HOST_ALONGSIDE_CORRECT_MODULE,
            _app("stapel_billing", label="billing"),
        ) == []

    def test_uninstalled_module_has_no_surface_to_be_unreachable(self, surface):
        """A service that drops a module reports nothing — it does not also
        have to remember to drop an assertion about it. (iron-translate is
        being removed from ironmemo as this lands.)"""
        assert surface(fixture.BROKEN_TRANSLATE_AT_ROOT) == []

    def test_only_the_violator_is_reported(self, surface):
        findings = surface(
            fixture.MIXED,
            _app("stapel_agent", label="agent"),
            _app("stapel_workspaces", label="workspaces"),
            _app("stapel_billing", label="billing"),
        )
        assert [f.id for f in findings] == [E005_MODULE_API_OFF_CANON]
        assert "'workspaces'" in findings[0].msg

    # --- deliberate placement is declared, not silently forgiven -----------

    def test_co_mounted_module_is_flagged_when_undeclared(self, surface):
        """stapel_gdpr served under the auth service's prefix. Real, and the
        red state is silence: say where it lives."""
        findings = surface(fixture.CO_MOUNTED_GDPR, _app("stapel_gdpr", label="gdpr"))
        assert [f.id for f in findings] == [E005_MODULE_API_OFF_CANON]

    def test_co_mounted_module_passes_once_declared(self, settings, surface):
        """...and green as soon as STAPEL_MOUNTS records the decision. The
        declaration is the deliverable: "this module is not at its own name"
        is exactly what a reader of the URLconf cannot otherwise recover."""
        settings.STAPEL_MOUNTS = {"gdpr": {"prefix": "auth/"}}
        assert surface(fixture.CO_MOUNTED_GDPR, _app("stapel_gdpr", label="gdpr")) == []

    # --- E006: the same defect with the segment count zero -----------------

    def test_installed_and_shipping_urls_but_never_mounted(self, settings, monkeypatch,
                                                           mock_apps):
        """ADO001's question, asked in-process — so it also covers the mounts
        ADO001 documents as opaque to its AST (variable/computed includes)."""
        from django.urls import clear_url_caches

        import tests.mounts_address_urls as fixture_urls
        from stapel_core.django import checks as checks_module

        monkeypatch.setattr(checks_module, "_declares_http_surface", lambda app: True)
        monkeypatch.setattr(fixture_urls, "urlpatterns", fixture.CORRECT_AGENT)
        settings.ROOT_URLCONF = URLS_ADDRESS
        clear_url_caches()
        mock_apps(_app("stapel_workspaces", label="workspaces"))

        findings = check_module_api_address()
        clear_url_caches()
        assert [f.id for f in findings] == [E006_MODULE_SURFACE_UNMOUNTED]
        assert "/workspaces/api/v1/" in findings[0].msg
        assert "found nothing" in findings[0].msg

    def test_headless_declaration_silences_e006(self, settings, monkeypatch, mock_apps):
        """The in-process twin of ADO001's ``# stapel: headless`` marker: a
        module wanted for its models/tasks only."""
        from django.urls import clear_url_caches

        import tests.mounts_address_urls as fixture_urls
        from stapel_core.django import checks as checks_module

        monkeypatch.setattr(checks_module, "_declares_http_surface", lambda app: True)
        monkeypatch.setattr(fixture_urls, "urlpatterns", fixture.CORRECT_AGENT)
        settings.ROOT_URLCONF = URLS_ADDRESS
        settings.STAPEL_HEADLESS_MODULES = ["workspaces"]
        clear_url_caches()
        mock_apps(_app("stapel_workspaces", label="workspaces"))

        findings = check_module_api_address()
        clear_url_caches()
        assert findings == []

    def test_module_shipping_no_urlconf_is_not_a_subject(self, settings, monkeypatch,
                                                         mock_apps):
        """No ``urls`` module — nothing was declared, so nothing is missing."""
        from django.urls import clear_url_caches

        import tests.mounts_address_urls as fixture_urls
        from stapel_core.django import checks as checks_module

        monkeypatch.setattr(checks_module, "_declares_http_surface", lambda app: False)
        monkeypatch.setattr(fixture_urls, "urlpatterns", fixture.CORRECT_AGENT)
        settings.ROOT_URLCONF = URLS_ADDRESS
        clear_url_caches()
        mock_apps(_app("stapel_workspaces", label="workspaces"))

        findings = check_module_api_address()
        clear_url_caches()
        assert findings == []

    def test_no_urlconf_skips(self):
        assert check_module_api_address() == []
