"""Settings namespace for the observability facade (``STAPEL_OBSERVABILITY``)."""
from stapel_core.conf import AppSettings

observability_settings = AppSettings(
    "STAPEL_OBSERVABILITY",
    defaults={
        # ── identity ────────────────────────────────────────────────────
        # Name stamped on every log line, metric and error report. None =
        # fall back to stapel_core.comm.config.service_name() (the same name
        # the comm envelope carries), so the two never disagree.
        "SERVICE_NAME": None,
        # ── structured logging ──────────────────────────────────────────
        # "json" (default) or "text". JSON is the point: a text log is
        # grepped, a structured log is queried.
        "LOG_FORMAT": "json",
        "LOG_LEVEL": "INFO",
        # Loggers configure_logging() leaves alone entirely (their own
        # handlers/propagation stay as the host set them).
        "LOG_EXEMPT_LOGGERS": [],
        # Extra static fields merged into every record (deployment, region…).
        "LOG_STATIC_FIELDS": {},
        # Record attributes rendered alongside the mandatory field set.
        "LOG_INCLUDE_SOURCE": False,
        # ── metrics ─────────────────────────────────────────────────────
        # Replace seam. The default is the Prometheus backend, which itself
        # degrades to a no-op (once-logged, plus check W002) when
        # prometheus_client is not installed — so `pip install
        # 'stapel-core[prometheus]'` is the whole of "turn metrics on", and
        # a host without it pays nothing and breaks nothing.
        "METRICS_BACKEND": (
            "stapel_core.observability.backends.PrometheusMetricsBackend"
        ),
        # Prefix on every metric name. Matches the STAPEL_METRICS_PREFIX
        # default of the /api/metrics/ endpoint so both halves of a
        # deployment's Prometheus surface share one namespace.
        "METRIC_NAMESPACE": "stapel_",
        # Default histogram buckets (seconds), tuned for HTTP/handler
        # latency. Per-call `buckets=` wins.
        "HISTOGRAM_BUCKETS": [
            0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
        ],
        # StatsdMetricsBackend target.
        "STATSD_HOST": "127.0.0.1",
        "STATSD_PORT": 8125,
        # ── error reporting ─────────────────────────────────────────────
        # Replace seam, Sentry-shaped interface. Default reports nowhere:
        # the framework must not decide that a host ships its exceptions to
        # a third party. Point it at SentryErrorReporter (or your own) to
        # turn it on.
        "ERROR_REPORTER": (
            "stapel_core.observability.errors.NoopErrorReporter"
        ),
        # ── request correlation ─────────────────────────────────────────
        # Header names TraceContextMiddleware reads and echoes.
        "REQUEST_ID_HEADER": "X-Request-ID",
        "TRACE_ID_HEADER": "X-Trace-Id",
        "CORRELATION_ID_HEADER": "X-Correlation-Id",
        # Accept trace ids presented by the caller. True is right behind a
        # trusted edge (a gateway/mesh that sets traceparent) and for
        # service-to-service calls — that is what makes one trace span
        # services. Incoming ids are always sanitized (length + alphabet),
        # so the exposure is a chosen id, never an injected log field.
        # Turn it off at an internet-facing edge that wants ids it minted.
        "TRUST_INCOMING_TRACE": True,
        # Stamp the trace/request ids on the response so a client (and an
        # access log) can quote the id of the operation it just triggered.
        "ECHO_TRACE_HEADERS": True,
        # TraceContextMiddleware records request count + duration.
        "REQUEST_METRICS": True,
        # ── error-reporting scope ───────────────────────────────────────
        # Log-record fields whose value is replaced with "***" before it
        # reaches a handler. A structured log takes whatever `extra=` hands
        # it; these names are the ones that must never be taken.
        "REDACT_FIELDS": [
            "password", "passwd", "secret", "token", "access_token",
            "refresh_token", "api_key", "apikey", "authorization",
            "cookie", "session", "private_key", "client_secret", "otp",
        ],
    },
    # Both are dotted paths naming the CLASS the process imports and runs,
    # so they are implicitly env-closed by the import_strings rule.
    import_strings=("METRICS_BACKEND", "ERROR_REPORTER"),
    # Header names carry trust weight: a stray same-named env var must not
    # change which header this process believes about a request's identity.
    no_env=(
        "REQUEST_ID_HEADER",
        "TRACE_ID_HEADER",
        "CORRELATION_ID_HEADER",
        "TRUST_INCOMING_TRACE",
    ),
)

__all__ = ["observability_settings"]
