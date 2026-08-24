"""Tests for stapel_core.observability — the facade, its seams and the
trace that ties an operation together across the comm envelope."""
import json
import logging

import pytest
from django.test import override_settings

from stapel_core.bus import Event
from stapel_core.observability import (
    LoggingErrorReporter,
    LoggingMetricsBackend,
    NoopErrorReporter,
    NoopMetricsBackend,
    PrometheusMetricsBackend,
    StatsdMetricsBackend,
    configure_logging,
    continue_trace,
    current_trace,
    format_traceparent,
    logging_config,
    metrics,
    new_span_id,
    new_trace_id,
    parse_traceparent,
    report_error,
    report_message,
    sanitize_id,
    start_trace,
    trace_ids,
)
from stapel_core.observability import errors as errors_mod
from stapel_core.observability.logs import JsonFormatter, TraceContextFilter


# ── recording backends used across the file ─────────────────────────────


class RecordingBackend(NoopMetricsBackend):
    available = True

    def __init__(self):
        self.calls = []

    def counter(self, name, value=1.0, labels=None, *, description=""):
        self.calls.append(("counter", name, value, dict(labels or {})))

    def gauge(self, name, value, labels=None, *, description=""):
        self.calls.append(("gauge", name, value, dict(labels or {})))

    def histogram(self, name, value, labels=None, *, description="", buckets=None):
        self.calls.append(("histogram", name, value, dict(labels or {})))

    def expose(self):
        return "# recording\n"


class ExplodingBackend(NoopMetricsBackend):
    available = True

    def counter(self, *a, **kw):
        raise RuntimeError("metrics backend is on fire")

    gauge = counter
    histogram = counter


@pytest.fixture
def recorder():
    backend = RecordingBackend()
    metrics.set_backend(backend)
    yield backend
    metrics.set_backend(None)


# ── trace context ───────────────────────────────────────────────────────


class TestTraceContext:
    def test_unbound_context_is_empty_never_none(self):
        ctx = current_trace()
        assert ctx.trace_id == ""
        assert trace_ids() == {
            "trace_id": "", "span_id": "", "correlation_id": "",
            "causation_id": "", "request_id": "",
        }

    def test_start_trace_mints_w3c_shaped_ids(self):
        with start_trace() as ctx:
            assert len(ctx.trace_id) == 32
            assert len(ctx.span_id) == 16
            int(ctx.trace_id, 16)
            int(ctx.span_id, 16)

    def test_correlation_defaults_to_the_trace(self):
        with start_trace() as ctx:
            assert ctx.correlation_id == ctx.trace_id

    def test_explicit_correlation_survives(self):
        with start_trace(correlation_id="order-7") as ctx:
            assert ctx.correlation_id == "order-7"
            assert ctx.trace_id != "order-7"

    def test_context_is_restored_on_exit(self):
        with start_trace(trace_id="a" * 32):
            with start_trace(trace_id="b" * 32):
                assert current_trace().trace_id == "b" * 32
            assert current_trace().trace_id == "a" * 32
        assert current_trace().trace_id == ""

    def test_context_is_restored_after_an_exception(self):
        with pytest.raises(ValueError):
            with start_trace(trace_id="c" * 32):
                raise ValueError("boom")
        assert current_trace().trace_id == ""

    def test_inherit_keeps_the_operation_and_starts_a_new_span(self):
        with start_trace(trace_id="d" * 32, correlation_id="op-1") as outer:
            with start_trace(inherit=True) as inner:
                assert inner.trace_id == outer.trace_id
                assert inner.correlation_id == "op-1"
                assert inner.span_id != outer.span_id

    def test_ids_off_the_wire_are_sanitized(self):
        with start_trace(trace_id='ab"c\n<script>') as ctx:
            assert ctx.trace_id == "abcscript"

    def test_ids_off_the_wire_are_length_capped(self):
        with start_trace(request_id="x" * 500) as ctx:
            assert len(ctx.request_id) == 128

    def test_sanitize_id_of_nothing_is_nothing(self):
        assert sanitize_id(None) == ""
        assert sanitize_id("   ") == ""

    def test_new_ids_are_distinct(self):
        assert new_trace_id() != new_trace_id()
        assert new_span_id() != new_span_id()


