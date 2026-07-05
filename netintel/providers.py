"""NetIntel providers — where IP knowledge actually comes from.

The provider is a single-strategy replace seam (``STAPEL_NETINTEL["PROVIDER"]``,
dotted path). Built-ins:

- ``NullProvider`` — always unknown; the default.
- ``MaxMindProvider`` — offline GeoLite2/GeoIP2 mmdb lookups (optional extra
  ``stapel-core[netintel-maxmind]``).
- ``HttpJsonProvider`` — generic HTTP JSON lookup covering ipinfo/IPQS-style
  APIs via a response-mapper seam, so we don't ship N thin adapters.

Providers may raise: callers go through ``stapel_core.netintel.classify_ip``,
which caches and fails open to the unknown profile.
"""
from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod

from .types import IpKind, IpProfile, unknown_profile

logger = logging.getLogger(__name__)

#: Small builtin heuristic list of well-known hosting/cloud/CDN ASNs. It is
#: deliberately NOT complete and is only a fallback: the accurate signal is an
#: offline MaxMind Anonymous-IP database (``is_hosting_provider``), which the
#: kind-derivation consults first. Extend per deployment via
#: ``STAPEL_NETINTEL["EXTRA_DATACENTER_ASNS"]``.
#:
#: Note on scope: only ASNs that serve *exclusively* infra/CDN egress belong
#: here. AS15169 (Google's main ASN) is intentionally absent — it also carries
#: consumer/residential Google traffic, so flagging it as datacenter would
#: mis-tier real users; AS396982 (Google Cloud) is the datacenter-only ASN.
HOSTING_ASNS = frozenset({
    16509,   # Amazon AWS
    14618,   # Amazon AES
    7224,    # Amazon AWS (additional)
    8075,    # Microsoft Azure
    8068,    # Microsoft (Azure / corp range)
    396982,  # Google Cloud (datacenter-only; NOT AS15169, see note above)
    13335,   # Cloudflare
    54113,   # Fastly
    14061,   # DigitalOcean
    16276,   # OVH
    24940,   # Hetzner
    63949,   # Linode / Akamai
    20473,   # Vultr / Choopa
    45102,   # Alibaba Cloud
    31898,   # Oracle Cloud
    51167,   # Contabo
    60781,   # Leaseweb
    9009,    # M247
})

#: Substrings that mark an ASN organization name as a hosting company.
_HOSTING_ORG_KEYWORDS = (
    "hosting",
    "datacenter",
    "data center",
    "cloud",
    "vps",
    "colocation",
    "dedicated server",
)


class NetIntelProvider(ABC):
    """Contract of an IP intelligence source."""

    @abstractmethod
    def classify(self, ip: str) -> IpProfile:
        """Return everything this provider knows about *ip*."""

    def country(self, ip: str) -> str | None:
        """ISO country code of *ip* (default: taken from ``classify``)."""
        return self.classify(ip).country


class NullProvider(NetIntelProvider):
    """Knows nothing — the default.

    An unconfigured framework must not pretend to know what kind of
    network an IP belongs to.
    """

    def classify(self, ip: str) -> IpProfile:
        return unknown_profile(ip)


