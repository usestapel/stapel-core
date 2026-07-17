"""
Django app configuration for common.django.

This app provides:
- Management commands for Staff group management
- Common utilities for JWT authentication
"""

from django.apps import AppConfig


class CommonDjangoConfig(AppConfig):
    """App config for common.django utilities."""

    name = 'stapel_core.django'
    label = 'common_django'
    verbose_name = 'Common Django Utilities'

    def ready(self):
        """
        Called when the app is ready.

        - Ensures DRF uses correct DEFAULT_SCHEMA_CLASS from settings
        - Can auto-load Staff group fixtures at startup
        """
        from django.conf import settings

        # System checks (registered on import; W-level, never block deploys).
        from stapel_core.netintel import checks as _netintel_checks  # noqa: F401
        # Staff-mandate checks (stapel_core.access): E-level for malformed
        # ROLES/MODELS/STEP_UP policy and an unenforceable STRICT mode,
        # W-level hints (incl. step-up degradation).
        from stapel_core.access import checks as _access_checks  # noqa: F401
        # Access audit forwarding (AS-6): subscribe dac_escalation /
        # step_up_denied → eventstore audit stream (+ optional NOTIFY shim).
        # Idempotent (dispatch_uid), best-effort (never breaks has_perm).
        from stapel_core.access.audit import connect_access_audit
        connect_access_audit()
        # Secret-provider seam checks (stapel_core.secrets): W-level — the env
        # default always works; a broken custom provider surfaces here.
        from stapel_core.secrets import checks as _secrets_checks  # noqa: F401
        # URL-mounting checks (stapel_core.django.checks): E-level for
        # LOGIN_URL/redirect settings pointing at an unresolvable path —
        # otherwise every login_required ends in a user-facing 404.
        from stapel_core.django import checks as _mounts_checks  # noqa: F401
        # Admin-visibility checks (stapel_core.django.admin.checks): E-level
        # for a malformed STAPEL_ADMIN["MODELS"] registry, W-level for
        # cross-service labels and secret-category downgrades.
        from stapel_core.django.admin import checks as _admin_checks  # noqa: F401
        # Navigation-registry checks (stapel_core.django.nav_checks): E-level
        # for a malformed STAPEL_SERVICES env-JSON or STAPEL_ADMIN["NAV_LINKS"]
        # overlay — otherwise the nav block silently renders empty.
        from stapel_core.django import nav_checks as _nav_checks  # noqa: F401
        # Bus-backend checks (stapel_core.bus.checks): E-level when the
        # configured STAPEL_BUS_BACKEND names a transport (kafka/nats) whose
        # client library is not installed — catches the "publish() raises
        # ModuleNotFoundError forever" misconfiguration at boot-smoke time
        # instead of the first (silently swallowed) publish in production.
        from stapel_core.bus import checks as _bus_checks  # noqa: F401
        # Config-manifest checks (stapel_core.config.checks): E-level when a
        # CONFIG.MD-declared (or call-site-declared) required key has no
        # value and no default — "required" was previously only enforced the
        # first time some code path called get_config(key); this is the
        # boot-smoke gate instead.
        from stapel_core.config import checks as _config_checks  # noqa: F401

        # Admin visibility (admin-suite AS-3): re-register contrib service
        # tables (auth.Group, sessions.Session) under declaration-aware admins
        # and apply STAPEL_ADMIN["MODELS"] overrides (None = unregister,
        # admin_class = swap). No-op without django.contrib.admin; list this
        # app after it (standard layout) so autodiscover has already run.
        from django.apps import apps as django_apps

        if django_apps.is_installed('django.contrib.admin'):
            from stapel_core.django.admin.registration import setup_admin_visibility

            setup_admin_visibility()

        # DRF caches api_settings on first access. If any module (e.g. drf-spectacular)
        # triggers that access before Django settings are fully loaded, the cache will
        # contain DRF defaults instead of our REST_FRAMEWORK config. Force a full reload
        # now that Django is ready and all settings are available.
        # Also patch APIView.authentication_classes — it's set at class-definition time
        # from the cached (stale) api_settings value, so we must update it too.
        try:
            from rest_framework.settings import api_settings
            from rest_framework.views import APIView
            api_settings.reload()
            APIView.authentication_classes = api_settings.DEFAULT_AUTHENTICATION_CLASSES
            APIView.permission_classes = api_settings.DEFAULT_PERMISSION_CLASSES
        except Exception:
            pass

        # Auto-load Staff group fixture if enabled
        auto_load = getattr(settings, 'STAFF_GROUP_AUTO_LOAD', False)
        fixture_path = getattr(settings, 'STAFF_GROUP_FIXTURE_PATH', None)

        if auto_load and fixture_path:
            try:
                from .groups import load_staff_group_if_empty
                load_staff_group_if_empty(fixture_path)
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"Could not auto-load Staff group fixture: {e}")

        # Framework-level fix: unpoison the drf-spectacular settings singleton
        # if it was built before this project's SPECTACULAR_SETTINGS assignment
        # (import-order bug — see _unpoison_spectacular_settings docstring).
        _unpoison_spectacular_settings()


