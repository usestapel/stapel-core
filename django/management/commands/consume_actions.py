"""Bus-to-registry bridge for Action events.

In a monolith actions are delivered in-process. In a bus deployment
(``STAPEL_COMM = {"ACTION_TRANSPORT": "bus"}``) the emitting service
publishes each action to a topic named after the action (``user.deleted``),
and every service that subscribes to remote actions runs::

    python manage.py consume_actions

The command consumes the topics for all actions the local apps subscribed
to via ``@on_action`` (or an explicit ``--topics`` subset) and hands each
event to the local registry. A handler exception propagates to the bus
backend so its retry/DLQ semantics apply.
"""
from __future__ import annotations

from stapel_core.bus import BaseBusConsumerCommand, Event
from stapel_core.comm.config import service_name
from stapel_core.comm.exceptions import ActionDeliveryError
from stapel_core.comm.registry import action_registry


class Command(BaseBusConsumerCommand):
    help = "Consume remote Action events and dispatch them to local @on_action handlers."

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--topics",
            nargs="*",
            default=None,
            help="Action names to consume (default: every action with a local subscriber)",
        )
        parser.add_argument(
            "--group",
            default=None,
            help="Consumer group (default: '<service>.actions')",
        )

    def handle(self, *args, **options):
        self.topics = options["topics"] or action_registry.names()
        if not self.topics:
            self.stderr.write("No local @on_action subscriptions; nothing to consume.")
            return
        self.consumer_group = options["group"] or f"{service_name()}.actions"
        super().handle(*args, **options)

    def handle_event(self, event: Event) -> None:
        handlers = action_registry.handlers(event.event_type)
        errors: list[Exception] = []
        for handler in handlers:
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001 — collected and re-raised below
                errors.append(exc)
        if errors:
            raise ActionDeliveryError(event.event_type, errors)
