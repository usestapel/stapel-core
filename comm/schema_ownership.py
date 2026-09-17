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

Every foreign copy is reported, naming both packages and the fact. A schema
for a fact you do not emit is never correct: at best it duplicates, at worst it
contradicts, and the contradiction is invisible until the day the owner adds a
field.

SEVERITY IS THE COPY'S DIVERGENCE, NOT ITS EXISTENCE
----------------------------------------------------
Shipping this at Error level while eight of our own libraries still vendored
the two schemas made the check an outage generator rather than a gate: a
fleet's cdn family went to ``Restarting`` on a copy that was byte-identical to
core's and had therefore never refused a payload and could not.

So the level follows the damage.

``E010`` — the copy DIFFERS from the owner's, or cannot be parsed to find out.
This is the failure above: a contract that rejects the owner's payload and
rolls a compliance action back while every receipt reports success. A boot is
the right place to stop.

``W010`` — the copy is semantically identical. It cannot misbehave today. It
is still wrong, still reported, and still deleted, because identical copies
are how divergent ones start and the cost of removing one is a deleted file.
A warning names it on every boot without taking a service down for a file that
is, at this moment, harmless.

Comparison is on the VALIDATING shape: annotation keywords (``description``,
``title``, ``$comment``, ``examples``, ``default``, ``$id``) are stripped and
keys are order-insensitive, because none of them can refuse a payload. Of the
eight copies found on 2026-09-17, six differed from core's only in their prose
— calling those Errors would have refused six boots over a docstring. Two
differed in the way that bites: ``owner`` pinned to ``{"const": "profile"}``
and four extra ``required`` fields, which rejects a receipt from any other
owner and is the live failure at the top of this file.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

E010 = "stapel_core.comm.E010"
W010 = "stapel_core.comm.W010"

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


#: JSON Schema keywords that document rather than validate. A copy that differs
#: only in these validates identically to the owner's and can refuse nothing.
ANNOTATION_KEYWORDS = frozenset(
    {"description", "title", "$comment", "examples", "default", "$id"}
)


def _validation_shape(node):
    """``node`` with every annotation keyword removed, recursively.

    What is left is exactly what decides whether a payload is accepted.
    """
    if isinstance(node, dict):
        return {
            key: _validation_shape(value)
            for key, value in node.items()
            if key not in ANNOTATION_KEYWORDS
        }
    if isinstance(node, list):
        return [_validation_shape(item) for item in node]
    return node


def _owner_schema(action: str):
    """Core's own copy of ``action``, parsed. ``None`` if it is not readable.

    Without it there is nothing to compare against, so every foreign copy is
    treated as divergent — the safe direction.
    """
    import json

    path = Path(__file__).resolve().parent.parent / "gdpr" / "schemas" / "emits"
    try:
        return _validation_shape(json.loads((path / f"{action}.json").read_text()))
    except (OSError, ValueError):
        return None


def _is_divergent(path: Path, action: str) -> bool:
    """Does this copy VALIDATE differently from the owner's schema?

    Compared on the validating shape, so a reflowed copy or one with its own
    prose is not divergence — it accepts and refuses exactly what the owner's
    does. A copy that will not parse counts as divergent, because a contract we
    cannot read is not one we can call harmless.
    """
    import json

    owner = _owner_schema(action)
    if owner is None:
        return True
    try:
        return _validation_shape(json.loads(path.read_text())) != owner
    except (OSError, ValueError):
        return True


def foreign_schema_copies(search_roots=None) -> list[tuple[str, str, str, bool]]:
    """``(action, shipping package, owning package, divergent)`` per copy.

    Walks the import path for ``*/schemas/emits/<action>.json`` and reports any
    whose shipping package is not the owner of that action. ``divergent`` says
    whether the copy differs from the owner's schema — see the module docstring
    for why that, and not the copy's mere existence, sets the severity.
    """
    import sys

    roots = [Path(p) for p in (search_roots or sys.path) if p]
    found: list[tuple[str, str, str, bool]] = []
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
                found.append((action, package, "stapel_core", _is_divergent(path, action)))
    return sorted(found)


def check_schema_ownership(app_configs=None, search_roots=None, **kwargs):
    """System check: nobody ships a schema for a fact they do not emit."""
    from django.core.checks import Error, Warning

    problems = []
    for action, package, owner, divergent in foreign_schema_copies(search_roots):
        shared = (
            f"{package} ships schemas/emits/{action}.json, but {action} is "
            f"emitted by {owner}. Whichever copy a service loads becomes that "
            f"service's contract. Delete the copy in {package} and let "
            f"{owner}'s schema serve."
        )
        if divergent:
            problems.append(
                Error(
                    f"{shared} This copy DIFFERS from {owner}'s, so it silently "
                    f"refuses the owner's payload — and a refusal inside an "
                    f"erasure's own transaction rolls the erasure back while "
                    f"every receipt still reports success.",
                    id=E010,
                    obj=f"{package}:{action}",
                )
            )
        else:
            problems.append(
                Warning(
                    f"{shared} This copy matches {owner}'s today, so nothing is "
                    f"refused yet; it becomes the error above on the day "
                    f"{owner} changes the schema and {package} does not.",
                    id=W010,
                    obj=f"{package}:{action}",
                )
            )
    return problems


__all__ = [
    "ANNOTATION_KEYWORDS",
    "E010",
    "W010",
    "CORE_OWNED_ACTIONS",
    "check_schema_ownership",
    "foreign_schema_copies",
]
