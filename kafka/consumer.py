"""
Base Kafka consumer as a Django management command.
"""

import logging
import os
import signal
import threading
import time
import traceback

from django.core.management.base import BaseCommand

from .config import KafkaConfig
from .events import Event
from .topics import dlq_topic

logger = logging.getLogger(__name__)


# Path written by the poll loop and read by the watchdog thread + the
# Docker HEALTHCHECK. Touch-based — mtime is enough to prove the loop
# advanced recently.
HEARTBEAT_PATH = os.environ.get("KAFKA_CONSUMER_HEARTBEAT", "/tmp/kafka_consumer_alive")
HEARTBEAT_STALENESS_S = int(os.environ.get("KAFKA_CONSUMER_HEARTBEAT_STALENESS_S", "120"))
WATCHDOG_INTERVAL_S = int(os.environ.get("KAFKA_CONSUMER_WATCHDOG_INTERVAL_S", "30"))


class BaseKafkaConsumerCommand(BaseCommand):
    """
    Base Django management command for Kafka consumers.

    Subclasses must define:
        topics: list[str] - topics to subscribe to
        consumer_group: str - consumer group ID
        handle_event(event: Event) -> None - process a single event

    Features:
        - Manual commit (at-least-once delivery)
        - Retry 3 times with exponential backoff
        - Dead letter topic for poison messages
        - Graceful shutdown on SIGINT/SIGTERM
    """

    topics: list[str] = []
    consumer_group: str = ""
    max_retries: int = 3
    base_backoff: float = 1.0  # seconds

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._running = True

    def add_arguments(self, parser):
        parser.add_argument(
            "--poll-timeout",
            type=float,
            default=0.1,
            help="Poll timeout in seconds (default: 0.1)",
        )

    def handle(self, *args, **options):
        from confluent_kafka import Consumer, KafkaError

        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        self._touch_heartbeat()  # baseline so the watchdog doesn't fire during startup
        self._start_watchdog()

        config = KafkaConfig.from_settings()
        conf = config.to_confluent_config()
        conf["group.id"] = self.consumer_group
        conf["auto.offset.reset"] = "earliest"
        conf["enable.auto.commit"] = False
        conf["session.timeout.ms"] = 10000  # 10s (default 45s) — faster rebalance on restart
        conf["heartbeat.interval.ms"] = 3000  # must be < session.timeout.ms / 3

        consumer = Consumer(conf)
        consumer.subscribe(self.topics)
        self.stdout.write(
            f"Kafka consumer started: group={self.consumer_group} topics={self.topics}"
        )

        poll_timeout = options.get("poll_timeout", 1.0)

        try:
            while self._running:
                msg = consumer.poll(timeout=poll_timeout)
                # Touch even on None — the loop advancing proves we're
                # not hung in poll(). Broker-disconnect noise is logged
                # by rdkafka separately; the consumer recovers on its
                # own once the broker is back, so the heartbeat firing
                # during the outage is the correct behaviour.
                self._touch_heartbeat()
                if msg is None:
                    continue

                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    logger.error("Kafka consumer error: %s", msg.error())
                    continue

                # Deserialize and process
                try:
                    event = Event.from_bytes(msg.value())
                except Exception:
                    logger.error(
                        "Failed to deserialize message from %s: %s",
                        msg.topic(),
                        traceback.format_exc(),
                    )
                    self._send_to_dlq(config, msg)
                    consumer.commit(message=msg)
                    continue

                # Retry with exponential backoff
                success = False
                for attempt in range(self.max_retries):
                    try:
                        self.handle_event(event)
                        success = True
                        break
                    except Exception:
                        wait = self.base_backoff * (2 ** attempt)
                        logger.warning(
                            "Event processing failed (attempt %d/%d), retrying in %.1fs: %s\n%s",
                            attempt + 1,
                            self.max_retries,
                            wait,
                            event.event_type,
                            traceback.format_exc(),
                        )
                        time.sleep(wait)

                if not success:
                    logger.error(
                        "Event processing failed after %d retries, sending to DLQ: %s %s",
                        self.max_retries,
                        event.event_type,
                        event.event_id,
                    )
                    self._send_to_dlq(config, msg)

                # Commit after processing (at-least-once)
                consumer.commit(message=msg)

        finally:
            self.stdout.write("Kafka consumer shutting down...")
            consumer.close()
            self.stdout.write("Kafka consumer stopped.")

    def handle_event(self, event: Event):
        """
        Process a single event. Override in subclass.

        Raises:
            Exception: If processing fails (will be retried).
        """
        raise NotImplementedError("Subclasses must implement handle_event()")

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        self.stdout.write(f"Received signal {signum}, shutting down gracefully...")
        self._running = False

    # ── liveness ──────────────────────────────────────────────

    def _touch_heartbeat(self) -> None:
        """Mtime of HEARTBEAT_PATH is the consumer's liveness signal.

        Read by the watchdog thread and by the Docker HEALTHCHECK
        (see iron-recordings.yml ``healthcheck`` stanza).
        """
        try:
            with open(HEARTBEAT_PATH, "a"):
                os.utime(HEARTBEAT_PATH, None)
        except OSError:
            # /tmp must always be writable; if it isn't there are bigger
            # problems than a stale heartbeat.
            logger.warning("Could not touch heartbeat at %s", HEARTBEAT_PATH)

    def _start_watchdog(self) -> None:
        """Daemon thread that os._exit(1)'s the worker on hung poll().

        rdkafka logs reconnect-failures forever but the Python poll()
        call itself stays responsive — so a stale heartbeat means the
        Python loop has stopped advancing, not a broker hiccup. Exiting
        triggers the compose ``restart: unless-stopped`` policy.
        """

        def watch():
            while True:
                time.sleep(WATCHDOG_INTERVAL_S)
                try:
                    age = time.time() - os.path.getmtime(HEARTBEAT_PATH)
                except OSError:
                    continue
                if age > HEARTBEAT_STALENESS_S:
                    logger.error(
                        "Watchdog: consumer heartbeat stale (%.0fs > %ds), exiting "
                        "to let docker restart the container",
                        age, HEARTBEAT_STALENESS_S,
                    )
                    os._exit(1)

        thread = threading.Thread(target=watch, daemon=True, name="kafka-watchdog")
        thread.start()

    def _send_to_dlq(self, config: KafkaConfig, msg):
        """Send a failed message to the dead letter queue topic."""
        try:
            from confluent_kafka import Producer

            dlq = dlq_topic(msg.topic())
            conf = config.to_confluent_config()
            producer = Producer(conf)
            producer.produce(
                topic=dlq,
                value=msg.value(),
                key=msg.key(),
                headers={"original_topic": msg.topic()},
            )
            producer.flush(timeout=5)
            logger.info("Message sent to DLQ: %s", dlq)
        except Exception:
            logger.error(
                "Failed to send message to DLQ: %s", traceback.format_exc()
            )
