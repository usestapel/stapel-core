"""The client IP of a request, and which proxy header is trusted for it.

Every IP-keyed decision in the fleet (rate limits, lockouts, audit and
session addresses, captcha ``remoteip``, IP classification) reads the
address through :func:`client_ip`, so the trust model lives in one place.

Configuration: ``STAPEL_SECURITY["TRUSTED_PROXY_HEADER"]``. The previous
home of the key, ``STAPEL_NETINTEL["TRUSTED_PROXY_HEADER"]``, is still
honoured when the new key is absent (``stapel_core.security.W001`` asks to
move it); when both are set the new key wins (``W002`` if they differ).
"""
from __future__ import annotations

from .conf import security_settings

KEY = "TRUSTED_PROXY_HEADER"
LEGACY_NAMESPACE = "STAPEL_NETINTEL"


def trusted_proxy_header() -> str | None:
    """META key of the trusted client-IP header, or ``None`` (REMOTE_ADDR only).

    Resolution: ``STAPEL_SECURITY`` dict → ``STAPEL_NETINTEL`` dict (legacy)
    → flat ``TRUSTED_PROXY_HEADER`` setting → ``None``. Never the process
    environment: a stray variable must not decide which header is believed.
    """
    from django.conf import settings

    current = getattr(settings, security_settings.namespace, None) or {}
    if KEY not in current:
        legacy = getattr(settings, LEGACY_NAMESPACE, None) or {}
        if KEY in legacy:
            return legacy[KEY]
    return security_settings.TRUSTED_PROXY_HEADER


def client_ip(request) -> str | None:
    """The client IP of a Django/DRF request.

    By default only ``REMOTE_ADDR`` is trusted. When the deployment sits
    behind a proxy that *strips and re-sets* a client-IP header, point
    ``STAPEL_SECURITY["TRUSTED_PROXY_HEADER"]`` at its META key (e.g.
    ``"HTTP_X_REAL_IP"``); the first hop of that header is used.

    .. warning:: Never set ``TRUSTED_PROXY_HEADER`` unless the edge proxy
       overwrites the header on every request — any client can send
       ``X-Forwarded-For`` and spoof an address, downgrading every
       IP-keyed decision built on top of this value.
    """
    if request is None:
        return None
    meta = getattr(request, "META", None) or {}

    header = trusted_proxy_header()
    if header:
        for candidate in str(meta.get(header, "")).split(","):
            candidate = candidate.strip()
            if candidate:
                return candidate
    return meta.get("REMOTE_ADDR") or None


__all__ = ["client_ip", "trusted_proxy_header"]
