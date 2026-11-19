"""System checks for the security settings namespace (``STAPEL_SECURITY``).

W001  ``STAPEL_NETINTEL["TRUSTED_PROXY_HEADER"]`` is set: the key moved to
      ``STAPEL_SECURITY``. It still works; this names the new home.
W002  both keys are set and disagree: ``STAPEL_SECURITY`` wins and the
      legacy value is ignored — said out loud, because which header is
      trusted for the client IP is a security decision.

W-level: nothing is broken by either, and a deprecation must not refuse a
deploy. Neither message prints a header value it does not need to.
"""
from django.core import checks

W001_LEGACY_PROXY_HEADER_KEY = "stapel_core.security.W001"
W002_PROXY_HEADER_KEYS_DISAGREE = "stapel_core.security.W002"


@checks.register("stapel_security")
def check_trusted_proxy_header_key(app_configs=None, **kwargs):
    from django.conf import settings

    from .proxy import KEY, LEGACY_NAMESPACE

    legacy = getattr(settings, LEGACY_NAMESPACE, None) or {}
    if KEY not in legacy:
        return []
    current = getattr(settings, "STAPEL_SECURITY", None) or {}
    if KEY in current and current[KEY] != legacy[KEY]:
        return [checks.Warning(
            f"{LEGACY_NAMESPACE}['{KEY}'] ({legacy[KEY]!r}) and "
            f"STAPEL_SECURITY['{KEY}'] ({current[KEY]!r}) disagree. "
            "client_ip() trusts the STAPEL_SECURITY value; the "
            f"{LEGACY_NAMESPACE} one is ignored.",
            hint=f"Delete '{KEY}' from {LEGACY_NAMESPACE} once the "
                 "STAPEL_SECURITY value is the header your edge proxy "
                 "overwrites.",
            id=W002_PROXY_HEADER_KEYS_DISAGREE,
        )]
    return [checks.Warning(
        f"{LEGACY_NAMESPACE}['{KEY}'] is deprecated: the trusted client-IP "
        f"header is a security setting, read from STAPEL_SECURITY['{KEY}']. "
        "The legacy key is still honoured.",
        hint=f"Move the value to STAPEL_SECURITY = {{'{KEY}': ...}} and "
             f"delete it from {LEGACY_NAMESPACE} (drop {LEGACY_NAMESPACE} "
             "entirely if nothing else is left in it). A "
             "SILENCED_SYSTEM_CHECKS entry for stapel_core.netintel.W003 "
             "that existed only because of this key can go too.",
        id=W001_LEGACY_PROXY_HEADER_KEY,
    )]


__all__ = [
    "W001_LEGACY_PROXY_HEADER_KEY",
    "W002_PROXY_HEADER_KEYS_DISAGREE",
    "check_trusted_proxy_header_key",
]
