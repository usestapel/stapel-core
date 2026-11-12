"""URL patterns of the sitemap, for splicing into the URLconf that serves the site root::

    from stapel_core.django.sitemap.urls import get_sitemap_urls

    urlpatterns = [..., *get_sitemap_urls()]

``sitemap.xml`` and ``sitemap-<n>.xml`` sit side by side; the index links the
pages relative to wherever it is mounted.
"""
from __future__ import annotations

from django.urls import path

from .views import SitemapIndexView, SitemapPageView

__all__ = ["get_sitemap_urls"]


def get_sitemap_urls() -> list:
    return [
        path("sitemap.xml", SitemapIndexView.as_view(), name="stapel-sitemap-index"),
        path("sitemap-<int:page>.xml", SitemapPageView.as_view(), name="stapel-sitemap-page"),
    ]
