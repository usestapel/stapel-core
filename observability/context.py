"""Trace context — the one id a business operation is followed by.

The thing a generic APM cannot do for us. An HTTP request fans out into
Actions and Functions across modules and services (:mod:`stapel_core.comm`);
without an id carried through the comm envelope those become a scatter of
unrelated log lines that can only be found by reading everything. With one,
a whole operation is ``trace_id = <x>`` in the aggregator.

Four ids, each answering a different question (OpenTelemetry-shaped, so an
OTel/W3C ``traceparent`` maps onto them without translation):

``trace_id``
    The whole distributed operation. 32 lowercase hex chars.
``span_id``
    This hop within it. 16 lowercase hex chars. A new one per service/handler.
``correlation_id``
    The BUSINESS operation, which may outlive one trace — an order, an
    erasure request, a booking. Defaults to the trace id when the caller
    names nothing better.
``causation_id``
    The id of the message that caused this one (the parent event's
    ``event_id``). ``trace_id`` says "same operation"; ``causation_id`` says
    "this happened BECAUSE of that", which is what reconstructs the fan-out
    as a tree rather than a bag.

Plus ``request_id`` — the inbound HTTP request, so a log line can be tied
back to an access-log entry / a value handed to the client.

Storage is a :class:`contextvars.ContextVar`, so the context follows the
logical flow: it is inherited by ``asyncio`` tasks, isolated per request
under ASGI, and per-thread under WSGI (each thread gets its own copy). It is
never global mutable state.

This module is deliberately **stdlib-only** — no Django, no settings. It is
imported from the bus envelope, which must stay constructible anywhere.
"""
from __future__ import annotations

import contextlib
import os
import re
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Iterator

__all__ = [
    "TraceContext",
    "current_trace",
    "trace_ids",
    "start_trace",
    "continue_trace",
    "bind_trace",
    "new_trace_id",
    "new_span_id",
    "sanitize_id",
    "parse_traceparent",
    "format_traceparent",
    "MAX_ID_LENGTH",
]

#: Longest incoming id accepted. Ids arrive from the network (a header, an
#: event envelope produced by another service) and land in every log line and
#: metric label the operation touches; an unbounded one is a cheap way to
#: bloat an aggregator. Anything longer is truncated, never rejected — losing
#: correlation is worse than a shortened id.
MAX_ID_LENGTH = 128

# Ids are opaque, but they are interpolated into structured logs and can end
# up in metric labels, so the alphabet is closed to characters that cannot
# break a downstream parser.
_ID_ALLOWED = re.compile(r"[^A-Za-z0-9_.:@=/+-]")

_TRACEPARENT = re.compile(
    r"^(?P<version>[0-9a-f]{2})-"
    r"(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<span_id>[0-9a-f]{16})-"
    r"(?P<flags>[0-9a-f]{2})$"
)


def new_trace_id() -> str:
    """A fresh 32-hex-char trace id (W3C ``trace-id`` shape)."""
    return os.urandom(16).hex()


def new_span_id() -> str:
    """A fresh 16-hex-char span id (W3C ``parent-id`` shape)."""
    return os.urandom(8).hex()


