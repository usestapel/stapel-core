"""Context processor for Django admin service navigation (admin-suite AS-4).

The registries live in :mod:`stapel_core.django.nav`; this processor just
exposes their render-ready output to ``admin/base_site.html``. The service
list comes from the ``STAPEL_SERVICES`` deploy-config (env-JSON, monolith
fallback), the extra sections from the two-channel ``NAV_LINKS`` registry —
no service list or dashboard map is hardcoded here anymore.
"""

from stapel_core.django.nav import (
    NavConfigError,
    build_modules,
    build_services,
    current_dashboard_url,
    current_swagger_url,
    nav_sections,
)


def stapel_services(request):
    """Add Stapel cross-service navigation to the admin template context.

    Fails soft: a malformed ``STAPEL_SERVICES`` / ``NAV_LINKS`` (already
    E-flagged by the ``stapel_nav`` system check) must not 500 the admin —
    the block simply renders empty.
    """
    user = getattr(request, "user", None)
    try:
        services = build_services()
        sections = nav_sections(user)
        dashboard_url = current_dashboard_url(user)
    except NavConfigError:
        services, sections, dashboard_url = [], {}, None

    from stapel_core.django.mounts import admin_login_url
    from stapel_core.django.nav import _current_prefix

    return {
        "stapel_services": services,
        # "All Services" collapses when the deployment has a single service
        # (a monolith or a one-service stack) — the section is redundant then.
        "stapel_services_multi": len(services) > 1,
        "stapel_nav_sections": sections,
        "current_swagger_url": current_swagger_url(),
        "current_service_prefix": _current_prefix(),
        "current_dashboard_url": dashboard_url,
        # Deployment-canonical admin-login path (mounts registry, script-prefix
        # aware) — lets module dashboards drop hardcoded "/auth/admin/login".
        "stapel_admin_login_url": admin_login_url(),
        # Modules of *this* process (INSTALLED_APPS introspection, §37) —
        # admin/Swagger/schema links per app, independent of STAPEL_SERVICES
        # (a monolith needs no env seed to see its own apps).
        "stapel_modules": build_modules(),
    }
