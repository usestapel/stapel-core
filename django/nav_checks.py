"""System checks for the navigation registries (tag ``stapel_nav``) — AS-4.

A malformed ``STAPEL_SERVICES`` (bad env-JSON, a service object missing
``name``/``prefix``) or a malformed ``STAPEL_ADMIN["NAV_LINKS"]`` overlay
would silently mean an empty navigation block instead of what was written —
E-level (deploy blocker), matching the ``stapel_mounts`` / ``stapel_admin``
policy. The rendering layer swallows :class:`NavConfigError` so the admin
never 500s; this check is what surfaces the misconfiguration.
"""
from __future__ import annotations

from django.core import checks

E001_BAD_SERVICES = "stapel_core.nav.E001"
E002_BAD_NAV_LINKS = "stapel_core.nav.E002"


@checks.register("stapel_nav")
def check_services(app_configs=None, **kwargs):
    """E001 — ``STAPEL_SERVICES`` must parse into a list of services."""
    from stapel_core.django.nav import NavConfigError, get_services

    try:
        get_services()
    except NavConfigError as exc:
        return [checks.Error(
            str(exc),
            hint='STAPEL_SERVICES is a JSON array of {"name": ..., "prefix": '
                 '...} objects (env-JSON, written by the project generators), '
                 "or a Django-setting list of the same shape; leave it unset "
                 "for a single-service monolith.",
            id=E001_BAD_SERVICES,
        )]
    return []


@checks.register("stapel_nav")
def check_nav_links(app_configs=None, **kwargs):
    """E002 — the ``STAPEL_ADMIN["NAV_LINKS"]`` merge-registry must parse."""
    from stapel_core.django.nav import NavConfigError, get_nav_links

    try:
        get_nav_links()
    except NavConfigError as exc:
        return [checks.Error(
            str(exc),
            hint="Each entry is {'section': 'tools|monitoring|dashboards', "
                 "'title': ..., 'url': ..., 'requires': 'staff|superuser|"
                 "low|mid|high', 'external': bool}; a partial dict patches a "
                 "code-registered link, None removes one.",
            id=E002_BAD_NAV_LINKS,
        )]
    return []


__all__ = [
    "E001_BAD_SERVICES",
    "E002_BAD_NAV_LINKS",
    "check_services",
    "check_nav_links",
]
