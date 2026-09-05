"""System check (tag ``stapel_error_pages``) — API urls without the JSON
error-page middleware.

A deployment that mounts routes under a configured API prefix
(``STAPEL_CORE["API_PREFIXES"]``, see
:mod:`stapel_core.django.api.error_pages`) but never installs
``ApiErrorPagesMiddleware`` gets Django's HTML "Not Found"/"Method Not
Allowed" pages on those routes for an unmatched path or wrong verb — the
defect the middleware exists to close. The middleware already ships inside
``stapel_core.django.settings.COMMON_MIDDLEWARE``, so a service on the shared
preset gets it for free; this check is for the project that assembled its own
``MIDDLEWARE`` list by hand (or trimmed the preset) and dropped it without
noticing, since nothing else says so — a missing middleware entry produces no
traceback, only a wrong response shape the day something asks for a path that
does not exist.
"""
from __future__ import annotations

from django.core import checks

from stapel_core.django.api.error_pages import MIDDLEWARE_PATH, is_api_path

W001_MIDDLEWARE_ABSENT = "stapel_core.error_pages.W001"


def _has_api_surface() -> bool:
    """True when this deployment's URLconf mounts anything under an API
    prefix. Uses :func:`stapel_core.django.urlsurvey.iter_surface`, which is
    itself a no-op (yields nothing) when the process has no ``ROOT_URLCONF``
    — a standalone package test harness — so this check needs no such guard
    of its own.
    """
    from stapel_core.django.urlsurvey import iter_surface

    for entry in iter_surface():
        full_path = entry.full_path
        if not full_path.startswith("/"):
            full_path = f"/{full_path}"
        if is_api_path(full_path):
            return True
    return False


@checks.register("stapel_error_pages")
def check_api_error_pages_middleware(app_configs=None, **kwargs):
    """W001 — API urls exist but ``ApiErrorPagesMiddleware`` is not wired in."""
    from django.conf import settings

    if MIDDLEWARE_PATH in (getattr(settings, "MIDDLEWARE", None) or []):
        return []
    if not _has_api_surface():
        return []
    return [checks.Warning(
        "This project mounts URLs under a configured API prefix "
        "(STAPEL_CORE['API_PREFIXES']) but "
        f"'{MIDDLEWARE_PATH}' is not in MIDDLEWARE: an unknown path or "
        "wrong method under those routes answers Django's HTML error page "
        "instead of the fleet's JSON envelope, so an API client or a probe "
        "parses HTML.",
        hint=f"Add '{MIDDLEWARE_PATH}' to MIDDLEWARE — it already ships in "
             "stapel_core.django.settings.COMMON_MIDDLEWARE for services on "
             "the shared settings preset.",
        id=W001_MIDDLEWARE_ABSENT,
    )]


__all__ = [
    "W001_MIDDLEWARE_ABSENT",
    "check_api_error_pages_middleware",
]
