"""A consumer with no partitions must not be able to look alive.

The incident these tests are written from, on a client host: a broker
disturbance, one ``SESSTMOUT ... revoking assignment and rejoining group``,
and then sixteen hours of a process that polled, touched its heartbeat file,
reported no error, kept its container ``Up`` — and was a member of no
consumer group, so nothing was consumed until a human restarted it.

Every clock here is injected; nothing sleeps.
"""
from __future__ import annotations

import sys
import types

import pytest

from stapel_core.bus.liveness import (
    STALL_EXIT_CODE,
    ConsumerLiveness,
    heartbeat_path,
    stall_seconds,
)


class Clock:
    """A hand-wound monotonic clock."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Exited(BaseException):
    """What the injected exit raises instead of leaving the process."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def _exit(code: int):
    raise Exited(code)


def make_liveness(tmp_path, clock, *, window=120.0, reachable=True, **kwargs):
    return ConsumerLiveness(
        group="svc.actions",
        topics=["a.topic"],
        backend="kafka",
        stall_window=window,
        heartbeat=str(tmp_path / "alive"),
        clock=clock,
        brokers_reachable=(reachable if callable(reachable) else lambda: reachable),
        exit_process=_exit,
        **kwargs,
    )


class TestTheRule:
    def test_a_revoked_assignment_that_never_returns_exits_non_zero(
        self, tmp_path, caplog
    ):
        """THE incident. The consumer is assigned, loses the assignment,
        keeps polling happily — and after the stall window it stops being a
        process that looks fine and becomes one the supervisor restarts."""
        clock = Clock()
        live = make_liveness(tmp_path, clock)
        live.on_assign([1, 2, 3])
        live.record_poll(3)

        live.on_revoke([1, 2, 3])  # SESSTMOUT — and no rejoin ever follows

        # Nineteen minutes of the exact loop that ran for sixteen hours.
        for _ in range(119):
            clock.advance(1)
            live.record_poll(0)
            live.check()

        clock.advance(2)
        live.record_poll(0)
        with caplog.at_level("CRITICAL"):
            with pytest.raises(Exited) as exc:
                live.check()

        assert exc.value.code == STALL_EXIT_CODE
        assert exc.value.code != 0
        message = caplog.records[-1].getMessage()
        assert "no partitions assigned" in message
        assert "svc.actions" in message
        assert "'assignment_size': 0" in message

    def test_a_consumer_that_never_joins_at_all_exits_too(self, tmp_path):
        """Not only lost assignments: a consumer that never got one is in
        exactly the same position — in no group, receiving nothing."""
        clock = Clock()
        live = make_liveness(tmp_path, clock)
        clock.advance(121)
        live.record_poll(0)
        with pytest.raises(Exited):
            live.check()

    def test_it_keeps_going_while_it_owns_partitions(self, tmp_path):
        clock = Clock()
        live = make_liveness(tmp_path, clock)
        live.on_assign([1])
        for _ in range(1000):
            clock.advance(10)
            live.record_poll(1)
            live.check()  # an idle but assigned consumer is alive

    def test_a_poll_that_stops_returning_is_a_stall_too(self, tmp_path):
        """Assigned, but the loop has not completed a poll in the window."""
        clock = Clock()
        live = make_liveness(tmp_path, clock)
        live.on_assign([1])
        live.record_poll(1)
        clock.advance(121)
        with pytest.raises(Exited):
            live.check()

    def test_unreachable_brokers_hold_the_restart_back(self, tmp_path, caplog):
        """The world is broken, not this process: a new one would find the
        same brokers missing, and a restart loop against a dead cluster is
        churn that buries the real signal."""
        clock = Clock()
        live = make_liveness(tmp_path, clock, reachable=False)
        live.on_assign([1])
        live.on_revoke([1])
        clock.advance(600)
        live.record_poll(0)
        with caplog.at_level("WARNING"):
            live.check()  # no exit
        assert any("do not answer" in r.getMessage() for r in caplog.records)

        # ...and the moment they answer again while we are still in no group,
        # the same loop exits.
        live._brokers_reachable = lambda: True
        clock.advance(11)  # past the probe interval
        live.record_poll(0)
        with pytest.raises(Exited):
            live.check()

    def test_a_fatal_client_error_does_not_wait_for_the_window(self, tmp_path):
        clock = Clock()
        live = make_liveness(tmp_path, clock)
        live.on_assign([1])
        live.on_broker_error(
            types.SimpleNamespace(name=lambda: "_FATAL", fatal=lambda: True)
        )
        with pytest.raises(Exited):
            live.check()

    def test_all_brokers_down_is_not_a_fatal_error(self, tmp_path):
        clock = Clock()
        live = make_liveness(tmp_path, clock, reachable=False)
        live.on_assign([1])
        live.on_broker_error(
            types.SimpleNamespace(name=lambda: "_ALL_BROKERS_DOWN", fatal=lambda: False)
        )
        live.check()  # no exit
        assert live.brokers_down_since is not None

    def test_window_zero_disables_the_exit(self, tmp_path):
        clock = Clock()
        live = make_liveness(tmp_path, clock, window=0)
        live.on_revoke([1])
        clock.advance(100_000)
        live.record_poll(0)
        live.check()  # a deployment that opted out keeps its old behaviour
        assert live.stall_reason() is None


