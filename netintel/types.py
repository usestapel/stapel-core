"""Value types of the netintel seam — the IP classification vocabulary.

``IpKind`` is deliberately a plain string vocabulary (not a Python enum):
profiles are cached in the Django cache and cross process/version
boundaries, and consumers (challenge matrix, rate limits, analytics) key
plain dicts by these strings.
"""
from __future__ import annotations

from dataclasses import dataclass


class IpKind:
    """Network classes an IP can belong to."""

    RESIDENTIAL = "residential"
    DATACENTER = "datacenter"
    VPN = "vpn"
    TOR = "tor"
    UNKNOWN = "unknown"

    #: Every valid kind, for validation and matrix iteration.
    ALL = (RESIDENTIAL, DATACENTER, VPN, TOR, UNKNOWN)


@dataclass(frozen=True)
class IpProfile:
    """What we know about one IP address.

    ``confidence`` is provider-defined in [0, 1]; ``None`` means the
    provider makes no claim (e.g. the unknown profile).
    """

    ip: str
    kind: str = IpKind.UNKNOWN
    asn: int | None = None
    asn_org: str | None = None
    country: str | None = None
    confidence: float | None = None


def unknown_profile(ip: str) -> IpProfile:
    """The fail-open profile: nothing is known about *ip*."""
    return IpProfile(ip=ip, kind=IpKind.UNKNOWN)


__all__ = ["IpKind", "IpProfile", "unknown_profile"]
