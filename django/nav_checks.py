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
E005_PICKER_TEMPLATE_SHADOWED = "stapel_core.nav.E005"
E006_NAV_CONTEXT_PROCESSOR_MISSING = "stapel_core.nav.E006"


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


@checks.register("stapel_nav")
def check_picker_renders(app_configs=None, **kwargs):
    """E005/E006 — the admin picker must actually be reachable at render time.

    This is the check whose absence let the feature disappear for months at a
    time. Everything else about the switcher was asserted somewhere —
    ``get_services`` parsing, the ``NAV_LINKS`` merge, ``E004``'s "a split
    deployment declared no registry" — and none of it noticed that the
    *template carrying the switcher was not the template the admin rendered*.

    ``django.contrib.admin`` ships its own ``admin/base_site.html`` and sits
    ahead of ``stapel_core.django`` in ``INSTALLED_APPS``, so under
    ``APP_DIRS`` alone core's copy is always shadowed. On a client fleet that
    happened the day stapel-core stopped being bind-mounted at
    ``/app/stapel_core``: ``iron-auth``'s hand-written ``DIRS`` entry became
    a path that does not exist, Django said nothing about it, and the admin
    quietly fell back to the stock template.

    :func:`stapel_core.django.admin.install.install_admin_nav` makes that
    unreachable by construction. This check is what says so out loud when a
    project defeats it anyway — by pinning its own ``OPTIONS["loaders"]``, by
    rebuilding ``TEMPLATES`` after ``ready()``, or by shipping an
    ``admin/base_site.html`` of its own that does not extend core's.
    """
    from django.conf import settings

    from stapel_core.django.admin.install import (
        CORE_TEMPLATES_DIR,
        NAV_CONTEXT_PROCESSOR,
    )

    if not getattr(settings, "TEMPLATES", None):
        return []  # no engine at all — django.contrib.admin's admin.E403

    findings = []

    try:
        from django.template.loader import get_template

        origin = get_template("admin/base_site.html").origin.name or ""
    except Exception:
        origin = None

    if origin is not None:
        import os

        resolved = os.path.realpath(origin)
        core_dir = os.path.realpath(CORE_TEMPLATES_DIR)
        project_dirs = []
        for engine in settings.TEMPLATES:
            if not isinstance(engine, dict):
                continue
            for entry in engine.get("DIRS") or []:
                path = os.path.realpath(str(entry))
                if path != core_dir:
                    project_dirs.append(path)
        from_core = resolved.startswith(core_dir + os.sep)
        # A project template is a deliberate override — its own business, and
        # it may well extend core's. Only the stock django.contrib.admin copy
        # (or anything else that is neither) means the picker is gone.
        from_project = any(resolved.startswith(d + os.sep) for d in project_dirs)
        if not from_core and not from_project:
            findings.append(checks.Error(
                f"admin/base_site.html resolves to {origin!r}, which is "
                f"neither stapel-core's copy nor one this project ships — so "
                f"the admin renders Django's stock header and the "
                f"cross-service service picker does not appear at all.",
                hint="stapel_core installs its template directory into every "
                     "Django engine at boot. Something removed it again: "
                     "check for OPTIONS['loaders'] pinned by hand (which "
                     "bypasses DIRS entirely), or for a TEMPLATES setting "
                     "rebuilt after apps are ready. stapel_core.django."
                     "settings.get_common_templates() is the supported "
                     "shape; a literal path to the library's template "
                     "directory is not (it rots the moment the library moves "
                     "from a bind mount to a wheel).",
                id=E005_PICKER_TEMPLATE_SHADOWED,
            ))

    engines_missing = []
    for index, engine in enumerate(settings.TEMPLATES):
        if not isinstance(engine, dict):
            continue
        if engine.get("BACKEND") != "django.template.backends.django.DjangoTemplates":
            continue
        processors = (engine.get("OPTIONS") or {}).get("context_processors") or []
        if NAV_CONTEXT_PROCESSOR not in processors:
            engines_missing.append(engine.get("NAME") or f"TEMPLATES[{index}]")
    if engines_missing:
        findings.append(checks.Error(
            f"The admin navigation context processor "
            f"({NAV_CONTEXT_PROCESSOR}) is missing from {engines_missing} — "
            f"the picker's template renders, but with no services in context, "
            f"so the header comes out empty.",
            hint="stapel_core adds it at boot; a TEMPLATES setting rebuilt "
                 "after apps are ready loses it again.",
            id=E006_NAV_CONTEXT_PROCESSOR_MISSING,
        ))
    return findings


__all__ = [
    "E001_BAD_SERVICES",
    "E002_BAD_NAV_LINKS",
    "W003_DUPLICATE_SERVICE_DASHBOARD",
    "E004_SERVICES_UNSET_IN_SPLIT_DEPLOYMENT",
    "E005_PICKER_TEMPLATE_SHADOWED",
    "E006_NAV_CONTEXT_PROCESSOR_MISSING",
    "check_services",
    "check_nav_links",
    "check_service_dashboard_duplicates",
    "check_services_declared",
    "check_picker_renders",
]
