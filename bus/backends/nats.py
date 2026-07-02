"""
NATS JetStream bus backend — the recommended event transport.

Select via environment (or Django setting):

    STAPEL_BUS_BACKEND=nats

One durable stream (``STAPEL_NATS_STREAM``) captures every event subject
(``<STAPEL_NATS_EVENT_PREFIX>.>``); topics map to subjects, so adding a
topic needs no broker-side changes. Consumers are durable pull consumers
named after the consumer group — replicas of the same group share the
consumer and load-balance.

Delivery semantics mirror the Kafka backend:
- at-least-once: a message is ack'd only after the handler succeeded or
  the message was confirmed in the DLQ subject (``<subject>.dlq``)
- handler failures retry 3× with backoff, then DLQ
- undecodable (poison) messages go straight to the DLQ instead of
  wedging the consumer
- ``Nats-Msg-Id: event_id`` enables JetStream's server-side duplicate
  suppression on publish

Requires nats-py (``pip install 'stapel-core[nats]'``).
"""
from __future__ import annotations

import asyncio
import logging
import re
import signal
import threading
import time
from typing import Callable

from ..base import BusBackend
from ..event import Event

logger = logging.getLogger(__name__)

DLQ_SUFFIX = ".dlq"
MAX_HANDLER_RETRIES = 3


