"""Metrics facade — instrument without importing a vendor.

::

    from stapel_core.observability import metrics

    metrics.counter("chat_messages_total", labels={"kind": "text"})
    metrics.gauge("outbox_backlog", 42)
    with metrics.timer("erasure_seconds", labels={"owner": "recordings"}):
        erase(...)

The same shape as ``analytics.track``: the call site names a measurement, the
deployment names the system it lands in
(``STAPEL_OBSERVABILITY["METRICS_BACKEND"]``). A library that imports
``prometheus_client`` has decided for every host that installs it; a library
that calls this has not.

**Nothing here raises.** A measurement is an observation of the work, not
part of it — an unreachable statsd, a missing client library, a label set
that contradicts an earlier one are logged once and dropped. Instrumentation
that can take a request down is worse than no instrumentation, because it
fails exactly when the system is already unhappy.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from contextlib import contextmanager
from typing import Iterable, Mapping

from .backends import MetricsBackend, NoopMetricsBackend

logger = logging.getLogger(__name__)

__all__ = [
    "counter",
    "gauge",
    "histogram",
    "observe",
    "timer",
    "get_backend",
    "set_backend",
    "reset_backend",
    "metric_name",
]

_lock = threading.Lock()
_backend: MetricsBackend | None = None
_override: MetricsBackend | None = None
_signal_connected = False
_warned: set = set()

# Prometheus/OpenMetrics name grammar. Applied by the facade so a call site
# cannot hand a backend a name it will reject — the sanitized name is stable,
# which matters more than being able to write a colon in a metric name.
_NAME_ILLEGAL = re.compile(r"[^a-zA-Z0-9_]")


def _connect_reset_signal() -> None:
    """Drop the memoized backend when settings change (tests, reconfigure)."""
    global _signal_connected
    if _signal_connected:
        return
    try:
        from django.test.signals import setting_changed
    except Exception:  # pragma: no cover - Django always present in practice
        _signal_connected = True
        return
    setting_changed.connect(_on_setting_changed, weak=False)
    _signal_connected = True


def _on_setting_changed(*, setting=None, **kwargs):
    if setting in (None, "STAPEL_OBSERVABILITY", "METRICS_BACKEND"):
        reset_backend()


def get_backend() -> MetricsBackend:
    """The configured backend, built once per process.

    A backend that cannot be imported or is not a
    :class:`~stapel_core.observability.backends.MetricsBackend` degrades to
    :class:`~stapel_core.observability.backends.NoopMetricsBackend` with a
    single log line — check ``stapel_core.observability.W001`` reports the
    same thing at deploy time, where it can still be fixed.
    """
    global _backend
    if _override is not None:
        return _override
    if _backend is not None:
        return _backend
    with _lock:
        if _backend is None:
            _backend = _build_backend()
            _connect_reset_signal()
    return _backend


def _build_backend() -> MetricsBackend:
    try:
        from .conf import observability_settings

        value = observability_settings.METRICS_BACKEND
    except Exception as exc:
        logger.warning(
            "stapel_core.observability: METRICS_BACKEND could not be resolved "
            "(%s); metrics are recorded nowhere.",
            exc,
        )
        return NoopMetricsBackend()

    if isinstance(value, MetricsBackend):
        return value
    if isinstance(value, type):
        try:
            instance = value()
        except Exception as exc:
            logger.warning(
                "stapel_core.observability: METRICS_BACKEND %r could not be "
                "instantiated (%s); metrics are recorded nowhere.",
                value,
                exc,
            )
            return NoopMetricsBackend()
        if isinstance(instance, MetricsBackend):
            return instance
        logger.warning(
            "stapel_core.observability: METRICS_BACKEND %r is not a "
            "MetricsBackend; metrics are recorded nowhere.",
            value,
        )
        return NoopMetricsBackend()
    logger.warning(
        "stapel_core.observability: METRICS_BACKEND %r is neither a class nor "
        "a MetricsBackend instance; metrics are recorded nowhere.",
        value,
    )
    return NoopMetricsBackend()


def set_backend(backend: MetricsBackend | None) -> None:
    """Pin *backend* for this process, ignoring settings. ``None`` unpins.

    For tests and for a host that builds its backend itself (a pre-configured
    registry, a shared client). Not the normal way in — the normal way is
    ``STAPEL_OBSERVABILITY["METRICS_BACKEND"]``.
    """
    global _override
    _override = backend


def reset_backend() -> None:
    """Forget the memoized backend; the next call rebuilds it from settings."""
    global _backend
    with _lock:
        _backend = None


def metric_name(name: str) -> str:
    """Namespace-prefix and sanitize *name*.

    Idempotent with respect to the prefix: a name that already starts with
    the configured namespace is not prefixed twice, so a module may spell
    the full name out.
    """
    try:
        from .conf import observability_settings

        namespace = observability_settings.METRIC_NAMESPACE or ""
    except Exception:
        namespace = ""
    clean = _NAME_ILLEGAL.sub("_", str(name)).strip("_") or "unnamed"
    prefix = _NAME_ILLEGAL.sub("_", namespace)
    if prefix and not clean.startswith(prefix):
        clean = f"{prefix}{clean}"
    return clean


def _default_buckets() -> Iterable[float] | None:
    try:
        from .conf import observability_settings

        return observability_settings.HISTOGRAM_BUCKETS or None
    except Exception:
        return None


def _guard(kind: str, name: str, exc: Exception) -> None:
    key = (kind, name)
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(
        "stapel_core.observability: recording %s %r failed (%s); further "
        "failures for it are silent.",
        kind,
        name,
        exc,
    )


def counter(
    name: str,
    value: float = 1.0,
    labels: Mapping | None = None,
    *,
    description: str = "",
) -> None:
    """Add *value* (default 1) to the counter *name*."""
    try:
        get_backend().counter(
            metric_name(name), value, labels, description=description
        )
    except Exception as exc:
        _guard("counter", name, exc)


def gauge(
    name: str,
    value: float,
    labels: Mapping | None = None,
    *,
    description: str = "",
) -> None:
    """Set the gauge *name* to *value*."""
    try:
        get_backend().gauge(
            metric_name(name), value, labels, description=description
        )
    except Exception as exc:
        _guard("gauge", name, exc)


def histogram(
    name: str,
    value: float,
    labels: Mapping | None = None,
    *,
    description: str = "",
    buckets: Iterable[float] | None = None,
) -> None:
    """Record one observation of *value* in the histogram *name*.

    Durations are **seconds** (the Prometheus convention); backends that
    want other units convert at their own boundary.
    """
    try:
        get_backend().histogram(
            metric_name(name),
            value,
            labels,
            description=description,
            buckets=buckets or _default_buckets(),
        )
    except Exception as exc:
        _guard("histogram", name, exc)


#: Alias — reads better at a call site that is recording a size, not a time.
observe = histogram


@contextmanager
def timer(
    name: str,
    labels: Mapping | None = None,
    *,
    description: str = "",
    buckets: Iterable[float] | None = None,
):
    """Time the block and record it as a histogram observation, in seconds.

    The measurement is taken in a ``finally``, so a block that raises is
    still measured — the latency of failures is usually the interesting half.
    """
    started = time.perf_counter()
    try:
        yield
    finally:
        histogram(
            name,
            time.perf_counter() - started,
            labels,
            description=description,
            buckets=buckets,
        )