class MaxMindProvider(NetIntelProvider):
    """Offline GeoLite2/GeoIP2 mmdb lookups via the ``geoip2`` package.

    Requires the optional extra: ``pip install stapel-core[netintel-maxmind]``.
    Database paths come from the constructor or, when omitted, from
    ``STAPEL_NETINTEL["MAXMIND_ASN_DB" / "MAXMIND_COUNTRY_DB" /
    "MAXMIND_ANONYMOUS_DB"]``; any of them may be absent — only the
    configured databases are consulted.

    Kind derivation, most authoritative first:

    1. Anonymous-IP flags: ``is_tor_exit_node`` → tor, ``is_anonymous_vpn``
       → vpn, ``is_hosting_provider`` → datacenter.
    2. ASN in the builtin ``HOSTING_ASNS`` list ∪ ``EXTRA_DATACENTER_ASNS``
       → datacenter.
    3. ASN organization name containing a hosting keyword → datacenter.
    4. ASN known at all → residential; otherwise unknown.
    """

    def __init__(
        self,
        asn_db: str | None = None,
        country_db: str | None = None,
        anonymous_db: str | None = None,
        extra_datacenter_asns: list[int] | None = None,
    ) -> None:
        self._asn_db = asn_db
        self._country_db = country_db
        self._anonymous_db = anonymous_db
        self._extra_datacenter_asns = extra_datacenter_asns
        self._readers: dict[str, object] = {}
        # The provider is memoized module-level (netintel._resolve_provider)
        # and therefore shared across worker threads. geoip2 Reader objects
        # are safe for concurrent reads, but this lazy open is a
        # check-then-set that two threads could race — guard it so each mmdb
        # Reader (mmap + file descriptor) is opened exactly once.
        self._reader_lock = threading.Lock()

    @staticmethod
    def _import_geoip2():
        try:
            import geoip2.database
            import geoip2.errors
        except ImportError as exc:
            raise ImportError(
                "MaxMindProvider requires the 'geoip2' package. "
                "Install it with: pip install stapel-core[netintel-maxmind]"
            ) from exc
        return geoip2.database, geoip2.errors

    def _reader(self, path: str, database_mod):
        reader = self._readers.get(path)
        if reader is None:
            with self._reader_lock:
                reader = self._readers.get(path)
                if reader is None:
                    reader = database_mod.Reader(path)
                    self._readers[path] = reader
        return reader

    def _datacenter_asns(self) -> set[int]:
        extra = self._extra_datacenter_asns
        if extra is None:
            from .conf import netintel_settings

            extra = netintel_settings.EXTRA_DATACENTER_ASNS or []
        return set(HOSTING_ASNS) | {int(a) for a in extra}

    def classify(self, ip: str) -> IpProfile:
        from .conf import netintel_settings

        database_mod, errors_mod = self._import_geoip2()
        asn_db = self._asn_db or netintel_settings.MAXMIND_ASN_DB
        country_db = self._country_db or netintel_settings.MAXMIND_COUNTRY_DB
        anonymous_db = self._anonymous_db or netintel_settings.MAXMIND_ANONYMOUS_DB

        is_vpn = is_tor = is_hosting = False
        asn: int | None = None
        asn_org: str | None = None
        country: str | None = None

        if anonymous_db:
            try:
                record = self._reader(anonymous_db, database_mod).anonymous_ip(ip)
                is_vpn = bool(getattr(record, "is_anonymous_vpn", False))
                is_tor = bool(getattr(record, "is_tor_exit_node", False))
                is_hosting = bool(getattr(record, "is_hosting_provider", False))
            except errors_mod.AddressNotFoundError:
                pass
        if asn_db:
            try:
                record = self._reader(asn_db, database_mod).asn(ip)
                asn = record.autonomous_system_number
                asn_org = record.autonomous_system_organization
            except errors_mod.AddressNotFoundError:
                pass
        if country_db:
            try:
                record = self._reader(country_db, database_mod).country(ip)
                country = record.country.iso_code
            except errors_mod.AddressNotFoundError:
                pass

        kind, confidence = self._derive_kind(is_vpn, is_tor, is_hosting, asn, asn_org)
        return IpProfile(
            ip=ip, kind=kind, asn=asn, asn_org=asn_org,
            country=country, confidence=confidence,
        )

    def _derive_kind(self, is_vpn, is_tor, is_hosting, asn, asn_org):
        if is_tor:
            return IpKind.TOR, 0.95
        if is_vpn:
            return IpKind.VPN, 0.95
        if is_hosting:
            return IpKind.DATACENTER, 0.9
        if asn is not None and asn in self._datacenter_asns():
            return IpKind.DATACENTER, 0.8
        if asn_org and any(k in asn_org.lower() for k in _HOSTING_ORG_KEYWORDS):
            return IpKind.DATACENTER, 0.6
        if asn is not None:
            return IpKind.RESIDENTIAL, 0.5
        return IpKind.UNKNOWN, None


