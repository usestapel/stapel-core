"""Value types of the privilege gateway.

A **verb** is the unit of privilege: a name, a JSON schema for its
arguments, a policy, and a handler (dotted path). The agent in a project
container gets the *capability* to call a verb, never the *credentials*
behind it — keys, passwords and scripts live behind the gateway in the
control plane (system-design §5.9, S1).

A **caller context** is who is asking: through which channel (HTTP from a
container, comm Function from a control-plane module, direct Python), on
behalf of which project/container, with which resolved tier. Handlers and
policy checks receive it; the audit line records it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping


@dataclass(frozen=True, slots=True)
class VerbPolicy:
    """Per-verb policy. Absent fields are *no additional restriction* —
    the restrictive default of the gateway is deny-by-default at the
    registry level (an undeclared verb does not exist).

    - ``tiers`` — tier names the verb is available on; ``None`` = every
      tier. When set, an unknown caller tier is a denial (fail-closed).
    - ``rate_limit`` — ``"30/m"``-style string (``s``/``m``/``h``/``d``)
      or ``"N/SECONDS"``; counted per ``(verb, project)``.
    - ``require_confirmation`` — two-phase execution: the call parks as a
      pending action and only runs after an out-of-band ``confirm()``.
    - ``audit_stream`` — eventstore stream for this verb's audit lines
      (default: the module-wide ``AUDIT_STREAM`` setting).
    """

    tiers: tuple[str, ...] | None = None
    rate_limit: str | None = None
    require_confirmation: bool = False
    audit_stream: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | "VerbPolicy" | None) -> "VerbPolicy":
        if data is None:
            return cls()
        if isinstance(data, VerbPolicy):
            return data
        unknown = set(data) - {"tiers", "rate_limit", "require_confirmation", "audit_stream"}
        if unknown:
            raise ValueError(f"unknown policy keys: {sorted(unknown)}")
        tiers = data.get("tiers")
        return cls(
            tiers=tuple(tiers) if tiers is not None else None,
            rate_limit=data.get("rate_limit"),
            require_confirmation=bool(data.get("require_confirmation", False)),
            audit_stream=data.get("audit_stream"),
        )

    def merged(self, patch: Mapping[str, Any]) -> "VerbPolicy":
        """Per-key merge: keys present in *patch* win, the rest keep."""
        current = {
            "tiers": self.tiers,
            "rate_limit": self.rate_limit,
            "require_confirmation": self.require_confirmation,
            "audit_stream": self.audit_stream,
        }
        current.update(patch)
        return VerbPolicy.from_mapping(current)


@dataclass(frozen=True, slots=True)
class VerbDeclaration:
    """One declared verb. ``handler`` is a dotted path (preferred — keeps
    registration import-light and lets settings declare verbs) or a
    callable ``handler(args: dict, caller: CallerContext) -> Any``.

    ``schema`` is mandatory: all input reaching a verb comes from an
    untrusted container (S5), so a verb without an argument contract does
    not get declared at all. A no-args verb declares
    ``{"type": "object", "additionalProperties": False}``.
    """

    name: str
    schema: dict
    handler: str | Callable[[dict, "CallerContext"], Any]
    policy: VerbPolicy = field(default_factory=VerbPolicy)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("verb needs a non-empty name")
        if not isinstance(self.schema, dict) or not self.schema:
            raise ValueError(
                f"verb {self.name!r} needs a JSON schema for its arguments "
                "(S5: container input is untrusted; a no-args verb declares "
                '{"type": "object", "additionalProperties": false})'
            )
        if not self.handler:
            raise ValueError(f"verb {self.name!r} needs a handler (dotted path or callable)")


@dataclass(slots=True)
class CallerContext:
    """Who is invoking a verb, and through which door.

    ``channel``: ``"http"`` (project container, scope-token + network
    checked), ``"comm"`` (control-plane module via Function), or
    ``"internal"`` (direct Python call inside the control plane).
    ``confirmed_by`` is set only on the execution leg of a confirmed
    two-phase call.
    """

    channel: str
    project: str | None = None
    container: str | None = None
    tier: str | None = None
    ip: str | None = None
    token_id: int | None = None
    subject: str | None = None
    confirmed_by: str | None = None


@dataclass(frozen=True, slots=True)
class PendingConfirmation:
    """Returned instead of a result when the verb's policy requires human
    confirmation: the call is parked, nothing executed yet."""

    confirmation_id: str
    verb: str
    expires_at: datetime


__all__ = [
    "CallerContext",
    "PendingConfirmation",
    "VerbDeclaration",
    "VerbPolicy",
]
