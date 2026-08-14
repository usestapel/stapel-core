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
    },
    # An entry here decides whether a credential is checked at all; a stray
    # same-named environment variable must never be able to extend it.
    no_env=("REVIEWED_AUTH_BACKENDS",),
)

__all__ = ["security_settings"]
