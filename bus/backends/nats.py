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

A durable consumer outlives the process that created it, so its
server-side config is reconciled against the declared one on every boot
(see ``reconcile_durable``) — otherwise a service that gains a topic goes
silently deaf on it.

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


# ----------------------------------------------------------------------
# Durable consumer reconciliation
# ----------------------------------------------------------------------
#
# ``js.pull_subscribe(durable=...)`` BINDS to an existing consumer and
# throws the ConsumerConfig it was handed away — nats-py's own code reads
# ``consumer_info(...)`` then sets ``should_create = False`` and never
# looks at ``config`` again. The consumer therefore keeps whatever config
# it was born with, forever.
#
# That is a silent-deafness defect, not a cosmetic one. A service that
# gains a subscription topic after its durable exists keeps the pre-deploy
# ``filter_subjects`` server-side: the worker logs the widened subject list
# it ASKED for, publishers keep writing to the stream, the events match no
# filter, and nothing errors anywhere. Observed live: a chat service logged
# ``subjects=[..., 'stapel.evt.user.created', ...]`` for days while the
# durable filtered the older set and the handler never ran once.
#
# So the declared config is compared against the live one before binding,
# and any drift is either reconciled in place or raised. There is no third
# outcome — in particular there is no "delete and recreate", which would
# reset the ack floor and replay the entire stream through the handler.

#: Fields JetStream can change on a live consumer (nats-server 2.10+).
#: The update goes through CONSUMER.DURABLE.CREATE against the existing
#: durable, which edits it in place: ``delivered`` and ``ack_floor`` are
#: untouched, so in-flight progress survives the reconciliation.
RECONCILABLE_FIELDS = (
    "filter_subject",
    "filter_subjects",
    "ack_wait",
    "max_deliver",
    "max_ack_pending",
    "backoff",
    "description",
    "metadata",
    "inactive_threshold",
    "num_replicas",
    "rate_limit_bps",
    "sample_freq",
    "headers_only",
)

#: Fields the server refuses to change after creation. They drift exactly
#: as silently as the subjects do — ``ack_policy=none`` makes every
#: ``msg.ack()`` a no-op, ``replay_policy=original`` paces delivery at the
#: original publish rate, a non-empty ``deliver_subject`` means the durable
#: is a push consumer that a pull binding will never drain — so they are
#: compared too. The only honest response is a loud failure: the fix is an
#: operator decision (rename the group, or delete the durable and accept
#: the replay), never something a boot sequence should take on itself.
IMMUTABLE_FIELDS = (
    "ack_policy",
    "replay_policy",
    "deliver_subject",
    "deliver_group",
    "max_waiting",
)

#: Deliberately compared by NEITHER list. These describe where a consumer
#: STARTS reading; the server applies them once, at creation, and an
#: existing durable is already past that point. A difference here is
#: history, not drift, and crash-looping over it would be a false alarm.
START_POSITION_FIELDS = ("deliver_policy", "opt_start_seq", "opt_start_time")


class ConsumerConfigConflict(RuntimeError):
    """A durable's live config cannot be reconciled with the declared one.

    Raised at startup, on purpose. A crash-loop naming the durable and both
    configurations is strictly better than a worker that runs, logs the
    subjects it wanted, and receives nothing.
    """


def _plain(value):
    """Enum -> its wire value; everything else unchanged."""
    return getattr(value, "value", value)


def subject_set(config) -> frozenset[str]:
    """The subject filter of *config* as a set.

    JetStream reports a single filter as ``filter_subject`` and multiple
    ones as ``filter_subjects``; normalising both into a set makes the two
    spellings of "the same filter" compare equal.
    """
    subjects = getattr(config, "filter_subjects", None)
    if subjects:
        return frozenset(subjects)
    single = getattr(config, "filter_subject", None)
    return frozenset([single]) if single else frozenset()


def _values_differ(field: str, declared, actual) -> bool:
    if field in ("filter_subject", "filter_subjects"):
        return False  # handled as one unit by subject_set()
    want = _plain(getattr(declared, field, None))
    if want is None:
        # Not declared: whatever the server has is not this deployment's
        # business (an operator may have tuned it deliberately).
        return False
    got = _plain(getattr(actual, field, None))
    if isinstance(want, (int, float)) and isinstance(got, (int, float)):
        return abs(float(want) - float(got)) > 1e-6
    return want != got


def consumer_drift(declared, actual) -> dict[str, tuple]:
    """Declared-vs-actual differences, as ``{field: (declared, actual)}``.

    Only fields the caller actually declared are compared, plus the subject
    filter (always) and ``deliver_subject`` (always — a push consumer under
    a pull binding is drift even though we declare nothing there).
    """
    drift: dict[str, tuple] = {}

    want_subjects, got_subjects = subject_set(declared), subject_set(actual)
    if want_subjects != got_subjects:
        drift["filter_subjects"] = (sorted(want_subjects), sorted(got_subjects))

    for field in RECONCILABLE_FIELDS + IMMUTABLE_FIELDS:
        if _values_differ(field, declared, actual):
            drift[field] = (
                _plain(getattr(declared, field, None)),
                _plain(getattr(actual, field, None)),
            )

    if "deliver_subject" not in drift and getattr(actual, "deliver_subject", None):
        drift["deliver_subject"] = (None, actual.deliver_subject)

    return drift