def _unpoison_spectacular_settings() -> dict:
    """Patch drf-spectacular's ``spectacular_settings`` singleton if it was
    built before the project's ``SPECTACULAR_SETTINGS`` assignment.

    Root cause: many projects write their Django settings module as::

        # config/settings/base.py
        from stapel_core.django.settings import *   # noqa: F401,F403
        ...
        SPECTACULAR_SETTINGS = get_spectacular_settings(...)   # further down

    Importing ``stapel_core.django.settings`` requires first fully executing
    its parent package, ``stapel_core/django/__init__.py``, which imports
    ``stapel_core.django.openapi`` -> ``stapel_core.django.openapi.schemas`` —
    the latter does a *non-lazy* ``from drf_spectacular.openapi import
    AutoSchema`` (needed as a base class for ``PermissionAwareAutoSchema``,
    so it can't be deferred the way ``stapel_core.django.openapi.swagger``
    deliberately defers its own drf-spectacular imports). That cascades into
    importing ``drf_spectacular.settings``, whose module body constructs the
    module-level ``spectacular_settings`` *singleton* by snapshotting
    ``django.conf.settings.SPECTACULAR_SETTINGS`` right then — i.e. *before*
    the project's settings module reaches its own ``SPECTACULAR_SETTINGS =
    get_spectacular_settings(...)`` assignment further down. drf-spectacular
    never re-reads the setting afterwards (no ``setting_changed`` receiver
    for it), so the singleton stays pinned to the empty defaults
    (``TITLE=''``, ``VERSION='0.0.0'``) for the rest of the process — i.e.
    every schema this process emits (live ``/schema/``, Swagger UI, and the
    offline ``spectacular`` management command) reports a blank title and
    ``0.0.0`` version, regardless of what the project actually configured.

    ``AppConfig.ready()`` runs from ``apps.populate()``, which Django calls
    only *after* settings are fully resolved — so patching the
    already-constructed singleton here, in place, via the
    apply_patches/clear_patches seam drf-spectacular ships for exactly this
    kind of override, reaches every module that already did ``from
    drf_spectacular.settings import spectacular_settings`` (same object, not
    a fresh one). ``spectacular_settings.reload()`` would *not* work:
    ``SpectacularSettings`` inherits ``APISettings.user_settings`` as-is,
    which is hardwired to the ``REST_FRAMEWORK`` key, not
    ``SPECTACULAR_SETTINGS``.

    Idempotent: if the import order was correct (singleton built after
    ``SPECTACULAR_SETTINGS`` was assigned, or ``SPECTACULAR_SETTINGS`` isn't
    set at all), the values already match and no patch is applied — zero
    effect. Safe if drf-spectacular isn't installed (ImportError -> no-op).

    Returns the dict of patches actually applied (empty if none were
    needed) — used by tests to assert on the fix without duplicating the
    patch-detection logic.
    """
    try:
        from drf_spectacular.settings import spectacular_settings
    except ImportError:
        return {}

    from django.conf import settings as django_settings

    real = getattr(django_settings, 'SPECTACULAR_SETTINGS', None) or {}
    patches = {
        key: real[key]
        for key in ('TITLE', 'VERSION', 'DESCRIPTION')
        if real.get(key) and getattr(spectacular_settings, key, None) != real[key]
    }
    if patches:
        spectacular_settings.apply_patches(patches)
    return patches
