"""stapel_core.observability — signals out, vendors swappable.

Four things a service needs and every service was building for itself
(docs/pending/data-storage-and-observability-v2.md §2):

============  ==========================================================
Structured    ``configure_logging()`` / ``logging_config()`` — one JSON
logging       object per record with a mandatory field set, secrets
              redacted at the formatter, trace ids on every line.
Metrics       ``metrics.counter/gauge/histogram/timer`` — a facade, the
              way ``analytics.track`` is a facade. Backend is a seam:
              Prometheus by default, statsd/logging/no-op/your own via
              ``STAPEL_OBSERVABILITY["METRICS_BACKEND"]``.
Errors        ``report_error()`` over an ``ERROR_REPORTER`` seam with a
              Sentry-shaped interface and a **no-op default** — the
              framework does not decide that your exceptions go to a
              third party.
Correlation   ``start_trace()`` / ``continue_trace()`` and trace ids
              carried in the comm envelope, so one business operation is
              one query in the aggregator instead of a scatter of lines
              across N services.
============  ==========================================================

The framework/platform border is the same one as everywhere else: this
package **emits** clean signals through seams; Prometheus, Grafana, Loki,
Sentry and Alertmanager **collect and display** them, and are a
deployment's business, not a library's.

Correlation is the part no off-the-shelf APM can do for us, because no APM
knows about ``stapel_core.comm``. A request binds a trace context
(``TraceContextMiddleware``); every ``comm.emit`` stamps ``trace_id`` /
``span_id`` / ``correlation_id`` / ``causation_id`` into the envelope;
delivery to a subscriber re-binds them on the far side (``causation_id``
becoming the id of the event that caused the work). An operation is then
followed with one predicate — ``trace_id = <x>`` — from the HTTP request
through every module and service it touched.

Minimal adoption::

    # settings.py
    from stapel_core.observability import logging_config
    LOGGING = logging_config(service="chat")
    MIDDLEWARE = [
        "django.middleware.security.SecurityMiddleware",
        "stapel_core.observability.middleware.TraceContextMiddleware",
        ...,
    ]
    STAPEL_OBSERVABILITY = {
        "METRICS_BACKEND":
            "stapel_core.observability.backends.PrometheusMetricsBackend",
        "ERROR_REPORTER":
            "stapel_core.observability.errors.SentryErrorReporter",
    }

    # anywhere
    from stapel_core.observability import metrics, report_error
    metrics.counter("messages_delivered_total", labels={"kind": "chat"})

Optional dependencies are guarded: ``prometheus_client`` and ``sentry-sdk``
are extras (``stapel-core[prometheus]``, ``stapel-core[sentry]``), and their
absence degrades the corresponding seam to a no-op that says so through a
system check — never an ImportError at request time.

Health and readiness are **not re-implemented here**: they already ship as
``stapel_core.django.monitoring.health`` (``/api/health/``,
``/api/health/ready/``, ``/api/health/live/``, ``/api/metrics/``, plus the
``register_dependency_check`` seam). They are re-exported from this module
so the observability surface is one import, and the facade's metrics are
appended to that existing ``/api/metrics/`` endpoint rather than to a second
one (:mod:`stapel_core.observability.exporter`).
"""
from __future__ import annotations

from . import backends, metrics
from .backends import (
    LoggingMetricsBackend,
    MetricsBackend,
    NoopMetricsBackend,
    PrometheusMetricsBackend,
    StatsdMetricsBackend,
)
from .context import (
    TraceContext,
    bind_trace,
    continue_trace,
    current_trace,
    format_traceparent,
    new_span_id,
    new_trace_id,
    parse_traceparent,
    sanitize_id,
    start_trace,
    trace_ids,
)
from .errors import (
    ErrorReporter,
    LoggingErrorReporter,
    NoopErrorReporter,
    SentryErrorReporter,
    get_error_reporter,
    report_error,
    report_message,
    reset_error_reporter,
    set_error_reporter,
)
from .logs import (
    JsonFormatter,
    TraceContextFilter,
    configure_logging,
    logging_config,
)

# Health/readiness live in stapel_core.django.monitoring.health, whose import
# pulls in the whole stapel_core.django package (drf-spectacular included) and
# needs configured settings. Re-exported lazily so `import
# stapel_core.observability` stays cheap and Django-free — the bus envelope
# imports this package's context module from anywhere.
_LAZY_HEALTH = {
    "health_check",
    "readiness_probe",
    "liveness_probe",
    "prometheus_metrics",
    "get_health_urls",
    "register_dependency_check",
    "register_metrics_exporter",
    "DEP_OK",
    "DEP_ERROR",
    "DEP_UNKNOWN",
}


def __getattr__(name: str):
    if name in _LAZY_HEALTH:
        from ..django.monitoring import health

        return getattr(health, name)
    if name == "observability_settings":
        from .conf import observability_settings

        return observability_settings
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(__all__) | _LAZY_HEALTH)


__all__ = [
    # metrics
    "metrics",
    "backends",
    "MetricsBackend",
    "NoopMetricsBackend",
    "LoggingMetricsBackend",
    "PrometheusMetricsBackend",
    "StatsdMetricsBackend",
    # logging
    "JsonFormatter",
    "TraceContextFilter",
    "configure_logging",
    "logging_config",
    # errors
    "ErrorReporter",
    "NoopErrorReporter",
    "LoggingErrorReporter",
    "SentryErrorReporter",
    "report_error",
    "report_message",
    "get_error_reporter",
    "set_error_reporter",
    "reset_error_reporter",
    # correlation
    "TraceContext",
    "current_trace",
    "trace_ids",
    "start_trace",
    "continue_trace",
    "bind_trace",
    "new_trace_id",
    "new_span_id",
    "sanitize_id",
    "parse_traceparent",
    "format_traceparent",
    # settings namespace (lazy)
    "observability_settings",
    # health/readiness (lazy re-export)
    "health_check",
    "readiness_probe",
    "liveness_probe",
    "prometheus_metrics",
    "get_health_urls",
    "register_dependency_check",
    "register_metrics_exporter",
]
