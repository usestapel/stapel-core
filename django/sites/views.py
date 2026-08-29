"""``GET …/site/`` — the storefront's first request, before its first paint.

One image serves every host, so the brand cannot be baked in at build time:
the SPA asks who it is at runtime. That makes this endpoint the most public
thing the fleet serves — it must answer a browser with no cookie, no session
and no account, and it must be cacheable, because the alternative is a
per-visitor round trip in front of every page load.

It therefore declares three things explicitly rather than inheriting them:
``AllowAny`` (the deployment's default permission class is usually not),
``authentication_classes = []`` (a stale or malformed cookie must not turn the
brand lookup into a 401), and ``stapel_anonymous_access = ANONYMOUS_ALLOWED``
(the adoption check asks every view to state its stance on guests, and "a
guest is exactly who this is for" is the stance).
"""
from __future__ import annotations

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from stapel_core.django.api.permissions import ANONYMOUS_ALLOWED

from .helpers import request_host, site_registry

__all__ = ["SiteBootstrapView", "site_payload"]

#: Five minutes: long enough that a crawl or a traffic spike does not become a
#: request per page view, short enough that a brand correction is live within
#: one coffee. ``public`` because the answer depends on the ``Host`` and on
#: nothing about the visitor — there is no cookie, no session and no user in it.
CACHE_CONTROL = "public, max-age=300"


def site_payload(request) -> dict:
    """The spec §3.3 body for this request. Pure — no HTTP, easy to assert on."""
    from django.conf import settings

    registry = site_registry()
    host = request_host(request)
    matched = registry.for_host(host) if registry else None
    site = matched or (registry.primary() if registry else None)

    if site is None:
        # Empty registry: the deployment never declared its sites, so there is
        # nothing to report and the storefront stays on its build-time
        # fallback. Reporting a fabricated brand here would be worse than
        # reporting none.
        return {
            "host": host,
            "matched": False,
            "primary": False,
            "locale": getattr(settings, "LANGUAGE_CODE", "") or "",
            "brand": None,
            "seo": {"index": True, "canonical_host": host},
        }

    seo = dict(site.seo)
    seo["index"] = bool(seo.get("index", True))
    seo["canonical_host"] = seo.get("canonical_host") or site.host
    return {
        "host": site.host,
        "matched": matched is not None,
        "primary": bool(site.primary),
        "locale": site.locale,
        "brand": site.brand.as_dict() if site.brand else None,
        "seo": seo,
    }


class SiteBootstrapView(APIView):
    """``GET /<auth-prefix>/api/v1/site/`` — host → brand, for the storefront."""

    permission_classes = [AllowAny]
    authentication_classes = []
    stapel_anonymous_access = ANONYMOUS_ALLOWED

    def get(self, request):
        response = Response(site_payload(request))
        response["Cache-Control"] = CACHE_CONTROL
        return response