def _durable_name(group: str) -> str:
    """NATS durable names must not contain dots/spaces/wildcards."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", group) or "stapel"


class NatsJetStreamBus(BusBackend):
    """JetStream-backed bus with a sync facade.

    nats-py is asyncio-only while publish() is called from synchronous
    Django code (views, the outbox relay), so the backend owns one
    event-loop thread and one connection per process. consume() blocks
    the calling thread (management command) and runs its own loop.
    """

    def __init__(self) -> None:
        self._loop = None
        self._nc = None
        self._js = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Sync bridge
    # ------------------------------------------------------------------

    def _run(self, coro, timeout: float = 30.0):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def _ensure_connected(self):
        if self._js is not None and not self._nc.is_closed:
            return
        with self._lock:
            if self._js is not None and not self._nc.is_closed:
                return
            from .._config import NatsBusConfig

            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                threading.Thread(
                    target=self._loop.run_forever, name="stapel-bus-nats", daemon=True
                ).start()

            async def _connect():
                import nats

                nc = await nats.connect(
                    NatsBusConfig.url(),
                    max_reconnect_attempts=-1,
                    reconnect_time_wait=1,
                )
                js = nc.jetstream()
                await self._ensure_stream(js)
                return nc, js

            self._nc, self._js = self._run(_connect())
            logger.info("NatsJetStreamBus connected to %s", NatsBusConfig.url())

    @staticmethod
    async def _ensure_stream(js):
        from .._config import NatsBusConfig

        stream = NatsBusConfig.stream()
        subjects = [f"{NatsBusConfig.subject_prefix()}.>"]
        try:
            await js.add_stream(name=stream, subjects=subjects)
        except Exception:
            # Already exists (possibly with tuned retention) — leave as is.
            logger.debug("JetStream stream %s already exists", stream)

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    def publish(self, topic: str, event: Event) -> None:
        from .._config import NatsBusConfig

        self._ensure_connected()
        subject = NatsBusConfig.subject_for(topic)

        async def _publish():
            await self._js.publish(
                subject,
                event.to_bytes(),
                headers={"Nats-Msg-Id": event.event_id},
            )

        self._run(_publish())
        logger.debug("NatsJetStreamBus published subject=%s id=%s", subject, event.event_id)

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
        asyncio.run(self._consume(topics, group, handler))

    async def _consume(self, topics: list[str], group: str, handler) -> None:
        import nats

        from .._config import NatsBusConfig

        nc = await nats.connect(
            NatsBusConfig.url(), max_reconnect_attempts=-1, reconnect_time_wait=1
        )
        js = nc.jetstream()
        await self._ensure_stream(js)

        from nats.js.api import ConsumerConfig

        subjects = [NatsBusConfig.subject_for(t) for t in topics]
        durable = _durable_name(group)
        sub = await js.pull_subscribe(
            "",  # subjects come from the consumer config
            durable=durable,
            stream=NatsBusConfig.stream(),
            config=ConsumerConfig(
                durable_name=durable,
                filter_subjects=subjects,
                max_deliver=-1,
                # Retries inside _process sleep up to 14s per message and a
                # batch of 10 is handled sequentially — far beyond the 30s
                # default ack_wait, after which JetStream would redeliver
                # messages we are still processing.
                ack_wait=300,
            ),
        )
        logger.info(
            "NatsJetStreamBus consuming durable=%s subjects=%s", durable, subjects
        )

        stopping = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stopping.set)
            except (NotImplementedError, ValueError):  # non-main thread / platform
                pass

        while not stopping.is_set():
            try:
                msgs = await sub.fetch(batch=10, timeout=5)
            except asyncio.TimeoutError:
                continue
            except nats.errors.TimeoutError:
                continue
            for msg in msgs:
                outcome = await loop.run_in_executor(
                    None, self._process, msg.data, handler
                )
                if outcome is None:
                    await msg.ack()
                else:
                    dlq_subject, payload = outcome
                    try:
                        # Deterministic msg-id so a redelivery after a failed
                        # ack does not duplicate the DLQ entry.
                        try:
                            dlq_headers = {
                                "Nats-Msg-Id": Event.from_bytes(payload).event_id + ".dlq"
                            }
                        except Exception:
                            dlq_headers = None
                        await js.publish(dlq_subject, payload, headers=dlq_headers)
                        await msg.ack()
                    except Exception:
                        logger.exception(
                            "NatsJetStreamBus failed to DLQ %s — leaving unacked "
                            "for redelivery", dlq_subject,
                        )
                        await msg.nak(delay=5)

        await nc.drain()

    def _process(self, data: bytes, handler) -> tuple[str, bytes] | None:
        """Run *handler* with retries.

        Returns None when the message is fully handled, or
        ``(dlq_subject, payload)`` when it must be parked in the DLQ.
        Runs in an executor thread — safe for Django ORM handlers.
        """
        from .._config import NatsBusConfig

        try:
            from django.db import close_old_connections
        except Exception:  # pragma: no cover
            close_old_connections = lambda: None  # noqa: E731

        try:
            event = Event.from_bytes(data)
        except Exception:
            logger.exception("NatsJetStreamBus undecodable message → DLQ")
            wrapper = Event(
                event_type="__undecodable__",
                service="bus",
                payload={"raw": data.decode("utf-8", errors="replace")},
            )
            return (
                NatsBusConfig.subject_for("__undecodable__") + DLQ_SUFFIX,
                wrapper.to_bytes(),
            )

        retries = 0
        while retries <= MAX_HANDLER_RETRIES:
            close_old_connections()
            try:
                handler(event)
                return None
            except Exception:
                retries += 1
                if retries > MAX_HANDLER_RETRIES:
                    logger.exception(
                        "NatsJetStreamBus DLQ event_id=%s type=%s",
                        event.event_id, event.event_type,
                    )
                    return (
                        NatsBusConfig.subject_for(event.event_type) + DLQ_SUFFIX,
                        event.to_bytes(),
                    )
                time.sleep(2 ** retries)
            finally:
                close_old_connections()
        return None  # pragma: no cover


# Convenience for debugging DLQ contents from a shell:
#   nats stream view stapel-events --filter 'stapel.evt.*.dlq'
def dlq_subject_for(topic: str) -> str:
    from .._config import NatsBusConfig

    return NatsBusConfig.subject_for(topic) + DLQ_SUFFIX


__all__ = ["NatsJetStreamBus", "dlq_subject_for"]