class TestALongHandlerIsNotAStall:
    def test_a_handler_slower_than_the_window_does_not_trigger_the_exit(
        self, tmp_path
    ):
        """Work in progress is not silence. A worker must never be killed
        for doing the job it exists for — that is a different condition
        (max.poll.interval.ms) with a different answer."""
        clock = Clock()
        live = make_liveness(tmp_path, clock)
        live.on_assign([1])
        live.record_poll(1)

        with live.handling():
            clock.advance(600)  # a ten-minute handler
            assert live.stall_reason() is None  # not even mid-handler

        # The loop polls again right after; nothing about those ten minutes
        # counts against it.
        live.record_poll(1)
        live.check()

    def test_a_long_handler_that_lost_the_assignment_gets_a_fresh_window(
        self, tmp_path
    ):
        """max.poll.interval.ms exceeded: the client left the group during
        the handler and rejoins on the next poll. It gets the full window to
        do so, rather than being shot the instant the handler returns."""
        clock = Clock()
        live = make_liveness(tmp_path, clock)
        live.on_assign([1])
        live.record_poll(1)

        with live.handling():
            clock.advance(600)
            live.on_revoke([1])  # the client was evicted mid-handler

        live.record_poll(0)
        live.check()  # no exit: the rejoin has not had its window yet

        clock.advance(121)
        live.record_poll(0)
        with pytest.raises(Exited):
            live.check()

    def test_max_poll_exceeded_is_reported_as_itself(self, tmp_path, caplog):
        clock = Clock()
        live = make_liveness(tmp_path, clock)
        with caplog.at_level("ERROR"):
            live.on_broker_error(
                types.SimpleNamespace(
                    name=lambda: "_MAX_POLL_EXCEEDED", fatal=lambda: False
                )
            )
        message = caplog.records[-1].getMessage()
        assert "max.poll.interval.ms" in message
        assert "SLOW HANDLER" in message
        assert live.fatal_error is None
        assert live.stall_reason() is None


class TestHeartbeat:
    def test_not_touched_while_unassigned(self, tmp_path):
        """The lie at the centre of the incident: the old loop touched this
        file after EVERY poll return, so sixteen hours of owning nothing
        kept it seconds old and every probe green."""
        clock = Clock()
        live = make_liveness(tmp_path, clock)
        live.on_assign([1])
        assert live.touch_heartbeat() is True
        assert (tmp_path / "alive").exists()

        live.on_revoke([1])
        stamp = (tmp_path / "alive").stat().st_mtime
        for _ in range(100):
            assert live.touch_heartbeat() is False
        assert (tmp_path / "alive").stat().st_mtime == stamp

    def test_an_unwritable_path_is_not_fatal(self, tmp_path):
        clock = Clock()
        live = ConsumerLiveness(
            group="g", heartbeat=str(tmp_path / "nope" / "deeper" / "alive"),
            clock=clock, exit_process=_exit,
        )
        live.on_assign([1])
        assert live.touch_heartbeat() is False


