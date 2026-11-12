"""``sitemap.xml`` (index) and ``sitemap-<n>.xml`` (pages) for the request's site.

Public and anonymous by design — a crawler has no cookie — and cacheable: the
answer depends on the ``Host`` only. A site whose registry entry says
``seo.index: false`` answers 404, so a staging brand does not advertise its
URLs.
"""
from __future__ import annotations

from django.http import Http404, HttpResponse
from django.views import View

from stapel_core.django.api.permissions import ANONYMOUS_ALLOWED
from stapel_core.django.sites.helpers import site_for_request

from .build import get_page, get_sitemaps, render_index
from .conf import sitemap_settings

__all__ = ["SitemapIndexView", "SitemapPageView", "site_and_origin"]

CONTENT_TYPE = "application/xml; charset=utf-8"


def site_and_origin(request) -> tuple[str, str]:
    """``(site key, "https://<host>")`` of the request's site.

    Raises 404 for a site that asks not to be indexed. With no registry the
    key is ``""`` and the origin is ``ORIGIN``, else the request's own.
    """
    site = site_for_request(request)
    if site is not None:
        if site.seo.get("index", True) is False:
            raise Http404("this site is not indexed")
        return site.key, f"https://{site.host}"
    origin = str(sitemap_settings.ORIGIN or "").rstrip("/")
    if not origin:
        origin = f"{request.scheme}://{request.get_host()}"
    return "", origin


def _response(body: str) -> HttpResponse:
    response = HttpResponse(body, content_type=CONTENT_TYPE)
    response["Cache-Control"] = f"public, max-age={max(0, int(sitemap_settings.CACHE_TTL))}"
    return response


class SitemapIndexView(View):
    """The sitemap index: one ``<sitemap>`` per page, next to this URL."""

    stapel_anonymous_access = ANONYMOUS_ALLOWED

    def get(self, request):
        site_key, origin = site_and_origin(request)
        built = get_sitemaps(site_key, origin)
        base = origin + request.path.rsplit("/", 1)[0]
        page_urls = [f"{base}/sitemap-{n}.xml" for n in range(1, built.count + 1)]
        return _response(render_index(page_urls, built.lastmods))


class SitemapPageView(View):
    """One page of ``<url>`` entries (``page`` is 1-based)."""

    stapel_anonymous_access = ANONYMOUS_ALLOWED

    def get(self, request, page: int):
        site_key, origin = site_and_origin(request)
        xml = get_page(site_key, get_sitemaps(site_key, origin), int(page))
        if xml is None:
            raise Http404("no such sitemap page")
        return _response(xml)
