"""Request → site, and the per-request frontend URL that follows from it.

Every place in the fleet that used to reach for ``STAPEL_AUTH["FRONTEND_URL"]``
while holding a request is a place that sends a user of one brand to the other
brand's domain. These two helpers are the replacement, and they are the whole
Django-side vocabulary a module needs.
"""
from __future__ import annotations

import logging
from typing import Optional

from stapel_core.sites import (
    Site,
    SiteRegistry,
    SitesConfigError,
    registry_from_settings,
)

__all__ = ["request_host", "site_for_request", "site_frontend_url", "site_registry"]

logger = logging.getLogger(__name__)

_EMPTY = SiteRegistry()
_warned = False


def site_registry() -> SiteRegistry:
    """This process's registry (cached), or an empty one if it does not parse.

    Fails **soft** on the request path, and only here: ``django/settings.py``
    already decided at boot that a broken registry degrades the deployment to
    single-host, and a request path that raises instead would turn that stated
    degradation into a 500 on every page. The failure is not swallowed — it is
    an E-level ``manage.py check`` finding (``stapel_core.sites.E001``), which
    reads the registry through :func:`registry_from_settings` directly.
    """
    global _warned
    try:
        return registry_from_settings()
    except SitesConfigError as exc:
        if not _warned:
            _warned = True
            logger.warning(
                "stapel-core: the site registry does not parse (%s); serving "
                "single-host. See stapel_core.sites.E001.", exc,
            )
        return _EMPTY


def request_host(request) -> str:
    """``request.get_host()``, or ``""`` when there is no usable host.

    ``get_host()`` raises ``DisallowedHost`` for a host outside
    ``ALLOWED_HOSTS``; a helper that lets that escape would turn a hostile
    ``Host:`` header into a 500 in whatever view called it, so it answers
    "no host" instead — which every caller here already handles.
    """
    getter = getattr(request, "get_host", None)
    if getter is None:
        return ""
    try:
        return getter() or ""
    except Exception:
        return ""


def site_for_request(request) -> Optional[Site]:
    """The site this request is for, falling back to the primary.

    ``None`` only when the registry is empty — that is the single-host
    deployment, and its callers keep their existing settings-derived behaviour
    rather than being handed a fabricated site.

    An unmatched host (a probe, an IP, a host in ``ALLOWED_HOSTS`` but not in
    the registry) resolves to the **primary** site: the deployment has a
    default brand and answering with no brand at all would blank the page.
    """
    registry = site_registry()
    if not registry:
        return None
    return registry.for_host(request_host(request)) or registry.primary()


def site_frontend_url(request, default: str) -> str:
    """``https://<host>`` for the site this request actually arrived on.

    *default* (``STAPEL_AUTH["FRONTEND_URL"]``) is returned for an empty
    registry and for an unmatched host — deliberately **not** the primary
    fallback of :func:`site_for_request`: this value ends up in a magic link
    or a password-reset email, and a link is only safe to mint for a host the
    registry actually recognises.
    """
    registry = site_registry()
    if not registry:
        return default
    site = registry.for_host(request_host(request))
    return f"https://{site.host}" if site else default
