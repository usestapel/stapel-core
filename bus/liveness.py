"""A consumer with no partitions is not alive, whatever the container says.

The failure this module exists for, observed on a client host: during a
broker disturbance a Kafka consumer logged

    SESSTMOUT ... revoking assignment and rejoining group

and then never rejoined. It sat in its poll loop for **16 hours**. The
process was healthy by every measure anything was looking at — it polled,
it logged nothing further, it touched its heartbeat file on every poll, the
container was ``Up`` and no healthcheck ever went red — while the consumer
group had ZERO members, lag grew without bound, and not one message was
processed until a human restarted the container.

Three things were wrong, and all three are fixed here.

1. **Nothing distinguished "idle" from "not in the group".** A poll that
   returns ``None`` looks identical in both cases. The only thing that tells
   them apart is the ASSIGNMENT, which nothing was reading.

2. **The heartbeat lied.** It was touched after every ``poll()`` return,
   including the returns of a consumer that owned no partitions — so the
   file stayed fresh for sixteen hours of doing nothing. A liveness file
   that a dead consumer keeps fresh is worse than no file: it is a green
   light wired to the wrong switch (see also the in-process watchdog this
   replaces, which read that same file and therefore also never fired).

3. **The cure was in-process.** librdkafka's own rejoin is the thing that
   silently failed; an in-process retry loop layered on top would be one
   more mechanism that can wedge in exactly the same way. So the mechanism
   here is to **exit non-zero** and let the supervisor (``restart:
   unless-stopped``, a k8s restart) hand the client a fresh process that
   joins the group cleanly. Exiting is boring, observable, and cannot itself
   get stuck.

The rule, as implemented by :meth:`ConsumerLiveness.stall_reason`:

    an EMPTY assignment (or no successful poll at all) for longer than
    ``STAPEL_BUS_CONSUMER_STALL_SECONDS`` — while the brokers are
    reachable — or a fatal client error, means this process is done.

Two conditions are deliberately NOT that:

*   **The brokers are unreachable.** Then an empty assignment is the
    correct state of the world and restarting the process cannot help; the
    heartbeat still goes stale, which is the signal an operator wants, and
    the exit is held back. Reachability is a real metadata request, not an
    assumption (``brokers_reachable``).

*   **A handler that runs longer than the stall window.** Work in progress
    is not a stall. The handler's own elapsed time is subtracted from both
    baselines when it finishes (:meth:`handling`), so a five-minute handler
    never spends the window, and a worker is never killed mid-message. A
    handler that outruns ``max.poll.interval.ms`` is a different condition
    with a different answer (raise that setting, or make the handler
    shorter) and is reported as itself — see :meth:`on_broker_error`.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import contextmanager
from typing import Callable, Sequence

from ._config import _get

logger = logging.getLogger(__name__)

#: Exit code for "this process gave up on itself; start another one".
#: 75 is EX_TEMPFAIL — the condition is transient and a restart is the fix.
STALL_EXIT_CODE = 75

#: Seconds of empty assignment before the process exits. 0 disables the exit
#: entirely (the heartbeat and the metrics stay on).
DEFAULT_STALL_SECONDS = 120.0

DEFAULT_HEARTBEAT_PATH = "/tmp/stapel-bus-consumer-alive"

#: What the heartbeat file SAYS, not only when it was last touched.
#:
#: The file used to be empty, so the probe could only read its mtime, and
#: an mtime cannot tell "this consumer is polling an assignment and idle"
#: apart from "this consumer is inside a handler that has legitimately run
#: for four minutes". The docstring of ``bus_consumer_alive`` admitted as
#: much and told operators to size ``--max-age`` above the longest handler.
#:
#: On 2026-09-20 a deployment had not, and the consequence was not a red
#: dashboard: the supervisor restarted a WORKING process, twice, each time
#: exactly while a provider was publishing the answer to the request that
#: process was waiting on. NATS core request-reply has no persistence, so
#: both answers — both paid for — were dropped by the broker. The probe was
#: the trigger of the outage it was supposed to detect.
#:
#: The process knows which of the two states it is in. Now it writes it
#: down, and the probe can stop guessing: work in progress is judged
#: against the handler's own budget, and a handler that outruns that budget
#: is still red, because that one really is hung.
HEARTBEAT_POLL = "poll"
HEARTBEAT_HANDLING = "handling"

ASSIGNMENT_GAUGE = "stapel_bus_consumer_assignment_size"
LAST_POLL_GAUGE = "stapel_bus_consumer_seconds_since_last_poll"
STALL_EXITS_COUNTER = "stapel_bus_consumer_stall_exits_total"

#: How often the gauges are republished, and how often an apparently stalled
#: consumer re-probes the brokers. Both run inside a loop that can spin many
#: times a second; neither is worth doing at that rate.
_PUBLISH_INTERVAL = 1.0
_PROBE_INTERVAL = 10.0


def stall_seconds() -> float:
    """``STAPEL_BUS_CONSUMER_STALL_SECONDS`` (env, then Django setting)."""
    raw = _get("STAPEL_BUS_CONSUMER_STALL_SECONDS", "")
    if raw == "":
        return DEFAULT_STALL_SECONDS
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        logger.warning(
            "bus: STAPEL_BUS_CONSUMER_STALL_SECONDS=%r is not a number — "
            "using the default of %ss",
            raw, DEFAULT_STALL_SECONDS,
        )
        return DEFAULT_STALL_SECONDS


def handler_budget_seconds() -> float:
    """``STAPEL_BUS_HANDLER_BUDGET_SECONDS`` — how long a handler may run.

    The longest one message may legitimately occupy this consumer. It is
    the number a healthcheck needs and never had: without it the probe has
    to treat every slow handler as a hang, and a deployment whose handler
    waits on a provider for minutes has to choose between a probe that
    kills working processes and one that never fires.

    0 (the default) means unstated, and the probe falls back to its own
    ``--max-age`` — exactly the behaviour every existing deployment has.
    """
    raw = _get("STAPEL_BUS_HANDLER_BUDGET_SECONDS", "")
    if raw == "":
        return 0.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        logger.warning(
            "bus: STAPEL_BUS_HANDLER_BUDGET_SECONDS=%r is not a number — "
            "treating it as unstated", raw,
        )
        return 0.0


def heartbeat_path() -> str:
    """``STAPEL_BUS_CONSUMER_HEARTBEAT_PATH`` (env, then Django setting).

    ``KAFKA_CONSUMER_HEARTBEAT`` is still honoured: it is what deployments
    that already point a HEALTHCHECK at this file have set.
    """
    return (
        _get("STAPEL_BUS_CONSUMER_HEARTBEAT_PATH", "")
        or _get("KAFKA_CONSUMER_HEARTBEAT", "")
        or DEFAULT_HEARTBEAT_PATH
    )


class ConsumerLiveness:
    """The liveness state of one consume loop.

    Everything that decides is injectable — ``clock``, ``brokers_reachable``
    and ``exit_process`` — so the whole rule is testable without a broker,
    without a container and without sleeping through a two-minute window.
    """

    def __init__(
        self,
        *,
        group: str,
        topics: Sequence[str] = (),
        backend: str = "",
        stall_window: float | None = None,
        heartbeat: str | None = None,
        clock: Callable[[], float] | None = None,
        brokers_reachable: Callable[[], bool] | None = None,
        exit_process: Callable[[int], None] | None = None,
    ) -> None:
        self.group = group
        self.topics = list(topics)
        self.backend = backend
        self.stall_window = (
            stall_seconds() if stall_window is None else max(0.0, float(stall_window))
        )
        self.heartbeat = heartbeat_path() if heartbeat is None else heartbeat
        # Resolved here rather than as a default argument: a default is
        # bound at import time, which would make the clock impossible to
        # replace from a test that imports this module first (every test).
        self._clock = clock if clock is not None else time.monotonic
        self._brokers_reachable = brokers_reachable or (lambda: True)
        self._exit = exit_process if exit_process is not None else sys.exit

        now = self._clock()
        self.assignment_size = 0
        self.started_at = now
        self.last_poll_at = now
        self.last_assignment_at = now
        self.last_message_at: float | None = None
        self.fatal_error: str | None = None
        self.last_error: str | None = None
        self.brokers_down_since: float | None = None
        self._handler_started_at: float | None = None
        self._last_publish: float | None = None
        self._last_probe: float | None = None
        self._probe_said: bool = True

    # ------------------------------------------------------------------
    # What the loop tells us
    # ------------------------------------------------------------------

    def on_assign(self, partitions) -> None:
        """Rebalance callback: this consumer now owns *partitions*."""
        self.assignment_size = len(partitions or [])
        if self.assignment_size:
            self.last_assignment_at = self._clock()
            self.brokers_down_since = None
        logger.info(
            "bus: consumer group=%s assigned %s partition(s)",
            self.group, self.assignment_size,
        )

    def on_revoke(self, partitions) -> None:
        """Rebalance callback: the assignment is gone (revoked or lost).

        This is the line the incident opened with. It is normal during a
        rebalance and catastrophic when the rejoin that should follow never
        happens, so it is logged at WARNING and it starts the clock.
        """
        self.assignment_size = 0
        logger.warning(
            "bus: consumer group=%s lost its assignment (%s partition(s) "
            "revoked) — it must rejoin within %ss or this process exits",
            self.group, len(partitions or []), self.stall_window or "∞",
        )

    def record_poll(self, assignment_size: int | None = None) -> None:
        """A ``poll()`` call returned (message or not) without raising."""
        now = self._clock()
        self.last_poll_at = now
        if assignment_size is not None:
            self.assignment_size = assignment_size
        if self.assignment_size > 0:
            self.last_assignment_at = now
            self.brokers_down_since = None

    def record_message(self) -> None:
        """A message arrived — proof of both a broker and an assignment."""
        now = self._clock()
        self.last_message_at = now
        self.last_assignment_at = now
        self.brokers_down_since = None

    def on_broker_error(self, error) -> None:
        """librdkafka's ``error_cb``. Never raises: it runs inside poll().

        Three classes get different answers:

        - ``fatal()`` — the client is unusable; nothing but a new process
          will fix it, so it becomes a stall reason on the next check.
        - ``_ALL_BROKERS_DOWN`` — the world is broken, not this process.
          Recorded, logged, and explicitly NOT a reason to exit.
        - ``_MAX_POLL_EXCEEDED`` — a handler outran
          ``max.poll.interval.ms`` and the client left the group over it.
          Reported as itself, because "make the handler faster or raise the
          interval" is a different fix from "restart me".
        """
        try:
            name = str(getattr(error, "name", lambda: "")() or "")
            text = str(error)
            fatal = bool(getattr(error, "fatal", lambda: False)())
        except Exception:  # pragma: no cover - defensive around a C object
            name, text, fatal = "", repr(error), False

        self.last_error = text
        if fatal:
            self.fatal_error = text
            logger.critical(
                "bus: consumer group=%s reported a FATAL client error: %s",
                self.group, text,
            )
            return
        if name == "_ALL_BROKERS_DOWN":
            if self.brokers_down_since is None:
                self.brokers_down_since = self._clock()
            logger.warning(
                "bus: consumer group=%s reports every broker down (%s) — "
                "waiting, not exiting: a new process would find the same "
                "brokers missing",
                self.group, text,
            )
            return
        if name == "_MAX_POLL_EXCEEDED":
            logger.error(
                "bus: consumer group=%s exceeded max.poll.interval.ms while a "
                "handler was running (%s). This is a SLOW HANDLER, not a "
                "stalled client: the fix is a shorter handler or a larger "
                "max.poll.interval.ms. The client leaves the group and "
                "rejoins on the next poll; this process is not killed for it.",
                self.group, text,
            )
            return
        logger.warning("bus: consumer group=%s client error: %s", self.group, text)

    @contextmanager
    def handling(self, budget: float | None = None):
        """Wrap one handler call. Handler time is not idle time.

        The elapsed time is credited back to both baselines on the way out,
        so a handler slower than the stall window cannot spend it. Credited
        on failure too — a handler that raised still ran.

        The heartbeat file is also STAMPED, on the way in, with the fact
        that a handler is running and how long it is allowed to take. That
        is the half an external probe could not see: while a handler runs
        the loop does not poll and the file is not refreshed, so a busy
        consumer and a hung one aged identically and a supervisor had to
        treat them identically. *budget* defaults to
        ``STAPEL_BUS_HANDLER_BUDGET_SECONDS``.
        """
        started = self._clock()
        self._handler_started_at = started
        budget = handler_budget_seconds() if budget is None else max(0.0, float(budget))
        self._write_heartbeat(HEARTBEAT_HANDLING, budget)
        try:
            yield
        finally:
            self._handler_started_at = None
            elapsed = self._clock() - started
            if elapsed > 0:
                now = self._clock()
                self.last_poll_at = min(now, self.last_poll_at + elapsed)
                self.last_assignment_at = min(now, self.last_assignment_at + elapsed)
            # Back to "polling" the moment the handler returns, so the next
            # probe judges the loop and not the message that just finished.
            self.touch_heartbeat()

    # ------------------------------------------------------------------
    # What we tell the world
    # ------------------------------------------------------------------

    def _write_heartbeat(self, state: str, budget: float = 0.0) -> bool:
        """Write the liveness file — only while partitions are owned.

        One line: ``<state> <wall clock> <budget>``. Wall clock, not the
        injected monotonic clock, because the reader is a DIFFERENT PROCESS
        and has no access to this one's monotonic baseline.
        """
        if self.assignment_size <= 0:
            return False
        try:
            with open(self.heartbeat, "w") as fh:
                fh.write(f"{state} {time.time():.3f} {budget:.3f}\n")
        except OSError:
            logger.debug("bus: heartbeat %s not writable", self.heartbeat, exc_info=True)
            return False
        return True

    def touch_heartbeat(self) -> bool:
        """Refresh the liveness file — only while partitions are owned.

        Returns whether the file was touched. The guard IS the mechanism:
        the old heartbeat was touched on every poll return, which is why a
        consumer with nothing assigned kept a container green for sixteen
        hours.
        """
        return self._write_heartbeat(HEARTBEAT_POLL)

    def publish_metrics(self, *, force: bool = False) -> None:
        """Set the liveness gauges (rate-limited; never raises)."""
        now = self._clock()
        if not force and self._last_publish is not None:
            if now - self._last_publish < _PUBLISH_INTERVAL:
                return
        self._last_publish = now
        labels = {"group": self.group}
        try:
            from ..observability import metrics

            metrics.gauge(
                ASSIGNMENT_GAUGE, self.assignment_size, labels,
                description="Partitions currently assigned to this consumer "
                            "(0 means it is in no group and receives nothing)",
                # Each consumer process owns a DIFFERENT share of the group's
                # partitions, so the question a deployment asks — "is this
                # service consuming the whole topic?" — is the sum across the
                # living processes. `max` would hide a worker that owns
                # nothing, which is the exact failure the guard above exists
                # for; `all` would emit one series per pid.
                multiprocess_mode="livesum",
            )
            metrics.gauge(
                LAST_POLL_GAUGE, max(0.0, now - self.last_poll_at), labels,
                description="Seconds since this consumer's last successful poll",
                # The alert is "has ANY consumer in this group stopped
                # polling", so the interesting value is the stalest one.
                # `live`: a process that has exited is not a stalled
                # consumer, and letting its final staleness grow forever
                # would be an alert that can never clear.
                multiprocess_mode="livemax",
            )
        except Exception:  # pragma: no cover - the facade already guards itself
            logger.debug("bus: liveness gauges not recorded", exc_info=True)

    def declare_metrics(self) -> None:
        """Create the series at startup, before anything is wrong.

        Same reason as the DLQ counter (see :mod:`stapel_core.bus.dlq`): an
        alert over a series that has never existed has no subject, and looks
        exactly like an alert that is not firing.
        """
        self.publish_metrics(force=True)
        try:
            from ..observability import metrics

            metrics.counter(
                STALL_EXITS_COUNTER, 0, {"group": self.group},
                description="Consumer processes that exited because they had "
                            "no assignment while the brokers were reachable",
            )
        except Exception:  # pragma: no cover - the facade already guards itself
            logger.debug("bus: stall counter not declared", exc_info=True)

    # ------------------------------------------------------------------
    # The rule
    # ------------------------------------------------------------------

    def facts(self) -> dict:
        now = self._clock()
        return {
            "group": self.group,
            "backend": self.backend,
            "topics": len(self.topics),
            "assignment_size": self.assignment_size,
            "seconds_since_poll": round(now - self.last_poll_at, 1),
            "seconds_since_assignment": round(now - self.last_assignment_at, 1),
            "seconds_since_message": (
                None if self.last_message_at is None
                else round(now - self.last_message_at, 1)
            ),
            "uptime_seconds": round(now - self.started_at, 1),
            "stall_window": self.stall_window,
            "fatal_error": self.fatal_error,
            "last_error": self.last_error,
        }

    def stall_reason(self) -> str | None:
        """Why this process should exit, or None to keep going."""
        if self.fatal_error:
            return f"the client reported a fatal error: {self.fatal_error}"
        if self.stall_window <= 0:
            return None
        if self._handler_started_at is not None:
            # Called from inside a handler (a nested loop, a test): work in
            # progress is never a stall, and we do not kill mid-message.
            return None

        now = self._clock()
        unassigned_for = (
            now - self.last_assignment_at if self.assignment_size <= 0 else 0.0
        )
        silent_for = now - self.last_poll_at
        if max(unassigned_for, silent_for) < self.stall_window:
            return None

        if not self._brokers_say_reachable(now):
            return None

        if unassigned_for >= self.stall_window:
            return (
                f"no partitions assigned for {unassigned_for:.0f}s "
                f"(limit {self.stall_window:.0f}s) while the brokers answer — "
                f"this consumer is in no group and receives nothing"
            )
        return (
            f"no successful poll for {silent_for:.0f}s "
            f"(limit {self.stall_window:.0f}s) while the brokers answer"
        )

    def _brokers_say_reachable(self, now: float) -> bool:
        """Ask the brokers, at most once per ``_PROBE_INTERVAL``."""
        if self._last_probe is not None and now - self._last_probe < _PROBE_INTERVAL:
            return self._probe_said
        self._last_probe = now
        try:
            self._probe_said = bool(self._brokers_reachable())
        except Exception:
            self._probe_said = False
        if not self._probe_said:
            logger.warning(
                "bus: consumer group=%s has no assignment, but the brokers do "
                "not answer either — holding the restart, because a new "
                "process would find the same brokers missing. The heartbeat "
                "file is going stale meanwhile.",
                self.group,
            )
        return self._probe_said

    def check(self) -> None:
        """Between messages: publish the gauges, exit if the rule says so."""
        self.publish_metrics()
        reason = self.stall_reason()
        if reason is not None:
            self.give_up(reason)

    def give_up(self, reason: str) -> None:
        """Log the facts at CRITICAL, count it, and leave non-zero."""
        facts = self.facts()
        try:
            from ..observability import metrics

            metrics.counter(
                STALL_EXITS_COUNTER, labels={"group": self.group},
                description="Consumer processes that exited because they had "
                            "no assignment while the brokers were reachable",
            )
        except Exception:  # pragma: no cover - the facade already guards itself
            logger.debug("bus: stall exit not counted", exc_info=True)
        logger.critical(
            "bus: consumer group=%s is not alive — %s. Facts: %s. Exiting %s "
            "so the supervisor restarts this process and the client rejoins "
            "the group; an in-process rejoin is exactly what failed silently.",
            self.group, reason, facts, STALL_EXIT_CODE,
        )
        self._exit(STALL_EXIT_CODE)


def heartbeat_age(path: str | None = None) -> float | None:
    """Seconds since the heartbeat file was touched; None if there is none."""
    target = path or heartbeat_path()
    try:
        return max(0.0, time.time() - os.path.getmtime(target))
    except OSError:
        return None


def heartbeat_state(path: str | None = None) -> dict | None:
    """What the heartbeat file says, or None if there is none.

    ``{"state": "poll"|"handling", "age": seconds, "budget": seconds}``.

    A file written before this existed is empty; it reads as ``poll`` with
    its mtime age, so an old consumer and a new probe agree.
    """
    target = path or heartbeat_path()
    age = heartbeat_age(target)
    if age is None:
        return None
    state, budget = HEARTBEAT_POLL, 0.0
    try:
        with open(target) as fh:
            parts = fh.read(200).split()
    except OSError:
        parts = []
    if parts and parts[0] in (HEARTBEAT_POLL, HEARTBEAT_HANDLING):
        state = parts[0]
        # The written timestamp beats the mtime: a filesystem whose mtime
        # granularity is a second would round a fresh handler into the past.
        if len(parts) > 1:
            try:
                age = max(0.0, time.time() - float(parts[1]))
            except ValueError:
                pass
        if len(parts) > 2:
            try:
                budget = max(0.0, float(parts[2]))
            except ValueError:
                pass
    return {"state": state, "age": age, "budget": budget}


__all__ = [
    "ASSIGNMENT_GAUGE",
    "ConsumerLiveness",
    "DEFAULT_HEARTBEAT_PATH",
    "DEFAULT_STALL_SECONDS",
    "HEARTBEAT_HANDLING",
    "HEARTBEAT_POLL",
    "LAST_POLL_GAUGE",
    "STALL_EXITS_COUNTER",
    "STALL_EXIT_CODE",
    "handler_budget_seconds",
    "heartbeat_age",
    "heartbeat_path",
    "heartbeat_state",
    "stall_seconds",
]
