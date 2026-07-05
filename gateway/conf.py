"""Settings namespace for the privilege gateway (``STAPEL_GATEWAY``)."""
from stapel_core.conf import AppSettings

gateway_settings = AppSettings(
    "STAPEL_GATEWAY",
    defaults={
        # Merge-registry of verbs (over register_verb()/@verb declarations):
        #   {"deploy": {"schema": {...}, "handler": "app.gw.deploy",
        #               "policy": {"tiers": ["business"], "rate_limit": "10/h"}}}
        # For a verb already registered in code the entry is a per-field
        # patch (policy merges per key); an entry of None disables the verb
        # (deny-by-default again). New names declare settings-only verbs.
        "VERBS": {},
        # Policy engine (replace-style seam): checks tiers + rate limit by
        # default; subclass to add checks (budget, freeze windows, ...).
        "POLICY_ENGINE": "stapel_core.gateway.policy.DefaultPolicyEngine",
        # Rate limiter used by the default engine. Cache-backed fixed
        # window, counted per (verb, project).
        "RATE_LIMITER": "stapel_core.gateway.ratelimit.CacheRateLimiter",
        # Audit sink (S6): callable(stream, payload, *, project, container).
        # Default appends to stapel_core.eventstore. Failures propagate —
        # a privileged call never completes silently unaudited.
        "AUDIT_SINK": "stapel_core.gateway.audit.eventstore_sink",
        # Default eventstore stream for audit lines (per-verb override:
        # policy.audit_stream).
        "AUDIT_STREAM": "audit",
        # Args are recorded on the audit line up to this many characters of
        # canonical JSON; longer args are replaced by a sha256 fingerprint.
        "AUDIT_ARGS_MAXLEN": 2048,
        # Scope tokens: default lifetime (seconds). Short-lived by design —
        # the container-manager reissues/rotates on container start.
        "TOKEN_TTL": 3600,
        # Network identity seam: callable(ip, token) -> bool. The default
        # enforces the token's bound network (exact IP or CIDR) and treats
        # an unbound token per REQUIRE_NETWORK_BINDING.
        "NETWORK_VERIFIER": "stapel_core.gateway.network.default_verifier",
        # When True, an HTTP call with a token that has no network binding
        # is refused: every container token must be pinned to its network
        # identity ("a request about project X comes from container X").
        "REQUIRE_NETWORK_BINDING": False,
        # Optional tier resolver: callable(project) -> tier name. Used when
        # the caller did not carry a tier and a verb restricts tiers.
        "TIER_RESOLVER": None,
        # Pending (require_confirmation) actions expire after this many
        # seconds if nobody confirms.
        "CONFIRMATION_TTL": 900,
    },
    import_strings=(
        "POLICY_ENGINE",
        "RATE_LIMITER",
        "AUDIT_SINK",
        "NETWORK_VERIFIER",
        "TIER_RESOLVER",
    ),
    # Every one of these decides what code runs on a privileged path or
    # relaxes a trust decision — a stray same-named env var must never
    # flip them silently.
    no_env=(
        "VERBS",
        "POLICY_ENGINE",
        "RATE_LIMITER",
        "AUDIT_SINK",
        "NETWORK_VERIFIER",
        "TIER_RESOLVER",
        "REQUIRE_NETWORK_BINDING",
    ),
)

__all__ = ["gateway_settings"]
