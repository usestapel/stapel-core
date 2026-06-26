"""
Base Django management command for bus consumers.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from .event import Event
from .router import get_bus


class BaseBusConsumerCommand(BaseCommand):
    """
    Subclass and set ``topics``, ``consumer_group``, implement ``handle_event``.

        class ConsumeProfiles(BaseBusConsumerCommand):
            topics = ["profile.changed"]
            consumer_group = "notifications"

            def handle_event(self, event: Event) -> None:
                ...
    """

    topics: list[str] = []
    consumer_group: str = ""

    def add_arguments(self, parser):
        parser.add_argument("--poll-timeout", type=float, default=0.1)

    def handle(self, *args, **options):
        bus = get_bus()
        self.stdout.write(
            f"Starting consumer group={self.consumer_group} "
            f"topics={self.topics} backend={bus.__class__.__name__}"
        )
        bus.consume(
            self.topics,
            self.consumer_group,
            self.handle_event,
            poll_timeout=options["poll_timeout"],
        )

    def handle_event(self, event: Event) -> None:
        raise NotImplementedError
