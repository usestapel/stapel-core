"""Cross-service navigation registries — admin-suite AS-4 (§2).

Two deploy-config registries feed the admin + Swagger service navigation,
replacing the legacy hardcode (the ``STAPEL_SERVICES`` list baked into
``core/config.py``, the Tools/Monitoring sections and per-module dashboards
baked into ``base_site.html`` / the Swagger inject):

- **STAPEL_SERVICES** — the sibling services of *this* deployment, an
  env-JSON (12-factor, read by both Python and the non-Django agent service;
  §2.2). Written by the generators (``stapel-create-project`` seeds it,
  ``stapel-new-service`` appends a row — the same discipline as
  ``STAPEL_BUS_ROUTES``), never hardcoded in the framework. A monolith leaves
  it unset: a single implicit service is derived from ``URL_PREFIX`` and the
  "All Services" section collapses.

- **NAV_LINKS** — extra tool/monitoring/dashboard links, a merge-registry
  (canonical seam, library-standard §3.3) with two channels (§2.3): a module
  registers its own dashboard in ``AppConfig.ready()`` via
  :func:`register_nav_link` (channel 1); the project adds/overrides/removes
  via ``STAPEL_ADMIN["NAV_LINKS"]`` — merge over code, ``None`` removes
  (channel 2). Fixed sections (``tools``, ``monitoring``, ``dashboards``) are
  mechanism; their contents are policy.

Rendering respects two gates:

- **staff/clearance gating** of every rendered link (``requires``) — filters
  by the viewer's admissibility; the target itself is protected by its own
  perimeter (nginx ``auth_request`` for Grafana, ``IsStaffUserForSwagger`` for
  the API docs — that is deploy-doc policy, not this module's job);
- **introspection env-gating** — the Swagger links (the current-service
  button and the per-service "API" links) render only when this deployment
  actually mounts the schema (``get_dev_urls`` mounts ``/swagger/`` only for
  ``DJANGO_ENV in {local, dev}``), detected by reversing ``swagger-ui``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

#: Navigation sections the mechanism fixes; NAV_LINKS entries must pick one.
SECTIONS = ("tools", "monitoring", "dashboards")

#: Recognized ``requires`` gates for a nav link (§2.3).
_STATIC_REQUIRES = ("staff", "superuser")
_CLEARANCE_REQUIRES = ("low", "mid", "high")


class NavConfigError(Exception):
    """A STAPEL_SERVICES / NAV_LINKS entry does not parse — surfaced as a
    system-check Error, never a 500."""


# ─── Service registry (STAPEL_SERVICES env-JSON) ────────────────────────────


@dataclass(frozen=True)
class Service:
    """One sibling service of the deployment (name + path prefix)."""

    name: str
    #: Path prefix relative to the deployment root, no slashes ("auth").
    #: Empty string = the deployment root (a monolith's single service).
    prefix: str = ""


def _resolve_setting(name: str) -> Any:
    """``settings.<name>`` (namespace/flat) → env var → None.

    STAPEL_SERVICES is deliberately env-readable: it is deploy config shared
    verbatim across services (and languages), not a trust decision.
    """
    import os

    from django.conf import settings

    value = getattr(settings, name, None)
    if value is not None:
        return value
    return os.environ.get(name)


def _current_prefix() -> str:
    from django.conf import settings

    return getattr(settings, "URL_PREFIX", "").strip("/")


def _parse_services(raw: Any) -> List[Service]:
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise NavConfigError(
                f"STAPEL_SERVICES is not valid JSON: {exc}"
            ) from exc
    else:
        parsed = raw
    if not isinstance(parsed, list):
        raise NavConfigError(
            f"STAPEL_SERVICES must be a JSON array of "
            f'{{"name": ..., "prefix": ...}} objects, got '
            f"{type(parsed).__name__}"
        )
    services: List[Service] = []
    for index, item in enumerate(parsed):
        if not isinstance(item, Mapping) or "name" not in item or "prefix" not in item:
            raise NavConfigError(
                f"STAPEL_SERVICES[{index}] must be an object with 'name' and "
                f"'prefix' keys, got {item!r}"
            )
        services.append(
            Service(name=str(item["name"]), prefix=str(item["prefix"]).strip("/"))
        )
    return services


def get_services() -> List[Service]:
    """Effective service list.

    ``STAPEL_SERVICES`` (env-JSON or a Django-setting list) when configured;
    otherwise the monolith fallback — one implicit service derived from
    ``URL_PREFIX`` (name from ``SERVICE_NAME`` / the prefix / "This service").
    Raises :class:`NavConfigError` on malformed config (surface via the
    system check, not a 500).
    """
    from django.conf import settings

    raw = _resolve_setting("STAPEL_SERVICES")
    if raw is None or raw == "" or raw == []:
        prefix = _current_prefix()
        name = (
            getattr(settings, "SERVICE_NAME", "")
            or (prefix.replace("-", " ").replace("_", " ").title() if prefix else "")
            or "This service"
        )
        return [Service(name=name, prefix=prefix)]
    return _parse_services(raw)


def swagger_mounted() -> bool:
    """True when this deployment mounts the Swagger UI (introspection on).

    ``get_dev_urls`` names the view ``swagger-ui`` and only mounts it for
    ``DJANGO_ENV in {local, dev}`` — a failed reverse means introspection is
    off, so Swagger links must not render.
    """
    from django.urls import NoReverseMatch, reverse

    try:
        reverse("swagger-ui")
        return True
    except NoReverseMatch:
        return False
    except Exception:
        return False


def build_services(*, include_swagger: Optional[bool] = None) -> List[dict]:
    """Render-ready services list (admin/swagger URLs + active flag).

    URLs are built through the current script prefix (mounts convention) so
    navigation survives a sub-path deployment. ``swagger_url`` is ``None``
    when introspection is not mounted (``include_swagger`` overrides the
    auto-detection, e.g. for tests).
    """
    from django.urls import get_script_prefix

    root = get_script_prefix()
    current = _current_prefix()
    show_swagger = swagger_mounted() if include_swagger is None else include_swagger

    out: List[dict] = []
    for svc in get_services():
        p = svc.prefix
        admin_url = f"{root}{p}/admin/" if p else f"{root}admin/"
        swagger_url = None
        if show_swagger:
            swagger_url = f"{root}{p}/swagger/" if p else f"{root}swagger/"
        out.append(
            {
                "name": svc.name,
                "prefix": p,
                "admin_url": admin_url,
                "swagger_url": swagger_url,
                "is_active": current == p or (not current and not p),
            }
        )
    return out


def current_swagger_url() -> Optional[str]:
    """This service's Swagger URL, or ``None`` when introspection is off."""
    if not swagger_mounted():
        return None
    from django.urls import get_script_prefix

    root = get_script_prefix()
    current = _current_prefix()
    return f"{root}{current}/swagger/" if current else f"{root}swagger/"


