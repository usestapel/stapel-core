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
        self._unknown_topics_seen: set[str] = set()

    def _log_once_per_topic(self, error) -> None:
        """One WARNING per distinct unknown topic, not one per poll."""
        text = str(error)
        if text in self._unknown_topics_seen:
            return
        self._unknown_topics_seen.add(text)
        logger.warning(
            "KafkaBus: topic not available yet (waiting for it to appear): %s", text
        )

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

    def _provision_topics(self, topics: list[str]) -> None:
        """Create the topics this consumer is about to subscribe to.

        A consumer already DECLARES its topics — it is passing them to
        `subscribe()` on the next line. Requiring someone to also list them,
        by hand, somewhere else (a deploy script, a runbook, an infra repo) is
        a second source of truth that drifts silently: the ironmemo stand ran
        for weeks with six recordings topics missing from its deploy script's
        list, and all that surfaced was an endless

            ERROR KafkaBus consumer error: KafkaError{code=UNKNOWN_TOPIC_OR_PART}

        on a container that looked healthy — while nothing whatsoever was
        delivered. The NATS backend never had this problem, because its stream
        captures `<prefix>.>` and a new topic needs no broker-side change at
        all; Kafka was the odd one out, so it catches up here.

        Best-effort by construction: an already-existing topic is the normal
        case, and a broker that refuses creation (no ACL — set
        `KAFKA_PROVISION_TOPICS=false` to skip this entirely and say so out
        loud) must not stop a consumer that may well have the topics already.
        """
        from stapel_core.bus._config import KafkaBusConfig

        if not KafkaBusConfig.provision_topics():
            return
        try:
            from confluent_kafka.admin import AdminClient, NewTopic

            admin = AdminClient(KafkaBusConfig.admin_config())
            existing = set(admin.list_topics(timeout=10).topics)
            missing = [t for t in dict.fromkeys(topics) if t not in existing]
            # A poison message goes to `<topic>.dlq` (see `_send_raw_to_dlq`);
            # a DLQ that does not exist means the poison message is dropped
            # instead of parked, so they are provisioned together.
            missing += [
                _dlq_topic(t) for t in dict.fromkeys(topics)
                if _dlq_topic(t) not in existing
            ]
            if not missing:
                return
            new_topics = [
                NewTopic(
                    name,
                    num_partitions=KafkaBusConfig.topic_partitions(),
                    replication_factor=KafkaBusConfig.topic_replication(),
                )
                for name in missing
            ]
            for name, future in admin.create_topics(new_topics).items():
                try:
                    future.result()
                    logger.info("KafkaBus created topic %s", name)
                except Exception as exc:  # already exists (race), or no ACL
                    logger.info("KafkaBus could not create topic %s: %s", name, exc)
        except Exception:
            logger.warning(
                "KafkaBus topic provisioning skipped (admin client unavailable)",
                exc_info=True,
            )

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
        self._provision_topics(topics)
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
                self._touch_heartbeat()
                if msg is None:
                    continue
                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    if msg.error().code() == KafkaError.UNKNOWN_TOPIC_OR_PART:
                        # Transient by nature: brokers report this while a
                        # freshly created topic propagates, and librdkafka
                        # re-reports it on every metadata refresh — several
                        # lines per second, per topic. At ERROR that buries
                        # the real failures in a log nobody can then read; the
                        # condition itself is handled by `_provision_topics`
                        # above and by simply waiting.
                        self._log_once_per_topic(msg.error())
                        continue
                    logger.error("KafkaBus consumer error: %s", msg.error())
                    continue

                try:
                    event = Event.from_bytes(msg.value())
                except Exception:
                    # Poison message: deserialization failure outside the
                    # retry loop would crash consume() and, with the offset
                    # uncommitted, wedge the partition on restart.
                    logger.exception(
                        "KafkaBus undecodable message on %s, sending raw to DLQ",
                        msg.topic(),
                    )
                    if self._send_raw_to_dlq(msg.topic(), msg.value()):
                        consumer.commit(msg)
                    continue

                retries = 0
                dlq_ok = True
                while retries <= 3:
                    try:
                        handler(event)
                        break
                    except Exception:
                        retries += 1
                        if retries > 3:
                            logger.exception("KafkaBus DLQ event_id=%s", event.event_id)
                            dlq_ok = self._send_to_dlq(msg.topic(), event)
                        else:
                            time.sleep(2 ** retries)
                # Commit only when the message was handled or confirmed in
                # the DLQ — otherwise the offset would advance past a
                # message that exists nowhere else (silent loss).
                if dlq_ok:
                    consumer.commit(msg)
        finally:
            consumer.close()

    def _send_to_dlq(self, original_topic: str, event: Event) -> bool:
        try:
            self.publish(_dlq_topic(original_topic), event)
            return True
        except Exception:
            logger.exception("KafkaBus failed to send to DLQ")
            return False

    def _send_raw_to_dlq(self, original_topic: str, raw: bytes) -> bool:
        """DLQ a message that could not even be deserialized."""
        try:
            event = Event(
                event_type="__undecodable__",
                service="bus",
                payload={"raw": raw.decode("utf-8", errors="replace"), "topic": original_topic},
            )
            self.publish(_dlq_topic(original_topic), event)
            return True
        except Exception:
            logger.exception("KafkaBus failed to DLQ undecodable message")
            return False

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
