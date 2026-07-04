"""stapel_core.netintel — IP intelligence as a core seam.

``classify_ip(ip) -> IpProfile`` and ``country_of(ip)`` tell the rest of the
framework what kind of network a client comes from (residential ISP,
datacenter/hosting, VPN, Tor) and which country. Consumers: the captcha
challenge policy (``stapel_core.captcha.policy``), OAuth region resolution,
rate-limit policies, analytics bot filtering.

Design rules (docs/geo-network-trust.md §0):

- The provider is a dotted-path replace seam — ``STAPEL_NETINTEL["PROVIDER"]``.
  Default is :class:`~stapel_core.netintel.providers.NullProvider`: an
  unconfigured framework knows nothing and says so.
- Results are cached in the Django cache (``CACHE_ALIAS``/``CACHE_TTL``) —
  classification runs on the hot path of middleware and decorators.
- **Fail-open**: any provider error is logged (once per provider class) and
  yields the unknown profile. ``classify_ip`` never raises to callers —
  a security signal must not take the service down.

TODO: a ``manage.py download_geolite`` management command (GeoLite2
fetch/refresh using the host's MaxMind license key) is planned but out of
scope for this package revision.
"""
from __future__ import annotations

import logging

from .providers import (
    HttpJsonProvider,
    MaxMindProvider,
    NetIntelProvider,
    NullProvider,
)
from .types import IpKind, IpProfile, unknown_profile

logger = logging.getLogger(__name__)

#: Prefix of every netintel cache key.
CACHE_KEY_PREFIX = "stapel-netintel:"

# Provider classes we already warned about — fail-open must not flood logs
# on the hot path. Tests may clear this set directly.
_warned_providers: set[str] = set()


def _cache():
    from django.core.cache import caches

    from .conf import netintel_settings

    return caches[str(netintel_settings.CACHE_ALIAS)]


def _resolve_provider() -> NetIntelProvider:
    """Instantiate the configured provider (dotted path, class or instance)."""
    from .conf import netintel_settings

    value = netintel_settings.PROVIDER
    if isinstance(value, str):
        from django.utils.module_loading import import_string

        value = import_string(value)
    if isinstance(value, type):
        value = value()
    if not isinstance(value, NetIntelProvider):
        raise TypeError(
            f"STAPEL_NETINTEL['PROVIDER'] resolved to {value!r}, "
            "which is not a NetIntelProvider"
        )
    return value


def _warn_once(provider_name: str, exc: Exception) -> None:
    if provider_name in _warned_providers:
        logger.debug("netintel provider %s failed again: %s", provider_name, exc)
        return
    _warned_providers.add(provider_name)
    logger.warning(
        "netintel provider %s failed (%s: %s) — failing open to the unknown "
        "profile; further failures of this provider are logged at DEBUG",
        provider_name, type(exc).__name__, exc,
    )


def classify_ip(ip: str | None) -> IpProfile:
    """Classify *ip*, caching the result. Never raises.

    Empty/None input, provider errors, cache errors and misconfiguration all
    fail open to the unknown profile (with a once-per-provider warning).
    """
    ip = str(ip) if ip else ""
    if not ip:
        return unknown_profile(ip)

    provider_name = "<unresolved>"
    try:
        provider = _resolve_provider()
        provider_name = type(provider).__name__

        from .conf import netintel_settings

        cache = _cache()
        key = CACHE_KEY_PREFIX + ip
        cached = cache.get(key)
        if cached is not None:
            return cached
        profile = provider.classify(ip)
        cache.set(key, profile, timeout=int(netintel_settings.CACHE_TTL))
        return profile
    except Exception as exc:
        _warn_once(provider_name, exc)
        return unknown_profile(ip)


def country_of(ip: str | None) -> str | None:
    """ISO country code of *ip*, or ``None``. Never raises (see classify_ip)."""
    return classify_ip(ip).country


def client_ip(request) -> str | None:
    """The client IP of a Django/DRF request, as netintel should see it.

    By default only ``REMOTE_ADDR`` is trusted. When the deployment sits
    behind a proxy that *strips and re-sets* a client-IP header, point
    ``STAPEL_NETINTEL["TRUSTED_PROXY_HEADER"]`` at its META key (e.g.
    ``"HTTP_X_FORWARDED_FOR"``); the first hop of that header is used.

    .. warning:: Never set ``TRUSTED_PROXY_HEADER`` unless the edge proxy
       overwrites the header on every request — any client can send
       ``X-Forwarded-For`` and spoof a residential address, downgrading
       every network-trust decision built on top of this value.
    """
    if request is None:
        return None
    meta = getattr(request, "META", None) or {}

    from .conf import netintel_settings

    header = netintel_settings.TRUSTED_PROXY_HEADER
    if header:
        for candidate in str(meta.get(header, "")).split(","):
            candidate = candidate.strip()
            if candidate:
                return candidate
    return meta.get("REMOTE_ADDR") or None


__all__ = [
    "CACHE_KEY_PREFIX",
    "HttpJsonProvider",
    "IpKind",
    "IpProfile",
    "MaxMindProvider",
    "NetIntelProvider",
    "NullProvider",
    "classify_ip",
    "client_ip",
    "country_of",
    "unknown_profile",
]
