"""System checks for the observability seams.

W-level throughout, and deliberately so (library-standard §3.7): every one
of these degrades to "records nothing" at runtime rather than failing a
request, so none of them may block a deploy. What they must not do is stay
quiet — a metrics backend that silently discards everything and a metrics
backend that works look identical from inside the process, and the whole
point of the facade is that a module's instrumentation is real.

W001 ``METRICS_BACKEND`` cannot be built (bad path / not a MetricsBackend).
W002 The backend built, but reports itself unavailable — the shape of
     "PrometheusMetricsBackend without prometheus_client installed", i.e.
     every measurement in the fleet goes nowhere and nothing says so.
W003 ``ERROR_REPORTER`` cannot be built, or is the no-op default while
     ``SENTRY_DSN`` is configured — the deployment clearly meant to report
     errors somewhere.
W004 Observability is configured, but ``TraceContextMiddleware`` is in no
     MIDDLEWARE — ``trace_id`` is then empty on every request-scoped log line
     and every event the request emits, which is the one field the whole
     correlation story rests on.

**All four are gated on evidence of intent** — a ``STAPEL_OBSERVABILITY``
block in the settings module (or a flat setting from the namespace). A
service that never adopted the facade is not told that a backend it never
asked for is not installed; the same rule ``stapel_core.netintel.W003``
follows. Adopting the facade is what turns these on.
"""
from django.core import checks

W001_METRICS_BACKEND_BROKEN = "stapel_core.observability.W001"
W002_METRICS_UNAVAILABLE = "stapel_core.observability.W002"
W003_ERROR_REPORTER = "stapel_core.observability.W003"
W004_NO_TRACE_MIDDLEWARE = "stapel_core.observability.W004"

_MIDDLEWARE_PATH = (
    "stapel_core.observability.middleware.TraceContextMiddleware"
)


def _adopted() -> bool:
    """Did this deployment configure observability at all?

    True when the settings module carries a ``STAPEL_OBSERVABILITY`` dict, or
    a flat setting named after one of the namespace's keys. Nothing here is
    worth saying to a service that never adopted the facade — its metrics
    calls do not exist either.
    """
    from django.conf import settings

    from .conf import observability_settings

    if getattr(settings, "STAPEL_OBSERVABILITY", None):
        return True
    return any(
        hasattr(settings, key) for key in observability_settings.defaults
    )


@checks.register("stapel_observability")
def check_metrics_backend(app_configs=None, **kwargs):
    from .backends import NoopMetricsBackend
    from .conf import observability_settings
    from .metrics import get_backend

    if not _adopted():
        return []

    errors = []
    declared = None
    try:
        declared = observability_settings.METRICS_BACKEND
    except Exception as exc:
        return [checks.Warning(
            f"STAPEL_OBSERVABILITY['METRICS_BACKEND'] cannot be resolved: "
            f"{exc}. Every metrics call is a no-op.",
            hint="Name a stapel_core.observability.backends.MetricsBackend "
                 "subclass by dotted path.",
            id=W001_METRICS_BACKEND_BROKEN,
        )]

    if _is_noop_declared(declared):
        # Chosen on purpose. "This deployment records no metrics" is a
        # decision, and a check that argues with a decision is noise.
        return []

    backend = get_backend()
    if isinstance(backend, NoopMetricsBackend):
        errors.append(checks.Warning(
            f"STAPEL_OBSERVABILITY['METRICS_BACKEND'] ({declared!r}) could not "
            "be built and fell back to NoopMetricsBackend. Every metrics call "
            "in this process records nothing.",
            hint="Check the dotted path and that the class subclasses "
                 "stapel_core.observability.backends.MetricsBackend.",
            id=W001_METRICS_BACKEND_BROKEN,
        ))
    elif not getattr(backend, "available", True):
        errors.append(checks.Warning(
            f"The metrics backend {type(backend).__name__} reports itself "
            "unavailable — measurements are recorded nowhere.",
            hint="For the default Prometheus backend this means "
                 "prometheus_client is not installed: pip install "
                 "'stapel-core[prometheus]'. To choose no metrics on purpose, "
                 "set METRICS_BACKEND to "
                 "'stapel_core.observability.backends.NoopMetricsBackend'.",
            id=W002_METRICS_UNAVAILABLE,
        ))
    return errors


