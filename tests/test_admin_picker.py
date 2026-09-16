"""The admin's cross-service picker renders. In both topologies. Unasked.

This file exists because nothing like it existed, and that is the whole
reason the feature kept disappearing.

Everything *around* the picker was tested: ``test_nav.py`` asserts the
registries parse, ``test_nav_modules.py`` renders ``base_site.html`` against
a hand-built context with core's template directory passed explicitly, and
``stapel_core.nav.E004`` asserts a split deployment declared a registry at
all. All of it stayed green through two separate multi-month outages,
because none of it asked the only question that matters: **when Django
renders the admin, does the switcher come out?**

It did not, twice, for different reasons:

* **2026-07-06 → 2026-09-02, every a client fleet service.** The service list moved
  from a library hardcode to the ``STAPEL_SERVICES`` deploy-config; the
  deployment predated the generators that seed it, fell into the monolith
  fallback, and every admin listed only itself.
* **2026-07-05 → 2026-09-17, a client fleet's ``iron-auth``.** It hand-wrote
  ``TEMPLATES`` with the library's bind-mount path,
  ``/app/stapel_core/django/templates``. The library became a pip wheel, the
  bind mount was deleted, the string stayed. Django does not object to a
  ``DIRS`` entry that does not exist, and ``APP_DIRS: True`` does not save
  it — ``django.contrib.admin`` ships its own ``admin/base_site.html`` and is
  listed first, so it wins the app-dirs search every time. The admin rendered
  Django's stock header for two and a half months in the one service the
  centralised admin login lands on.

So the tests below render the real admin through the real client and read the
real HTML, under a ``TEMPLATES`` block that deliberately does **not** mention
stapel-core — ``DIRS: []``, ``APP_DIRS: True``, no nav context processor.
That is the iron-auth shape. Every one of them fails with the installer disabled.
"""
import uuid

import pytest
from django.contrib import admin
from django.db import connection
from django.test import Client, override_settings

from stapel_core.django.users.models import User

# The iron-auth shape: an engine that names stapel-core nowhere at all.
# django.contrib.admin sits ahead of stapel_core.django, so under APP_DIRS
# alone its base_site.html wins and the picker is gone.
BARE_TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {
        "context_processors": [
            "django.template.context_processors.request",
            "django.contrib.auth.context_processors.auth",
            "django.contrib.messages.context_processors.messages",
        ],
    },
}]

PICKER_ENV = dict(
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
    SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies",
    MESSAGE_STORAGE="django.contrib.messages.storage.cookie.CookieStorage",
    STATIC_URL="/static/",
    TEMPLATES=BARE_TEMPLATES,
    AUTHENTICATION_BACKENDS=[
        "stapel_core.access.backend.MandateBackend",
        "stapel_core.access.backend.AuditedModelBackend",
    ],
)


def _ensure_tables(*models):
    existing = set(connection.introspection.table_names())
    for model in models:
        if model._meta.db_table not in existing:
            with connection.schema_editor() as editor:
                editor.create_model(model)


@pytest.fixture(scope="session")
def _picker_tables(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        with override_settings(**PICKER_ENV):
            from django.contrib.admin.models import LogEntry
            from django.contrib.sessions.models import Session

            _ensure_tables(LogEntry, Session)


@pytest.fixture
def picker_env(db, _picker_tables):
    with override_settings(**PICKER_ENV):
        snapshot = dict(admin.site._registry)
        try:
            yield
        finally:
            admin.site._registry.clear()
            admin.site._registry.update(snapshot)


def _superuser_client():
    """A superuser — the mandate is not what is under test here, the header
    is, and a superuser reaches the admin index in every access posture."""
    user = User.objects.create(
        username=f"picker_{uuid.uuid4().hex[:10]}",
        is_staff=True,
        is_superuser=True,
    )
    client = Client()
    client.force_login(
        user, backend="stapel_core.access.backend.AuditedModelBackend"
    )
    return client


def _admin_html(path="/admin/"):
    response = _superuser_client().get(path)
    assert response.status_code == 200, response.status_code
    return response.content.decode()


# ---------------------------------------------------------------------------
# The two topologies the owner asked for, rendered.
# ---------------------------------------------------------------------------


def test_monolith_admin_shows_the_picker_listing_itself(picker_env, settings):
    """A monolith declares no registry — and still shows a switcher.

    The "All Services" section used to collapse below two entries, which made
    "this deployment has one service" and "the service registry was lost
    again" look identical on screen. A monolith now lists itself.
    """
    settings.STAPEL_SERVICES = None
    settings.URL_PREFIX = ""
    settings.SERVICE_NAME = "Shop"

    html = _admin_html()

    assert "stapel-nav" in html, "the whole navigation block is missing"
    assert "All Services" in html
    assert "Shop" in html


def test_microservice_admin_links_every_sibling(picker_env, settings):
    """A split deployment lists its siblings, each linked to its own admin."""
    settings.STAPEL_SERVICES = (
        '[{"name": "Iron Auth", "prefix": "auth"}, '
        '{"name": "Iron Billing", "prefix": "billing"}, '
        '{"name": "Iron Recordings", "prefix": "recordings"}]'
    )
    settings.URL_PREFIX = "auth"

    html = _admin_html()

    assert "stapel-nav" in html
    for name, prefix in [
        ("Iron Auth", "auth"),
        ("Iron Billing", "billing"),
        ("Iron Recordings", "recordings"),
    ]:
        assert name in html, f"{name} is missing from the picker"
        assert f'href="/{prefix}/admin/"' in html, f"{name} has no working link"


def test_the_picker_survives_a_dead_hand_written_dirs_entry(
    picker_env, settings
):
    """iron-auth's exact regression, as a test.

    A service that hand-wrote its ``TEMPLATES`` and named the library's
    template directory by a container path that no longer exists. Before
    ``install_admin_nav`` this rendered Django's stock header and nothing
    said so.
    """
    settings.STAPEL_SERVICES = (
        '[{"name": "Iron Auth", "prefix": "auth"}, '
        '{"name": "Iron CDN", "prefix": "cdn"}]'
    )
    settings.URL_PREFIX = "auth"
    settings.TEMPLATES = [{
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": ["/app/stapel_core/django/templates"],  # gone since 2026-07-05
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": [
            "django.template.context_processors.request",
            "django.contrib.auth.context_processors.auth",
            "django.contrib.messages.context_processors.messages",
        ]},
    }]

    html = _admin_html()

    assert "stapel-nav" in html
    assert 'href="/cdn/admin/"' in html


