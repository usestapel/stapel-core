"""Canonical mount registry — where Stapel modules live in *this* deployment.

The framework must never hardcode root-relative URL targets ("/auth/admin/login/",
"/admin/"): every such string silently assumes the module is mounted at the
domain root, and breaks the moment a deployment mounts the whole project under
a path prefix (reverse-proxy sub-path, monolith included under
``path("myproject/", include("core.urls"))``, ``FORCE_SCRIPT_NAME``, …).

Cross-module link targets (admin login, admin index, service navigation) are
therefore *derived* from this registry:

- **local** mounts live in this process's URLconf and are resolved with
  ``django.urls.reverse`` via their URL namespace — reverse follows the
  URLconf wherever the host mounts the module (include-prefix mounting) and
  already prepends the script prefix (``SCRIPT_NAME`` / ``FORCE_SCRIPT_NAME``
  mounting);
- **external** mounts are sibling services behind the same proxy, expressed
  as a path prefix relative to the deployment root; built URLs prepend the
  current script prefix so they too survive sub-path deployments.

Builtin mounts (merge-over-builtins, like every other Stapel registry):

- ``admin`` — local, URL namespace ``admin`` (Django admin of this service);
- ``auth``  — external at ``f"{STAPEL_AUTH_SERVICE_PREFIX}/"`` when that
  setting is non-empty. The default (``"auth"``) preserves the historical
  microservices layout: a dedicated auth service owns the admin login.
  A monolith (no dedicated auth service) sets
  ``STAPEL_AUTH_SERVICE_PREFIX = ""`` and login derives to the local
  ``reverse("admin:login")``.

Override / extend via the ``STAPEL_MOUNTS`` setting (merged over builtins,
``None`` removes a key)::

    STAPEL_MOUNTS = {
        "auth": {"prefix": "sso/", "external": True},   # moved auth service
        "auth": None,                                    # no auth service
        "billing": {"prefix": "billing/", "external": True, "name": "Billing"},
        "studio": {"prefix": "studio/", "namespace": "studio"},  # local module
    }

House convention for modules (MODULE.md → "URL mounting"): a module never
emits an absolute path — only ``reverse()`` / URL names / this registry.
Settings that take URL targets (``LOGIN_URL``, ``LOGIN_REDIRECT_URL``, …)
should be URL *names* (``"admin:index"``) or derived lazily from here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from django.utils.functional import lazy


class MountConfigError(Exception):
    """A STAPEL_MOUNTS entry does not parse — reported as a system-check Error."""


@dataclass(frozen=True)
class Mount:
    """One module mount point of the current deployment."""

    key: str
    #: Path prefix relative to the deployment root — no leading slash,
    #: trailing slash normalized ("auth/"). Empty string = deployment root.
    prefix: str = ""
    #: True — served by another service behind the same proxy (not resolvable
    #: in this process; URLs are built as script_prefix + prefix + suffix).
    external: bool = False
    #: URL namespace for reverse() (local mounts), e.g. "admin".
    namespace: Optional[str] = None
    #: Human-readable label for navigation (feeds future NAV_LINKS / AS-4).
    name: str = ""


def _norm_prefix(prefix: str) -> str:
    prefix = (prefix or "").strip("/")
    return f"{prefix}/" if prefix else ""


def _coerce(key: str, entry: Any) -> Mount:
    if isinstance(entry, Mount):
        return entry
    if isinstance(entry, str):
        return Mount(key=key, prefix=_norm_prefix(entry))
    if isinstance(entry, dict):
        unknown = set(entry) - {"prefix", "external", "namespace", "name"}
        if unknown:
            raise MountConfigError(
                f"STAPEL_MOUNTS[{key!r}]: unknown keys {sorted(unknown)} "
                "(allowed: prefix, external, namespace, name)"
            )
        return Mount(
            key=key,
            prefix=_norm_prefix(entry.get("prefix", "")),
            external=bool(entry.get("external", False)),
            namespace=entry.get("namespace") or None,
            name=entry.get("name", ""),
        )
    raise MountConfigError(
        f"STAPEL_MOUNTS[{key!r}]: expected a dict/str/Mount or None, "
        f"got {type(entry).__name__}"
    )


def _builtin_mounts() -> Dict[str, Mount]:
    from django.conf import settings

    mounts: Dict[str, Mount] = {
        "admin": Mount(key="admin", prefix="admin/", namespace="admin", name="Admin"),
    }
    auth_prefix = getattr(settings, "STAPEL_AUTH_SERVICE_PREFIX", "auth") or ""
    if auth_prefix:
        mounts["auth"] = Mount(
            key="auth", prefix=_norm_prefix(auth_prefix), external=True, name="Auth"
        )
    return mounts


def get_mounts() -> Dict[str, Mount]:
    """Effective mounts: builtins merged with ``settings.STAPEL_MOUNTS``.

    Merge-over-builtins semantics: an overlay entry replaces the builtin of
    the same key; ``None`` removes it. Raises :class:`MountConfigError` on a
    malformed entry (surface it via the system check, not a 500).
    """
    from django.conf import settings

    merged = _builtin_mounts()
    overlay = getattr(settings, "STAPEL_MOUNTS", None) or {}
    if not isinstance(overlay, dict):
        raise MountConfigError(
            f"STAPEL_MOUNTS must be a dict, got {type(overlay).__name__}"
        )
    for key, entry in overlay.items():
        if entry is None:
            merged.pop(key, None)
            continue
        merged[key] = _coerce(key, entry)
    return merged


def get_mount(key: str) -> Optional[Mount]:
    """The effective mount for *key*, or None when absent/removed."""
    return get_mounts().get(key)


def mount_path(key: str, suffix: str = "") -> Optional[str]:
    """Script-prefix-aware absolute path inside a mount.

    ``mount_path("auth", "admin/login/")`` → ``"/auth/admin/login/"`` at the
    domain root, ``"/myproject/auth/admin/login/"`` under
    ``FORCE_SCRIPT_NAME``/``SCRIPT_NAME`` = ``/myproject``. Returns None when
    the mount is absent. Prefix-based — for local mounts prefer
    :func:`mount_reverse` (follows the URLconf, not the declared prefix).
    """
    from django.urls import get_script_prefix

    mount = get_mount(key)
    if mount is None:
        return None
    return f"{get_script_prefix()}{mount.prefix}{suffix.lstrip('/')}"


def mount_reverse(key: str, name: str, **kwargs) -> Optional[str]:
    """``reverse()`` a named URL inside a local mount's namespace.

    Returns None when the mount is absent, external, has no namespace, or
    the name does not reverse (caller decides the fallback).
    """
    from django.urls import reverse

    mount = get_mount(key)
    if mount is None or mount.external or not mount.namespace:
        return None
    try:
        return reverse(f"{mount.namespace}:{name}", **kwargs)
    except Exception:  # NoReverseMatch / unset ROOT_URLCONF — caller falls back
        return None


#: The only sub-surfaces a Stapel module's backend may occupy under its own
#: mount root (BACKLOG §37 canon): ``/<mod>/api/`` (versioned inside),
#: ``/<mod>/swagger/``, ``/<mod>/schema.json``, ``/<mod>/admin/``. A bare
#: ``/<mod>`` root or any other suffix is frontend territory — a reverse
#: proxy that reserves the whole ``/<mod>`` prefix for the backend silently
#: kills the SPA page living there (the incident this registry exists to
#: prevent: nginx reserved the bare ``/calendar`` root, breaking the
#: frontend's calendar page). Fixed by canon, independent of whatever prefix
#: a given deployment mounts the module at.
MODULE_RESERVED_SUFFIXES = ("api/", "swagger/", "schema.json", "admin/")


def reserved_paths() -> Dict[str, list]:
    """§37 reservation, machine-readable: for every Stapel module actually
    installed in *this* process, the sub-surfaces the backend claims under
    that module's own mount root.

    Module discovery mirrors :func:`stapel_core.django.nav.discover_modules`
    (same ``INSTALLED_APPS`` introspection — the ``stapel_module`` marker or
    the published ``stapel_*`` pip-package convention; ``stapel_core`` itself
    always excluded) so this never drifts from what the nav/admin surface
    already shows as "this process's modules". Everything a module mounts
    *outside* :data:`MODULE_RESERVED_SUFFIXES` is frontend territory —
    consumed by ``GET /nav`` (the ``reserved_paths`` field), by deploy-config
    generators (nginx/traefik location blocks should reserve only these
    sub-paths, never the bare module prefix) and by the KB, so all three read
    the one list instead of re-deriving the canon by hand.
    """
    from django.apps import apps as django_apps

    from .nav import is_stapel_app

    return {
        app_config.label: list(MODULE_RESERVED_SUFFIXES)
        for app_config in django_apps.get_app_configs()
        if is_stapel_app(app_config)
    }


def _iter_url_patterns(patterns, prefix: str = ""):
    """Depth-first walk of a URLconf pattern list.

    Yields ``(full_path, url_pattern)`` for every leaf ``URLPattern`` —
    ``full_path`` is the best-effort concatenation of every ancestor route
    string down to this pattern. Good enough to test for the *presence* of a
    canonical path segment (:func:`stapel_core.django.checks.check_module_surface_containment`'s
    only use), not a guarantee of the exact browser-facing URL: ``path()``
    routes concatenate cleanly; a ``re_path()`` ancestor contributes its raw
    regex source (anchors/groups and all) — no stapel module in this
    repository uses ``re_path()`` for its own mount, so this is not a
    practical gap today.
    """
    from django.urls import URLPattern, URLResolver

    for entry in patterns:
        full = f"{prefix}{entry.pattern}"
        if isinstance(entry, URLResolver):
            yield from _iter_url_patterns(entry.url_patterns, full)
        elif isinstance(entry, URLPattern):
            yield full, entry


def _path_segments(full_path: str) -> list:
    """Non-empty ``/``-delimited segments of *full_path*, regex anchors
    stripped — good enough for exact-token membership tests
    (``"api" in segments``), not for reconstructing a real URL."""
    return [seg for seg in full_path.strip("^$").split("/") if seg]


def _callback_owner_app_label(callback) -> Optional[str]:
    """The ``app_label`` of the Stapel module that owns *callback*, or
    ``None`` when it belongs to no installed Stapel module (a host's own
    view, or a third-party one — not this check's business).

    Class-based views keep the class on the view function
    (``view_class`` — plain Django, ``cls`` — DRF's ``APIView.as_view()``);
    function-based views/lambdas are used as-is. Ownership is decided the
    same way module discovery is (``__module__`` dotted-path prefix against
    each installed Stapel app's ``AppConfig.name`` — covers both the
    ``stapel_*`` pip packages and a project's own marked ``apps/*``).
    """
    from django.apps import apps as django_apps

    from .nav import is_stapel_app

    view = getattr(callback, "view_class", None) or getattr(callback, "cls", None) or callback
    module_name = getattr(view, "__module__", "") or ""
    if not module_name:
        return None
    for app_config in django_apps.get_app_configs():
        if not is_stapel_app(app_config):
            continue
        name = app_config.name
        if module_name == name or module_name.startswith(f"{name}."):
            return app_config.label
    return None


def admin_login_url() -> str:
    """The deployment-canonical admin-login path.

    Derivation order:

    1. an **external** ``auth`` mount (dedicated auth service) —
       ``<script_prefix><auth_prefix>admin/login/``;
    2. the locally mounted admin — ``reverse("admin:login")`` (correct under
       any include prefix and any script prefix);
    3. the declared ``admin`` mount prefix (admin not in this URLconf —
       e.g. settings evaluated without URLs);
    4. ``<script_prefix>admin/login/``.

    With default settings this yields exactly the historical value
    ``"/auth/admin/login/"``.
    """
    from django.urls import get_script_prefix

    auth = get_mount("auth")
    if auth is not None and auth.external:
        return f"{get_script_prefix()}{auth.prefix}admin/login/"
    url = mount_reverse("admin", "login")
    if url:
        return url
    admin = get_mount("admin")
    if admin is not None:
        return f"{get_script_prefix()}{admin.prefix}login/"
    return f"{get_script_prefix()}admin/login/"


def admin_index_url() -> str:
    """The deployment-canonical admin-index path (post-login landing).

    Prefers the locally mounted admin (``reverse("admin:index")``) — the
    service the user just logged into — then an external ``auth`` mount's
    admin, then the declared/implicit admin prefix.
    """
    from django.urls import get_script_prefix

    url = mount_reverse("admin", "index")
    if url:
        return url
    auth = get_mount("auth")
    if auth is not None and auth.external:
        return f"{get_script_prefix()}{auth.prefix}admin/"
    admin = get_mount("admin")
    if admin is not None:
        return f"{get_script_prefix()}{admin.prefix}"
    return f"{get_script_prefix()}admin/"


#: Lazy variants for settings modules — evaluated per use, after the URLconf
#: exists: ``LOGIN_URL = lazy_admin_login_url()``.
lazy_admin_login_url = lazy(admin_login_url, str)
lazy_admin_index_url = lazy(admin_index_url, str)


__all__ = [
    "Mount",
    "MountConfigError",
    "get_mounts",
    "get_mount",
    "mount_path",
    "mount_reverse",
    "admin_login_url",
    "admin_index_url",
    "lazy_admin_login_url",
    "lazy_admin_index_url",
    "MODULE_RESERVED_SUFFIXES",
    "reserved_paths",
]