# ─── NAV_LINKS merge-registry (code channel + settings channel) ─────────────


@dataclass(frozen=True)
class NavLink:
    """One extra navigation link (tool / monitoring / dashboard)."""

    key: str
    section: str
    title: str
    url: str
    #: Viewer gate: "staff" | "superuser" | a clearance level (low/mid/high).
    requires: str = "staff"
    #: True — opens an out-of-app target (rendered target="_blank"); the URL
    #: is used verbatim (script-prefix already baked in by whoever set it).
    external: bool = False
    #: True — this link is *the* current service's own dashboard, the
    #: explicit AS-4 §2 arbitration flag consumed by
    #: :func:`current_dashboard_url`. Set by the owning module at
    #: registration time; never inferred.
    service_dashboard: bool = False


#: Channel 1 — links registered in code by modules' AppConfig.ready().
_code_links: "Dict[str, NavLink]" = {}


def _validate_section(section: str, *, source: str) -> str:
    if section not in SECTIONS:
        raise NavConfigError(
            f"{source}: unknown section {section!r} (allowed: {list(SECTIONS)})"
        )
    return section


def _validate_requires(requires: str, *, source: str) -> str:
    if requires not in (*_STATIC_REQUIRES, *_CLEARANCE_REQUIRES):
        raise NavConfigError(
            f"{source}: unknown requires {requires!r} "
            f"(allowed: staff, superuser, low, mid, high)"
        )
    return requires


