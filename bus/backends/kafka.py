"""
Kafka bus backend — production transport via confluent-kafka.

Set in Django settings:
    STAPEL_BUS_BACKEND = "stapel_core.bus.backends.kafka.KafkaBus"
"""
from __future__ import annotations

import logging
import signal
import threading
import time
from typing import Callable

from ..base import DEFAULT_FLUSH_TIMEOUT, BusBackend
from ..dlq import record_parked
from ..event import Event
from ..liveness import ConsumerLiveness

logger = logging.getLogger(__name__)

DLQ_SUFFIX = ".dlq"

#: Upper bound on one ``poll()`` call, whatever the caller passed. A loop
#: that can block forever cannot notice anything, including that it has
#: stopped being a member of its group.
MAX_POLL_TIMEOUT = 1.0

#: Bound on the metadata request that answers "are the brokers reachable?".
BROKER_PROBE_TIMEOUT = 5.0


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

    def flush(self, timeout: float = DEFAULT_FLUSH_TIMEOUT) -> int:
        """Wait for librdkafka's queue to drain; return what is left in it.

        ``Producer.flush(timeout)`` serves the delivery reports of everything
        queued and returns the number of messages still pending, which is
        exactly this method's contract. A producer that was never created has
        nothing queued by construction — and must not be created here, since
        that would open a broker connection on the way out of a process that
        never published anything.
        """
        producer = self._producer
        if producer is None:
            return 0
        return producer.flush(timeout)

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
        a second source of truth that drifts silently: a client stand ran
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

        # Built before the Consumer, because the client's own error callback
        # is wired into its config: `error_cb` is how librdkafka reports the
        # things that never become a message — every broker down, the
        # session timeout that opened the incident, a fatal client state.
        liveness = ConsumerLiveness(
            group=group,
            topics=topics,
            backend="kafka",
            brokers_reachable=lambda: self._brokers_reachable(consumer),
        )
        config["error_cb"] = liveness.on_broker_error
        consumer = Consumer(config)
        self._subscribe(consumer, topics, liveness)
        liveness.declare_metrics()

        running = threading.Event()
        running.set()

        def _shutdown(signum, frame):
            logger.info("KafkaBus shutdown signal received")
            running.clear()

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        # A poll that can block forever is a loop that can notice nothing.
        poll_timeout = min(max(float(poll_timeout), 0.0), MAX_POLL_TIMEOUT)

        try:
            while running.is_set():
                msg = consumer.poll(timeout=poll_timeout)
                liveness.record_poll(self._assignment_size(consumer))
                liveness.touch_heartbeat()
                # Between messages — never inside a handler — the loop asks
                # whether it is still a member of anything. It exits the
                # process when it is not; see stapel_core.bus.liveness.
                liveness.check()
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

                liveness.record_message()

                try:
                    event = Event.from_bytes(msg.value())
                except Exception:
                    # Poison message: deserialization failure outside the
                    # retry loop would crash consume() and, with the offset
                    # uncommitted, wedge the partition on restart.
                    if self._send_raw_to_dlq(msg.topic(), msg.value()):
                        consumer.commit(msg)
                    continue

                # The whole attempt ladder — handler plus its backoff sleeps
                # — is work in progress, not idleness. `handling()` credits
                # that time back, so a slow message can never spend the
                # stall window and get its own worker killed for it.
                with liveness.handling():
                    dlq_ok = self._deliver(msg, event, handler)
                # Commit only when the message was handled or confirmed in
                # the DLQ — otherwise the offset would advance past a
                # message that exists nowhere else (silent loss).
                if dlq_ok:
                    consumer.commit(msg)
        finally:
            consumer.close()

    def _deliver(self, msg, event: Event, handler) -> bool:
        """Run *handler* with the retry ladder; True when the offset may move."""
        from stapel_core.django.db import worker_db_lifecycle

        retries = 0
        while retries <= 3:
            try:
                # Each ATTEMPT starts from a connection known to answer.
                # Without this the retries below are structurally incapable
                # of helping the most common failure a long-lived consumer
                # has: the database dropped the idle connection, so all four
                # attempts reuse the same dead socket and the event is DLQ'd
                # — and so is every event after it, forever, because nothing
                # ever resets it. (a client stand, 46h of lost notifications,
                # 2026-08-26.) The NATS backend and the function server
                # already did this; the Kafka path was the one loop that did
                # not.
                with worker_db_lifecycle():
                    handler(event)
                return True
            except Exception:
                retries += 1
                if retries > 3:
                    return self._send_to_dlq(msg.topic(), event)
                time.sleep(2 ** retries)
        return True  # pragma: no cover - the ladder always returns above

    @staticmethod
    def _subscribe(consumer, topics: list[str], liveness: ConsumerLiveness) -> None:
        """Subscribe with rebalance callbacks — the assignment is the signal.

        ``on_lost`` exists since confluent-kafka 1.6 and is what fires when
        the group membership is lost rather than handed over cleanly, which
        is the incident's exact shape; an older client reports the same thing
        through ``on_revoke``.
        """
        callbacks = {
            "on_assign": lambda _c, parts: liveness.on_assign(parts),
            "on_revoke": lambda _c, parts: liveness.on_revoke(parts),
            "on_lost": lambda _c, parts: liveness.on_revoke(parts),
        }
        try:
            consumer.subscribe(topics, **callbacks)
        except TypeError:  # pragma: no cover - confluent-kafka < 1.6
            callbacks.pop("on_lost")
            consumer.subscribe(topics, **callbacks)

    @staticmethod
    def _assignment_size(consumer) -> int | None:
        """How many partitions the client thinks it owns, or None if it cannot say.

        The rebalance callbacks above are the primary source; this is the
        cross-check, so that a client which somehow never fires them is still
        read correctly rather than declared stalled.
        """
        try:
            return len(consumer.assignment())
        except Exception:
            return None

    @staticmethod
    def _brokers_reachable(consumer) -> bool:
        """Is the cluster answering? A real metadata request, not a guess.

        This is what separates "we fell out of the group" (a restart fixes
        it) from "the brokers are gone" (a restart is pointless churn).
        """
        try:
            metadata = consumer.list_topics(timeout=BROKER_PROBE_TIMEOUT)
            return bool(getattr(metadata, "brokers", None))
        except Exception:
            return False

    def _send_to_dlq(self, original_topic: str, event: Event) -> bool:
        record_parked(original_topic, event)
        try:
            self.publish(_dlq_topic(original_topic), event)
            return True
        except Exception:
            logger.exception("KafkaBus failed to send to DLQ")
            return False

    def _send_raw_to_dlq(self, original_topic: str, raw: bytes) -> bool:
        """DLQ a message that could not even be deserialized."""
        record_parked(original_topic, reason="undecodable")
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

    # The in-process watchdog thread that used to live here is gone on
    # purpose. It read the very heartbeat file the loop refreshed after every
    # poll — including the polls of a consumer that owned nothing — so during
    # the sixteen-hour outage it saw a file seconds old and did nothing, all
    # night. A watchdog whose evidence the failing loop keeps writing cannot
    # detect that loop failing. The heartbeat now means "assigned and
    # polling" (stapel_core.bus.liveness), the loop itself decides to exit,
    # and judging staleness is the supervisor's job through
    # `manage.py bus_consumer_alive` — a process outside the one being judged.