def _is_noop_declared(declared) -> bool:
    from .backends import NoopMetricsBackend

    if isinstance(declared, type):
        return issubclass(declared, NoopMetricsBackend)
    return isinstance(declared, NoopMetricsBackend)


def _is_noop_declared_reporter(declared) -> bool:
    from .errors import NoopErrorReporter

    if isinstance(declared, type):
        return issubclass(declared, NoopErrorReporter)
    return isinstance(declared, NoopErrorReporter)


@checks.register("stapel_observability")
def check_error_reporter(app_configs=None, **kwargs):
    import os

    from django.conf import settings

    from .conf import observability_settings
    from .errors import NoopErrorReporter, get_error_reporter

    if not _adopted():
        return []

    # Reading the setting is itself the first thing that can fail: an
    # import_strings key resolves its dotted path on access, so a typo raises
    # HERE rather than at the reporter.
    try:
        declared = observability_settings.ERROR_REPORTER
    except Exception as exc:
        return [checks.Warning(
            f"STAPEL_OBSERVABILITY['ERROR_REPORTER'] cannot be resolved: {exc}. "
            "Every report_error() in this process discards its exception.",
            hint="Name a stapel_core.observability.errors.ErrorReporter "
                 "subclass by dotted path.",
            id=W003_ERROR_REPORTER,
        )]

    reporter = get_error_reporter()

    if isinstance(reporter, NoopErrorReporter) and not _is_noop_declared_reporter(
        declared
    ):
        return [checks.Warning(
            f"STAPEL_OBSERVABILITY['ERROR_REPORTER'] ({declared!r}) could not "
            "be built and fell back to NoopErrorReporter. Every report_error() "
            "in this process discards its exception.",
            hint="Check the dotted path and that the class subclasses "
                 "stapel_core.observability.errors.ErrorReporter.",
            id=W003_ERROR_REPORTER,
        )]

    if not isinstance(reporter, NoopErrorReporter) and not getattr(
        reporter, "active", True
    ):
        return [checks.Warning(
            f"The error reporter {type(reporter).__name__} reports itself "
            "inactive — exceptions passed to report_error() go nowhere.",
            hint="For SentryErrorReporter this means sentry-sdk is not "
                 "installed: pip install 'stapel-core[sentry]'.",
            id=W003_ERROR_REPORTER,
        )]

    dsn = os.environ.get("SENTRY_DSN") or getattr(settings, "SENTRY_DSN", "")
    if dsn and isinstance(reporter, NoopErrorReporter):
        return [checks.Warning(
            "SENTRY_DSN is configured but "
            "STAPEL_OBSERVABILITY['ERROR_REPORTER'] is still the no-op "
            "default, so report_error() discards everything.",
            hint="Set ERROR_REPORTER to "
                 "'stapel_core.observability.errors.SentryErrorReporter'.",
            id=W003_ERROR_REPORTER,
        )]
    return []


@checks.register("stapel_observability")
def check_trace_middleware(app_configs=None, **kwargs):
    from django.conf import settings

    if not _adopted():
        return []

    middleware = list(getattr(settings, "MIDDLEWARE", None) or ())
    if not middleware or _MIDDLEWARE_PATH in middleware:
        return []
    return [checks.Warning(
        "TraceContextMiddleware is not in MIDDLEWARE, so no request starts a "
        "trace: trace_id/request_id are empty on request-scoped log records "
        "and on every comm envelope those requests emit.",
        hint=f"Add '{_MIDDLEWARE_PATH}' to MIDDLEWARE, as early as possible.",
        id=W004_NO_TRACE_MIDDLEWARE,
    )]
