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


class FunctionPayloadTooLarge(FunctionCallError):
    """A request or reply exceeds the transport's per-message size limit.

    NATS caps a single message (1 MiB by default) and REFUSES to send an
    oversized one. Before this class existed the failure was invisible in the
    worst possible way: the server had already RUN the function, then
    ``msg.respond()`` raised ``MaxPayloadError`` inside the subscription
    callback, nothing was ever sent back, and the caller sat until its timeout
    and reported a generic failure. The work was done, the answer thrown away,
    and the log line naming the real cause lived on the other host.

    Measured on ironmemo (2026-08-06): ``llm.complete`` over a meeting
    transcript, upload path, exactly this shape.

    A function is a request/response seam, not a file transfer. If a payload
    legitimately grows past the limit, the answer is a reference — an object
    key the caller fetches — not a bigger message. Raising the broker's
    ``max_payload`` buys headroom, not a different answer.
    """

    def __init__(self, name: str, size: int, limit: int, *, direction: str):
        self.function = name
        self.size = size
        self.limit = limit
        self.direction = direction  # "request" | "reply"
        super().__init__(
            f"function '{name}': {direction} is {size} bytes, over the "
            f"transport limit of {limit} bytes. The broker refuses to send it, "
            f"so the call cannot complete. Either return a REFERENCE the caller "
            f"resolves (object key / URL) instead of the bulk itself, or raise "
            f"the broker's max_payload if this size is genuinely expected."
        )


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


class ProjectionError(CommError):
    """A projection failed to apply an event or rebuild (runtime)."""


class ProjectionConfigError(CommError):
    """A Projection declaration is invalid (missing attribute, two
    projections targeting one table, a model not derived from
    ProjectionModel, rebuild without a source_of_truth). Raised loudly at
    app-ready validation — a misdeclared read-model never silently drifts."""


class EmitOutsideAtomicError(CommError):
    """emit() was called outside transaction.atomic() while the outbox is on.

    The outbox guarantee — the event leaves iff the surrounding transaction
    commits — only holds when the outbox row is written inside the same
    transaction as the business mutation. Outside atomic the row commits on
    its own, detached from whatever mutation it describes. Raised only when
    ``STAPEL_COMM["EMIT_OUTSIDE_ATOMIC"] = "error"``; the default is a
    logged warning.
    """
