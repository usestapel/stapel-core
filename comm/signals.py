"""Signal primitive — "show this to whoever is watching right now".

The fourth comm primitive, next to Action, Function and Task. The first
three all address **code**; Signal addresses a **human's screen**:

    Function — "answer me now" (the caller waits).
    Action   — "this happened, the system must know" (outbox, at-least-once).
    Task     — "do long work, the system waits, not the caller".
    Signal   — "show this to a live observer, if one is watching".

There is no obligation towards the observer. Someone who is not watching is
owed nothing after the fact: when they look, they read the current state over
REST. A signal's value expires in seconds.

    from stapel_core.comm import signal

    with mutate_and_emit() as emit_event:              # or transaction.atomic()
        recording.status = "ready"
        recording.save(update_fields=["status"])
        emit_event("recording.completed", {...})       # the fact — durable
        signal(f"recordings:ws:{workspace_id}",         # the screen — ephemeral
               "recording.status",
               {"recording_id": str(recording.pk), "status": recording.status})

What is guaranteed
------------------
* Delivery only to subscribers connected **at the moment of the emit**.
* Ordering only **within one stream key**.
* A signal never overtakes the transaction it describes: delivery is
  scheduled through ``transaction.on_commit`` (outside a transaction that
  runs immediately, which is the same statement — there is nothing to wait
  for). Unlike :func:`~stapel_core.comm.emit` there is no outbox row.

What is NOT guaranteed
----------------------
* Delivery at all — no subscriber, a dropped socket, a full layer: the frame
  disappears, and that is correct behaviour, not an incident.
* Ordering between stream keys, redelivery, or any history. Durability, where
  a module needs it (chat), belongs to the module's own MODEL — a resumable
  stream replays from that, never from the transport.

**Client invariant**: state that cannot be recovered with a REST request must
not travel by Signal. A signal saves polling; it does not replace the truth.
Keep payloads minimal for the same reason a stream key is scoped — content is
allowed only on a stream whose authorization gate matches the read right for
that content (workspace-wide streams carry ids and status changes, the
content is fetched over REST with its per-object checks).

Signal is not "Action delivered to the browser"
-----------------------------------------------
Signals never ride the outbox. The outbox is at-least-once with retries up to
300s: delivering "typing…" five minutes late is absurd, and it accumulates
rows in a table with no retention. An Action's subscriber is a module obliged
to handle it; a Signal's subscribers are 0..N browsers whose absence is
normal. The canonical BRIDGE is encouraged instead: an ``@on_action`` handler
(or the same ``mutate_and_emit`` block) translates a committed fact into a
``signal()``. The fact travels reliably, the screen notification ephemerally.
No client-side dedup is needed — a signal is a reason to refetch, and a
refetch is idempotent.

Transport seam
--------------
This module is the emitter only: ~stdlib, no channels, no redis, no ASGI.
Delivery is an axis, ``STAPEL_COMM["SIGNAL_TRANSPORT"]``, closed by default —
without a configured transport ``signal()`` is a silent no-op, and every host
that serves HTTP only (which today is every host but one) keeps working with
clients that poll or refetch. The delivery half — consumers, per-stream
authorization, revoke/kick, system checks — lives in the separate
``stapel-realtime`` library, which registers itself into this seam. Emitting
must be free for all 26 libraries, or modules will "save" on signalling.

(Unrelated to :mod:`stapel_core.signals`, which is ``django.dispatch`` —
same-process extension points for host projects.)
"""
from __future__ import annotations

import logging
import re
from typing import Callable

from .config import comm_setting
from .exceptions import InvalidSignalType, InvalidStreamKey

logger = logging.getLogger(__name__)

#: Wire envelope version. Bumped only for a breaking envelope change; the
#: version travels in every frame so a client can refuse what it cannot read.
SIGNAL_ENVELOPE_VERSION = 1

#: ``transport(stream_key, frame) -> None``. See :func:`register_signal_transport`.
SignalTransport = Callable[[str, dict], None]

