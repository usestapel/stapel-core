"""Settings namespace for the staff mandate (``STAPEL_ACCESS``)."""
from stapel_core.conf import AppSettings

access_settings = AppSettings(
    "STAPEL_ACCESS",
    defaults={
        # Merge-registry of role definitions over the builtins
        # viewer(LOW) / editor(MID) / admin(HIGH):
        #   {"accountant": {"clearance": "low", "apps": {"stapel_billing": "high"}},
        #    "viewer": None}          # None disables a builtin
        # See stapel_core.access.roles.
        "ROLES": {},
        # Merge-registry of model declaration overrides over @access
        # decorators, keyed by "app_label.ModelName":
        #   {"stapel_listings.Listing": {"delete": "mid"},
        #    "stapel_outbox.OutboxEvent": None}   # None = back to implicit standard
        "MODELS": {},
        # Ordered chain of role sources for a user. Each entry is a dotted
        # path (or callable) of signature (user) -> list[str] | None; the
        # first non-None result is authoritative (an empty list means "this
        # user has no roles", it does NOT fall through). Default degradation
        # chain: JWT claim (AS-2 transport) -> local staff_roles field ->
        # Django groups named "role:<name>".
        "ROLE_SOURCES": (
            "stapel_core.access.sources.claim_roles",
            "stapel_core.access.sources.user_field_roles",
            "stapel_core.access.sources.group_roles",
        ),
        # DAC escalation policy (A4). False (default): a manual Permission
        # grant above the mandate is honored but audited (log + signal +
        # access_report line). True: the mandate is a ceiling — grants above
        # clearance are denied for staff (enforced by AuditedModelBackend).
        "STRICT": False,
        # Reserved (в.1): runtime-editable role *definitions* behind an
        # explicit flag. NOT implemented in this version — the settings
        # registry stays the source of truth; see the mini-design in
        # stapel_core.access.roles. Setting True today only triggers a
        # W-level system check.
        "RUNTIME_ROLE_DEFINITIONS": False,
        # Step-up on HIGH admin operations (AS-6, Q8a — on by default in the
        # standard preset, not opt-in). A model operation whose *required*
        # level is one of LEVELS additionally demands a fresh verification
        # grant for SCOPE before StapelModelAdmin lets it through — the
        # mandate says a role *may* act, step-up says it was re-proven
        # recently. See stapel_core.access.stepup.
        #   {"ENFORCE": True, "LEVELS": ["high"], "SCOPE": "sensitive",
        #    "MAX_AGE": 900}
        # Degradation (admin-suite §3.7): self-disables when no verification
        # factor is registered (no stapel-auth / host factor) — enforcing an
        # unobtainable grant would brick every HIGH operation.
        "STEP_UP": {},
        # Audit sink for access events (access.dac_escalation /
        # access.step_up_denied), mirroring STAPEL_GATEWAY["AUDIT_SINK"]:
        # callable(stream, payload, *, project, container). Default appends to
        # stapel_core.eventstore. Unlike the gateway (whose audit *is* the
        # authorization record on the privileged path), access forwarding is
        # best-effort telemetry — the AuditedModelBackend log line is the
        # durable record — so a sink failure is logged, not raised (breaking
        # has_perm on a telemetry outage would lock admins out).
        "AUDIT_SINK": "stapel_core.access.audit.eventstore_sink",
        # Eventstore stream access audit lines land on.
        "AUDIT_STREAM": "audit",
        # Optional alerting shim: callable(event: str, payload: dict) invoked
        # after the sink (e.g. push to notifications / SIEM). Dotted path or
        # callable; None disables. Best-effort like the sink.
        "NOTIFY": None,
    },
    import_strings=("AUDIT_SINK", "NOTIFY"),
    # Every key shapes a trust decision — a stray same-named env var must
    # never flip any of them silently. AUDIT_STREAM stays env-readable (a
    # routing convenience, not a trust decision), like the gateway's.
    no_env=(
        "ROLES", "MODELS", "ROLE_SOURCES", "STRICT", "RUNTIME_ROLE_DEFINITIONS",
        "STEP_UP", "AUDIT_SINK", "NOTIFY",
    ),
)

__all__ = ["access_settings"]
