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

import ipaddress
import logging
import threading
import time

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

# Consecutive provider failures that trip the circuit breaker. Once tripped,
# classify_ip returns a local unknown profile for a short window WITHOUT
# calling the provider — so a single unhealthy provider (external API down,
# quota exhausted, blocking socket) cannot pile every request onto it. The
# per-IP negative cache below handles repeat lookups of the *same* address;
# the breaker handles a flood of *distinct* addresses that would each miss
# the per-IP cache.
_BREAKER_THRESHOLD = 5

# provider class name -> [consecutive_failures, open_until_epoch_seconds].
# Guarded by _provider_lock. Reset on setting_changed and by tests.
_breaker: dict[str, list[float]] = {}

# Memoized provider instance (H2): _resolve_provider used to build a NEW
# instance per call, so per-instance caches (MaxMind Reader mmaps/fds) were
# never reused. The instance is shared across worker threads; construction is
# guarded and invalidated on setting_changed.
_provider_lock = threading.Lock()
_provider_instance: NetIntelProvider | None = None


def _reset_state(*, setting=None, **kwargs) -> None:
    """Drop the memoized provider and circuit-breaker state.

    Connected to Django's ``setting_changed`` (a config change starts fresh)
    and callable directly from tests. Only reacts to netintel-relevant
    settings so unrelated ``override_settings`` blocks stay cheap.
    """
    from .conf import netintel_settings

    if setting is not None and setting != netintel_settings.namespace \
            and setting not in netintel_settings.defaults:
        return
    global _provider_instance
    with _provider_lock:
        _provider_instance = None
        _breaker.clear()


try:  # Django present — keep the singleton honest across override_settings.
    from django.test.signals import setting_changed

    setting_changed.connect(_reset_state, weak=False)
except Exception:  # pragma: no cover - Django not importable at import time
    pass


def _cache():
    from django.core.cache import caches

    from .conf import netintel_settings

    return caches[str(netintel_settings.CACHE_ALIAS)]


def _resolve_provider() -> NetIntelProvider:
    """The configured provider (dotted path, class or instance), memoized.

    The instance is built once and reused across calls and threads — this is
    what makes MaxMindProvider's per-instance Reader cache effective. It is
    invalidated on ``setting_changed`` via :func:`_reset_state`.
    """
    global _provider_instance
    instance = _provider_instance
    if instance is not None:
        return instance
    with _provider_lock:
        if _provider_instance is not None:
            return _provider_instance
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
        _provider_instance = value
        return value


def _breaker_is_open(provider_name: str) -> bool:
    """True while *provider_name*'s breaker is tripped (skip the provider)."""
    state = _breaker.get(provider_name)
    if state is None:
        return False
    open_until = state[1]
    if open_until and time.monotonic() < open_until:
        return True
    return False


def _breaker_record_success(provider_name: str) -> None:
    _breaker.pop(provider_name, None)


def _breaker_record_failure(provider_name: str, window: float) -> None:
    state = _breaker.get(provider_name)
    if state is None:
        state = [0.0, 0.0]
        _breaker[provider_name] = state
    state[0] += 1
    if state[0] >= _BREAKER_THRESHOLD:
        state[1] = time.monotonic() + max(1.0, window)


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

    The input is normalized to the canonical compressed address form so that
    equivalent spellings of one address (IPv6 zero-compression, bracketed
    forms, ``%zone`` suffixes) share a single cache key and a single provider
    lookup; a value that is not an IP address fails open without touching the
    provider.
    """
    ip = str(ip) if ip else ""
    if not ip:
        return unknown_profile(ip)
    try:
        ip = ipaddress.ip_address(ip.strip().strip("[]").split("%")[0]).compressed
    except ValueError:
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
        # Circuit breaker: after N consecutive failures, stop hitting an
        # unhealthy provider for a short window and serve a local unknown.
        if _breaker_is_open(provider_name):
            return unknown_profile(ip)
        profile = provider.classify(ip)
        _breaker_record_success(provider_name)
        cache.set(key, profile, timeout=int(netintel_settings.CACHE_TTL))
        return profile
    except Exception as exc:
        _warn_once(provider_name, exc)
        _handle_failure(ip, provider_name)
        return unknown_profile(ip)


def _handle_failure(ip: str, provider_name: str) -> None:
    """Negative-cache the fail-open result and advance the circuit breaker.

    Both steps are best-effort: a failing cache backend or settings read must
    not turn a fail-open into a raise.
    """
    try:
        from .conf import netintel_settings

        negative_ttl = int(netintel_settings.NEGATIVE_CACHE_TTL)
        _breaker_record_failure(provider_name, float(negative_ttl))
        if negative_ttl > 0:
            _cache().set(
                CACHE_KEY_PREFIX + ip, unknown_profile(ip), timeout=negative_ttl
            )
    except Exception:
        logger.debug("netintel negative-cache/breaker update failed", exc_info=True)


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
