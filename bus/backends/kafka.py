"""
Kafka bus backend — production transport via confluent-kafka.

Set in Django settings:
    STAPEL_BUS_BACKEND = "stapel_core.bus.backends.kafka.KafkaBus"
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
from typing import Callable

from ..base import BusBackend
from ..event import Event

logger = logging.getLogger(__name__)

HEARTBEAT_PATH = os.environ.get("KAFKA_CONSUMER_HEARTBEAT", "/tmp/kafka_consumer_alive")
HEARTBEAT_STALENESS_S = int(os.environ.get("KAFKA_CONSUMER_HEARTBEAT_STALENESS_S", "120"))
WATCHDOG_INTERVAL_S = int(os.environ.get("KAFKA_CONSUMER_WATCHDOG_INTERVAL_S", "30"))

DLQ_SUFFIX = ".dlq"


def _dlq_topic(topic: str) -> str:
    return topic + DLQ_SUFFIX


class KafkaBus(BusBackend):
    """Thin wrapper around confluent-kafka Producer/Consumer."""

    def __init__(self) -> None:
        self._producer = None
        self._producer_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    def _get_producer(self):
        if self._producer is not None:
            return self._producer
        with self._producer_lock:
            if self._producer is not None:
                return self._producer
            from confluent_kafka import Producer
            from stapel_core.bus._config import KafkaBusConfig
            self._producer = Producer(KafkaBusConfig.producer_config())
        return self._producer

    def publish(self, topic: str, event: Event) -> None:
        producer = self._get_producer()
        key_bytes = (event.key or event.event_id).encode("utf-8")
        producer.produce(
            topic,
            key=key_bytes,
            value=event.to_bytes(),
            callback=self._delivery_callback,
        )
        producer.poll(0)

    @staticmethod
    def _delivery_callback(err, msg):
        if err:
            logger.error("KafkaBus delivery failed: %s topic=%s", err, msg.topic())
        else:
            logger.debug("KafkaBus delivered topic=%s offset=%s", msg.topic(), msg.offset())

    # ------------------------------------------------------------------
    # Consume
    # ------------------------------------------------------------------

    def consume(
        self,
        topics: list[str],
        group: str,
        handler: Callable[[Event], None],
        *,
        poll_timeout: float = 0.1,
    ) -> None:
        from confluent_kafka import Consumer, KafkaError
        from stapel_core.bus._config import KafkaBusConfig

        config = KafkaBusConfig.consumer_config(group)
        consumer = Consumer(config)
        consumer.subscribe(topics)

        running = threading.Event()
        running.set()

        def _shutdown(signum, frame):
            logger.info("KafkaBus shutdown signal received")
            running.clear()

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        self._start_watchdog(running)

        try:
            while running.is_set():
                msg = consumer.poll(timeout=poll_timeout)
                if msg is None:
                    continue
                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    logger.error("KafkaBus consumer error: %s", msg.error())
                    continue

                self._touch_heartbeat()
                event = Event.from_bytes(msg.value())
                retries = 0
                while retries <= 3:
                    try:
                        handler(event)
                        break
                    except Exception:
                        retries += 1
                        if retries > 3:
                            logger.exception("KafkaBus DLQ event_id=%s", event.event_id)
                            self._send_to_dlq(msg.topic(), event)
                        else:
                            time.sleep(2 ** retries)
                consumer.commit(msg)
        finally:
            consumer.close()

    def _send_to_dlq(self, original_topic: str, event: Event) -> None:
        try:
            self.publish(_dlq_topic(original_topic), event)
        except Exception:
            logger.exception("KafkaBus failed to send to DLQ")

    @staticmethod
    def _touch_heartbeat() -> None:
        try:
            open(HEARTBEAT_PATH, "w").close()
        except OSError:
            pass

    def _start_watchdog(self, running: threading.Event) -> None:
        def _watch():
            while running.is_set():
                time.sleep(WATCHDOG_INTERVAL_S)
                try:
                    mtime = os.path.getmtime(HEARTBEAT_PATH)
                    age = time.time() - mtime
                    if age > HEARTBEAT_STALENESS_S:
                        logger.critical("KafkaBus heartbeat stale (%.0fs), exiting", age)
                        running.clear()
                        os.kill(os.getpid(), signal.SIGTERM)
                except FileNotFoundError:
                    pass

        t = threading.Thread(target=_watch, daemon=True)
        t.start()