class TestSettings:
    def test_stall_window_default_and_override(self, monkeypatch):
        monkeypatch.delenv("STAPEL_BUS_CONSUMER_STALL_SECONDS", raising=False)
        assert stall_seconds() == 120.0
        monkeypatch.setenv("STAPEL_BUS_CONSUMER_STALL_SECONDS", "45")
        assert stall_seconds() == 45.0
        monkeypatch.setenv("STAPEL_BUS_CONSUMER_STALL_SECONDS", "0")
        assert stall_seconds() == 0.0

    def test_a_nonsense_window_falls_back_loudly(self, monkeypatch, caplog):
        monkeypatch.setenv("STAPEL_BUS_CONSUMER_STALL_SECONDS", "soon")
        with caplog.at_level("WARNING"):
            assert stall_seconds() == 120.0
        assert "not a number" in caplog.records[-1].getMessage()

    def test_heartbeat_path_honours_the_old_env_var(self, monkeypatch):
        monkeypatch.delenv("STAPEL_BUS_CONSUMER_HEARTBEAT_PATH", raising=False)
        monkeypatch.setenv("KAFKA_CONSUMER_HEARTBEAT", "/tmp/legacy-probe")
        assert heartbeat_path() == "/tmp/legacy-probe"
        monkeypatch.setenv("STAPEL_BUS_CONSUMER_HEARTBEAT_PATH", "/tmp/new-probe")
        assert heartbeat_path() == "/tmp/new-probe"


class TestMetrics:
    def test_the_gauges_and_the_counter(self, tmp_path):
        from stapel_core.observability import metrics
        from stapel_core.observability.backends import MetricsBackend

        recorded: list[tuple] = []

        class Recording(MetricsBackend):
            def counter(self, name, value=1.0, labels=None, *, description=""):
                recorded.append(("counter", name, value))

            def gauge(self, name, value, labels=None, *, description=""):
                recorded.append(("gauge", name, value))

            def histogram(self, name, value, labels=None, *, description="", buckets=None):
                pass

        metrics.set_backend(Recording())
        try:
            clock = Clock()
            live = make_liveness(tmp_path, clock)
            live.on_assign([1, 2])
            live.record_poll(2)
            live.declare_metrics()
            names = {name for _, name, _ in recorded}
            assert "stapel_bus_consumer_assignment_size" in names
            assert "stapel_bus_consumer_seconds_since_last_poll" in names
            assert "stapel_bus_consumer_stall_exits_total" in names
            assert ("gauge", "stapel_bus_consumer_assignment_size", 2) in recorded

            recorded.clear()
            live.on_revoke([1, 2])
            clock.advance(200)
            live.record_poll(0)
            with pytest.raises(Exited):
                live.check()
            assert ("counter", "stapel_bus_consumer_stall_exits_total", 1.0) in recorded
        finally:
            metrics.reset_backend()


class TestTheProbeCommand:
    """`manage.py bus_consumer_alive` — the Docker HEALTHCHECK."""

    def _run(self, **options):
        from django.core.management import call_command

        from stapel_core.django.management.commands.bus_consumer_alive import Command

        return call_command(Command(), **options)

    def test_returns_one_on_a_stale_file(self, tmp_path):
        import os
        import time

        probe = tmp_path / "alive"
        probe.write_text("")
        old = time.time() - 500
        os.utime(probe, (old, old))
        with pytest.raises(SystemExit) as exc:
            self._run(path=str(probe), max_age=90.0)
        assert exc.value.code == 1

    def test_returns_one_when_there_is_no_file_at_all(self, tmp_path):
        with pytest.raises(SystemExit) as exc:
            self._run(path=str(tmp_path / "never"), max_age=90.0)
        assert exc.value.code == 1

    def test_returns_zero_on_a_fresh_file(self, tmp_path):
        probe = tmp_path / "alive"
        probe.write_text("")
        self._run(path=str(probe), max_age=90.0)  # no SystemExit


