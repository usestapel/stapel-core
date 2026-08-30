"""Action primitive — transactional fire-and-forget events.

emit() writes the event into the outbox inside the caller's DB transaction;
delivery happens after commit (immediately via on_commit, with the outbox
relay retrying anything that failed). Subscribers MUST be idempotent —
delivery is at-least-once, exactly like a broker.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Callable

from ..bus.event import Event
from .config import comm_setting, service_name
from .exceptions import ActionDeliveryError, EmitOutsideAtomicError
from .registry import ActionHandler, action_registry

logger = logging.getLogger(__name__)


def on_action(name: str, *, schema: dict | None = None) -> Callable[[ActionHandler], ActionHandler]:
    """Decorator: subscribe to action *name*.

        @on_action("user.deleted")
        def erase_profile(event): ...
    """

    def decorator(handler: ActionHandler) -> ActionHandler:
        subscribe_action(name, handler, schema=schema)
        return handler

    return decorator


def subscribe_action(name: str, handler: ActionHandler, *, schema: dict | None = None) -> None:
    action_registry.subscribe(name, handler)
    action_registry.register_schema(name, schema)


def emit(
    name: str,
    payload: dict | None = None,
    *,
    key: str | None = None,
    version: int = 1,
    service: str | None = None,
) -> Event:
    """Emit action *name*. Returns the Event envelope.

    Inside transaction.atomic() the event is persisted with the transaction
    and delivered after commit; a rollback discards it — the event never
    lies about state that was not committed.

    Two runtime guards protect that guarantee (outbox mode only):

    - Called *outside* an atomic block, the outbox row would commit on its
      own, detached from the mutation it describes (the listings L2 bug:
      crash between save() and emit() = published-but-unindexed forever).
      Behavior per ``STAPEL_COMM["EMIT_OUTSIDE_ATOMIC"]``: ``"warn"``
      (default, logs with caller location), ``"error"`` (raises
      :class:`EmitOutsideAtomicError`), ``"allow"``. This also fires for
      emit inside an ``on_commit`` callback — an event written *after*
      commit is lost if the process dies in between.
    - If emit fails inside an atomic block, the transaction is marked
      rollback-only before the exception propagates. Even a caller that
      swallows the exception (the categories C1 bug) cannot commit the
      mutation without its event: they commit together or not at all.

    Prefer :func:`mutate_and_emit` for the mutation+emit pattern.
    """
    event = Event(
        event_type=name,
        service=service or service_name(),
        payload=payload or {},
        version=version,
        key=key,
    )

    if not comm_setting("OUTBOX_ENABLED", True):
        action_registry.validate(name, event.payload)
        deliver(event)
        return event

    from django.db import transaction

    connection = transaction.get_connection()
    if not connection.in_atomic_block:
        _emit_outside_atomic(name)
    try:
        action_registry.validate(name, event.payload)
        _emit_via_outbox(event)
    except Exception:
        # Swallow-proofing: a failed emit must never let the surrounding
        # mutation commit without its event. Marking the transaction
        # rollback-only makes catching this exception harmless — the atomic
        # block rolls back regardless.
        if connection.in_atomic_block:
            transaction.set_rollback(True)
        raise
    return event


def _emit_outside_atomic(name: str) -> None:
    mode = comm_setting("EMIT_OUTSIDE_ATOMIC", "warn")
    if mode == "allow":
        return
    message = (
        "emit(%r) called outside transaction.atomic(): the outbox row commits "
        "detached from the mutation it describes. Wrap mutation+emit in "
        "stapel_core.comm.mutate_and_emit() (or transaction.atomic())."
    )
    if mode == "error":
        raise EmitOutsideAtomicError(message % name)
    logger.warning(message, name, stack_info=True)


@contextmanager
def mutate_and_emit(using: str | None = None, *, savepoint: bool = True):
    """Mutation + emit as one transaction — the canonical outbox pattern.

        from stapel_core.comm import mutate_and_emit

        with mutate_and_emit() as emit:
            listing.status = ListingStatus.PUBLISHED
            listing.save(update_fields=["status"])
            emit("listing.published", {"listing_id": str(listing.pk)},
                 key=str(listing.pk))

    Everything in the block — the ORM writes and the outbox rows — commits
    or rolls back as one unit; delivery happens after commit. The yielded
    callable has the exact :func:`emit` signature and may be called any
    number of times (0..N: conditional emits and fanouts are fine); plain
    ``emit()`` and ``emit_*`` helper functions called inside the block get
    the same protection, so ``with mutate_and_emit():`` without ``as`` is a
    valid self-documenting form.

    Guards (beyond ``transaction.atomic(using, savepoint)`` semantics):

    - a failed emit marks the transaction rollback-only, so swallowing the
      exception inside the block still rolls the whole mutation back;
    - the yielded callable raises ``RuntimeError`` if it leaks out of the
      block and is called after exit (its emit would be a separate
      transaction);
    - nesting inside a wider ``transaction.atomic()`` is safe — everything
      joins the outer transaction, events still leave only on outer commit.

    Delivery stays at-least-once (outbox relay retries) — subscribers must
    be idempotent, exactly as with plain ``emit()``.
    """
    from django.db import transaction

    emitter = _ScopedEmitter()
    try:
        with transaction.atomic(using=using, savepoint=savepoint):
            yield emitter
    finally:
        emitter._active = False


class _ScopedEmitter:
    """emit() bound to a mutate_and_emit() block; inert once the block exits."""

    __slots__ = ("_active", "events")

    def __init__(self) -> None:
        self._active = True
        self.events: list[Event] = []

    def __call__(
        self,
        name: str,
        payload: dict | None = None,
        *,
        key: str | None = None,
        version: int = 1,
        service: str | None = None,
    ) -> Event:
        if not self._active:
            raise RuntimeError(
                "mutate_and_emit() emitter used after its block exited — this "
                "emit would run in a separate transaction, detached from the "
                "mutation. Emit inside the with-block."
            )
        event = emit(name, payload, key=key, version=version, service=service)
        self.events.append(event)
        return event


def _emit_via_outbox(event: Event) -> None:
    from django.db import transaction

    from ..django.outbox.models import OutboxEvent

    row = OutboxEvent.objects.create(topic=event.event_type, event_json=event.to_json())
    transaction.on_commit(lambda: _dispatch_row(row.pk))


def _dispatch_row(pk) -> None:
    """First-chance delivery right after commit. Failures stay in the outbox
    for the relay (dispatch_outbox) — never raised into the request cycle."""
    from ..django.outbox.relay import dispatch_one

    try:
        dispatch_one(pk)
    except Exception:
        logger.exception("outbox first-chance dispatch failed for row %s", pk)


def deliver_to_subscribers(event, handlers) -> list[Exception]:
    """Run *handlers* for *event*; return only the failures worth retrying.

    Two kinds of handler failure look identical to a bus and are not:

    - a **transient** one (the database was down, a peer timed out). Raising
      it is right: redelivery is exactly the repair.
    - an **unprocessable payload** — the message decoded, reached working
      code, and that code refused its *values*. Django raises
      :class:`~django.core.exceptions.ValidationError` for this, and the
      commonest instance in this fleet is a malformed id reaching a queryset:
      ``AUTH_USER_MODEL.id`` is a UUID, so ``filter(user_id="not-a-uuid")``
      raises inside ``UUIDField.to_python``. ``ValidationError`` is not a
      subclass of ``ValueError``, so the ``except (ValueError, TypeError)``
      guard almost every consumer wrote does not catch it.

    Redelivering the second kind produces the same refusal forever. It is a
    poison pill: at-least-once delivery turns one malformed payload into an
    unbounded retry loop that blocks every event behind it on that partition,
    and the handler is not at fault — nothing it could have done would make
    that id parse.

    So an escaping ``ValidationError`` is parked, not raised: counted in
    ``bus_dlq_total{reason="unprocessable"}`` (a number an operator can alarm
    on) and logged with its traceback, and delivery moves on. Set
    ``STAPEL_COMM["UNPROCESSABLE_PAYLOAD"] = "raise"`` to stop the line
    instead.

    This is a floor under every consumer, not permission to skip the guard:
    a handler that validates its own payload gets a clean skip and a log line
    of its own choosing, which is better than a DLQ entry.

    The convention that follows, and it is the whole contract:
    **``ValidationError`` is never a retry signal.** A handler that hits a
    genuinely transient condition — a survivor row that has not landed yet, a
    peer that timed out — must raise something else (a ``RuntimeError``
    subclass naming the condition, e.g. the fleet's ``MergeTargetNotReady``).
    Raising ``ValidationError`` for a transient case now means "give up on
    this event", which is not what the raiser meant.
    """
    from django.core.exceptions import ValidationError

    retriable: list[Exception] = []
    for handler in handlers:
        try:
            handler(event)
        except ValidationError as exc:
            if comm_setting("UNPROCESSABLE_PAYLOAD", "park") == "raise":
                logger.exception(
                    "action handler %r rejected the payload of %s",
                    handler, event.event_type,
                )
                retriable.append(exc)
                continue
            _park_unprocessable(event, handler, exc)
        except Exception as exc:  # noqa: BLE001 — collected for the caller
            logger.exception(
                "action handler %r failed for %s", handler, event.event_type
            )
            retriable.append(exc)
    return retriable


def _park_unprocessable(event, handler, exc) -> None:
    """Count and log one unprocessable payload. Never raises.

    Loud on purpose. Parking means the work in that handler did NOT happen,
    and the worst instance is a GDPR erasure handler quietly not erasing. The
    line names the action, the event id, the handler and the values the field
    refused, so the fix does not require reproducing the payload.
    """
    from ..bus.dlq import record_parked

    logger.error(
        "action %s event_id=%s: handler %r refused the payload as "
        "unprocessable (%s). Redelivery cannot repair a value no field can "
        "coerce, so it is parked, not retried — THE HANDLER'S WORK DID NOT "
        "HAPPEN. Alert on bus_dlq_total{reason=\"unprocessable\"}.",
        event.event_type, getattr(event, "event_id", None), handler,
        "; ".join(getattr(exc, "messages", None) or [str(exc)]),
        exc_info=True,
    )
    record_parked(event.event_type, event, reason="unprocessable")


def deliver(event: Event) -> None:
    """Deliver *event* over the configured transport. Raises on failure so
    the outbox can retry.

    Task events are special-cased: with ``TASK_DISPATCH == "bus"`` every
    ``task.*`` event goes straight to :mod:`stapel_core.bus` even when
    ``ACTION_TRANSPORT`` is inprocess — a monolith keeps Actions in-process
    while Tasks travel through a broker to a dedicated worker. Both the
    first-chance on_commit dispatch and the outbox relay funnel through
    here, so the routing holds on retries too.
    """
    if (
        event.event_type.startswith("task.")
        and comm_setting("TASK_DISPATCH", "action") == "bus"
    ):
        from ..bus import publish

        publish(event.event_type, event)
        return

    transport = comm_setting("ACTION_TRANSPORT", "inprocess")

    if transport == "inprocess":
        # The subscriber inherits the trace the event carries: its log lines,
        # its metrics and anything it emits in turn join the operation that
        # caused it, with causation_id pointing back at this event. Without
        # this the fan-out is a scatter of unrelated records — and the outbox
        # makes that worse, since a handler can run minutes later in another
        # process, where nothing about the originating request is in scope.
        from ..observability.context import continue_trace

        with continue_trace(event):
            errors = deliver_to_subscribers(
                event, action_registry.handlers(event.event_type)
            )
        if errors:
            raise ActionDeliveryError(event.event_type, errors)
        return

    if transport in ("bus", "memory"):
        from ..bus import publish

        publish(event.event_type, event)
        return

    raise ActionDeliveryError(
        event.event_type,
        [ValueError(f"unknown ACTION_TRANSPORT {transport!r}")],
    )
