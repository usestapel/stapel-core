"""Clearance levels and admin categories.

Three clearance levels with a total order — ``LOW < MID < HIGH``
(admin-suite §3.1). The fourth step exists on both sides already: below —
non-staff (no admin entry at all), above — superuser (outside the mandate,
A5). Two extra *requirement-only* members extend the same order for model
declarations:

- ``SUPERUSER`` — no clearance satisfies it; only the superuser bypass
  (Django semantics, via ModelBackend / the explicit superuser rule in
  MandateBackend) grants the operation. Used by ``@access.secret``.
- ``FORBIDDEN`` — the declaration forbids the operation for everyone but a
  superuser; used by ``@access.ops`` for add/change/delete (read-only
  journals). Distinct from SUPERUSER only in intent/reporting — enforcement
  is identical.

A role's clearance is always one of LOW/MID/HIGH; access = ``clearance >=
required``, which is False by construction for the two sentinel members.
"""
from __future__ import annotations

import enum

from .exceptions import AccessConfigError

#: Admin visibility categories of a model declaration (admin-suite §1.1).
CATEGORIES = ("business", "ops", "secret")


class Level(enum.IntEnum):
    LOW = 10
    MID = 20
    HIGH = 30
    # Requirement-only sentinels — never a role clearance:
    SUPERUSER = 40
    FORBIDDEN = 50

    @classmethod
    def parse(cls, value: object, *, clearance_only: bool = False) -> "Level":
        """Coerce a settings value (``"low"`` / ``Level.LOW``) to a Level.

        ``clearance_only`` restricts to LOW/MID/HIGH — role clearances can
        never be the requirement sentinels.
        """
        if isinstance(value, cls):
            level = value
        elif isinstance(value, str):
            try:
                level = cls[value.strip().upper()]
            except KeyError:
                raise AccessConfigError(
                    f"unknown access level {value!r} "
                    f"(expected one of {[m.name.lower() for m in cls]})"
                ) from None
        else:
            raise AccessConfigError(
                f"access level must be a string or Level, got {type(value).__name__}"
            )
        if clearance_only and level not in (cls.LOW, cls.MID, cls.HIGH):
            raise AccessConfigError(
                f"clearance must be one of low/mid/high, got {level.name.lower()!r}"
            )
        return level


def parse_category(value: object) -> str:
    if not isinstance(value, str) or value not in CATEGORIES:
        raise AccessConfigError(
            f"unknown admin category {value!r} (expected one of {list(CATEGORIES)})"
        )
    return value


__all__ = ["CATEGORIES", "Level", "parse_category"]
