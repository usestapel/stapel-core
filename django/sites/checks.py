"""Deploy-time gates for the site registry (tag ``stapel_sites``).

The registry decides which hosts this deployment answers to, which origins may
open a socket and where a login link points. Every failure mode below is
invisible from inside a running process — a malformed registry degrades to "no
sites", which looks exactly like a single-host deployment until the second host
404s — so they are answered at ``manage.py check`` time.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from django.core import checks

from stapel_core.django.check_guard import (
    SecurityCriticalError,
    declare_security_critical,
)
from stapel_core.sites import (
    SitesConfigError,
    registrable_domain,
    registry_from_settings,
)

E001_BAD_SITES = "stapel_core.sites.E001"
E002_PRIMARY_RULE = "stapel_core.sites.E002"

#: The id IS its security-critical declaration, so no blanket
#: SILENCED_SYSTEM_CHECKS line can mute it (stapel_core.django.check_guard).
E003_COOKIE_DOMAIN_SPANS_SITES = declare_security_critical(
    "stapel_core.sites.E003",
    "a shared cookie Domain= across two registrable domains is cookie-tossing: "
    "either domain's subdomains can write a cookie the other domain reads",
)

W001_FRONTEND_URL_UNREGISTERED = "stapel_core.sites.W001"

__all__ = [
    "E001_BAD_SITES",
    "E002_PRIMARY_RULE",
    "E003_COOKIE_DOMAIN_SPANS_SITES",
    "W001_FRONTEND_URL_UNREGISTERED",
    "check_sites_registry",
    "check_cookie_domain_scope",
    "check_frontend_url_host",
]

_SHAPE_HINT = (
    'STAPEL_SITES is {"sites": [{"host": "example.com", "aliases": '
    '["www.example.com"], "primary": true, "locale": "ru", "brand": {"key": '
    '"acme", "name": "…", "theme": "acme", "logo": "/brand/acme/logo.svg", '
    '"legal": {…}}, "seo": {"index": true}}]} — as a Django setting, or as '
    "JSON in the file named by STAPEL_SITES_FILE / inline in STAPEL_SITES_JSON."
)


def _registry_or_error():
    """Load the registry; return ``(registry, SitesConfigError|None)``."""
    try:
        return registry_from_settings(), None
    except SitesConfigError as exc:
        return None, exc


@checks.register("stapel_sites")
def check_sites_registry(app_configs=None, **kwargs):
    """E001/E002 — the registry must parse, and it must name one primary.

    E002 is split out from E001 because it is the rule an operator breaks by
    adding a second brand and forgetting the flag, and it has a one-word fix.
    Both are E-level: a registry that fails to load leaves the deployment
    silently single-host, which is the second brand being down.
    """
    _registry, exc = _registry_or_error()
    if exc is None:
        return []
    if getattr(exc, "code", None) == "primary":
        return [checks.Error(
            str(exc),
            hint='Mark exactly one site "primary": true. It is the fallback '
                 "for an unmatched Host header and the site used where there "
                 "is no request at all (an email minted in a Celery task).",
            id=E002_PRIMARY_RULE,
        )]
    return [checks.Error(str(exc), hint=_SHAPE_HINT, id=E001_BAD_SITES)]


@checks.register("stapel_sites")
def check_cookie_domain_scope(app_configs=None, **kwargs):
    """E003 — one ``Domain=`` cookie cannot cover two registrable domains.

    ``JWT_COOKIE_DOMAIN`` writes ``Domain=`` onto the session cookies. With the
    registry spanning more than one registrable domain that setting is not
    merely useless (a cookie scoped to ``example.com`` is never sent to
    ``example.org`` — browsers enforce that, and no setting changes it): it is a
    **cookie-tossing** risk, because any subdomain of the named domain can then
    write a cookie the apex reads. Host-only cookies (``JWT_COOKIE_DOMAIN =
    None``, the shipped default) are the invariant that makes multi-brand safe.
    """
    from django.conf import settings

    domain = getattr(settings, "JWT_COOKIE_DOMAIN", None)
    if not domain:
        return []
    registry, _exc = _registry_or_error()
    if registry is None or not registry:
        return []
    domains = sorted({registrable_domain(h) for h in registry.hosts()})
    if len(domains) < 2:
        return []
    return [SecurityCriticalError(
        f"JWT_COOKIE_DOMAIN={domain!r} is set while the site registry spans "
        f"{len(domains)} registrable domains ({', '.join(domains)}). A shared "
        "Domain= cookie cannot cover two registrable domains — the browser "
        "will never send it to the other one — and scoping the cookie to a "
        "domain instead of a host opens cookie-tossing: any subdomain of that "
        "domain can write a cookie the apex accepts as its own session.",
        hint="Leave JWT_COOKIE_DOMAIN unset (None) — the shipped default. "
             "Cookies stay host-only, each brand gets its own first-party "
             "session, and one user account still spans both hosts.",
        id=E003_COOKIE_DOMAIN_SPANS_SITES,
    )]


@checks.register("stapel_sites")
def check_frontend_url_host(app_configs=None, **kwargs):
    """W001 — ``STAPEL_AUTH["FRONTEND_URL"]`` should name a registered host.

    Links minted without a request (an email from a Celery task) fall back to
    ``FRONTEND_URL``; pointing it at a host the registry does not know means
    those links land on a host this deployment does not claim to serve.
    W-level: a staging deployment legitimately runs with the registry of
    production hosts and a local frontend.
    """
    from django.conf import settings

    registry, _exc = _registry_or_error()
    if registry is None or not registry:
        return []
    auth = getattr(settings, "STAPEL_AUTH", None)
    if not isinstance(auth, dict) or "FRONTEND_URL" not in auth:
        return []
    frontend_url = auth.get("FRONTEND_URL") or ""
    if not frontend_url:
        return []
    host = (urlsplit(str(frontend_url)).hostname or "").lower()
    if host and registry.for_host(host) is not None:
        return []
    return [checks.Warning(
        f"STAPEL_AUTH['FRONTEND_URL']={frontend_url!r} points at "
        f"{host or 'no host'}, which is not in the site registry "
        f"({', '.join(registry.hosts())}). Links minted outside a request "
        "(emails sent from a worker) use this value, so they will send users "
        "to a host this deployment does not serve.",
        hint="Point FRONTEND_URL at the primary site "
             "(https://<primary host>), or add that host to STAPEL_SITES. "
             "Links minted inside a request already follow the request's own "
             "host via stapel_core.django.sites.site_frontend_url().",
        id=W001_FRONTEND_URL_UNREGISTERED,
    )]
