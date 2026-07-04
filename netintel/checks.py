"""System checks for the netintel provider seam.

W-level by design (library-standard §3.7): a broken netintel provider
degrades to the unknown profile at runtime (fail-open), it must not block
a deploy.
"""
from django.core import checks

W001_UNIMPORTABLE = "stapel_core.netintel.W001"
W002_NOT_A_PROVIDER = "stapel_core.netintel.W002"


@checks.register("stapel_netintel")
def check_netintel_provider(app_configs=None, **kwargs):
    from django.utils.module_loading import import_string

    from .conf import netintel_settings
    from .providers import NetIntelProvider

    value = netintel_settings.PROVIDER
    if isinstance(value, str):
        try:
            value = import_string(value)
        except ImportError as exc:
            return [checks.Warning(
                f"STAPEL_NETINTEL['PROVIDER'] ({netintel_settings.PROVIDER!r}) "
                f"cannot be imported: {exc}. classify_ip() will fail open to "
                "the unknown profile on every request.",
                hint="Point PROVIDER at a stapel_core.netintel.providers."
                     "NetIntelProvider subclass (dotted path).",
                id=W001_UNIMPORTABLE,
            )]
    is_provider = isinstance(value, NetIntelProvider) or (
        isinstance(value, type) and issubclass(value, NetIntelProvider)
    )
    if not is_provider:
        return [checks.Warning(
            f"STAPEL_NETINTEL['PROVIDER'] resolved to {value!r}, which is not "
            "a NetIntelProvider. classify_ip() will fail open to the unknown "
            "profile on every request.",
            hint="Subclass stapel_core.netintel.providers.NetIntelProvider.",
            id=W002_NOT_A_PROVIDER,
        )]
    return []


__all__ = ["check_netintel_provider", "W001_UNIMPORTABLE", "W002_NOT_A_PROVIDER"]