class TestTraceparent:
    def test_round_trip(self):
        with start_trace() as ctx:
            header = format_traceparent(ctx)
            parsed = parse_traceparent(header)
            assert parsed["trace_id"] == ctx.trace_id
            assert parsed["span_id"] == ctx.span_id

    def test_incoming_traceparent_seeds_the_trace_and_the_cause(self):
        header = f"00-{'1' * 32}-{'2' * 16}-01"
        with start_trace(traceparent=header) as ctx:
            assert ctx.trace_id == "1" * 32
            # the caller's span is what caused this hop
            assert ctx.causation_id == "2" * 16
            assert ctx.span_id != "2" * 16

    @pytest.mark.parametrize(
        "value",
        ["", "garbage", "00-short-0000000000000000-01", f"00-{'0' * 32}-{'2' * 16}-01"],
    )
    def test_a_header_we_did_not_write_is_not_an_error(self, value):
        assert parse_traceparent(value) is None

    def test_non_w3c_ids_render_no_header_rather_than_a_bad_one(self):
        with start_trace(trace_id="order-42") as ctx:
            assert format_traceparent(ctx) == ""


# ── the comm envelope carries the trace ─────────────────────────────────


class TestEnvelopeCorrelation:
    def test_emit_inside_a_trace_stamps_the_envelope(self):
        with start_trace(correlation_id="op-9") as ctx:
            event = Event(event_type="x.happened", service="svc")
        assert event.trace_id == ctx.trace_id
        assert event.correlation_id == "op-9"
        assert event.span_id == ctx.span_id

    def test_an_untraced_process_publishes_what_it_always_did(self):
        event = Event(event_type="x.happened", service="svc")
        assert event.trace_id == ""
        assert event.correlation_id == ""

    def test_trace_survives_the_json_round_trip(self):
        with start_trace(correlation_id="op-9"):
            event = Event(event_type="x.happened", service="svc")
        restored = Event.from_json(event.to_json())
        assert restored.trace_id == event.trace_id
        assert restored.correlation_id == "op-9"
        assert restored.causation_id == event.causation_id

    def test_rehydration_does_not_restamp_with_the_current_trace(self):
        # The outbox relay reads a row minutes later, in another process: the
        # event still belongs to the operation that wrote it.
        raw = Event(event_type="x.happened", service="svc").to_json()
        with start_trace(trace_id="e" * 32):
            restored = Event.from_json(raw)
        assert restored.trace_id == ""

    def test_an_envelope_from_before_the_field_existed_reads_as_untraced(self):
        legacy = json.dumps(
            {"event_type": "x.happened", "service": "svc", "payload": {}}
        )
        restored = Event.from_json(legacy)
        assert restored.trace_id == ""
        assert restored.correlation_id == ""

    def test_trace_does_not_change_event_equality(self):
        # Two events are the same event because of what they say, not because
        # of which trace observed them.
        with start_trace():
            a = Event(event_type="x", service="s", event_id="1", timestamp=1)
        with start_trace():
            b = Event(event_type="x", service="s", event_id="1", timestamp=1)
        assert a.trace_id != b.trace_id
        assert a == b

    def test_continue_trace_inherits_the_operation_and_names_the_cause(self):
        with start_trace(correlation_id="op-3"):
            incoming = Event(event_type="x.happened", service="svc")
        with continue_trace(incoming) as ctx:
            assert ctx.trace_id == incoming.trace_id
            assert ctx.correlation_id == "op-3"
            assert ctx.causation_id == incoming.event_id
            assert ctx.span_id != incoming.span_id

    def test_continue_trace_on_an_untraced_envelope_starts_a_trace(self):
        incoming = Event(event_type="x.happened", service="svc")
        with continue_trace(incoming) as ctx:
            assert len(ctx.trace_id) == 32

    def test_continue_trace_accepts_a_plain_dict(self):
        with continue_trace(
            {"trace_id": "f" * 32, "event_id": "evt-1", "correlation_id": "op-4"}
        ) as ctx:
            assert ctx.trace_id == "f" * 32
            assert ctx.causation_id == "evt-1"