class TestTheKafkaLoopIsWired:
    """The rule is only worth anything if the real loop applies it."""

    @pytest.fixture
    def kafka(self, monkeypatch):
        import time as time_module

        from stapel_core.bus.backends import kafka as kafka_module

        # The loop's own clock, wound by hand: otherwise a two-minute stall
        # window would have to be waited out for real.
        state = {"now": 1000.0}

        def monotonic():
            state["now"] += 0.5
            return state["now"]

        monkeypatch.setattr(time_module, "monotonic", monotonic)
        monkeypatch.setattr(kafka_module.signal, "signal", lambda *a, **kw: None)
        monkeypatch.setattr(kafka_module.time, "sleep", lambda seconds: None)

        class FakeKafkaError:
            _PARTITION_EOF = object()
            UNKNOWN_TOPIC_OR_PART = object()

        package = types.ModuleType("confluent_kafka")
        package.Consumer = FakeConsumer
        package.KafkaError = FakeKafkaError
        admin = types.ModuleType("confluent_kafka.admin")
        admin.AdminClient = lambda config: types.SimpleNamespace(
            list_topics=lambda timeout=None: types.SimpleNamespace(topics={}),
            create_topics=lambda new_topics: {},
        )
        admin.NewTopic = lambda *a, **kw: None
        package.admin = admin
        monkeypatch.setitem(sys.modules, "confluent_kafka", package)
        monkeypatch.setitem(sys.modules, "confluent_kafka.admin", admin)
        return kafka_module

    def test_the_loop_exits_when_the_assignment_never_comes_back(
        self, kafka, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("STAPEL_BUS_CONSUMER_STALL_SECONDS", "120")
        monkeypatch.setenv(
            "STAPEL_BUS_CONSUMER_HEARTBEAT_PATH", str(tmp_path / "alive")
        )
        monkeypatch.setattr(kafka.KafkaBus, "_provision_topics", lambda self, t: None)

        FakeConsumer.behaviour = "revoke_and_never_return"
        bus = kafka.KafkaBus()
        with pytest.raises(SystemExit) as exc:
            bus.consume(["a.topic"], "svc.actions", lambda e: None, poll_timeout=0)

        assert exc.value.code == STALL_EXIT_CODE
        assert FakeConsumer.last.closed is True
        # Nothing was assigned at the end, so nothing was ever promised:
        assert not (tmp_path / "alive").exists()

    def test_the_error_callback_is_wired_into_the_client_config(
        self, kafka, monkeypatch, tmp_path
    ):
        """`error_cb` is the only way the session timeout, the all-brokers-
        down and the fatal states are reported at all — they never become a
        message, so a loop that only looks at messages cannot see them."""
        monkeypatch.setattr(kafka.KafkaBus, "_provision_topics", lambda self, t: None)
        monkeypatch.setenv("STAPEL_BUS_CONSUMER_STALL_SECONDS", "0")
        monkeypatch.setenv(
            "STAPEL_BUS_CONSUMER_HEARTBEAT_PATH", str(tmp_path / "alive")
        )
        FakeConsumer.behaviour = "stop_at_once"
        with pytest.raises(_StopLoop):
            kafka.KafkaBus().consume(["a.topic"], "g", lambda e: None, poll_timeout=0)
        assert callable(FakeConsumer.last.config["error_cb"])
        assert "on_assign" in FakeConsumer.last.callbacks
        assert "on_revoke" in FakeConsumer.last.callbacks


class FakeConsumer:
    """A librdkafka client that can be told how to misbehave."""

    behaviour = "stop_at_once"
    last: "FakeConsumer" = None

    def __init__(self, config):
        self.config = config
        self.callbacks: dict = {}
        self.closed = False
        self._polls = 0
        self._assignment: list = []
        self._clock = 0.0
        FakeConsumer.last = self

    def subscribe(self, topics, **callbacks):
        self.topics = topics
        self.callbacks = callbacks
        if self.behaviour == "revoke_and_never_return":
            self._assignment = [object()]
            callbacks["on_assign"](self, self._assignment)

    def assignment(self):
        return self._assignment

    def list_topics(self, timeout=None):
        return types.SimpleNamespace(brokers={0: object()}, topics={})

    def poll(self, timeout=None):
        self._polls += 1
        if self.behaviour == "stop_at_once":
            raise _StopLoop()
        if self.behaviour == "revoke_and_never_return" and self._polls == 1:
            revoked, self._assignment = self._assignment, []
            self.callbacks["on_revoke"](self, revoked)
        # Time passes while a consumer that owns nothing polls nothing.
        return None

    def commit(self, msg):
        pass

    def close(self):
        self.closed = True


class _StopLoop(BaseException):
    """How the fake leaves a `while True` without pretending to be an error."""
