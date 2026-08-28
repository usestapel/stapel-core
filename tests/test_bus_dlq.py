"""Giving up on an event must be a NUMBER, not only a log line.

Same outage as `test_worker_db_hygiene.py`, second half of the lesson.
Eight login codes were parked in the dead-letter queue over two and a half
days (2026-08-25 → 2026-08-28). Every one of them was logged at ERROR. The
containers reported "Up", the HTTP layer kept answering "Verification code
sent successfully" because publishing to the bus really had succeeded, and
the outage ended when a human happened to look at the logs.

The signal existed the whole time. Nothing counted it, so nothing could
alarm on it — and a DLQ nobody alarms on is a place work goes to be
forgotten quietly.
"""
import pytest

from stapel_core.bus.event import Event

class RecordingMetrics:
    """Captures counter calls so a test can assert on the number itself."""

    def __init__(self):
        self.counters: list[tuple] = []

    def counter(self, name, value=1.0, labels=None, *, description=""):
        self.counters.append((name, value, dict(labels or {})))

    def gauge(self, *a, **kw):  # pragma: no cover - unused here
        pass

    def histogram(self, *a, **kw):  # pragma: no cover - unused here
        pass

    def available(self) -> bool:
        return True


@pytest.fixture
def recorded_metrics(monkeypatch):
    from stapel_core.observability import metrics as metrics_module

    backend = RecordingMetrics()
    monkeypatch.setattr(metrics_module, "get_backend", lambda: backend)
    return backend


class TestDlqIsCounted:
    """Giving up on an event must be a NUMBER, not only a log line.

    Eight login codes were parked over two and a half days. Every one was
    logged at ERROR, the containers stayed "Up", and the outage ended when a
    human happened to look. The signal existed; nothing counted it, so
    nothing could alarm on it.
    """

    def test_a_parked_event_increments_the_counter(self, recorded_metrics, monkeypatch):
        from stapel_core.bus.backends.kafka import KafkaBus

        monkeypatch.setattr(KafkaBus, "publish", lambda self, topic, event: None)
        KafkaBus()._send_to_dlq(
            "a.topic", Event(event_type="otp.requested", service="auth", payload={})
        )
        names = [c[0] for c in recorded_metrics.counters]
        assert any(n.endswith("bus_dlq_total") for n in names), names

    def test_the_label_says_which_topic_and_which_event(self, recorded_metrics, monkeypatch):
        """A single number nobody can break down tells an operator that
        SOMETHING is being dropped, which is not actionable at 3am."""
        from stapel_core.bus.backends.kafka import KafkaBus

        monkeypatch.setattr(KafkaBus, "publish", lambda self, topic, event: None)
        KafkaBus()._send_to_dlq(
            "a.topic", Event(event_type="otp.requested", service="auth", payload={})
        )
        labels = recorded_metrics.counters[0][2]
        assert labels["topic"] == "a.topic"
        assert labels["event_type"] == "otp.requested"
        assert labels["reason"] == "handler"

    def test_an_undecodable_message_is_counted_separately(self, recorded_metrics, monkeypatch):
        """A producer/consumer format split is a different failure from a
        handler bug and needs a different answer, so it must be separable."""
        from stapel_core.bus.backends.kafka import KafkaBus

        monkeypatch.setattr(KafkaBus, "publish", lambda self, topic, event: None)
        KafkaBus()._send_raw_to_dlq("a.topic", b"not an event")
        reasons = [c[2]["reason"] for c in recorded_metrics.counters]
        assert "undecodable" in reasons

    def test_a_metrics_backend_that_is_down_does_not_swallow_the_event(self, monkeypatch):
        """The caller is already on a failure path. A metrics outage must not
        turn a parked event into a crash that loses it entirely."""
        from stapel_core.bus.backends.kafka import KafkaBus
        from stapel_core.observability import metrics as metrics_module

        def boom():
            raise RuntimeError("metrics backend is down")

        monkeypatch.setattr(metrics_module, "get_backend", boom)
        published: list = []
        monkeypatch.setattr(KafkaBus, "publish", lambda self, topic, event: published.append(event))
        assert KafkaBus()._send_to_dlq(
            "a.topic", Event(event_type="otp.requested", service="auth", payload={})
        )
        assert len(published) == 1
