"""Module discovery for the admin/API navigation (BACKLOG §37).

A monolith is not seeded with STAPEL_SERVICES to see its own apps — it
introspects INSTALLED_APPS directly (§37-уточнение, 2026-07-10). Covers:
``_is_stapel_app`` filtering (marker + the ``stapel_*`` pip-package
convention + core exclusion), ``discover_modules``/``build_modules`` with
2-3 mocked modules (admin/Swagger/schema links, per-module mount preferred
over the deployment-wide fallback), the single-module case, the admin
context processor, the ``/nav`` JSON endpoint, and a template-level render
of the admin-index "Apps" block.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.template.loader import render_to_string
from django.test import RequestFactory, override_settings

from stapel_core.django import nav
from stapel_core.django.nav import (
    ModuleNav,
    _is_stapel_app,
    build_modules,
    discover_modules,
)
from stapel_core.django.nav_views import nav_view

CORE_TEMPLATES_DIR = str(Path(nav.__file__).resolve().parent / "templates")
STUB_TEMPLATES_DIR = str(Path(__file__).resolve().parent / "template_stubs")

rf = RequestFactory()


def _app(name, label=None, verbose_name=None, stapel_module=None):
    """A minimal stand-in for a Django AppConfig — just the attributes
    discover_modules() reads, so tests never need a real installed app."""
    kwargs = dict(name=name, label=label or name.rsplit(".", 1)[-1], verbose_name=verbose_name)
    if stapel_module is not None:
        kwargs["stapel_module"] = stapel_module
    return SimpleNamespace(**kwargs)


@pytest.fixture(autouse=True)
def _clean_nav_settings(settings):
    settings.STAPEL_SERVICES = None
    settings.URL_PREFIX = ""
    # Monolith scenario throughout this file — no dedicated auth service, so
    # the admin (and per-module admin index) is local.
    settings.STAPEL_AUTH_SERVICE_PREFIX = ""
    nav.clear_nav_links()
    yield
    nav.clear_nav_links()


# ---------------------------------------------------------------------------
# _is_stapel_app — marker + pip-package convention + core exclusion
# ---------------------------------------------------------------------------


class TestIsStapelApp:
    def test_pip_module_auto_detected(self):
        assert _is_stapel_app(_app("stapel_billing", label="billing")) is True

    def test_core_itself_excluded(self):
        assert _is_stapel_app(_app("stapel_core", label="stapel_core")) is False

    def test_core_internal_subapp_excluded(self):
        # e.g. stapel_core.django.users, .outbox, .taskstore, … — framework
        # plumbing, not a content module.
        assert _is_stapel_app(_app("stapel_core.django.users", label="users")) is False

    def test_unrelated_app_excluded_by_default(self):
        assert _is_stapel_app(_app("myproject.blog", label="blog")) is False

    def test_local_app_opts_in_via_marker(self):
        # A project's own apps/* — no "stapel_" name, but the tools skeleton
        # stamps the marker so it still shows up in the nav.
        assert _is_stapel_app(
            _app("myproject.apps.tools", label="tools", stapel_module=True)
        ) is True

    def test_pip_module_can_opt_out_explicitly(self):
        assert _is_stapel_app(
            _app("stapel_weird", label="weird", stapel_module=False)
        ) is False


# ---------------------------------------------------------------------------
# discover_modules / build_modules — mocked INSTALLED_APPS
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_apps(monkeypatch):
    """Patch django.apps.apps.get_app_configs() to return *configs — the
    same registry object discover_modules() reads, wherever it imports it
    from."""

    def _set(*configs):
        import django.apps

        monkeypatch.setattr(django.apps.apps, "get_app_configs", lambda: list(configs))

    return _set


def test_discover_modules_filters_and_sorts(mock_apps):
    mock_apps(
        _app("stapel_core", label="stapel_core"),
        _app("stapel_core.django.users", label="users"),
        _app("stapel_listings", label="listings", verbose_name="Listings and catalog"),
        _app("stapel_billing", label="billing", verbose_name="Stapel Billing"),
        _app("myproject.blog", label="blog"),  # no marker — excluded
    )
    modules = discover_modules()
    # Sorted by display name ("Listings and catalog" < "Stapel Billing").
    assert [m.key for m in modules] == ["listings", "billing"]
    assert all(isinstance(m, ModuleNav) for m in modules)


def test_discover_modules_admin_url_falls_back_without_urlconf(mock_apps, settings):
    """No ROOT_URLCONF in the package harness → admin:app_list can't reverse;
    falls back to the mounts-registry prefix, still script-prefix aware."""
    settings.URL_PREFIX = ""
    mock_apps(_app("stapel_billing", label="billing", verbose_name="Billing"))
    modules = discover_modules()
    assert len(modules) == 1
    assert modules[0].admin_url == "/admin/billing/"
    assert modules[0].swagger_url is None
    assert modules[0].schema_url is None


def test_single_module_service(mock_apps):
    """A single-module deployment still gets a working nav entry — no
    STAPEL_SERVICES seeding required (that registry stays for siblings)."""
    mock_apps(_app("stapel_translate", label="translate", verbose_name="Stapel Translate"))
    modules = build_modules()
    assert len(modules) == 1
    assert modules[0]["key"] == "translate"
    assert modules[0]["admin_url"].endswith("/translate/")


@override_settings(ROOT_URLCONF="tests.nav_modules_urls")
def test_discover_modules_prefers_own_swagger_mount(mock_apps):
    mock_apps(_app("stapel_billing", label="billing", verbose_name="Billing"))
    modules = discover_modules()
    assert modules[0].admin_url == "/admin/billing/"
    assert modules[0].swagger_url == "/billing/swagger/"
    assert modules[0].schema_url == "/billing/schema/"


@override_settings(ROOT_URLCONF="tests.nav_modules_urls")
def test_discover_modules_falls_back_to_deployment_wide_swagger(mock_apps):
    """listings mounts no swagger of its own → the shared deployment Swagger
    is a real, working link (never a guessed path that 404s)."""
    mock_apps(_app("stapel_listings", label="listings", verbose_name="Listings"))
    modules = discover_modules()
    assert modules[0].swagger_url == "/swagger/"
    assert modules[0].schema_url == "/schema/"


@override_settings(ROOT_URLCONF="tests.nav_modules_urls")
def test_discover_modules_two_or_three_mocked(mock_apps):
    """The exact scenario from the task: 2-3 mocked modules, links correct."""
    mock_apps(
        _app("stapel_billing", label="billing", verbose_name="Billing"),
        _app("stapel_listings", label="listings", verbose_name="Listings"),
        _app("stapel_notifications", label="notifications", verbose_name="Notifications"),
    )
    modules = build_modules()
    by_key = {m["key"]: m for m in modules}
    assert set(by_key) == {"billing", "listings", "notifications"}
    assert by_key["billing"]["swagger_url"] == "/billing/swagger/"
    assert by_key["listings"]["swagger_url"] == "/swagger/"  # fallback
    assert by_key["notifications"]["swagger_url"] == "/swagger/"  # fallback
    for entry in modules:
        assert entry["admin_url"] == f"/admin/{entry['key']}/"


# ---------------------------------------------------------------------------
# Admin context processor
# ---------------------------------------------------------------------------


def test_context_processor_exposes_stapel_modules(mock_apps):
    from stapel_core.django.admin.context import stapel_services

    mock_apps(_app("stapel_billing", label="billing", verbose_name="Billing"))
    request = rf.get("/admin/")
    request.user = SimpleNamespace(is_authenticated=True, is_staff=True, is_superuser=False)
    ctx = stapel_services(request)
    assert ctx["stapel_modules"] == build_modules()
    assert ctx["stapel_modules"][0]["key"] == "billing"


# ---------------------------------------------------------------------------
# /nav JSON endpoint
# ---------------------------------------------------------------------------


class TestNavView:
    def test_requires_staff(self):
        request = rf.get("/nav/")
        request.user = SimpleNamespace(is_authenticated=True, is_staff=False)
        response = nav_view(request)
        assert response.status_code == 403

    def test_requires_authenticated(self):
        request = rf.get("/nav/")
        request.user = SimpleNamespace(is_authenticated=False, is_staff=False)
        response = nav_view(request)
        assert response.status_code == 403

    def test_staff_gets_modules_and_services(self, mock_apps, settings):
        settings.URL_PREFIX = "shop/"
        mock_apps(_app("stapel_billing", label="billing", verbose_name="Billing"))
        request = rf.get("/nav/")
        request.user = SimpleNamespace(is_authenticated=True, is_staff=True, is_superuser=False)
        response = nav_view(request)
        assert response.status_code == 200
        import json

        payload = json.loads(response.content)
        assert payload["modules"][0]["key"] == "billing"
        assert len(payload["services"]) == 1
        assert payload["services"][0]["prefix"] == "shop"


# ---------------------------------------------------------------------------
# Template: admin/base_site.html "Apps" block
# ---------------------------------------------------------------------------


TEMPLATE_ENV = dict(
    TEMPLATES=[
        {
            "BACKEND": "django.template.backends.django.DjangoTemplates",
            "DIRS": [STUB_TEMPLATES_DIR, CORE_TEMPLATES_DIR],
            "APP_DIRS": False,
            "OPTIONS": {"context_processors": []},
        }
    ],
    STATIC_URL="/static/",
    # base_site.html's branding block does {% url 'admin:index' %} — give it
    # a real "admin" namespace to resolve against.
    ROOT_URLCONF="tests.nav_modules_urls",
)


def _render_base_site(**extra_context):
    context = {
        "title": "Site administration",
        "user": SimpleNamespace(is_authenticated=True),
        "stapel_services": [{"name": "This service", "prefix": "", "admin_url": "/admin/", "swagger_url": None, "is_active": True}],
        "stapel_services_multi": False,
        "stapel_nav_sections": {},
        "current_swagger_url": None,
        "current_dashboard_url": None,
        "stapel_modules": [],
        **extra_context,
    }
    with override_settings(**TEMPLATE_ENV):
        return render_to_string("admin/base_site.html", context)


def test_template_renders_apps_dropdown_with_modules():
    html = _render_base_site(
        stapel_modules=[
            {"key": "billing", "name": "Billing", "admin_url": "/admin/billing/",
             "swagger_url": "/billing/swagger/", "schema_url": "/billing/schema/"},
            {"key": "listings", "name": "Listings", "admin_url": "/admin/listings/",
             "swagger_url": "/swagger/", "schema_url": "/schema/"},
        ]
    )
    assert "Apps" in html
    assert '<a href="/admin/billing/">Admin</a>' in html
    assert '<a href="/billing/swagger/">Swagger</a>' in html
    assert '<a href="/billing/schema/">Schema</a>' in html
    assert '<a href="/admin/listings/">Admin</a>' in html
    assert "Billing" in html and "Listings" in html


def test_template_omits_apps_dropdown_when_no_modules():
    html = _render_base_site(stapel_modules=[])
    assert "Apps ▾" not in html


def test_template_hides_whole_nav_when_no_services():
    """Unauthenticated / no services at all → the whole nav bar (Apps
    included) stays hidden, matching the pre-existing Services behavior."""
    html = _render_base_site(stapel_services=[], stapel_modules=[
        {"key": "billing", "name": "Billing", "admin_url": "/admin/billing/",
         "swagger_url": None, "schema_url": None},
    ])
    assert "Apps ▾" not in html
    assert "Billing" not in html
