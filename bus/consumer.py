"""
Base Django management command for bus consumers.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

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
        parser.add_argument(
            "--allow-in-process",
            action="store_true",
            help=(
                "Run even on an in-process bus. Only meaningful in tests, "
                "where publisher and consumer share one process."
            ),
        )

    def handle(self, *args, **options):
        bus = get_bus()
        # A standalone consumer PROCESS on an in-process bus can never
        # receive anything: the queue lives in the publisher's memory, so
        # consume() drains an empty queue and returns, the process exits 0,
        # and a container restart policy turns that into an infinite silent
        # loop. Core 0.11.0 flipped the default from Kafka to MemoryBus, and
        # every deployment that did not then set STAPEL_BUS_BACKEND got
        # exactly that — no error, no events, just restarts (ironmemo stand,
        # weeks). Refuse loudly instead.
        if getattr(bus, "in_process", False) and not options.get("allow_in_process"):
            raise CommandError(
                f"{type(bus).__name__} is an in-process bus — a separate "
                f"consumer process can never receive events published by "
                f"another process, so this command would exit immediately "
                f"and restart forever. Point STAPEL_BUS_BACKEND at a broker "
                f"backend (e.g. 'stapel_core.bus.backends.kafka.KafkaBus' "
                f"with KAFKA_BOOTSTRAP_SERVERS), or pass --allow-in-process "
                f"if this really is a single-process test."
            )
        self.stdout.write(
            f"Starting consumer group={self.consumer_group} "
            f"topics={self.topics} backend={bus.__class__.__name__}"
        )
        bus.consume(
            self.topics,
            self.consumer_group,
            self._handle_traced,
            poll_timeout=options["poll_timeout"],
        )

    def _handle_traced(self, event: Event) -> None:
        """Bind the trace the message carries, then hand it to the subclass.

        A worker consuming a topic is the far end of an operation that
        started in somebody's HTTP request, possibly in another service. Its
        log lines belong to that operation, and until they carry its
        ``trace_id`` there is no way to see the two halves together. Bound
        here rather than in :meth:`handle_event` so every existing consumer
        subclass gets it without changing a line.
        """
        from ..observability.context import continue_trace

        with continue_trace(event):
            self.handle_event(event)

    def handle_event(self, event: Event) -> None:
        raise NotImplementedError
