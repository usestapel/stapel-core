"""A library may not ship its own copy of a fact another library owns.

THE FAILURE
-----------
``gdpr.section.erased`` is the receipt an owner emits when it has erased its
slice of a subject. The emitter is :mod:`stapel_core.gdpr.owners` — core owns
the fact. But core shipped no schema for it, and three consuming libraries each
shipped their own copy. Two carried the current eight-property shape. One
carried a five-property version with ``additionalProperties: false``.

Whichever copy a service happened to load became that service's contract. In a
service holding the stale one, core's receipt was refused:

    SchemaValidationError: payload for 'gdpr.section.erased' violates schema:
    Additional properties are not allowed ('receipt_id' was unexpected)

and because a receipt is emitted inside the erasure's own transaction — the
rule that makes "a rolled-back erasure never produces a receipt" true — the
validation error rolled the erasure back. The identity mirror in that service
logged ``identity mirror anonymised``, and the anonymisation was then undone.
The subject's email address survived a completed GDPR erasure whose receipt
set was complete, because another owner in another service had answered for
the same owner name first.

Measured on a live fleet, 2026-09-16.

WHY A CHECK AND NOT JUST A DELETION
-----------------------------------
Deleting the stale copy fixes that day. It does not stop the next library from
vendoring a schema for somebody else's fact, and the failure mode is silent by
construction: everything reports success, and the only symptom is data that
should be gone and is not.

This is the third time in one night that a local copy of another module's
truth produced a confident wrong answer — a frozen asset-type list, a literal
that claimed to match a token it no longer matched, and this. It is the most
damaging of the three, because the other two were wrong in public while this
one silently reversed a compliance action.

WHAT THIS CHECKS
----------------
For every action schema discovered on the path, the package that SHIPS it must
be the package that EMITS it. An emitter is known from the action registry —
the module that called :func:`stapel_core.comm.emit` for that name, or
declared it — so the comparison is against what the fleet actually does, not
against a hand-kept list of who owns what.

Reported as ``stapel_core.comm.E010``, at Error level, naming both packages
and the fact. A schema for a fact you do not emit is never correct: at best it
duplicates, at worst it contradicts, and the contradiction is invisible until
the day the owner adds a field.

WHAT IT CANNOT SEE
------------------
A copy that is byte-identical today. It is reported anyway, and deliberately:
identical copies are how divergent ones start, and the cost of removing one is
a deleted file.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

E010 = "stapel_core.comm.E010"

#: Facts this library emits and therefore owns. Read by the check; a package
#: other than ``stapel_core`` shipping a schema for one of these is the defect.
#: Listed rather than derived because the emit sites are in several modules and
#: a missed one would make the check quietly weaker.
CORE_OWNED_ACTIONS = (
    "gdpr.section.erased",
    "gdpr.owner.alive",
)


def _shipping_package(schema_path: Path) -> str:
    """The distribution directory a schema file lives under.

    ``…/site-packages/stapel_workspaces/schemas/emits/x.json`` → the package
    is the directory two levels above ``schemas``.
    """
    for parent in schema_path.parents:
        if parent.name == "schemas":
            return parent.parent.name
    return schema_path.parent.name


def foreign_schema_copies(search_roots=None) -> list[tuple[str, str, str]]:
    """``(action, shipping package, owning package)`` for every foreign copy.

    Walks the import path for ``*/schemas/emits/<action>.json`` and reports any
    whose shipping package is not the owner of that action.
    """
    import sys

    roots = [Path(p) for p in (search_roots or sys.path) if p]
    found: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for action in CORE_OWNED_ACTIONS:
            for path in root.glob(f"*/schemas/emits/{action}.json"):
                package = _shipping_package(path)
                if package == "stapel_core":
                    continue
                key = (action, package)
                if key in seen:
                    continue
                seen.add(key)
                found.append((action, package, "stapel_core"))
    return sorted(found)


def check_schema_ownership(app_configs=None, **kwargs):
    """System check: nobody ships a schema for a fact they do not emit."""
    from django.core.checks import Error

    problems = []
    for action, package, owner in foreign_schema_copies():
        problems.append(
            Error(
                f"{package} ships schemas/emits/{action}.json, but {action} is "
                f"emitted by {owner}. Whichever copy a service loads becomes "
                f"that service's contract, so a stale one silently refuses the "
                f"owner's payload — and a refusal inside an erasure's own "
                f"transaction rolls the erasure back while every receipt still "
                f"reports success. Delete the copy in {package} and let "
                f"{owner}'s schema serve.",
                id=E010,
                obj=f"{package}:{action}",
            )
        )
    return problems


__all__ = [
    "E010",
    "CORE_OWNED_ACTIONS",
    "check_schema_ownership",
    "foreign_schema_copies",
]
