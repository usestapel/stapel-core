"""
Redis Streams bus backend — consumer-group transport via redis-py.

Set in Django settings:
    STAPEL_BUS_BACKEND = "stapel_core.bus.backends.redis_streams.RedisStreamsBus"

or the shorthand (env or setting):
    STAPEL_BUS_BACKEND=redis_streams   # or the alias: redis

One Redis stream per topic (``XADD``, optionally capped with ``MAXLEN ~``);
one consumer group per subscriber, named after the ``group`` passed to
``consume()`` — same convention as the Kafka backend's ``group.id`` and the
NATS backend's durable name. Multiple processes consuming the same ``group``
share the group and load-balance via ``XREADGROUP``, each identified by its
own consumer name (``group:host:pid``) so a crashed replica's pending
entries can be recovered by another.

Delivery semantics mirror the Kafka/NATS backends:
- at-least-once: ``XACK`` only after the handler succeeded or the message
  was confirmed in the DLQ stream (``<topic>.dlq``)
- handler failures retry 3x with backoff, then DLQ
- undecodable (poison) messages go straight to the DLQ instead of wedging
  the consumer
- a fresh consumer group starts at the beginning of the stream (``id="0"``),
  matching Kafka's ``auto.offset.reset=earliest`` and JetStream's default
  DeliverAll policy for a new durable
- entries left pending by a consumer that died mid-handler (crash, kill -9,
  OOM) are reclaimed via ``XAUTOCLAIM`` once idle past
  ``STAPEL_REDIS_BUS_CLAIM_IDLE_MS`` and re-run through the same
  retry/DLQ path — checked once per poll-loop iteration, so nothing waits
  longer than one ``poll_timeout`` past the idle threshold to be noticed

Requires redis-py (``pip install 'stapel-core[redis-bus]'``).
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from typing import Callable

from ..base import BusBackend
from ..event import Event

logger = logging.getLogger(__name__)

DLQ_SUFFIX = ".dlq"
MAX_HANDLER_RETRIES = 3
READ_COUNT = 10
CLAIM_COUNT = 10


def _dlq_topic(topic: str) -> str:
    return topic + DLQ_SUFFIX


def _consumer_name(group: str) -> str:
    """Unique per-process consumer identity within a shared group.

    Redis Streams needs a distinct consumer name per replica so a crashed
    replica's pending entries (attributed to its name in the PEL) remain
    visible to XAUTOCLAIM from a *different* name — reusing the same name
    across restarts would let a fresh process silently "become" the dead
    one and inherit its PEL without ever claiming, defeating recovery.
    """
    return f"{group}:{socket.gethostname()}:{os.getpid()}"


class RedisStreamsBus(BusBackend):
    """Redis Streams (XADD / XREADGROUP+XACK / XAUTOCLAIM) bus backend."""

    def __init__(self) -> None:
        self._client = None
        self._client_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _get_client(self):
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client
            import redis
            from .._config import RedisStreamsBusConfig
            self._client = redis.Redis.from_url(
                RedisStreamsBusConfig.url(), decode_responses=False,
            )
        return self._client

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    def publish(self, topic: str, event: Event) -> None:
        from .._config import RedisStreamsBusConfig

        client = self._get_client()
        maxlen = RedisStreamsBusConfig.maxlen()
        kwargs = {"maxlen": maxlen, "approximate": True} if maxlen else {}
        client.xadd(topic, {"data": event.to_bytes()}, **kwargs)
        logger.debug("RedisStreamsBus published topic=%s id=%s", topic, event.event_id)

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
        from .._config import RedisStreamsBusConfig

        client = self._get_client()
        consumer_name = _consumer_name(group)
        idle_ms = RedisStreamsBusConfig.claim_idle_ms()
        block_ms = max(int(poll_timeout * 1000), 1)

        for topic in topics:
            self._ensure_group(client, topic, group)

        running = threading.Event()
        running.set()

        def _shutdown(signum, frame):
            logger.info("RedisStreamsBus shutdown signal received")
            running.clear()

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        logger.info(
            "RedisStreamsBus consuming group=%s consumer=%s topics=%s",
            group, consumer_name, topics,
        )

        while running.is_set():
            self._reclaim_pending(client, topics, group, consumer_name, idle_ms, handler)

            streams = {topic: ">" for topic in topics}
            response = client.xreadgroup(
                group, consumer_name, streams, count=READ_COUNT, block=block_ms,
            )
            if not response:
                continue
            for stream_name, messages in response:
                topic = stream_name.decode("utf-8")
                for msg_id, fields in messages:
                    self._handle(client, topic, group, msg_id, fields, handler)

    def _ensure_group(self, client, topic: str, group: str) -> None:
        """Create the consumer group, starting from the beginning of the
        stream — idempotent (an already-existing group is left untouched,
        including whatever offset it has advanced to)."""
        import redis

        try:
            client.xgroup_create(name=topic, groupname=group, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def _reclaim_pending(self, client, topics, group, consumer_name, idle_ms, handler) -> None:
        """Claim (and process) pending entries abandoned by a dead consumer.

        Runs once per poll-loop iteration — XAUTOCLAIM is a single round
        trip per topic, and anything not fully drained this pass (more
        than ``CLAIM_COUNT`` stale entries) is picked up again next pass.
        """
        for topic in topics:
            try:
                _cursor, claimed, _deleted = client.xautoclaim(
                    name=topic,
                    groupname=group,
                    consumername=consumer_name,
                    min_idle_time=idle_ms,
                    start_id="0-0",
                    count=CLAIM_COUNT,
                )
            except Exception:
                logger.exception("RedisStreamsBus XAUTOCLAIM failed topic=%s", topic)
                continue
            for msg_id, fields in claimed:
                logger.warning(
                    "RedisStreamsBus reclaimed pending entry topic=%s id=%s "
                    "(idle >= %sms, original consumer presumed dead)",
                    topic, msg_id, idle_ms,
                )
                self._handle(client, topic, group, msg_id, fields, handler)

    def _handle(self, client, topic, group, msg_id, fields, handler) -> None:
        try:
            event = Event.from_bytes(fields[b"data"])
        except Exception:
            # Poison message: deserialization failure outside the retry
            # loop would crash consume() and, with the entry still pending,
            # wedge the group on restart.
            logger.exception(
                "RedisStreamsBus undecodable message on %s id=%s, sending raw to DLQ",
                topic, msg_id,
            )
            if self._send_raw_to_dlq(topic, fields.get(b"data", b"")):
                client.xack(topic, group, msg_id)
            return

        retries = 0
        dlq_ok = True
        while retries <= MAX_HANDLER_RETRIES:
            try:
                handler(event)
                break
            except Exception:
                retries += 1
                if retries > MAX_HANDLER_RETRIES:
                    logger.exception(
                        "RedisStreamsBus DLQ event_id=%s topic=%s", event.event_id, topic,
                    )
                    dlq_ok = self._send_to_dlq(topic, event)
                else:
                    time.sleep(2 ** retries)
        # Ack only when handled or confirmed in the DLQ — otherwise the
        # entry stays in the PEL (owned by us) instead of being lost; a
        # future XAUTOCLAIM pass will retry it once idle again.
        if dlq_ok:
            client.xack(topic, group, msg_id)

    def _send_to_dlq(self, original_topic: str, event: Event) -> bool:
        try:
            self.publish(_dlq_topic(original_topic), event)
            return True
        except Exception:
            logger.exception("RedisStreamsBus failed to send to DLQ")
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
            logger.exception("RedisStreamsBus failed to DLQ undecodable message")
            return False


# Convenience for debugging DLQ contents from a shell:
#   redis-cli XRANGE user.deleted.dlq - +
def dlq_topic_for(topic: str) -> str:
    return _dlq_topic(topic)


__all__ = ["RedisStreamsBus", "dlq_topic_for"]