def register_nav_link(
    key: str,
    *,
    section: str,
    title: str,
    url: str,
    requires: str = "staff",
    external: bool = False,
    service_dashboard: bool = False,
) -> None:
    """Register a module's own nav link (channel 1, called from ready()).

    Idempotent per key (re-import / repeated ready() is safe). The project
    overrides or removes it via ``STAPEL_ADMIN["NAV_LINKS"][key]`` (channel 2).

    ``service_dashboard=True`` marks this link as *the* current service's own
    dashboard for :func:`current_dashboard_url` (AS-4 §2 arbitration) — an
    explicit, module-declared choice rather than an inferred one. A module
    that owns exactly one dashboard for its service should set it.
    """
    source = f"register_nav_link({key!r})"
    _validate_section(section, source=source)
    _validate_requires(requires, source=source)
    _code_links[key] = NavLink(
        key=key,
        section=section,
        title=title,
        url=url,
        requires=requires,
        external=bool(external),
        service_dashboard=bool(service_dashboard),
    )


def unregister_nav_link(key: str) -> None:
    """Remove a code-registered link (mainly for tests / dynamic teardown)."""
    _code_links.pop(key, None)


def clear_nav_links() -> None:
    """Drop all code-registered links (test isolation)."""
    _code_links.clear()


def _coerce_link(key: str, entry: Any, base: Optional[NavLink]) -> NavLink:
    """A settings-overlay entry → NavLink, patched over *base* when present."""
    source = f"STAPEL_ADMIN['NAV_LINKS'][{key!r}]"
    if not isinstance(entry, Mapping):
        raise NavConfigError(
            f"{source} must be a dict or None, got {type(entry).__name__}"
        )
    unknown = set(entry) - {
        "section", "title", "url", "requires", "external", "service_dashboard",
    }
    if unknown:
        raise NavConfigError(f"{source}: unknown keys {sorted(unknown)}")

    if base is None:
        missing = {"section", "title", "url"} - set(entry)
        if missing:
            raise NavConfigError(
                f"{source} adds a new link but is missing {sorted(missing)} "
                "(a partial patch is only valid over a code-registered link)"
            )
    section = _validate_section(
        entry.get("section", base.section if base else None), source=source
    )
    requires = _validate_requires(
        entry.get("requires", base.requires if base else "staff"), source=source
    )
    return NavLink(
        key=key,
        section=section,
        title=entry.get("title", base.title if base else ""),
        url=entry.get("url", base.url if base else ""),
        requires=requires,
        external=bool(entry.get("external", base.external if base else False)),
        service_dashboard=bool(
            entry.get(
                "service_dashboard", base.service_dashboard if base else False
            )
        ),
    )


def get_nav_links() -> List[NavLink]:
    """Effective links: code registrations merged with the settings overlay.

    Merge-over-code semantics: an overlay dict patches (or adds) a link,
    ``None`` removes it. Raises :class:`NavConfigError` on a malformed
    overlay (surface via the system check).
    """
    from stapel_core.django.admin.conf import admin_settings

    merged: Dict[str, NavLink] = dict(_code_links)
    overlay = admin_settings.NAV_LINKS or {}
    if not isinstance(overlay, Mapping):
        raise NavConfigError(
            f"STAPEL_ADMIN['NAV_LINKS'] must be a dict, got {type(overlay).__name__}"
        )
    for key, entry in overlay.items():
        if entry is None:
            merged.pop(key, None)
            continue
        merged[key] = _coerce_link(key, entry, merged.get(key))
    return list(merged.values())


def _viewer_allowed(user, requires: str) -> bool:
    """Is *user* admissible to see a link gated by *requires*?"""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if not getattr(user, "is_staff", False):
        return False
    if getattr(user, "is_superuser", False):
        return True  # A5 — superuser is beyond the mandate, sees everything
    if requires == "superuser":
        return False
    if requires in _CLEARANCE_REQUIRES:
        try:
            from stapel_core.access.levels import Level
            from stapel_core.access.roles import clearance_for
            from stapel_core.access.sources import user_roles

            clearance = clearance_for(user_roles(user))
            if clearance is None:
                return False
            return clearance >= Level.parse(requires, clearance_only=True)
        except Exception:
            # Mandate not engaged (no roles configured) — degrade to staff.
            return True
    return True  # "staff"


