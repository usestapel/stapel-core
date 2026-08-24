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
        # Ignored-env-var check (stapel_core.conf_checks): W-level when an env
        # var is set whose name an AppSettings namespace lists in
        # import_strings without env_overridable. Such a var is silently not
        # read since the implicit-no_env rule, so the operator believes one
        # implementation is loaded while another runs; this says so at
        # `manage.py check` time instead of leaving it to a manifest grep.
        from stapel_core import conf_checks as _conf_checks  # noqa: F401
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
        # AUTHENTICATION_BACKENDS checks (auth_backend_checks): E-level when
        # a backend overrides authenticate() without declaring that it
        # verifies a credential — the shape that turned a whole product's
        # password login into "any nonempty string" (audit AUTH-01).
        from stapel_core.django import (  # noqa: F401
            auth_backend_checks as _auth_backend_checks,
        )
        # CORS pair checks (stapel_core.django.cors_checks): E-level when
        # allow-all is combined with credentials — django-cors-headers then
        # reflects the caller's Origin, which is audit CDN-01 reproduced in
        # Python for every service, with no nginx involved.
        from stapel_core.django import cors_checks as _cors_checks  # noqa: F401
        # Revocation escape-hatch check (stapel_core.django.blacklist_checks):
        # W-level when STAPEL_BLACKLIST_FAIL_OPEN is on — both blacklists fail
        # closed by default, and the one setting that reopens them is
        # otherwise invisible in a running system.
        from stapel_core.django import (  # noqa: F401
            blacklist_checks as _blacklist_checks,
        )
        # Admin-visibility checks (stapel_core.django.admin.checks): E-level
        # for a malformed STAPEL_ADMIN["MODELS"] registry, W-level for
        # cross-service labels and secret-category downgrades.
        from stapel_core.django.admin import checks as _admin_checks  # noqa: F401
        # Navigation-registry checks (stapel_core.django.nav_checks): E-level
        # for a malformed STAPEL_SERVICES env-JSON or STAPEL_ADMIN["NAV_LINKS"]
        # overlay — otherwise the nav block silently renders empty.
        from stapel_core.django import nav_checks as _nav_checks  # noqa: F401
        # Captcha checks (stapel_core.django.captcha_checks): E-level when a
        # backend is named but cannot be built (no secret, bad dotted path) —
        # the shape that used to silently degrade to "pass every token" and
        # take the brute-force floor under OTP/reset/magic-link with it.
        from stapel_core.django import (  # noqa: F401
            captcha_checks as _captcha_checks,
        )
        # Adoption checks (stapel_core.django.adoption_checks): E-level when
        # the AUTH_ANONYMOUS axis is on and a view gates on a bare
        # IsAuthenticated without saying whether guests are meant to pass —
        # the finding is "declare a stance", never "close the view".
        from stapel_core.django import adoption_checks as _adoption_checks  # noqa: F401
        # Template-strictness check (stapel_core.templates):
        # W-level under DEBUG when an engine renders a missing variable as the
        # empty string — the default that turns a renamed context variable
        # into an email with a hole in it instead of an error.
        from stapel_core import templates as _template_strictness  # noqa: F401
        # Bus-backend checks (stapel_core.bus.checks): E-level when the
        # configured STAPEL_BUS_BACKEND names a transport (kafka/nats) whose
        # client library is not installed — catches the "publish() raises
        # ModuleNotFoundError forever" misconfiguration at boot-smoke time
        # instead of the first (silently swallowed) publish in production.
        from stapel_core.bus import checks as _bus_checks  # noqa: F401
        # Comm payload-validation checks (stapel_core.comm.checks): E-level
        # when schema validation is on (the default) but jsonschema cannot be
        # imported — otherwise every cross-service Function call carrying a
        # schema fails at request time; W-level when validation is off, so an
        # opt-out stays a stated choice.
        from stapel_core.comm import checks as _comm_checks  # noqa: F401
        # Config-manifest checks (stapel_core.config.checks): E-level when a
        # CONFIG.MD-declared (or call-site-declared) required key has no
        # value and no default — "required" was previously only enforced the
        # first time some code path called get_config(key); this is the
        # boot-smoke gate instead.
        from stapel_core.config import checks as _config_checks  # noqa: F401
        # CDN-field checks (stapel_core.django.cdn.checks): E-level when a
        # declared CdnImageField/CdnImageListField's image_type is missing
        # from STAPEL_CDN["ASSET_TYPES"], or when any such field exists but
        # no cdn.* comm route is wired at all — the "half the stack is
        # modular, half isn't" design gap (cdn-modularity.md §0.1/§0.5).
        from stapel_core.django.cdn import checks as _cdn_checks  # noqa: F401
        # Check-silencing guard (stapel_core.django.check_guard): E-level when
        # a blanket SILENCED_SYSTEM_CHECKS line mutes a check a library
        # declares security-critical, W-level listing everything else it
        # mutes. Nothing in the fleet read that setting before this.
        from stapel_core.django import check_guard as _check_guard  # noqa: F401
        # Deployment-posture coherence (stapel_core.django.presets): E-level
        # when a security-relevant value of the declared preset is not what
        # this deployment runs. The preset is the convenience; this check is
        # the contract — a posture nobody re-derives goes stale the first time
        # a settings module overrides one line.
        from stapel_core.django import presets as _presets  # noqa: F401
        # Production secret guards as checks (stapel_core.django.prodguard):
        # E-level for a placeholder/short SECRET_KEY or the shipped database
        # password. The guards existed for years as functions a settings
        # module had to call; registering them here is what makes adoption
        # stop being something each product must remember.
        from stapel_core.django.prodguard import (
            register_checks as _register_prodguard_checks,
        )
        _register_prodguard_checks()
        # Mandate seam (stapel_core.django.mandate): E-level when a view gates
        # on HasWorkspaceMandate and this deployment can ask nobody whether a
        # user holds one — such a view answers 503 for every request, and the
        # deploy gate is a better place to find that out than production.
        from stapel_core.django.mandate import (
            register_checks as _register_mandate_checks,
            subscribe_mandate_invalidation,
        )
        _register_mandate_checks()
        # Revocation must reach the mandate cache: the workspaces Actions that
        # take a mandate away drop the cached answer as they arrive, so the
        # TTL bounds a bus failure rather than the normal path.
        subscribe_mandate_invalidation()
        # Boot-gate checks (stapel_core.django.boot): W-level when the gate is
        # not enforcing, and W-level when a hand-rolled MIDDLEWARE never
        # picked up BootGateMiddleware — the second one is the only way a
        # non-conforming project learns its E-gates never run under gunicorn.
        from stapel_core.django import boot as _boot_checks  # noqa: F401
        # Schema-drift probe on /api/health/ + /api/metrics/
        # (stapel_core.django.monitoring.schema_health). A stand ran twelve
        # hours on an unmigrated schema while reporting healthy because
        # nothing in the process ever asked. Registering here is what makes
        # the answer exist without every product remembering to wire it;
        # nothing queries the database until the first scrape.
        from stapel_core.django.monitoring.schema_health import register_schema_check

        register_schema_check()

        # Observability seams (stapel_core.observability.checks): W-level,
        # and gated on evidence that this deployment adopted the facade — a
        # metrics backend that silently discards every measurement looks
        # exactly like one that works, from inside the process.
        from stapel_core.observability import checks as _obs_checks  # noqa: F401
        # Facade metrics onto the /api/metrics/ endpoint this service already
        # serves. Registering here is what makes a module's counter appear on
        # the scrape URL without every product wiring an exporter; nothing is
        # collected until the first scrape.
        from stapel_core.observability.exporter import (
            register_prometheus_exporter,
        )

        register_prometheus_exporter()

        # Verification factors declared by the host in
        # STAPEL_VERIFICATION["EXTRA_FACTORS"] (#145). MODULE.md documents the
        # setting as THE way a host substitutes or adds a factor, but until
        # 0.16.1 nothing in the framework applied it — declaring it did
        # nothing, silently, and a product had to call the loader from its own
        # app layer to make a security fix real. Registered pinned, so the
        # host's id wins over the library factor stapel-auth registers later,
        # whatever the INSTALLED_APPS order is.
        from stapel_core.verification.factors import load_configured_factors

        load_configured_factors()

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

    # SERVE_PERMISSIONS is the same bug with teeth. A blank TITLE is
    # embarrassing; a lost SERVE_PERMISSIONS means the singleton keeps
    # drf-spectacular's own default — AllowAny — and the schema, Swagger UI and
    # ReDoc serve the whole API surface, permission classes included, to anyone
    # who can reach the service. apply_patches() refuses this key by name, so
    # it is written straight onto the singleton, and onto the view classes that
    # read it once at import time.
    if real.get('SERVE_PERMISSIONS') and _unpoison_serve_permissions(
        spectacular_settings, real['SERVE_PERMISSIONS']
    ):
        patches['SERVE_PERMISSIONS'] = real['SERVE_PERMISSIONS']

    return patches


