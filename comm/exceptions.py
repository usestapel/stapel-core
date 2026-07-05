"""Exceptions for the comm layer."""
from __future__ import annotations


class CommError(Exception):
    """Base class for comm-layer errors."""


class FunctionNotRegistered(CommError):
    """No provider registered (or reachable) for the function name."""


class FunctionRouteNotConfigured(CommError):
    """HTTP transport is active but no route matches the function name."""


class FunctionCallError(CommError):
    """The provider raised / the remote call failed."""


class ActionDeliveryError(CommError):
    """One or more subscribers failed; the outbox will retry the event."""

    def __init__(self, topic: str, errors: list[Exception]):
        self.topic = topic
        self.errors = errors
        super().__init__(
            f"{len(errors)} handler(s) failed for action '{topic}': "
            + "; ".join(repr(e) for e in errors)
        )


class SchemaValidationError(CommError):
    """Payload does not match the registered schema."""


class EmitOutsideAtomicError(CommError):
    """emit() was called outside transaction.atomic() while the outbox is on.

    The outbox guarantee — the event leaves iff the surrounding transaction
    commits — only holds when the outbox row is written inside the same
    transaction as the business mutation. Outside atomic the row commits on
    its own, detached from whatever mutation it describes. Raised only when
    ``STAPEL_COMM["EMIT_OUTSIDE_ATOMIC"] = "error"``; the default is a
    logged warning.
    """