def _conflict(durable: str, declared, actual, drift: dict, reason: str, cause=None):
    detail = "; ".join(
        f"{field}: declared={want!r} actual={got!r}" for field, (want, got) in sorted(drift.items())
    )
    error = ConsumerConfigConflict(
        f"JetStream durable {durable!r} cannot be reconciled ({reason}). "
        f"Drift — {detail}. Declared subjects={sorted(subject_set(declared))} "
        f"actual subjects={sorted(subject_set(actual))}. "
        f"Refusing to delete and recreate the consumer: that would reset the "
        f"ack floor and replay the whole stream. Resolve by hand — widen the "
        f"consumer with `nats consumer edit`, or rename the consumer group."
    )
    if cause is not None:
        error.__cause__ = cause
    return error


async def _consumer_info(js, stream: str, durable: str):
    """Live consumer info, or None when the durable does not exist yet."""
    from nats.js.errors import NotFoundError

    try:
        return await js.consumer_info(stream, durable)
    except NotFoundError:
        return None


async def reconcile_durable(js, stream: str, durable: str, declared) -> str:
    """Make the live consumer *durable* match *declared* before binding.

    Returns ``"absent"`` (nothing to reconcile — pull_subscribe will create
    it), ``"matched"`` or ``"reconciled"``. Raises `ConsumerConfigConflict`
    when the drift cannot be applied in place.
    """
    info = await _consumer_info(js, stream, durable)
    if info is None:
        return "absent"

    actual = info.config
    drift = consumer_drift(declared, actual)
    if not drift:
        return "matched"

    blocked = sorted(set(drift) & set(IMMUTABLE_FIELDS))
    if blocked:
        raise _conflict(
            durable, declared, actual, drift,
            f"immutable field(s) differ: {', '.join(blocked)}",
        )

    logger.warning(
        "NatsJetStreamBus reconciling durable=%s in place — drift: %s. "
        "Subjects before=%s after=%s. Ack floor is preserved (in-place "
        "update, not recreate).",
        durable,
        {field: {"declared": want, "actual": got} for field, (want, got) in sorted(drift.items())},
        sorted(subject_set(actual)),
        sorted(subject_set(declared)),
    )

    # nats-py has no update_consumer(); add_consumer() against an existing
    # durable maps to CONSUMER.DURABLE.CREATE, which the server treats as an
    # in-place edit. Newer clients may expose update_consumer() — prefer it.
    update = getattr(js, "update_consumer", None) or js.add_consumer
    try:
        await update(stream, config=declared)
    except Exception as exc:  # server rejected the edit — never fall through
        raise _conflict(
            durable, declared, actual, drift,
            f"the server rejected the in-place update: {exc}", cause=exc,
        ) from exc

    return "reconciled"


async def assert_consumer_matches(js, stream: str, durable: str, declared) -> None:
    """Startup invariant: declared == actual, verified against the server.

    Runs after the subscription is bound, for freshly created durables too,
    so "the consumer really is filtering what this worker thinks it is" is
    an observable fact in the logs rather than an assumption.
    """
    info = await _consumer_info(js, stream, durable)
    if info is None:  # pragma: no cover — bound, so it exists
        raise ConsumerConfigConflict(
            f"JetStream durable {durable!r} vanished right after it was bound"
        )
    drift = consumer_drift(declared, info.config)
    if drift:
        raise _conflict(
            durable, declared, info.config, drift,
            "still differs after setup — the server accepted the update "
            "without applying it",
        )
    logger.info(
        "NatsJetStreamBus durable=%s verified against the server: subjects=%s",
        durable, sorted(subject_set(info.config)),
    )


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
        stream = NatsBusConfig.stream()
        declared = ConsumerConfig(
            durable_name=durable,
            filter_subjects=subjects,
            max_deliver=-1,
            # Retries inside _process sleep up to 14s per message and a
            # batch of 10 is handled sequentially — far beyond the 30s
            # default ack_wait, after which JetStream would redeliver
            # messages we are still processing.
            ack_wait=300,
        )
        # An existing durable ignores `config` on bind — reconcile first.
        outcome = await reconcile_durable(js, stream, durable, declared)
        sub = await js.pull_subscribe(
            "",  # subjects come from the consumer config
            durable=durable,
            stream=stream,
            config=declared,
        )
        await assert_consumer_matches(js, stream, durable, declared)
        logger.info(
            "NatsJetStreamBus consuming durable=%s (%s) subjects=%s",
            durable, outcome, subjects,
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


__all__ = [
    "ConsumerConfigConflict",
    "IMMUTABLE_FIELDS",
    "NatsJetStreamBus",
    "RECONCILABLE_FIELDS",
    "START_POSITION_FIELDS",
    "assert_consumer_matches",
    "consumer_drift",
    "dlq_subject_for",
    "reconcile_durable",
    "subject_set",
]
