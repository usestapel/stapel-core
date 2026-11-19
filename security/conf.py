"""Settings namespace for the fleet security gates (``STAPEL_SECURITY``)."""
from stapel_core.conf import AppSettings

security_settings = AppSettings(
    "STAPEL_SECURITY",
    defaults={
        # Dotted paths in AUTHENTICATION_BACKENDS that a project has reviewed
        # and accepts as-is. Exists for third-party backends, which cannot
        # carry the ``verifies_credentials`` declaration the boot check asks
        # of our own. Listing one is a decision the project makes on the
        # record, not a way to silence the check wholesale.
        "REVIEWED_AUTH_BACKENDS": [],
        # META key of a proxy-set client-IP header that client_ip() trusts,
        # e.g. "HTTP_X_REAL_IP". None (default) = trust REMOTE_ADDR only.
        # Only set this when the edge proxy strips/overwrites the header on
        # every request — client-supplied values are trivially spoofed.
        # Read through stapel_core.security.trusted_proxy_header(), which
        # falls back to the legacy STAPEL_NETINTEL key.
        "TRUSTED_PROXY_HEADER": None,
    },
    # An entry in REVIEWED_AUTH_BACKENDS decides whether a credential is
    # checked at all, and TRUSTED_PROXY_HEADER decides which address every
    # IP-keyed limit believes: a stray same-named environment variable must
    # never be able to set either.
    no_env=("REVIEWED_AUTH_BACKENDS", "TRUSTED_PROXY_HEADER"),
)

__all__ = ["security_settings"]
