"""System checks for the netintel provider seam.

W-level by design (library-standard §3.7): a broken netintel provider
degrades to the unknown profile at runtime (fail-open), it must not block
a deploy.

W003 covers the quieter failure: nothing is broken, the seam simply was
never wired. The default ``NullProvider`` answers "unknown" for every
address, so every rule an operator wrote against a network class is dead
code and nothing says so — the same silent-condition obligation
``stapel_core.conf.W001`` discharges for ignored environment variables.
"""
from django.core import checks

W001_UNIMPORTABLE = "stapel_core.netintel.W001"
W002_NOT_A_PROVIDER = "stapel_core.netintel.W002"
W003_NOT_CONFIGURED = "stapel_core.netintel.W003"

# STAPEL_NETINTEL keys whose non-default value means the operator sat down to
# configure IP intelligence. CACHE_*/NEGATIVE_CACHE_TTL are deliberately not
# here: they tune the plumbing and are set by people copying a settings
# template, so they are no evidence of intent.
_INTENT_KEYS = (
    "MAXMIND_ASN_DB",
    "MAXMIND_COUNTRY_DB",
    "MAXMIND_ANONYMOUS_DB",
    "HTTP_URL_TEMPLATE",
    "HTTP_API_KEY",
    "EXTRA_DATACENTER_ASNS",
    "TRUSTED_PROXY_HEADER",
)


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
    return _check_provider_configured(value)


def _check_provider_configured(provider):
    """W003 — the seam is depended on but no provider was ever configured."""
    from django.conf import settings

    from .providers import NullProvider

    # Exactly NullProvider, not a subclass: a subclass is somebody's own
    # provider and may well classify.
    is_null = provider is NullProvider or type(provider) is NullProvider
    if not is_null or getattr(settings, "DEBUG", False):
        # DEBUG: a developer machine has no GeoIP databases and no network
        # trust decisions worth making. The warning is about a deployment.
        return []

    reasons = _demand_for_ip_intelligence()
    if not reasons:
        # No evidence anything reads IpProfile.kind here. The core cannot
        # enumerate host code that calls classify_ip or @captcha_protected,
        # so it warns only on demand it can point at — a check that fires on
        # every project that never touches network trust is noise, and noise
        # is how a real finding gets scrolled past.
        return []
    return [checks.Warning(
        "IP intelligence is not configured: STAPEL_NETINTEL['PROVIDER'] is "
        "the default NullProvider, so classify_ip() answers kind='unknown' "
        "for every address — no request is ever classified as datacenter, "
        "vpn, tor or residential. This deployment expects otherwise: "
        + "; ".join(reasons) + ".",
        hint="Set STAPEL_NETINTEL['PROVIDER'] to "
             "'stapel_core.netintel.providers.MaxMindProvider' (offline mmdb, "
             "pip install stapel-core[netintel-maxmind]) or to "
             "'stapel_core.netintel.providers.HttpJsonProvider'. If this "
             "deployment really is meant to run without IP intelligence, drop "
             "the settings named above (the rules they express cannot fire) "
             "or add 'stapel_core.netintel.W003' to SILENCED_SYSTEM_CHECKS.",
        id=W003_NOT_CONFIGURED,
    )]


def _demand_for_ip_intelligence():
    """Configuration that can only pay off when IPs are actually classified.

    Names settings only — never their values: HTTP_API_KEY is a credential
    and a system-check message ends up in deploy logs.
    """
    from .conf import netintel_settings

    reasons = []
    configured = [
        key for key in _INTENT_KEYS
        if getattr(netintel_settings, key, None) != netintel_settings.defaults[key]
    ]
    if configured:
        reasons.append(
            "STAPEL_NETINTEL configures "
            + ", ".join(configured)
            + " while PROVIDER stays at the default, so nothing reads that "
            "configuration"
        )

    dead_kinds = _captcha_rules_that_can_never_fire()
    if dead_kinds:
        reasons.append(
            "STAPEL_CAPTCHA has challenge rules keyed by network class ("
            + ", ".join(dead_kinds)
            + ") and no request can ever carry one"
        )
    return reasons


def _captcha_rules_that_can_never_fire():
    """Kinds the host wrote captcha rules for, other than 'unknown'.

    With NullProvider every request is 'unknown', so a matrix entry or an
    action override keyed by any other kind is unreachable — the host tuned
    a policy that cannot run. Reads the captcha namespace lazily: netintel
    sits below captcha and must not import it at module load.
    """
    from stapel_core.captcha.conf import captcha_settings

    kinds = set()
    matrix = captcha_settings.CHALLENGE_MATRIX
    if isinstance(matrix, dict):
        kinds.update(matrix)
    overrides = captcha_settings.ACTION_OVERRIDES
    if isinstance(overrides, dict):
        for per_action in overrides.values():
            # "+1" bumps whatever level 'unknown' got — that still works
            # without a provider. Only a {kind: level} dict names kinds.
            if isinstance(per_action, dict):
                kinds.update(per_action)
    kinds.discard("unknown")
    return sorted(str(kind) for kind in kinds)


__all__ = [
    "W001_UNIMPORTABLE",
    "W002_NOT_A_PROVIDER",
    "W003_NOT_CONFIGURED",
    "check_netintel_provider",
]
