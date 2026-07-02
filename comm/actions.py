"""Action primitive — transactional fire-and-forget events.

emit() writes the event into the outbox inside the caller's DB transaction;
delivery happens after commit (immediately via on_commit, with the outbox
relay retrying anything that failed). Subscribers MUST be idempotent —
delivery is at-least-once, exactly like a broker.
"""
from __future__ import annotations

import logging
from typing import Callable

from ..bus.event import Event
from .config import comm_setting, service_name
from .exceptions import ActionDeliveryError
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
    """
    event = Event(
        event_type=name,
        service=service or service_name(),
        payload=payload or {},
        version=version,
        key=key,
    )
    action_registry.validate(name, event.payload)

    if comm_setting("OUTBOX_ENABLED", True):
        _emit_via_outbox(event)
    else:
        deliver(event)
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


def deliver(event: Event) -> None:
    """Deliver *event* over the configured transport. Raises on failure so
    the outbox can retry."""
    transport = comm_setting("ACTION_TRANSPORT", "inprocess")

    if transport == "inprocess":
        errors: list[Exception] = []
        for handler in action_registry.handlers(event.event_type):
            try:
                handler(event)
            except Exception as exc:
                logger.exception(
                    "action handler %r failed for %s", handler, event.event_type
                )
                errors.append(exc)
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