def _prefix_url(root: str, url: str, external: bool) -> str:
    """Script-prefix an internal absolute path; leave external/absolute URLs."""
    if external or "://" in url or url.startswith("//"):
        return url
    if url.startswith("/"):
        return f"{root.rstrip('/')}{url}"
    return url


def nav_sections(user) -> Dict[str, List[dict]]:
    """Render-ready links grouped by section, filtered by *user* admission.

    Empty sections are dropped. URLs are script-prefixed (internal targets)
    so navigation survives sub-path deployments.
    """
    from django.urls import get_script_prefix

    root = get_script_prefix()
    result: Dict[str, List[dict]] = {section: [] for section in SECTIONS}
    for link in get_nav_links():
        if not _viewer_allowed(user, link.requires):
            continue
        result[link.section].append(
            {
                "title": link.title,
                "url": _prefix_url(root, link.url, link.external),
                "external": link.external,
            }
        )
    return {section: items for section, items in result.items() if items}


def current_dashboard_url(user) -> Optional[str]:
    """This service's own dashboard link, if a local module registered one.

    Selection (AS-4 §2, arbitrated): an explicit ``service_dashboard=True``
    link (set via :func:`register_nav_link` / the ``NAV_LINKS`` overlay) wins
    — the first admissible one, in registry order, when more than one module
    claims the flag (a misconfiguration; see ``stapel_core.nav.W003``).
    Only when *no* link carries the flag does this fall back to the legacy
    heuristic for backward compatibility: the first admissible
    ``dashboards``/``tools`` link whose URL points inside the current service
    prefix (a monolith with no prefix accepts any local link). ``None`` when
    the service has no dashboard either way.
    """
    from django.urls import get_script_prefix

    root = get_script_prefix()
    current = _current_prefix()
    links = get_nav_links()

    # Explicit flag — takes priority over the prefix heuristic.
    for link in links:
        if not link.service_dashboard:
            continue
        if link.section not in ("dashboards", "tools"):
            continue
        if link.external or not _viewer_allowed(user, link.requires):
            continue
        return _prefix_url(root, link.url, link.external)

    # Fallback — legacy prefix-matching heuristic (pre-flag behavior).
    for link in links:
        if link.section not in ("dashboards", "tools"):
            continue
        if link.external or not _viewer_allowed(user, link.requires):
            continue
        path = link.url.lstrip("/").split("?")[0]
        if current and path.startswith(f"{current}/"):
            return _prefix_url(root, link.url, link.external)
        if not current and path:  # monolith — any local dashboard qualifies
            return _prefix_url(root, link.url, link.external)
    return None


# ─── Module registry (INSTALLED_APPS introspection, BACKLOG §37) ───────────
#
# The *service* registry above (STAPEL_SERVICES) is sown by deploy config —
# right for a microservices topology, where a process cannot see its
# siblings any other way. A monolith needs no such seed: it hosts every one
# of its Stapel modules in the same INSTALLED_APPS list already, so it can
# discover them directly by introspection (§37-уточнение, 2026-07-10).
#
# A module counts as a "Stapel module" when either:
#   - its AppConfig carries ``stapel_module = True`` (a class or instance
#     attribute — the explicit marker a project's own local app sets to opt
#     in, e.g. ``apps/tools``: "скелет tools ставит маркер своим"), or
#   - it is one of the published ``stapel_*`` pip packages (AppConfig.name
#     starts with "stapel_", the PyPI/import-name convention every module in
#     this repo follows) — auto-detected, no marker needed. ``stapel_core``
#     itself (and its ``stapel_core.django.*`` internal apps: users, outbox,
#     taskstore, …) is the framework, not a content module, and is always
#     excluded.
# An AppConfig can also opt *out* of the auto-detect with
# ``stapel_module = False`` (rare — a stapel_* package that is pure library,
# no admin/API surface of its own).


def _is_stapel_app(app_config: Any) -> bool:
    marker = getattr(app_config, "stapel_module", None)
    if marker is not None:
        return bool(marker)
    name = app_config.name
    return name.startswith("stapel_") and not (
        name == "stapel_core" or name.startswith("stapel_core.")
    )


