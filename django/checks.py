"""System checks for URL mounting (tag ``stapel_mounts``).

E-level — an auth-redirect setting (``LOGIN_URL`` / ``LOGOUT_REDIRECT_URL`` /
an explicitly set ``LOGIN_REDIRECT_URL``) that points at a path this
deployment cannot serve is a deploy blocker: it turns every ``login_required``
into a user-facing 404 *after* the redirect, which no smoke test of the page
itself catches. W-level — hints (Django's untouched stock default), never
blocking.

Paths belonging to a declared **external** mount (``STAPEL_MOUNTS`` /
``STAPEL_AUTH_SERVICE_PREFIX`` — a sibling service behind the same proxy)
cannot be verified in-process and are skipped: they are the deployment
contract, not this URLconf's business.
"""
from __future__ import annotations

from django.core import checks

E001_LOGIN_URL_UNRESOLVABLE = "stapel_core.mounts.E001"
E002_REDIRECT_URL_UNRESOLVABLE = "stapel_core.mounts.E002"
E003_BAD_MOUNTS = "stapel_core.mounts.E003"
W001_STOCK_LOGIN_REDIRECT = "stapel_core.mounts.W001"

#: Django's own untouched defaults — flagged W, not E: a service that never
#: redirects there (pure API, no login_required) should not be blocked.
#: Anything *explicitly configured* that doesn't resolve is an Error.
_DJANGO_STOCK_DEFAULTS = {
    "LOGIN_URL": "/accounts/login/",
    "LOGIN_REDIRECT_URL": "/accounts/profile/",
}

_HINT = (
    "URL-target settings must survive any mount prefix: use a URL name "
    "(LOGIN_REDIRECT_URL = 'admin:index'), a lazy derivation "
    "(stapel_core.django.mounts.lazy_admin_login_url()), or declare the "
    "external service mount (STAPEL_AUTH_SERVICE_PREFIX / STAPEL_MOUNTS) "
    "instead of hardcoding a root-relative path."
)


def _external_prefixes() -> list[str]:
    from stapel_core.django.mounts import get_mounts

    return [m.prefix for m in get_mounts().values() if m.external and m.prefix]


def _strip_script_prefix(path: str) -> str:
    """Convert a browser-facing path to a URLconf path (resolve() input)."""
    from django.urls import get_script_prefix

    script_prefix = get_script_prefix()
    if script_prefix != "/" and path.startswith(script_prefix):
        return "/" + path[len(script_prefix):]
    return path


def _target_resolves(value: str) -> bool | None:
    """True/False — the target does/doesn't resolve; None — unverifiable here."""
    from django.urls import NoReverseMatch, Resolver404, resolve, reverse

    if not value:
        return None
    if "://" in value or value.startswith("//"):
        return None  # absolute URL — a cross-host contract, not our URLconf
    if not value.startswith("/"):
        # URL name / namespaced name ("admin:index") — the recommended form.
        try:
            reverse(value)
            return True
        except NoReverseMatch:
            return False
    path = _strip_script_prefix(value)
    for prefix in _external_prefixes():
        if path.startswith(f"/{prefix}"):
            return None  # another service's URL space
    try:
        resolve(path.split("?")[0])
        return True
    except Resolver404:
        return False


@checks.register("stapel_mounts")
def check_mounts_config(app_configs=None, **kwargs):
    """E003 — the STAPEL_MOUNTS merge-registry must parse."""
    from stapel_core.django.mounts import MountConfigError, get_mounts

    try:
        get_mounts()
    except MountConfigError as exc:
        return [checks.Error(
            str(exc),
            hint="Entries are {'prefix': 'auth/', 'external': True, "
                 "'namespace': ..., 'name': ...}, a prefix string, or None "
                 "to remove a builtin mount.",
            id=E003_BAD_MOUNTS,
        )]
    return []


@checks.register("stapel_mounts")
def check_auth_redirect_settings(app_configs=None, **kwargs):
    """E001/E002/W001 — LOGIN_URL & friends must point somewhere that exists."""
    from django.conf import settings

    from stapel_core.django.mounts import MountConfigError

    if not getattr(settings, "ROOT_URLCONF", ""):
        return []  # standalone package harness — nothing to resolve against

    findings = []
    targets = (
        ("LOGIN_URL", E001_LOGIN_URL_UNRESOLVABLE),
        ("LOGOUT_REDIRECT_URL", E002_REDIRECT_URL_UNRESOLVABLE),
        ("LOGIN_REDIRECT_URL", E002_REDIRECT_URL_UNRESOLVABLE),
    )
    for name, check_id in targets:
        raw = getattr(settings, name, None)
        if raw is None:
            continue
        value = str(raw)  # unwrap lazy proxies
        try:
            resolves = _target_resolves(value)
        except MountConfigError:
            continue  # E003 already reported
        if resolves is not False:
            continue
        if value == _DJANGO_STOCK_DEFAULTS.get(name):
            findings.append(checks.Warning(
                f"{name} is Django's stock default {value!r}, which this "
                "URLconf does not serve — fine if nothing redirects there, "
                "a user-facing 404 otherwise.",
                hint=_HINT,
                id=W001_STOCK_LOGIN_REDIRECT,
            ))
            continue
        findings.append(checks.Error(
            f"{name} = {value!r} does not resolve in this deployment "
            "(resolve() found no URL pattern and it matches no declared "
            "external mount) — every redirect there is a user-facing 404.",
            hint=_HINT,
            id=check_id,
        ))
    return findings


__all__ = [
    "E001_LOGIN_URL_UNRESOLVABLE",
    "E002_REDIRECT_URL_UNRESOLVABLE",
    "E003_BAD_MOUNTS",
    "W001_STOCK_LOGIN_REDIRECT",
    "check_auth_redirect_settings",
    "check_mounts_config",
]