# <mod>:<scope_type>:<scope_id>[:<topic>] — the canonical stream key
# (realtime-substrate / stapel-realtime-design §6.1). The scope is PART of the
# name, so a group physically cannot cross a workspace; knowing the name grants
# nothing, subscription is authorized separately and fail-closed.
_SEGMENT = r"[A-Za-z0-9_-]+"
_STREAM_KEY_RE = re.compile(rf"^{_SEGMENT}:{_SEGMENT}:{_SEGMENT}(:{_SEGMENT})?$")

# Frame types owned by the stapel-realtime wire protocol. A signal may not
# claim one: the consumer would read a courtesy frame as protocol.
RESERVED_FRAME_TYPES = frozenset({
    "hello", "welcome", "replay", "replay_done", "live", "ephemeral",
    "ping", "pong", "resync", "kick", "error",
})
_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")

_transports: dict[str, SignalTransport] = {}


def stream_key(
    module: str,
    scope_type: str,
    scope_id: str | int,
    topic: str | None = None,
) -> str:
    """Build a canonical stream key ``<mod>:<scope_type>:<scope_id>[:<topic>]``.

        stream_key("recordings", "ws", workspace_id)   # recordings:ws:42
        stream_key("chat", "conv", conversation_id)    # chat:conv:7
        stream_key("video", "lobby", join_code)        # video:lobby:AB12CD

    The scope is part of the name on purpose — a stream is addressable only
    within the scope whose right the subscriber's ``authorize()`` hook checks.
    """
    parts = [module, scope_type, str(scope_id)]
    if topic is not None:
        parts.append(topic)
    key = ":".join(parts)
    _validate_stream_key(key)
    return key


def register_signal_transport(name: str, transport: SignalTransport) -> None:
    """Register a delivery backend under *name* for ``SIGNAL_TRANSPORT``.

    The contract of *transport*:

    * called as ``transport(stream_key, frame)`` — the routing key and the
      complete wire envelope (see :func:`signal`), already JSON-shaped;
    * called AFTER the surrounding transaction commits, in the committing
      thread — it must not block on a slow client (fan out to a layer and
      return; back-pressure is the transport's problem, never the emitter's);
    * may raise: the caller of ``signal()`` is protected, the exception is
      logged and the frame dropped, which is a legal outcome for a signal;
    * must be idempotent-safe to skip — no observer means no delivery, and
      that is not an error condition.

    ``stapel-realtime`` registers ``"channels"`` from its ``AppConfig.ready()``;
    the microservice-mode ``"bus"`` transport (NATS ``stapel.ws.*``) is future
    work behind the same seam. A dotted path to a callable also works without
    any registration.
    """
    _transports[name] = transport


def signal_transport() -> SignalTransport | None:
    """Resolve ``STAPEL_COMM["SIGNAL_TRANSPORT"]`` to a callable, or None.

    ``"none"`` (the default), an empty value or ``False`` mean "no delivery
    backend on this host" — the documented, supported configuration. An
    unresolvable value resolves to None as well (a signal must not break a
    request over configuration), and is reported at boot by the
    ``stapel_core.comm.E003`` system check.
    """
    value = comm_setting("SIGNAL_TRANSPORT", "none")
    if not value or value == "none":
        return None
    if callable(value):
        return value
    if value in _transports:
        return _transports[value]
    if isinstance(value, str) and "." in value:
        from django.utils.module_loading import import_string

        try:
            resolved = import_string(value)
        except ImportError:
            logger.error(
                'STAPEL_COMM["SIGNAL_TRANSPORT"] = %r cannot be imported; '
                "signals are being dropped on this host.", value,
            )
            return None
        if not callable(resolved):
            logger.error(
                'STAPEL_COMM["SIGNAL_TRANSPORT"] = %r is not callable; '
                "signals are being dropped on this host.", value,
            )
            return None
        return resolved
    logger.error(
        'STAPEL_COMM["SIGNAL_TRANSPORT"] = %r is neither "none", a registered '
        "transport name, nor a dotted path — signals are being dropped on "
        "this host. Registered: %s",
        value, sorted(_transports) or "(none)",
    )
    return None