@dataclass(frozen=True)
class ModuleNav:
    """One discovered Stapel module of *this* process."""

    key: str  # app_label
    name: str  # verbose_name
    admin_url: str
    #: None when introspection (Swagger/schema) is not mounted in this
    #: deployment (``get_dev_urls`` only mounts it for DJANGO_ENV in
    #: {local, dev}) — never a link to a page that 404s.
    swagger_url: Optional[str] = None
    schema_url: Optional[str] = None


def _reverse_or_none(name: str, **kwargs) -> Optional[str]:
    from django.urls import NoReverseMatch, reverse

    try:
        return reverse(name, **kwargs)
    except NoReverseMatch:
        return None
    except Exception:
        return None


def _module_admin_url(app_config: Any) -> str:
    """The stock Django per-app admin index — ``/admin/<app_label>/``.

    ``admin:app_list`` is a builtin Django URL name that reverses for any
    app_label regardless of whether that app registered models with this
    admin site, so this always succeeds once ``django.contrib.admin`` is
    installed; a bare prefix (mounts-registry aware) covers the standalone
    case (no ROOT_URLCONF, e.g. a package test harness).
    """
    url = _reverse_or_none("admin:app_list", kwargs={"app_label": app_config.label})
    if url:
        return url
    from stapel_core.django.mounts import admin_index_url

    return f"{admin_index_url()}{app_config.label}/"


def _module_api_urls(app_config: Any) -> tuple[Optional[str], Optional[str]]:
    """(swagger_url, schema_url) for *app_config*.

    Prefers a per-module mount: a module that includes
    :func:`stapel_core.django.openapi.swagger.get_swagger_urls` under its own
    URL namespace (the §37 canon — ``/<mod>/swagger/`` + ``/<mod>/schema/``,
    one schema per module instead of one blob for the whole deployment) gets
    its own links. Today's typical monolith mounts a single deployment-wide
    Swagger instead; falling back to it keeps the link real (never a guess
    at a path that 404s) rather than emitting nothing.
    """
    swagger = _reverse_or_none(f"{app_config.label}:swagger-ui")
    schema = _reverse_or_none(f"{app_config.label}:schema")
    if swagger or schema:
        return swagger, schema
    if not swagger_mounted():
        return None, None
    return current_swagger_url(), _reverse_or_none("schema")


def discover_modules() -> List[ModuleNav]:
    """Every installed Stapel module of this process, admin+API linked.

    Sorted by display name for a stable render order. Safe to call with no
    apps registered (an empty INSTALLED_APPS or a non-Django harness) — with
    no ``django.apps`` app registry to walk, this is not the framework's
    business to reconstruct; ``manage.py`` always has one.
    """
    from django.apps import apps as django_apps

    modules: List[ModuleNav] = []
    for app_config in django_apps.get_app_configs():
        if not _is_stapel_app(app_config):
            continue
        swagger_url, schema_url = _module_api_urls(app_config)
        modules.append(
            ModuleNav(
                key=app_config.label,
                name=getattr(app_config, "verbose_name", app_config.label) or app_config.label,
                admin_url=_module_admin_url(app_config),
                swagger_url=swagger_url,
                schema_url=schema_url,
            )
        )
    modules.sort(key=lambda m: m.name.lower())
    return modules


def build_modules() -> List[dict]:
    """Render-ready module list — the shape the admin template/``/nav`` need."""
    return [
        {
            "key": m.key,
            "name": m.name,
            "admin_url": m.admin_url,
            "swagger_url": m.swagger_url,
            "schema_url": m.schema_url,
        }
        for m in discover_modules()
    ]


__all__ = [
    "SECTIONS",
    "NavConfigError",
    "Service",
    "NavLink",
    "ModuleNav",
    "get_services",
    "swagger_mounted",
    "build_services",
    "current_swagger_url",
    "register_nav_link",
    "unregister_nav_link",
    "clear_nav_links",
    "get_nav_links",
    "nav_sections",
    "current_dashboard_url",
    "discover_modules",
    "build_modules",
]