def default_response_mapper(data: dict, ip: str) -> IpProfile:
    """Map an ipinfo/IPQS-style JSON payload to an ``IpProfile``.

    Recognizes the common field spellings; anything exotic goes through
    ``STAPEL_NETINTEL["HTTP_RESPONSE_MAPPER"]`` instead.
    """
    country = data.get("country") or data.get("country_code") or None

    asn_raw = data.get("asn", data.get("ASN"))
    asn_org = data.get("asn_org") or data.get("organization") or None
    org = data.get("org")
    if isinstance(asn_raw, dict):  # ipinfo "asn": {"asn": "AS123", "name": ...}
        asn_org = asn_org or asn_raw.get("name")
        asn_raw = asn_raw.get("asn")
    if asn_raw is None and isinstance(org, str) and org.upper().startswith("AS"):
        head, _, tail = org.partition(" ")  # ipinfo "org": "AS15169 Google LLC"
        asn_raw, asn_org = head, asn_org or (tail or None)
    elif asn_org is None and isinstance(org, str):
        asn_org = org
    asn = None
    if asn_raw is not None:
        digits = str(asn_raw).upper().removeprefix("AS")
        if digits.isdigit():
            asn = int(digits)

    def _flag(*names):
        for name in names:
            if bool(data.get(name)):
                return True
        return False

    if _flag("tor", "is_tor", "is_tor_exit_node"):
        kind = IpKind.TOR
    elif _flag("vpn", "is_vpn", "proxy", "is_proxy"):
        kind = IpKind.VPN
    elif _flag("hosting", "is_hosting", "datacenter", "is_datacenter"):
        kind = IpKind.DATACENTER
    elif data.get("connection_type", "").lower() == "residential":
        kind = IpKind.RESIDENTIAL
    else:
        kind = IpKind.UNKNOWN

    confidence = data.get("confidence")
    return IpProfile(
        ip=ip, kind=kind, asn=asn, asn_org=asn_org, country=country,
        confidence=float(confidence) if confidence is not None else None,
    )


class HttpJsonProvider(NetIntelProvider):
    """Generic HTTP JSON lookup — one adapter for ipinfo/IPQS-style APIs.

    ``STAPEL_NETINTEL`` keys (constructor arguments take precedence):

    - ``HTTP_URL_TEMPLATE`` — e.g. ``"https://ipinfo.io/{ip}/json"``.
    - ``HTTP_API_KEY`` — sent as ``Authorization: Bearer <key>`` when set.
    - ``HTTP_RESPONSE_MAPPER`` — dotted path (or callable) of
      ``mapper(data: dict, ip: str) -> IpProfile``; default is
      :func:`default_response_mapper`.

    Network errors and non-2xx responses raise — ``classify_ip`` fails open.
    """

    timeout = 5.0

    def __init__(
        self,
        url_template: str | None = None,
        api_key: str | None = None,
        response_mapper=None,
    ) -> None:
        self._url_template = url_template
        self._api_key = api_key
        self._response_mapper = response_mapper

    def _mapper(self):
        mapper = self._response_mapper
        if mapper is None:
            from .conf import netintel_settings

            mapper = netintel_settings.HTTP_RESPONSE_MAPPER
        if mapper is None:
            return default_response_mapper
        if isinstance(mapper, str):
            from django.utils.module_loading import import_string

            mapper = import_string(mapper)
        return mapper

    def classify(self, ip: str) -> IpProfile:
        import requests

        from .conf import netintel_settings

        url_template = self._url_template or netintel_settings.HTTP_URL_TEMPLATE
        if not url_template:
            raise ValueError(
                "HttpJsonProvider needs STAPEL_NETINTEL['HTTP_URL_TEMPLATE'] "
                "(a URL with an {ip} placeholder)"
            )
        api_key = self._api_key or netintel_settings.HTTP_API_KEY

        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        response = requests.get(
            url_template.format(ip=ip), headers=headers, timeout=self.timeout
        )
        response.raise_for_status()
        return self._mapper()(response.json(), ip)


__all__ = [
    "HOSTING_ASNS",
    "HttpJsonProvider",
    "MaxMindProvider",
    "NetIntelProvider",
    "NullProvider",
    "default_response_mapper",
]
