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
W003_DUPLICATE_SERVICE_DASHBOARD = "stapel_core.nav.W003"
E004_SERVICES_UNSET_IN_SPLIT_DEPLOYMENT = "stapel_core.nav.E004"


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


@checks.register("stapel_nav")
def check_service_dashboard_duplicates(app_configs=None, **kwargs):
    """W003 — at most one ``service_dashboard=True`` link is expected.

    ``current_dashboard_url`` picks the first admissible flagged link in
    registry order, so a second one is not a 500 — but it is very likely a
    mistake (two modules, or a code link plus an overlay add, both claiming
    to be *the* service dashboard). Warn instead of failing soft silently.
    """
    from stapel_core.django.nav import NavConfigError, get_nav_links

    try:
        links = get_nav_links()
    except NavConfigError:
        return []  # already reported by E002

    flagged = [link.key for link in links if link.service_dashboard]
    if len(flagged) <= 1:
        return []
    return [checks.Warning(
        f"Multiple NAV_LINKS entries set service_dashboard=True: {flagged}. "
        f"current_dashboard_url() will use the first one in registry order "
        f"({flagged[0]!r}) and ignore the rest.",
        hint="Only one module/link should own service_dashboard=True per "
             "deployment; unset it on the others via register_nav_link(...) "
             "or STAPEL_ADMIN['NAV_LINKS'][key] = {'service_dashboard': False}.",
        id=W003_DUPLICATE_SERVICE_DASHBOARD,
    )]


@checks.register("stapel_nav")
def check_services_declared(app_configs=None, **kwargs):
    """E004 — a split deployment must declare ``STAPEL_SERVICES``.

    The failure this closes is the one AS-4 opened. Moving the service list
    out of the framework into deploy-config was right, but the fallback for
    "no registry" is the *monolith* answer — one implicit service derived
    from ``URL_PREFIX`` — and a split deployment that was never re-seeded is
    indistinguishable from a monolith. It boots, it passes every check, and
    the admin simply stops being able to reach a sibling service: the "All
    Services" section collapses (``stapel_services_multi`` is false for a
    one-entry list) and nothing anywhere says why. Navigation that vanishes
    quietly is worse than navigation that was never built, because nobody
    goes looking for a regression the deploy gate called green.

    So the deployment is asked to be consistent with itself: if the mount
    registry claims a sibling service exists (an **external** mount that is
    not this service), the navigation registry has to know about it too.
    A true monolith declares no external mount and stays clean.
    """
    from stapel_core.django.nav import (
        NavConfigError,
        services_declared,
        sibling_prefixes,
    )

    try:
        if services_declared():
            return []
    except NavConfigError:
        return []  # malformed — E001 owns that story

    try:
        siblings = sibling_prefixes()
    except Exception:
        # A mount registry that does not parse is stapel_mounts' E, and a
        # URLconf that will not load is not this check's verdict to give.
        return []
    if not siblings:
        return []

    return [checks.Error(
        f"STAPEL_SERVICES is not set, but this deployment declares sibling "
        f"services behind the same proxy ({', '.join(siblings)}). The admin "
        f"service switcher is therefore rendering the single-service monolith "
        f"fallback — from this service's admin there is no link to any other "
        f"service's admin.",
        hint='Set STAPEL_SERVICES to the deployment\'s service registry — a '
             'JSON array of {"name": ..., "prefix": ...}, one entry per '
             "service, in the shared deploy env (12-factor: the same value "
             "for every service of the deployment). stapel-create-project "
             "seeds it and stapel-new-service appends to it; a deployment "
             "that predates those generators has to seed it once. A genuine "
             "monolith instead sets STAPEL_AUTH_SERVICE_PREFIX = '' and has "
             "no sibling to list.",
        id=E004_SERVICES_UNSET_IN_SPLIT_DEPLOYMENT,
    )]


__all__ = [
    "E001_BAD_SERVICES",
    "E002_BAD_NAV_LINKS",
    "W003_DUPLICATE_SERVICE_DASHBOARD",
    "E004_SERVICES_UNSET_IN_SPLIT_DEPLOYMENT",
    "check_services",
    "check_nav_links",
    "check_service_dashboard_duplicates",
    "check_services_declared",
]
