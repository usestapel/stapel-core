"""One fan-out, one copy of a fact.

An action is delivered to every subscriber registered for its name, and more
than one of them can be entitled to state the same fact. When they do, the
duplicate is not a retry — it is one delivery claiming twice that something
happened once. A GDPR erasure receipt is the case that forced this: a receipt
asserts that a deletion occurred, so two of them for one part assert two
deletions, which is a false record that no reader can reconcile.

The scope is deliberately one *delivery*, not one process and not one
correlation id. Delivery is at-least-once: a redelivery is a second, separate
fan-out and MUST be free to state the fact again, because the first one may
never have arrived. Suppressing that would trade a duplicate for a receipt
nobody ever sees.

Outside a scope — a handler called directly, a unit test — ``claim_once``
always grants the claim. The guard exists to make the fan-out safe, not to
make a function refuse to run.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

#: The facts already claimed in the fan-out in flight, or ``None`` outside one.
_claimed: ContextVar["set[str] | None"] = ContextVar(
    "stapel_delivery_claimed", default=None
)


@contextmanager
def delivery_scope():
    """Arm the guard for one fan-out. Nests: an inner delivery starts clean."""
    token = _claimed.set(set())
    try:
        yield
    finally:
        _claimed.reset(token)


def claim_once(key: str) -> bool:
    """``True`` the first time *key* is claimed in this delivery.

    ``True`` always when no delivery is in flight.
    """
    claimed = _claimed.get()
    if claimed is None:
        return True
    if key in claimed:
        return False
    claimed.add(key)
    return True


def in_delivery() -> bool:
    """Whether a fan-out is in flight in this context."""
    return _claimed.get() is not None


__all__ = ["claim_once", "delivery_scope", "in_delivery"]