@pytest.mark.django_db
class TestSubscriberInheritsTheTrace:
    def test_a_handler_and_its_own_emit_join_the_causing_operation(self):
        from stapel_core.comm import deliver, subscribe_action
        from stapel_core.comm.registry import action_registry

        seen = {}
        derived = []

        def handler(event):
            seen.update(trace_ids())
            derived.append(Event(event_type="y.derived", service="svc"))

        subscribe_action("x.happened", handler)
        try:
            with start_trace(correlation_id="op-11"):
                incoming = Event(event_type="x.happened", service="svc")
            with override_settings(STAPEL_COMM={"ACTION_TRANSPORT": "inprocess"}):
                deliver(incoming)
        finally:
            action_registry._subscribers.pop("x.happened", None)

        assert seen["trace_id"] == incoming.trace_id
        assert seen["correlation_id"] == "op-11"
        # this happened BECAUSE of that event
        assert seen["causation_id"] == incoming.event_id
        assert derived[0].trace_id == incoming.trace_id
        assert derived[0].causation_id == incoming.event_id

    def test_the_trace_does_not_leak_past_delivery(self):
        from stapel_core.comm import deliver

        event = Event(event_type="nobody.listens", service="svc")
        with override_settings(STAPEL_COMM={"ACTION_TRANSPORT": "inprocess"}):
            deliver(event)
        assert current_trace().trace_id == ""


# ── metrics facade ──────────────────────────────────────────────────────


class TestMetricsFacade:
    def test_counter_reaches_the_backend(self, recorder):
        metrics.counter("messages_total", labels={"kind": "text"})
        kind, name, value, labels = recorder.calls[0]
        assert (kind, value, labels) == ("counter", 1.0, {"kind": "text"})
        assert name == "stapel_messages_total"

    def test_gauge_and_histogram_reach_the_backend(self, recorder):
        metrics.gauge("backlog", 42)
        metrics.histogram("size_bytes", 1024)
        assert [c[0] for c in recorder.calls] == ["gauge", "histogram"]

    def test_observe_is_histogram(self, recorder):
        metrics.observe("size_bytes", 7)
        assert recorder.calls[0][0] == "histogram"

    def test_timer_records_a_duration_in_seconds(self, recorder):
        with metrics.timer("work_seconds"):
            pass
        kind, name, value, _ = recorder.calls[0]
        assert kind == "histogram"
        assert name == "stapel_work_seconds"
        assert 0 <= value < 5

    def test_timer_measures_a_block_that_raises(self, recorder):
        with pytest.raises(ValueError):
            with metrics.timer("work_seconds"):
                raise ValueError("boom")
        assert recorder.calls and recorder.calls[0][0] == "histogram"

    def test_names_are_namespaced_once(self):
        assert metrics.metric_name("x_total") == "stapel_x_total"
        assert metrics.metric_name("stapel_x_total") == "stapel_x_total"

    def test_illegal_characters_are_made_prometheus_legal(self):
        assert metrics.metric_name("http.request-duration") == (
            "stapel_http_request_duration"
        )

    @override_settings(STAPEL_OBSERVABILITY={"METRIC_NAMESPACE": "iron_"})
    def test_namespace_is_a_setting(self):
        metrics.reset_backend()
        assert metrics.metric_name("x_total") == "iron_x_total"

    def test_a_burning_backend_never_reaches_the_caller(self):
        metrics.set_backend(ExplodingBackend())
        try:
            metrics.counter("x_total")
            metrics.gauge("y", 1)
            metrics.histogram("z", 1)
        finally:
            metrics.set_backend(None)


class TestMetricsBackendSeam:
    def test_default_is_the_prometheus_backend(self):
        metrics.set_backend(None)
        metrics.reset_backend()
        assert isinstance(metrics.get_backend(), PrometheusMetricsBackend)

    @override_settings(
        STAPEL_OBSERVABILITY={
            "METRICS_BACKEND":
                "stapel_core.observability.backends.LoggingMetricsBackend"
        }
    )
    def test_the_backend_is_swappable_by_dotted_path(self):
        assert isinstance(metrics.get_backend(), LoggingMetricsBackend)

    @override_settings(
        STAPEL_OBSERVABILITY={"METRICS_BACKEND": "nope.NotAThing"}
    )
    def test_an_unimportable_backend_degrades_to_noop(self, caplog):
        with caplog.at_level(logging.WARNING):
            backend = metrics.get_backend()
        assert isinstance(backend, NoopMetricsBackend)

    @override_settings(
        STAPEL_OBSERVABILITY={"METRICS_BACKEND": "stapel_core.conf.AppSettings"}
    )
    def test_a_class_that_is_not_a_backend_degrades_to_noop(self):
        assert isinstance(metrics.get_backend(), NoopMetricsBackend)

    def test_setting_changed_rebuilds_the_backend(self):
        metrics.set_backend(None)
        metrics.reset_backend()
        first = metrics.get_backend()
        with override_settings(
            STAPEL_OBSERVABILITY={
                "METRICS_BACKEND":
                    "stapel_core.observability.backends.NoopMetricsBackend"
            }
        ):
            assert metrics.get_backend() is not first
        assert not isinstance(metrics.get_backend(), NoopMetricsBackend)


