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

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from stapel_core.django.api.permissions import ANONYMOUS_ALLOWED

from .helpers import request_host, site_registry

__all__ = ["SiteBootstrapSerializer", "SiteBootstrapView", "site_payload"]

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


class SiteBrandSerializer(serializers.Serializer):
    """What the storefront paints on this host (``stapel_core.sites.Brand``)."""

    key = serializers.CharField(help_text="Stable brand key")
    name = serializers.CharField(help_text="Brand name")
    title = serializers.CharField(help_text="Document title; may be empty")
    logo = serializers.CharField(help_text="Logo URL or asset path; may be empty")
    theme = serializers.CharField(help_text="Theme key; may be empty")
    legal = serializers.DictField(
        child=serializers.CharField(), help_text="Legal texts/links by key"
    )


class SiteSeoSerializer(serializers.Serializer):
    """Indexing hints; a site may declare further keys, passed through as-is."""

    index = serializers.BooleanField(help_text="Whether crawlers may index this host")
    canonical_host = serializers.CharField(help_text="Host canonical URLs point at")


class SiteBootstrapSerializer(serializers.Serializer):
    """Declaration of :func:`site_payload` for the contract; never used to render."""

    host = serializers.CharField(help_text="The matched site's host (or the request host)")
    matched = serializers.BooleanField(help_text="The request host is a declared site or alias")
    primary = serializers.BooleanField(help_text="The answering site is the primary one")
    locale = serializers.CharField(help_text="The site's locale; may be empty")
    brand = SiteBrandSerializer(
        allow_null=True, help_text="null when the deployment declares no sites"
    )
    seo = SiteSeoSerializer()


class SiteBootstrapView(APIView):
    """``GET /<auth-prefix>/api/v1/site/`` — host → brand, for the storefront."""

    permission_classes = [AllowAny]
    authentication_classes = []
    stapel_anonymous_access = ANONYMOUS_ALLOWED

    @extend_schema(responses={200: SiteBootstrapSerializer})
    def get(self, request):
        response = Response(site_payload(request))
        response["Cache-Control"] = CACHE_CONTROL
        return response
