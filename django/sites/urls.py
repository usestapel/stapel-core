"""URL patterns for the site bootstrap.

Shipped as a function rather than a ``urlpatterns`` list so the mounting
service decides the prefix while the *path* stays fleet-wide identical. The
fleet mounts it inside stapel-auth's ``urls_v1``, which makes the address
``/<auth-prefix>/api/v1/site/`` in every deployment — a storefront can hardcode
one relative URL and be right everywhere.
"""
from __future__ import annotations

from django.urls import path

from .views import SiteBootstrapView

__all__ = ["get_site_urls"]


def get_site_urls() -> list:
    """The bootstrap route, for splicing into a service's URLconf::

        from stapel_core.django.sites.urls import get_site_urls

        urlpatterns = [..., *get_site_urls()]
    """
    return [path("site/", SiteBootstrapView.as_view(), name="site-bootstrap")]
