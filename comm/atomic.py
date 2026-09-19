"""Atomic-block DEPTH, and the baseline a test suite measures it against.

``emit()``'s outside-atomic guard used to ask ``connection.in_atomic_block``.
That is the right question in production and a question with a constant
answer under tests: pytest-django (like Django's own ``TestCase``) wraps every
database test in a transaction, so ``in_atomic_block`` is True from the first
line of the test body to the last. The guard could therefore never fire in any
library's test suite — a gate that cannot start. The one consumer library that
wanted it had to re-implement depth tracking in its own conftest.

What tells the two apart is not whether a transaction is open but whether the
CALLER opened one. Every ``transaction.atomic()`` pushes an entry onto the
connection (``atomic_blocks``, or a savepoint id on older wrappers), so the
depth at the start of a test is a baseline, and an ``emit()`` that happens at
that same depth opened nothing of its own — exactly the production shape of
"outside any atomic block", visible from inside the test transaction.

Armed only by :mod:`stapel_core.testing` (a test suite opting in). With no
baseline armed — every production process — ``in_own_atomic_block`` is
``connection.in_atomic_block`` and nothing changes.
"""
from __future__ import annotations

import threading

_baselines: dict[str, int] = {}
_lock = threading.Lock()


def atomic_depth(connection) -> int:
    """How many nested atomic blocks are open on *connection*.

    ``atomic_blocks`` is Django's own list (one entry per ``Atomic.__enter__``,
    outermost included) and is what this reads. The fallback covers a
    connection wrapper that does not keep it: the outermost block sets
    ``in_atomic_block`` without pushing a savepoint, every nested one pushes
    exactly one entry — so the sum is the same number.
    """
    blocks = getattr(connection, "atomic_blocks", None)
    if blocks is not None:
        return len(blocks)
    depth = len(getattr(connection, "savepoint_ids", ()) or ())
    return depth + (1 if connection.in_atomic_block else 0)


def set_baseline(connection) -> int:
    """Arm the gate on *connection*: its current depth is "no atomic block"."""
    depth = atomic_depth(connection)
    with _lock:
        _baselines[connection.alias] = depth
    return depth


def clear_baseline(alias: str) -> None:
    with _lock:
        _baselines.pop(alias, None)


def baseline(alias: str) -> int | None:
    return _baselines.get(alias)


def is_armed(alias: str) -> bool:
    return alias in _baselines


def in_own_atomic_block(connection) -> bool:
    """True when the caller is inside an atomic block IT opened.

    Production (no baseline): the plain ``in_atomic_block``. Under an armed
    test gate: strictly deeper than the baseline the fixture recorded.
    """
    base = _baselines.get(connection.alias)
    if base is None:
        return bool(connection.in_atomic_block)
    return atomic_depth(connection) > base
