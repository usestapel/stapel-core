"""Role definitions — settings merge-registry over builtins.

A role *definition* (name → clearance + app scopes) is deploy config, not
database state (admin-suite §3.3): access policy is fixed administratively
(code review, versioning, identical across services by deploy), not edited
by a runtime button — otherwise an admin role with access to the role table
escalates itself and MAC degenerates into DAC. Role *assignment*
(user → roles) belongs to the auth service (AS-2, single writer — A2).

Builtins: ``viewer`` (LOW), ``editor`` (MID), ``admin`` (HIGH).
``STAPEL_ACCESS["ROLES"]`` merges over them: a dict entry patches a builtin
per key or defines a new role (``clearance`` required), ``None`` disables a
builtin. App scopes (Q7 — in v1)::

    STAPEL_ACCESS = {
        "ROLES": {
            "accountant": {"clearance": "low",
                           "apps": {"stapel_billing": "high"}},
            "moderator":  {"clearance": "mid"},
            "viewer": None,
        },
    }

Within a scoped app the ``apps`` entry *replaces* the base clearance for
that app_label (it may lower it as well as raise it); everywhere else the
base clearance applies. A user's effective clearance for an app is the
maximum across their roles.

MINI-DESIGN (в.1, reserved — NOT implemented here; UI is AS-3+ scope):
runtime-editable role definitions behind an explicit flag.

- Flag: ``STAPEL_ACCESS["RUNTIME_ROLE_DEFINITIONS"] = True`` (no_env — it
  relaxes a trust decision, so it must be set in code-reviewed settings,
  never via a stray environment variable). Default False; the settings
  registry above remains the base and the default source of truth.
- Storage: a ``RoleDefinition`` model in **stapel-auth only** (single
  writer, A2), editable in the auth admin behind clearance HIGH + step-up.
  Other services never grow the table.
- Sync of definitions: auth emits ``staff.role.definition.updated`` /
  ``.deleted`` comm-Actions (schema in ``schemas/emits/``, outbox
  discipline); consumer services keep a read-only cache (Redis or local
  table) keyed by role name. Sync-down is **replace** per role name — the
  event payload is the full definition, consumers overwrite (same semantics
  as the AS-2 ``staff_roles`` claim sync; в.3).
- Merge order with this flag on: builtins → settings ``ROLES`` → runtime
  overlay. Runtime entries may only *add* roles or patch runtime-created
  ones; a runtime entry that shadows a settings-defined role is ignored
  with a W-check — settings stay authoritative, so a compromised auth admin
  cannot silently rewrite the reviewed policy.
- Self-escalation guard: the auth admin surface must refuse a definition
  edit that would raise the clearance of any role held by the editing user
  (checked against the editor's own ``staff_roles``) — that is the exact
  MAC→DAC degeneration the settings registry exists to prevent.
- Degradation: consumers that have not received the sync yet simply do not
  know the new role name and ignore it in claims (forward-compatible, same
  rule as unknown claim roles today).
"""
from __future__ import annotations

import dataclasses
from typing import Any, Iterable, Mapping

from .exceptions import AccessConfigError
from .levels import Level

ROLE_KEYS = {"clearance", "apps"}


@dataclasses.dataclass(frozen=True)
class RoleDefinition:
    name: str
    clearance: Level
    #: app_label -> clearance override inside that app (domain scope, Q7).
    apps: Mapping[str, Level] = dataclasses.field(default_factory=dict)

    def clearance_for(self, app_label: str | None) -> Level:
        if app_label is not None and app_label in self.apps:
            return self.apps[app_label]
        return self.clearance


BUILTIN_ROLES: dict[str, RoleDefinition] = {
    "viewer": RoleDefinition("viewer", Level.LOW),
    "editor": RoleDefinition("editor", Level.MID),
    "admin": RoleDefinition("admin", Level.HIGH),
}


def _parse_apps(value: Any, *, source: str) -> dict[str, Level]:
    if not isinstance(value, Mapping):
        raise AccessConfigError(f"{source}['apps'] must be a dict, got {type(value).__name__}")
    return {
        app_label: Level.parse(level, clearance_only=True)
        for app_label, level in value.items()
    }


def effective_roles() -> dict[str, RoleDefinition]:
    """Builtins merged with ``STAPEL_ACCESS["ROLES"]`` (None disables)."""
    from .conf import access_settings

    overlay: Mapping[str, Any] = access_settings.ROLES or {}
    roles = dict(BUILTIN_ROLES)
    for name, entry in overlay.items():
        source = f"STAPEL_ACCESS['ROLES'][{name!r}]"
        if entry is None:
            roles.pop(name, None)
            continue
        if not isinstance(entry, Mapping):
            raise AccessConfigError(f"{source} must be a dict or None, got {type(entry).__name__}")
        unknown = set(entry) - ROLE_KEYS
        if unknown:
            raise AccessConfigError(f"{source} has unknown keys: {sorted(unknown)}")
        base = roles.get(name)
        if base is None and "clearance" not in entry:
            raise AccessConfigError(f"{source} defines a new role and must set 'clearance'")
        clearance = (
            Level.parse(entry["clearance"], clearance_only=True)
            if "clearance" in entry
            else base.clearance  # type: ignore[union-attr]
        )
        apps = (
            _parse_apps(entry["apps"], source=source)
            if "apps" in entry
            else dict(base.apps) if base is not None else {}
        )
        roles[name] = RoleDefinition(name=name, clearance=clearance, apps=apps)
    return roles


def clearance_for(role_names: Iterable[str], app_label: str | None = None) -> Level | None:
    """Max clearance of *role_names* for *app_label* (None → no known roles).

    Unknown names are silently ignored (forward compatibility: a new role
    may arrive in a token before — or after — the deploy config that
    defines it; degradation is soft, admin-suite §3.3).
    """
    registry = effective_roles()
    levels = [
        registry[name].clearance_for(app_label)
        for name in role_names
        if name in registry
    ]
    return max(levels) if levels else None


__all__ = ["BUILTIN_ROLES", "RoleDefinition", "clearance_for", "effective_roles"]