def test_admin_base_site_resolves_to_core_not_django_stock(picker_env):
    """The mechanism, asserted directly: core's template wins the lookup."""
    from django.template.loader import get_template

    from stapel_core.django.admin.install import CORE_TEMPLATES_DIR

    origin = get_template("admin/base_site.html").origin.name
    assert origin.startswith(CORE_TEMPLATES_DIR), origin


def test_a_project_template_still_wins(picker_env, settings, tmp_path):
    """Core is appended, not prepended — a deliberate override still wins."""
    project = tmp_path / "templates" / "admin"
    project.mkdir(parents=True)
    (project / "base_site.html").write_text("{% block branding %}mine{% endblock %}")
    settings.TEMPLATES = [{
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [str(tmp_path / "templates")],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    }]

    from django.template.loader import get_template

    origin = get_template("admin/base_site.html").origin.name
    assert origin.startswith(str(tmp_path)), origin


# ---------------------------------------------------------------------------
# Present-but-unreachable beats silently missing.
# ---------------------------------------------------------------------------


def test_a_sibling_the_registry_forgot_is_shown_and_flagged(
    picker_env, settings
):
    """A mount-declared sibling missing from STAPEL_SERVICES still renders.

    Dropping it would be indistinguishable from this whole bug: the admin
    looks healthy and the sibling is simply unreachable from it.
    """
    settings.STAPEL_SERVICES = '[{"name": "Iron Auth", "prefix": "auth"}]'
    settings.URL_PREFIX = "auth"
    settings.STAPEL_MOUNTS = {
        "billing": {"prefix": "billing/", "external": True},
    }

    html = _admin_html()

    assert "Billing" in html, "a forgotten sibling vanished from the picker"
    assert "not in registry" in html
    assert 'href="/billing/admin/"' in html


# ---------------------------------------------------------------------------
# The check that shouts when someone defeats the installer anyway.
# ---------------------------------------------------------------------------


def test_check_flags_a_shadowed_picker_template(picker_env, settings):
    from stapel_core.django.nav_checks import (
        E005_PICKER_TEMPLATE_SHADOWED,
        check_picker_renders,
    )

    # A project that pins its own loaders opts out of DIRS entirely — the one
    # way left to lose the template, and the one the installer cannot repair.
    settings.TEMPLATES = [{
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": False,
        "OPTIONS": {
            "context_processors": [
                "stapel_core.django.admin.context.stapel_services",
            ],
            "loaders": ["django.template.loaders.app_directories.Loader"],
        },
    }]

    ids = [f.id for f in check_picker_renders()]
    assert E005_PICKER_TEMPLATE_SHADOWED in ids


def test_check_flags_a_missing_context_processor(picker_env, settings):
    from stapel_core.django.nav_checks import (
        E006_NAV_CONTEXT_PROCESSOR_MISSING,
        check_picker_renders,
    )

    # Rebuilt after ready(), so the installer's repair is gone.
    settings.TEMPLATES = [{
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    }]
    engine = settings.TEMPLATES[0]
    engine["OPTIONS"]["context_processors"] = [
        "django.template.context_processors.request",
    ]

    ids = [f.id for f in check_picker_renders()]
    assert E006_NAV_CONTEXT_PROCESSOR_MISSING in ids


def test_checks_are_silent_on_a_correctly_installed_deployment(picker_env):
    from stapel_core.django.nav_checks import check_picker_renders

    assert check_picker_renders() == []


# ---------------------------------------------------------------------------
# The installer itself.
# ---------------------------------------------------------------------------


def test_installer_is_idempotent(picker_env):
    from stapel_core.django.admin.install import install_admin_nav

    install_admin_nav()
    assert install_admin_nav() == {"dirs": [], "context_processors": []}


def test_installer_leaves_a_jinja2_engine_alone(picker_env, settings):
    from stapel_core.django.admin.install import install_admin_nav

    settings.TEMPLATES = [
        {
            "BACKEND": "django.template.backends.jinja2.Jinja2",
            "DIRS": [],
            "APP_DIRS": False,
            "OPTIONS": {},
        },
    ]
    install_admin_nav()
    assert settings.TEMPLATES[0]["DIRS"] == []
