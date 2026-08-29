"""The Django half of the site registry (:mod:`stapel_core.sites`).

Request → site (:mod:`~stapel_core.django.sites.helpers`), the public bootstrap
the storefront reads before its first paint
(:mod:`~stapel_core.django.sites.views`, mounted through
:func:`~stapel_core.django.sites.urls.get_site_urls`), and the deploy-time
gates (:mod:`~stapel_core.django.sites.checks`).

The view and the URLs are exposed lazily: importing this package must not drag
DRF into a settings module or a management command that only wanted
``site_for_request``.
"""
from __future__ import annotations

from django.core.signals import setting_changed
from django.dispatch import receiver

from stapel_core.sites import reset_sites_cache

from .helpers import site_for_request, site_frontend_url, site_registry

__all__ = [
    "SiteBootstrapView",
    "get_site_urls",
    "site_for_request",
    "site_frontend_url",
    "site_registry",
]


@receiver(setting_changed, dispatch_uid="stapel_core.sites.reset_cache")
def _reset_registry_on_setting_change(sender, setting=None, **kwargs):
    """A test that overrides ``STAPEL_SITES`` must get the overridden registry.

    The registry is parsed once per process on purpose (it is read on every
    request), which makes an override invisible without this — the shape where
    a test passes against a cached answer nobody set.
    """
    if setting == "STAPEL_SITES":
        reset_sites_cache()


def __getattr__(name):
    if name == "SiteBootstrapView":
        from .views import SiteBootstrapView

        return SiteBootstrapView
    if name == "get_site_urls":
        from .urls import get_site_urls

        return get_site_urls
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