def _unpoison_serve_permissions(spectacular_settings, declared) -> bool:
    """Force *declared* onto the settings singleton and drf-spectacular's views.

    ``SERVE_PERMISSIONS`` is an import-string setting: the singleton holds
    imported classes while the project's dict holds dotted paths, so both are
    resolved before comparing — otherwise this would rewrite on every call and
    could never report "nothing to do".

    Returns True when something was actually changed.
    """
    import inspect

    from drf_spectacular import views as spectacular_views
    from rest_framework.settings import perform_import
    from rest_framework.views import APIView

    desired = list(perform_import(declared, 'SERVE_PERMISSIONS') or ())
    changed = list(getattr(spectacular_settings, 'SERVE_PERMISSIONS', None) or ()) != desired
    spectacular_settings.SERVE_PERMISSIONS = desired

    # The views bind ``permission_classes = spectacular_settings.
    # SERVE_PERMISSIONS`` at class-definition time, so patching the singleton
    # alone leaves them holding the poisoned list — the same reason ready()
    # already rebinds DRF's own APIView attributes below.
    for _, view_cls in inspect.getmembers(spectacular_views, inspect.isclass):
        if not issubclass(view_cls, APIView):
            continue
        # ONLY the views drf-spectacular defines. Its module does
        # `from rest_framework.views import APIView`, so getmembers yields
        # DRF's base class too — and rebinding permission_classes there
        # rewrites the default for EVERY view in the process that does not
        # declare its own. That is not a documentation artifact: it made
        # passkey and TOTP endpoints staff-only at runtime in every service
        # declaring SPECTACULAR_SETTINGS, i.e. it locked users out of login.
        if not view_cls.__module__.startswith('drf_spectacular'):
            continue
        if list(getattr(view_cls, 'permission_classes', ()) or ()) != desired:
            view_cls.permission_classes = desired
            changed = True
    return changed