def signal(
    stream: str,
    type: str,
    payload: dict | None = None,
    *,
    using: str | None = None,
) -> dict:
    """Emit an ephemeral frame on *stream*. Returns the wire envelope.

        signal("recordings:ws:42", "recording.status",
               {"recording_id": "...", "status": "ready"})

    The envelope (wire v1, shared with ``stapel-realtime``)::

        {"v": 1, "type": "recording.status",
         "stream": "recordings:ws:42", "payload": {...}}

    ``stream`` is optional in the schema (a v1 socket carries one stream, so a
    client can ignore it) but always populated here: it is what makes a
    multiplexed socket possible later without a v2 envelope, and it costs one
    key today.

    There is deliberately no ``seq``. Frame kind is structural, not a flag: a
    journal frame carries ``seq`` from the module's own persisted model, an
    ephemeral frame cannot, so a signal can never be mistaken for — or
    persisted as — journal state. Modules that need a journal write their
    model first and then signal the group (store-first).

    Returns the envelope whether or not it was delivered, or even schedulable
    — the return value describes the frame, not its fate.

    Raises :class:`InvalidStreamKey` / :class:`InvalidSignalType` for a
    malformed address; those are programming errors and are raised even with
    no transport configured, so the no-op default still gates the canon.
    Everything downstream of that (scheduling, transport failure) is logged
    and swallowed: a courtesy to an observer must never break the caller.
    """
    _validate_stream_key(stream)
    _validate_type(type)
    if payload is not None and not isinstance(payload, dict):
        raise TypeError(
            f"signal({stream!r}, {type!r}) payload must be a dict of "
            f"JSON-serializable values, got {payload.__class__.__name__}"
        )

    frame = {
        "v": SIGNAL_ENVELOPE_VERSION,
        "type": type,
        "stream": stream,
        "payload": payload or {},
    }

    transport = signal_transport()
    if transport is None:
        return frame

    try:
        from django.db import transaction

        transaction.on_commit(
            lambda: _deliver(transport, stream, frame), using=using
        )
    except Exception:
        logger.warning(
            "signal(%r, %r) could not be scheduled for delivery; frame "
            "dropped (the observer will resync over REST)",
            stream, type, exc_info=True,
        )
    return frame


def _deliver(transport: SignalTransport, stream: str, frame: dict) -> None:
    """Hand the frame to the transport after commit. Never raises: this runs
    inside an ``on_commit`` callback, where an exception would surface in the
    caller's commit — for a frame whose loss is explicitly correct."""
    try:
        transport(stream, frame)
    except Exception:
        logger.warning(
            "signal transport failed for stream %r (type %r); frame dropped",
            stream, frame.get("type"), exc_info=True,
        )


def _validate_stream_key(key: str) -> None:
    if not isinstance(key, str) or not _STREAM_KEY_RE.match(key):
        raise InvalidStreamKey(
            f"{key!r} is not a canonical stream key. Expected "
            "'<mod>:<scope_type>:<scope_id>[:<topic>]' with segments of "
            "[A-Za-z0-9_-], e.g. 'recordings:ws:42' or 'chat:conv:7'. The "
            "scope is part of the name so a stream physically cannot cross "
            "it — build keys with stapel_core.comm.stream_key()."
        )


def _validate_type(type: str) -> None:
    if not isinstance(type, str) or not _TYPE_RE.match(type):
        raise InvalidSignalType(
            f"{type!r} is not a valid signal type. Expected a dotted lower "
            "snake_case name, e.g. 'recording.status' or 'typing'."
        )
    if type in RESERVED_FRAME_TYPES:
        raise InvalidSignalType(
            f"{type!r} is reserved by the stapel-realtime wire protocol "
            f"({', '.join(sorted(RESERVED_FRAME_TYPES))}) — a consumer would "
            "read this courtesy frame as a protocol frame. Name the signal "
            "after what happened, e.g. '<object>.status'."
        )


__all__ = [
    "SIGNAL_ENVELOPE_VERSION",
    "RESERVED_FRAME_TYPES",
    "SignalTransport",
    "signal",
    "signal_transport",
    "stream_key",
    "register_signal_transport",
]
