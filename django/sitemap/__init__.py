"""Site-scoped sitemap aggregator over ``<module>.sitemap_entries`` Functions.

Modules that own public URLs (CMS pages, listings, profiles) each provide a
comm Function answering the provider contract in :mod:`.build`; core asks
every registered one, per site, and serves a standards-compliant sitemap
index plus paged sitemaps (50 000 URLs / 50 MiB per file), cached. No module
is imported: a provider is a Function name in a MERGE registry::

    # in the provider module's AppConfig.ready()
    from stapel_core.django.sitemap import register_sitemap_provider
    register_sitemap_provider("cms.sitemap_entries")

    # or in the host's settings (None removes one)
    STAPEL_SITEMAP = {"PROVIDERS": {"cms": "cms.sitemap_entries"}}

    # in the URLconf that serves the site root
    from stapel_core.django.sitemap.urls import get_sitemap_urls
    urlpatterns += get_sitemap_urls()

A provider that does not answer is skipped (logged) and the partial set is
cached for ``PARTIAL_CACHE_TTL`` only.
"""
from __future__ import annotations

from .build import (
    SitemapSet,
    build_sitemaps,
    collect_entries,
    get_page,
    get_sitemaps,
    invalidate_sitemaps,
    register_sitemap_provider,
    render_index,
    render_pages,
    sitemap_providers,
    unregister_sitemap_provider,
)

__all__ = [
    "SitemapIndexView",
    "SitemapPageView",
    "SitemapSet",
    "build_sitemaps",
    "collect_entries",
    "get_page",
    "get_sitemap_urls",
    "get_sitemaps",
    "invalidate_sitemaps",
    "register_sitemap_provider",
    "render_index",
    "render_pages",
    "sitemap_providers",
    "unregister_sitemap_provider",
]


def __getattr__(name):
    if name in ("SitemapIndexView", "SitemapPageView"):
        from . import views

        return getattr(views, name)
    if name == "get_sitemap_urls":
        from .urls import get_sitemap_urls

        return get_sitemap_urls
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
