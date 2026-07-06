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
    },
    # Every key shapes a trust decision — a stray same-named env var must
    # never flip any of them silently.
    no_env=("ROLES", "MODELS", "ROLE_SOURCES", "STRICT", "RUNTIME_ROLE_DEFINITIONS"),
)

__all__ = ["access_settings"]
