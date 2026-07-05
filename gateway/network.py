"""Network identity check — "a request about project X comes from the
network of project X's container".

The check is the third authorization factor (system-design §5.9: the
project ID is public addressing, the scope token is the right to speak,
the network identity ties the two to a physical caller). It is a seam:
``STAPEL_GATEWAY["NETWORK_VERIFIER"]`` is a dotted path to
``callable(ip: str | None, token: ScopeToken) -> bool``; Studio's
deployment can swap in a verifier that asks the container-manager which
container owns a source address right now.

The default verifier enforces the binding recorded on the token at
issuance (exact IP or CIDR, IPv4/IPv6 via :mod:`ipaddress`). A token
without a binding passes only while ``REQUIRE_NETWORK_BINDING`` is off;
switching it on makes unpinned tokens unusable over HTTP — the strict
posture for real container fleets.

Only ``REMOTE_ADDR`` is consulted — never a forwarded-for header: those
are caller-controlled unless a trusted proxy rewrites them, and trusting
a proxy is a deployment decision that belongs in a custom verifier.
"""
from __future__ import annotations

import ipaddress

from .conf import gateway_settings


def default_verifier(ip: str | None, token) -> bool:
    bound = getattr(token, "network", None)
    if not bound:
        return not bool(gateway_settings.REQUIRE_NETWORK_BINDING)
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    try:
        if "/" in bound:
            return addr in ipaddress.ip_network(bound, strict=False)
        return addr == ipaddress.ip_address(bound)
    except ValueError:
        # A malformed binding never degrades into "allow".
        return False


def verify_network(ip: str | None, token) -> bool:
    verifier = gateway_settings.NETWORK_VERIFIER
    return bool(verifier(ip, token))


__all__ = ["default_verifier", "verify_network"]