class TestPrometheusBackend:
    def _backend(self):
        pytest.importorskip("prometheus_client")
        from prometheus_client import CollectorRegistry

        return PrometheusMetricsBackend(registry=CollectorRegistry())

    def test_records_and_exposes(self):
        backend = self._backend()
        backend.counter("t_messages_total", 3, {"kind": "text"})
        text = backend.expose()
        assert "t_messages_total" in text
        assert 'kind="text"' in text

    def test_conflicting_label_sets_are_dropped_not_raised(self, caplog):
        backend = self._backend()
        backend.counter("t_conflict_total", 1, {"a": "1"})
        with caplog.at_level(logging.WARNING):
            backend.counter("t_conflict_total", 1, {"b": "2"})
        # second registration was refused by the client; nothing propagated
        assert "t_conflict_total" in backend.expose()

    def test_missing_client_library_degrades_instead_of_raising(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def no_prometheus(name, *args, **kwargs):
            if name == "prometheus_client":
                raise ImportError("no prometheus_client")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_prometheus)
        backend = PrometheusMetricsBackend()
        assert backend.available is False
        backend.counter("x_total")
        assert backend.expose() == ""


class TestStatsdBackend:
    def test_sends_a_datagram_per_measurement(self, monkeypatch):
        sent = []
        backend = StatsdMetricsBackend(host="127.0.0.1", port=9)
        monkeypatch.setattr(backend, "_send", lambda line: sent.append(line))
        backend.counter("x_total", 2, {"a": "b"})
        backend.gauge("y", 5)
        backend.histogram("z_seconds", 0.5)
        assert sent[0] == "x_total:2|c|#a:b"
        assert sent[1] == "y:5|g"
        # seconds at the facade, milliseconds on the wire
        assert sent[2] == "z_seconds:500.0|ms"

    def test_an_unreachable_agent_is_not_the_callers_problem(self):
        backend = StatsdMetricsBackend(host="0.0.0.0", port=0)
        backend.counter("x_total")


# ── structured logging ──────────────────────────────────────────────────


def _format(record, **kwargs):
    return json.loads(JsonFormatter(service="svc", **kwargs).format(record))


