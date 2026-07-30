"""Exhaustive field partition for a copy seam.

A seam that materializes one row from another (a recurring series' master →
its occurrence, a template → an instance, a draft → a published record) always
starts as a hand-written list of the fields to carry over. That list is
correct exactly once. The next field added to the model is silently *not*
carried, and nothing anywhere says so — in a real product this lost
``admit_required_default`` and ``pin_code`` from a meeting room, so an "open"
recurring series slammed the door on the first join and a PIN-protected series
materialized rooms with no PIN. Both were found by users, not by tests.

The mechanism here is small and does one thing: it turns *forgetting* into
*having to decide*. Declare the partition on the model —

    class Room(models.Model):
        ...
        FIELD_SPEC = FieldSpec(
            copy=("access_level", "admit_required", "pin_code"),
            recompute=("id", "code", "title", "created_by", "calendar_event_id"),
            never=("created_at", "started_at", "is_active"),
        )

— and every concrete field must appear in exactly one of the three lists:

``copy``
    carried verbatim from the source row.
``recompute``
    the seam derives it (a fresh identity, the new parent's id, a value from
    the payload). Not copied, but not ignored either.
``never``
    deliberately left at the model default (lifecycle timestamps, state that
    belongs to the new row's own life).

Add a field, and :meth:`FieldSpec.validate` names it as unassigned until the
author says which of the three it is — i.e. until they have decided whether it
reaches the copy. :meth:`FieldSpec.values` runs that validation before
building the dict, so the seam itself fails loudly rather than quietly
shipping an incomplete row.

**Honest boundary: this checks that a decision was made, not that it was
right.** A field listed in ``never`` that should have been copied passes
green. What the mechanism removes is the silent case — the field nobody ever
classified — which is the one that leaked in production.
"""
from __future__ import annotations

from dataclasses import dataclass


class FieldSpecError(Exception):
    """The declared partition does not exhaustively cover the model."""


@dataclass(frozen=True)
class FieldSpec:
    """Declaration of how each concrete field of a model crosses a copy seam.

    ``copy`` / ``recompute`` / ``never`` are field *names* (``field.name``, so
    a FK is ``"created_by"``, not ``"created_by_id"``).
    """

    copy: tuple[str, ...] = ()
    recompute: tuple[str, ...] = ()
    never: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "copy", tuple(self.copy))
        object.__setattr__(self, "recompute", tuple(self.recompute))
        object.__setattr__(self, "never", tuple(self.never))
        # Models whose partition this spec has already been checked against.
        object.__setattr__(self, "_validated", set())

    @property
    def declared(self) -> set[str]:
        return set(self.copy) | set(self.recompute) | set(self.never)

    def validate(self, model) -> None:
        """Raise :class:`FieldSpecError` unless the partition covers *model*.

        Three ways to be wrong, all reported by name: a concrete field in no
        list (the one that leaks), a listed name that is not a field of the
        model (a rename left behind), and a name in more than one list (the
        author did not in fact decide).
        """
        actual = {f.name for f in model._meta.concrete_fields}
        problems: list[str] = []

        unassigned = sorted(actual - self.declared)
        if unassigned:
            problems.append(
                f"fields of {model.__name__} assigned to neither copy, "
                f"recompute nor never: {unassigned} — say which, so the "
                f"decision is on record"
            )

        unknown = sorted(self.declared - actual)
        if unknown:
            problems.append(
                f"declared names that are not concrete fields of "
                f"{model.__name__}: {unknown}"
            )

        overlaps = sorted(
            name for name in self.declared
            if (name in self.copy) + (name in self.recompute) + (name in self.never) > 1
        )
        if overlaps:
            problems.append(
                f"names in more than one list: {overlaps} — a field crosses "
                f"the seam exactly one way"
            )

        if problems:
            raise FieldSpecError("; ".join(problems))
        self._validated.add(model)

    def values(self, source) -> dict:
        """``{field_name: value}`` for the ``copy`` half of *source*.

        Validates the partition against ``type(source)`` first (once per
        model), so a seam built on an incomplete declaration fails loudly at
        the seam instead of writing a half-populated row.
        """
        model = type(source)
        if model not in self._validated:
            self.validate(model)
        return {name: getattr(source, name) for name in self.copy}


__all__ = ["FieldSpec", "FieldSpecError"]
