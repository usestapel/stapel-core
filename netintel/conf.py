"""Settings namespace for IP intelligence (``STAPEL_NETINTEL``)."""
from stapel_core.conf import AppSettings

netintel_settings = AppSettings(
    "STAPEL_NETINTEL",
    defaults={
        # Provider seam (replace-style): dotted path, class or instance of a
        # NetIntelProvider. Default deliberately knows nothing — an
        # unconfigured framework must not pretend to classify networks.
        "PROVIDER": "stapel_core.netintel.providers.NullProvider",
        # Django cache used for classification results.
        "CACHE_ALIAS": "default",
        # Result TTL in seconds (24h): classification runs on the hot path
        # of middleware/decorators.
        "CACHE_TTL": 86400,
        # MaxMindProvider: paths to GeoLite2/GeoIP2 mmdb files (None = that
        # database is not consulted).
        "MAXMIND_ASN_DB": None,
        "MAXMIND_COUNTRY_DB": None,
        "MAXMIND_ANONYMOUS_DB": None,
        # Extra ASNs (ints) the host wants treated as hosting/datacenter,
        # merged over the small builtin heuristic list.
        "EXTRA_DATACENTER_ASNS": [],
        # HttpJsonProvider: URL with an {ip} placeholder, optional bearer
        # key, optional dotted path/callable mapping the JSON response to
        # an IpProfile (default mapper covers ipinfo/IPQS-style payloads).
        "HTTP_URL_TEMPLATE": None,
        "HTTP_API_KEY": None,
        "HTTP_RESPONSE_MAPPER": None,
        # META key of a proxy-set client-IP header, e.g.
        # "HTTP_X_FORWARDED_FOR". None (default) = trust REMOTE_ADDR only.
        # Only set this when a trusted proxy strips/overwrites the header —
        # client-supplied values are trivially spoofed.
        "TRUSTED_PROXY_HEADER": None,
    },
)

__all__ = ["netintel_settings"]