def _record(msg="hello", level=logging.INFO, **extra):
    record = logging.LogRecord("my.logger", level, __file__, 10, msg, (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


class TestJsonFormatter:
    def test_mandatory_field_set(self):
        payload = _format(_record())
        assert set(payload) >= {
            "ts", "level", "service", "logger", "msg",
            "trace_id", "span_id", "correlation_id", "causation_id",
            "request_id",
        }
        assert payload["level"] == "INFO"
        assert payload["service"] == "svc"
        assert payload["logger"] == "my.logger"
        assert payload["msg"] == "hello"

    def test_one_json_object_per_record(self):
        line = JsonFormatter(service="svc").format(_record())
        assert "\n" not in line
        json.loads(line)

    def test_trace_ids_come_from_the_context_in_flight(self):
        with start_trace(correlation_id="op-2") as ctx:
            payload = _format(_record())
        assert payload["trace_id"] == ctx.trace_id
        assert payload["correlation_id"] == "op-2"

    def test_extra_fields_are_carried_through(self):
        payload = _format(_record(user_count=3, tenant="acme"))
        assert payload["user_count"] == 3
        assert payload["tenant"] == "acme"

    def test_sensitive_extras_are_redacted_at_the_formatter(self):
        payload = _format(
            _record(password="hunter2", api_key="sk-live", tenant="acme")
        )
        assert payload["password"] == "***"
        assert payload["api_key"] == "***"
        assert payload["tenant"] == "acme"

    def test_redaction_is_case_insensitive(self):
        assert _format(_record(Authorization="Bearer x"))["Authorization"] == "***"

    def test_exceptions_become_queryable_fields(self):
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = _record(msg="failed", level=logging.ERROR)
            record.exc_info = sys.exc_info()
        payload = _format(record)
        assert payload["exc_type"] == "ValueError"
        assert payload["exc_message"] == "boom"
        assert "ValueError: boom" in payload["stack"]

    def test_unserializable_values_do_not_lose_the_line(self):
        payload = _format(_record(thing=object()))
        assert payload["msg"] == "hello"
        assert isinstance(payload["thing"], str)

    def test_static_fields_are_merged(self):
        payload = _format(_record(), static_fields={"deployment": "eu-1"})
        assert payload["deployment"] == "eu-1"

    def test_source_is_opt_in(self):
        assert "line" not in _format(_record())
        assert _format(_record(), include_source=True)["line"] == 10


class TestTraceContextFilter:
    def test_stamps_ids_onto_the_record_for_any_formatter(self):
        record = _record()
        with start_trace() as ctx:
            TraceContextFilter().filter(record)
        assert record.trace_id == ctx.trace_id

    def test_never_drops_a_record(self):
        assert TraceContextFilter().filter(_record()) is True


class TestLoggingConfig:
    def test_default_config_is_json_on_stdout(self):
        config = logging_config(service="chat")
        assert config["handlers"]["stapel"]["formatter"] == "json"
        assert config["handlers"]["stapel"]["stream"] == "ext://sys.stdout"
        assert config["handlers"]["stapel"]["filters"] == ["trace_context"]
        assert config["formatters"]["json"]["service"] == "chat"

    @override_settings(STAPEL_OBSERVABILITY={"LOG_FORMAT": "text"})
    def test_text_is_available_for_a_developer_terminal(self):
        assert logging_config()["handlers"]["stapel"]["formatter"] == "text"

    def test_extra_loggers_are_merged(self):
        config = logging_config(loggers={"noisy": {"level": "ERROR"}})
        assert config["loggers"]["noisy"]["level"] == "ERROR"

    def test_configure_logging_applies_it(self, capsys):
        previous = logging.root.handlers[:]
        previous_level = logging.root.level
        try:
            configure_logging(service="chat", level="INFO")
            logging.getLogger("t.observability").info("wired", extra={"k": 1})
            payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
            assert payload["service"] == "chat"
            assert payload["msg"] == "wired"
            assert payload["k"] == 1
        finally:
            logging.root.handlers[:] = previous
            logging.root.setLevel(previous_level)


# ── error-reporting seam ────────────────────────────────────────────────


class RecordingReporter(errors_mod.ErrorReporter):
    def __init__(self):
        self.exceptions = []
        self.messages = []

    def capture_exception(self, exc=None, *, context=None, tags=None, level="error"):
        self.exceptions.append((exc, dict(context or {}), dict(tags or {})))
        return "evt-1"

    def capture_message(self, message, *, context=None, tags=None, level="error"):
        self.messages.append((message, dict(context or {}), dict(tags or {})))
        return "evt-2"


class TestErrorReporterSeam:
    def test_default_reports_nowhere(self):
        errors_mod.set_error_reporter(None)
        errors_mod.reset_error_reporter()
        reporter = errors_mod.get_error_reporter()
        assert isinstance(reporter, NoopErrorReporter)
        assert reporter.active is False
        assert report_error(ValueError("boom")) is None

    def test_a_configured_reporter_receives_the_exception(self):
        recording = RecordingReporter()
        errors_mod.set_error_reporter(recording)
        try:
            assert report_error(ValueError("boom"), context={"order": 7}) == "evt-1"
            assert report_message("threshold crossed") == "evt-2"
        finally:
            errors_mod.set_error_reporter(None)
        assert isinstance(recording.exceptions[0][0], ValueError)
        assert recording.exceptions[0][1] == {"order": 7}
        assert recording.messages[0][0] == "threshold crossed"

    def test_report_error_defaults_to_the_exception_being_handled(self):
        recording = RecordingReporter()
        errors_mod.set_error_reporter(recording)
        try:
            try:
                raise KeyError("missing")
            except KeyError:
                report_error()
        finally:
            errors_mod.set_error_reporter(None)
        assert isinstance(recording.exceptions[0][0], KeyError)

    def test_a_failing_reporter_does_not_become_a_second_error(self):
        class Exploding(errors_mod.ErrorReporter):
            def capture_exception(self, *a, **kw):
                raise RuntimeError("reporter down")

        errors_mod.set_error_reporter(Exploding())
        try:
            assert report_error(ValueError("boom")) is None
        finally:
            errors_mod.set_error_reporter(None)

    @override_settings(
        STAPEL_OBSERVABILITY={
            "ERROR_REPORTER":
                "stapel_core.observability.errors.LoggingErrorReporter"
        }
    )
    def test_the_reporter_is_swappable_by_dotted_path(self):
        assert isinstance(errors_mod.get_error_reporter(), LoggingErrorReporter)

    @override_settings(STAPEL_OBSERVABILITY={"ERROR_REPORTER": "nope.NotAThing"})
    def test_an_unimportable_reporter_degrades_to_noop(self):
        assert isinstance(errors_mod.get_error_reporter(), NoopErrorReporter)

    def test_logging_reporter_carries_the_trace_into_the_record(self, caplog):
        errors_mod.set_error_reporter(LoggingErrorReporter("t.errors"))
        try:
            with caplog.at_level(logging.ERROR, logger="t.errors"):
                with start_trace() as ctx:
                    report_error(ValueError("boom"), context={"order": 7})
        finally:
            errors_mod.set_error_reporter(None)
        record = caplog.records[-1]
        assert record.trace_id == ctx.trace_id
        assert record.order == 7

    def test_sentry_reporter_without_the_sdk_is_inactive_not_fatal(
        self, monkeypatch
    ):
        import builtins

        real_import = builtins.__import__

        def no_sentry(name, *args, **kwargs):
            if name == "sentry_sdk":
                raise ImportError("no sentry_sdk")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_sentry)
        reporter = errors_mod.SentryErrorReporter()
        assert reporter.active is False
        assert reporter.capture_exception(ValueError("boom")) is None


# ── middleware ──────────────────────────────────────────────────────────


@pytest.fixture
def rf():
    from django.test import RequestFactory

    return RequestFactory()


def _middleware(response=None, seen=None):
    from django.http import HttpResponse

    from stapel_core.observability.middleware import TraceContextMiddleware

    def get_response(request):
        if seen is not None:
            seen.append(trace_ids())
        return response or HttpResponse("ok")

    return TraceContextMiddleware(get_response)


class TestTraceContextMiddleware:
    def test_a_request_starts_a_trace(self, rf):
        seen = []
        response = _middleware(seen=seen)(rf.get("/x"))
        assert len(seen[0]["trace_id"]) == 32
        assert response["X-Trace-Id"] == seen[0]["trace_id"]

    def test_ids_are_put_on_the_request(self, rf):
        request = rf.get("/x")
        _middleware()(request)
        assert request.trace_id and request.request_id and request.correlation_id

    def test_an_incoming_trace_is_joined_not_replaced(self, rf):
        seen = []
        _middleware(seen=seen)(
            rf.get("/x", HTTP_X_TRACE_ID="a" * 32, HTTP_X_REQUEST_ID="req-1")
        )
        assert seen[0]["trace_id"] == "a" * 32
        assert seen[0]["request_id"] == "req-1"

    def test_a_traceparent_joins_the_caller_trace(self, rf):
        seen = []
        _middleware(seen=seen)(
            rf.get("/x", HTTP_TRACEPARENT=f"00-{'b' * 32}-{'c' * 16}-01")
        )
        assert seen[0]["trace_id"] == "b" * 32
        assert seen[0]["causation_id"] == "c" * 16

    @override_settings(STAPEL_OBSERVABILITY={"TRUST_INCOMING_TRACE": False})
    def test_a_distrusting_edge_mints_its_own_trace(self, rf):
        seen = []
        _middleware(seen=seen)(rf.get("/x", HTTP_X_TRACE_ID="a" * 32))
        assert seen[0]["trace_id"] != "a" * 32

    def test_an_injected_id_never_reaches_a_log_field_raw(self, rf):
        seen = []
        _middleware(seen=seen)(rf.get("/x", HTTP_X_TRACE_ID='ev"il\nnext'))
        assert seen[0]["trace_id"] == "evilnext"

    @override_settings(STAPEL_OBSERVABILITY={"ECHO_TRACE_HEADERS": False})
    def test_echo_is_a_setting(self, rf):
        assert "X-Trace-Id" not in _middleware()(rf.get("/x"))

    def test_the_trace_does_not_outlive_the_request(self, rf):
        _middleware()(rf.get("/x"))
        assert current_trace().trace_id == ""

    def test_requests_are_measured_with_bounded_label_cardinality(
        self, rf, recorder
    ):
        _middleware()(rf.get("/orders/12345"))
        names = [c[1] for c in recorder.calls]
        assert "stapel_http_requests_total" in names
        assert "stapel_http_request_duration_seconds" in names
        labels = recorder.calls[0][3]
        assert labels["method"] == "GET"
        assert labels["status"] == "200"
        # the URL PATTERN, never the resolved path — an id in a label is how
        # instrumentation takes down the system it measures
        assert "12345" not in labels["route"]

    @override_settings(STAPEL_OBSERVABILITY={"REQUEST_METRICS": False})
    def test_request_metrics_can_be_turned_off(self, rf, recorder):
        _middleware()(rf.get("/x"))
        assert recorder.calls == []


# ── the /api/metrics/ join ──────────────────────────────────────────────


class TestExporterJoin:
    def test_facade_metrics_land_on_the_existing_endpoint(self, recorder):
        from stapel_core.observability.exporter import facade_exposition

        assert facade_exposition() == "# recording\n"

    def test_registration_is_idempotent(self):
        from stapel_core.observability import exporter

        exporter.register_prometheus_exporter()
        assert exporter.register_prometheus_exporter() is False

    def test_a_backend_with_nothing_to_expose_contributes_nothing(self):
        from stapel_core.observability.exporter import facade_exposition

        metrics.set_backend(NoopMetricsBackend())
        try:
            assert facade_exposition() == ""
        finally:
            metrics.set_backend(None)


# ── system checks ───────────────────────────────────────────────────────


class TestChecks:
    def test_a_service_that_never_adopted_the_facade_is_not_nagged(self):
        from stapel_core.observability import checks

        with override_settings():
            from django.conf import settings

            for key in ("STAPEL_OBSERVABILITY",):
                if hasattr(settings, key):
                    delattr(settings._wrapped, key)
            assert checks.check_metrics_backend() == []
            assert checks.check_error_reporter() == []
            assert checks.check_trace_middleware() == []

    @override_settings(
        STAPEL_OBSERVABILITY={"METRICS_BACKEND": "nope.NotAThing"}
    )
    def test_w001_names_a_backend_that_could_not_be_built(self):
        from stapel_core.observability import checks

        metrics.set_backend(None)
        metrics.reset_backend()
        ids = [w.id for w in checks.check_metrics_backend()]
        assert checks.W001_METRICS_BACKEND_BROKEN in ids

    @override_settings(
        STAPEL_OBSERVABILITY={
            "METRICS_BACKEND":
                "stapel_core.observability.backends.NoopMetricsBackend"
        }
    )
    def test_choosing_no_metrics_on_purpose_is_not_a_warning(self):
        from stapel_core.observability import checks

        metrics.set_backend(None)
        metrics.reset_backend()
        assert checks.check_metrics_backend() == []

    @override_settings(
        STAPEL_OBSERVABILITY={"ERROR_REPORTER": "nope.NotAThing"}
    )
    def test_w003_names_a_reporter_that_could_not_be_built(self):
        from stapel_core.observability import checks

        errors_mod.set_error_reporter(None)
        errors_mod.reset_error_reporter()
        ids = [w.id for w in checks.check_error_reporter()]
        assert checks.W003_ERROR_REPORTER in ids

    @override_settings(
        STAPEL_OBSERVABILITY={"LOG_FORMAT": "json"}, MIDDLEWARE=["x.Y"]
    )
    def test_w004_says_no_request_starts_a_trace(self):
        from stapel_core.observability import checks

        ids = [w.id for w in checks.check_trace_middleware()]
        assert checks.W004_NO_TRACE_MIDDLEWARE in ids

    @override_settings(
        STAPEL_OBSERVABILITY={"LOG_FORMAT": "json"},
        MIDDLEWARE=[
            "stapel_core.observability.middleware.TraceContextMiddleware"
        ],
    )
    def test_w004_is_silent_once_the_middleware_is_wired(self):
        from stapel_core.observability import checks

        assert checks.check_trace_middleware() == []
