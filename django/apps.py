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
        # ROLES/MODELS policy and an unenforceable STRICT mode, W-level hints.
        from stapel_core.access import checks as _access_checks  # noqa: F401

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