def sanitize_id(value: Any) -> str:
    """Make an untrusted id safe to log, label and forward.

    Non-strings stringify; disallowed characters are dropped; the result is
    truncated to :data:`MAX_ID_LENGTH`. Returns ``""`` for anything empty —
    which callers read as "no id was supplied", not "an id of nothing".
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = _ID_ALLOWED.sub("", text.strip())
    return text[:MAX_ID_LENGTH]


@dataclass(frozen=True)
class TraceContext:
    """The ids in flight for the current logical operation."""

    trace_id: str = ""
    span_id: str = ""
    correlation_id: str = ""
    causation_id: str = ""
    request_id: str = ""

    def as_dict(self) -> dict:
        """The five ids as a plain dict — log fields, envelope fields, tags.

        Always all five keys, empty string for "not set", so a consumer never
        has to branch on presence.
        """
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "request_id": self.request_id,
        }

    def child(self, *, causation_id: str = "") -> "TraceContext":
        """Same trace and business operation, a new hop."""
        return replace(
            self,
            span_id=new_span_id(),
            causation_id=sanitize_id(causation_id) or self.causation_id,
        )

    def traceparent(self) -> str:
        """This context as a W3C ``traceparent`` header value."""
        return format_traceparent(self)


_EMPTY = TraceContext()

_context: ContextVar[TraceContext] = ContextVar(
    "stapel_trace_context", default=_EMPTY
)


def current_trace() -> TraceContext:
    """The context in flight. Never ``None`` — an unbound context is empty."""
    return _context.get()


def trace_ids() -> dict:
    """The five ids of the context in flight, as a dict."""
    return _context.get().as_dict()


def _resolve(
    *,
    trace_id: str | None,
    span_id: str | None,
    correlation_id: str | None,
    causation_id: str | None,
    request_id: str | None,
    inherit: bool,
) -> TraceContext:
    base = _context.get() if inherit else _EMPTY

    trace = sanitize_id(trace_id) or base.trace_id or new_trace_id()
    span = sanitize_id(span_id) or new_span_id()
    # The business id defaults to the trace id rather than staying empty:
    # every operation then HAS one, so a query by correlation_id is never
    # silently empty for services that never bothered to name one.
    correlation = sanitize_id(correlation_id) or base.correlation_id or trace
    causation = sanitize_id(causation_id)
    request = sanitize_id(request_id) or base.request_id

    return TraceContext(
        trace_id=trace,
        span_id=span,
        correlation_id=correlation,
        causation_id=causation,
        request_id=request,
    )


@contextlib.contextmanager
def start_trace(
    *,
    trace_id: str | None = None,
    span_id: str | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    request_id: str | None = None,
    inherit: bool = False,
    traceparent: str | None = None,
) -> Iterator[TraceContext]:
    """Bind a trace context for the duration of the block.

    Every id is optional: what is not supplied is generated (``trace_id``,
    ``span_id``) or derived (``correlation_id`` falls back to the trace id).
    Ids coming off the wire are sanitized — callers may pass a raw header
    value.

    *traceparent* accepts a W3C header value and seeds ``trace_id`` from it
    (the incoming ``span_id`` becomes this hop's ``causation_id``: it names
    the span that caused us). Explicit keyword arguments win over it.

    *inherit* keeps the ids already in flight as the base, which is what a
    nested unit of work wants; the default starts clean, which is what an
    inbound request wants.

    The previous context is restored on exit, exception or not.
    """
    if traceparent:
        parsed = parse_traceparent(traceparent)
        if parsed:
            trace_id = trace_id or parsed["trace_id"]
            causation_id = causation_id or parsed["span_id"]

    ctx = _resolve(
        trace_id=trace_id,
        span_id=span_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
        request_id=request_id,
        inherit=inherit,
    )
    token = _context.set(ctx)
    try:
        yield ctx
    finally:
        _context.reset(token)


@contextlib.contextmanager
def continue_trace(envelope: Any) -> Iterator[TraceContext]:
    """Bind the context an incoming message carries — the subscriber side.

    *envelope* is a :class:`stapel_core.bus.Event` (or any object/dict with
    the same field names). The trace and the business correlation are
    inherited unchanged, ``causation_id`` becomes the incoming message's
    ``event_id`` (this work happened BECAUSE of that message) and a fresh
    ``span_id`` marks this hop.

    An envelope carrying no trace — one published before this facade existed,
    or by a service that does not stamp it — starts a new trace rather than
    running uncorrelated.
    """
    get = envelope.get if isinstance(envelope, dict) else (
        lambda k, d="": getattr(envelope, k, d)
    )
    with start_trace(
        trace_id=get("trace_id", ""),
        correlation_id=get("correlation_id", ""),
        causation_id=get("event_id", "") or get("causation_id", ""),
        request_id=get("request_id", ""),
    ) as ctx:
        yield ctx


def bind_trace(ctx: TraceContext):
    """Set *ctx* as the context in flight; returns the reset token.

    The escape hatch for code that cannot use the ``with`` form (a framework
    hook that enters and exits in different callbacks). Callers own the
    reset: ``token = bind_trace(ctx)`` … ``_context.reset(token)``. Prefer
    :func:`start_trace`.
    """
    return _context.set(ctx)


def parse_traceparent(value: str) -> dict | None:
    """Parse a W3C ``traceparent``. Returns ``None`` if it is not one.

    A malformed header is not an error — it is a header we did not write.
    The caller starts a fresh trace instead of failing a request over it.
    """
    if not value or not isinstance(value, str):
        return None
    match = _TRACEPARENT.match(value.strip().lower())
    if not match:
        return None
    parsed = match.groupdict()
    # all-zero ids are the spec's "invalid" sentinel
    if parsed["trace_id"] == "0" * 32 or parsed["span_id"] == "0" * 16:
        return None
    return parsed


def format_traceparent(ctx: TraceContext) -> str:
    """Render *ctx* as a W3C ``traceparent`` value (``""`` when unusable).

    Only ids that already have the W3C shape are rendered — a service that
    seeded its trace from some other system's id format gets an empty string
    rather than a header downstream parsers will reject.
    """
    trace = ctx.trace_id
    span = ctx.span_id or new_span_id()
    if len(trace) != 32 or len(span) != 16:
        return ""
    try:
        int(trace, 16)
        int(span, 16)
    except ValueError:
        return ""
    return f"00-{trace}-{span}-01"
