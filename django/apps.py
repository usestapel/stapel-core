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
