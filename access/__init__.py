"""``stapel_core.access`` — mandatory access control (MAC) for staff/admin.

Defensive frame (docs/admin-suite.md §3): staff permissions are a *computed
function* of (model declaration × role clearance), not accumulated rows in
the permissions table. This is the AS-1 mechanism; role transport (JWT claim,
``staff_roles`` field, StaffRole assignments in stapel-auth) is AS-2, admin
visibility built on top of it is AS-3.

Invariants (admin-suite §3.0) and where they live here:

- **A1** — computability: :class:`~stapel_core.access.backend.MandateBackend`
  evaluates ``has_perm`` at call time from the ``@access`` declaration and the
  role registry. Nothing is materialized into ``auth_permission``; there is
  nothing to drift or rebuild.
- **A2** — single writer: this package only *reads* roles (via the
  ``ROLE_SOURCES`` chain). It has no API to assign a role, and the local
  admin of a service cannot raise clearance — role definitions come from
  settings (deploy config), assignments from the auth service (AS-2).
- **A3** — revocation latency: roles are re-read on every request (the chain
  is evaluated per user instance; the claim source reflects the current
  token). No permission rows are cached in the database, so a revoked role
  stops granting as soon as the transport stops presenting it.
- **A4** — no silent escalation: DAC grants above the mandate are honored
  only through :class:`~stapel_core.access.backend.AuditedModelBackend`,
  which logs and signals every use, and ``access_report`` lists every grant
  above clearance. ``STAPEL_ACCESS["STRICT"] = True`` turns the mandate into
  a ceiling (escalation denied).
- **A5** — superuser is outside the mandate (Django semantics preserved).

Public API::

    from stapel_core.access import access, Level

    @access.standard            # business; view=LOW, add/change=MID, delete=HIGH
    class Listing(models.Model): ...

    @access(view=Level.MID, delete=Level.HIGH, category="business")
    class Invoice(models.Model): ...

Settings namespace: ``STAPEL_ACCESS`` (see :mod:`stapel_core.access.conf`).
"""
from .audit import connect_access_audit
from .backend import AuditedModelBackend, MandateBackend
from .declaration import AccessDeclaration, access, declared_access, effective_access
from .exceptions import AccessConfigError
from .levels import CATEGORIES, Level
from .roles import RoleDefinition, clearance_for, effective_roles
from .signals import dac_escalation, step_up_denied
from .sources import CLAIM_ATTR, claim_roles, group_roles, user_field_roles, user_roles
from .stepup import step_up_active, step_up_blocks, step_up_config

__all__ = [
    "AccessConfigError",
    "AccessDeclaration",
    "AuditedModelBackend",
    "CATEGORIES",
    "CLAIM_ATTR",
    "Level",
    "MandateBackend",
    "RoleDefinition",
    "access",
    "claim_roles",
    "clearance_for",
    "connect_access_audit",
    "dac_escalation",
    "declared_access",
    "effective_access",
    "effective_roles",
    "group_roles",
    "step_up_active",
    "step_up_blocks",
    "step_up_config",
    "step_up_denied",
    "user_field_roles",
    "user_roles",
]
