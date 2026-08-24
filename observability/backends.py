"""Metrics backends — the vendor half of the metrics facade.

Modules instrument themselves against :mod:`stapel_core.observability.metrics`
and never import ``prometheus_client``; which time-series system the numbers
land in is a deployment's answer, given once as
``STAPEL_OBSERVABILITY["METRICS_BACKEND"]``.

The contract every backend keeps, and the reason it is worth stating: **a
metric never breaks the thing it measures.** An instrument call is not
business logic — it is an observation of it — so a backend that raises, a
label set that contradicts an earlier one, an unreachable statsd socket, a
missing client library: all of them are absorbed here and reported once, to a
log, and never to the caller. The facade in :mod:`.metrics` holds the same
line at its own level.
"""
from __future__ import annotations

import logging
import socket
import threading
from typing import Iterable, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "MetricsBackend",
    "NoopMetricsBackend",
    "LoggingMetricsBackend",
    "PrometheusMetricsBackend",
    "StatsdMetricsBackend",
]


def _label_items(labels: Mapping | None) -> tuple:
    """Labels as a stable, hashable, string-valued tuple of pairs."""
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


class MetricsBackend:
    """The interface :mod:`stapel_core.observability.metrics` speaks.

    Subclass (or duck-type) and name the class in
    ``STAPEL_OBSERVABILITY["METRICS_BACKEND"]``. Instantiated once per
    process, lazily, and re-instantiated on ``setting_changed``.

    Every method takes the *final* metric name (the facade has already
    applied the namespace prefix) and must not raise.
    """

    #: Whether this backend can currently record anything. A backend that
    #: answers False is reported by check W002 instead of silently eating
    #: every measurement.
    available = True

    def counter(
        self,
        name: str,
        value: float = 1.0,
        labels: Mapping | None = None,
        *,
        description: str = "",
    ) -> None:
        """Add *value* to a monotonically increasing series."""

    def gauge(
        self,
        name: str,
        value: float,
        labels: Mapping | None = None,
        *,
        description: str = "",
    ) -> None:
        """Set a series to *value* (a level: queue depth, pool size)."""

    def histogram(
        self,
        name: str,
        value: float,
        labels: Mapping | None = None,
        *,
        description: str = "",
        buckets: Iterable[float] | None = None,
    ) -> None:
        """Record one observation of a distribution (a latency, a size)."""

    def expose(self) -> str:
        """Exposition text for a scrape endpoint, or ``""`` if not applicable.

        Push-style backends (statsd) and no-op backends have nothing to
        expose; ``/api/metrics/`` simply appends nothing for them.
        """
        return ""


class NoopMetricsBackend(MetricsBackend):
    """Records nothing. The honest answer for a host with no metrics stack."""

    available = False


class LoggingMetricsBackend(MetricsBackend):
    """Emits each measurement as a structured DEBUG log record.

    Useful in development and in tests: it proves the instrumentation is
    reached without requiring a time-series database, and under the JSON
    formatter the measurement is queryable like any other event.
    """

    def __init__(self, logger_name: str = "stapel.metrics") -> None:
        self._logger = logging.getLogger(logger_name)

    def _emit(self, kind, name, value, labels):
        self._logger.debug(
            "metric %s %s=%s",
            kind,
            name,
            value,
            extra={
                "metric_kind": kind,
                "metric_name": name,
                "metric_value": value,
                "metric_labels": dict(labels or {}),
            },
        )

    def counter(self, name, value=1.0, labels=None, *, description=""):
        self._emit("counter", name, value, labels)

    def gauge(self, name, value, labels=None, *, description=""):
        self._emit("gauge", name, value, labels)

    def histogram(self, name, value, labels=None, *, description="", buckets=None):
        self._emit("histogram", name, value, labels)


class PrometheusMetricsBackend(MetricsBackend):
    """The default backend: ``prometheus_client`` collectors, created on demand.

    Prometheus wants each metric declared once, with a fixed label-name set,
    before it is used. Instrumentation does not work that way — a module
    calls ``counter("x", labels={...})`` wherever the event happens — so the
    collector is created on the first call for a given ``(name, label
    names)`` and cached.

    Two failure modes that would otherwise be nasty, absorbed here:

    * **``prometheus_client`` not installed.** The class still constructs and
      reports :attr:`available` ``False`` — it is the default backend, and a
      default that raises on import would make an unremarkable HTTP service
      fail to boot for a dependency it never asked for. Check W002 says so
      at ``manage.py check`` time; :func:`metrics.counter` and friends
      silently do nothing.
    * **The same metric name used with two different label sets.** Prometheus
      raises on the second registration; the facade logs it once and drops
      the measurement, because a mislabeled counter is not a reason for a
      request to fail.
    """

    def __init__(self, registry=None) -> None:
        self._lock = threading.Lock()
        self._collectors: dict = {}
        self._warned: set = set()
        self._registry = registry
        self._client = None
        try:
            import prometheus_client  # noqa: F401
        except ImportError:
            logger.info(
                "stapel_core.observability: prometheus_client is not installed, "
                "metrics are recorded nowhere. Install it with "
                "pip install 'stapel-core[prometheus]', or set "
                "STAPEL_OBSERVABILITY['METRICS_BACKEND'] to a backend you have."
            )
        else:
            self._client = prometheus_client
            if self._registry is None:
                self._registry = prometheus_client.REGISTRY

    @property
    def available(self) -> bool:  # type: ignore[override]
        return self._client is not None

    def _collector(self, kind, name, label_names, description, buckets):
        key = (kind, name, label_names)
        with self._lock:
            existing = self._collectors.get(key)
            if existing is not None:
                return existing
            client = self._client
            kwargs = {"registry": self._registry}
            try:
                if kind == "counter":
                    collector = client.Counter(
                        name, description or name, label_names, **kwargs
                    )
                elif kind == "gauge":
                    collector = client.Gauge(
                        name, description or name, label_names, **kwargs
                    )
                else:
                    if buckets:
                        kwargs["buckets"] = tuple(buckets)
                    collector = client.Histogram(
                        name, description or name, label_names, **kwargs
                    )
            except Exception as exc:
                # Duplicate registration under a different label set, an
                # illegal metric name, a registry conflict — a measurement
                # problem, never the caller's problem.
                self._warn_once(key, name, exc)
                self._collectors[key] = False
                return False
            self._collectors[key] = collector
            return collector

    def _warn_once(self, key, name, exc):
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning(
            "stapel_core.observability: metric %r could not be registered "
            "(%s); measurements for it are dropped.",
            name,
            exc,
        )

    def _record(self, kind, name, value, labels, description, buckets=None):
        if self._client is None:
            return
        items = _label_items(labels)
        label_names = tuple(k for k, _ in items)
        collector = self._collector(kind, name, label_names, description, buckets)
        if collector is False:
            return
        try:
            target = (
                collector.labels(*[v for _, v in items]) if items else collector
            )
            if kind == "counter":
                target.inc(value)
            elif kind == "gauge":
                target.set(value)
            else:
                target.observe(value)
        except Exception as exc:
            self._warn_once(("record", kind, name), name, exc)

    def counter(self, name, value=1.0, labels=None, *, description=""):
        self._record("counter", name, value, labels, description)

    def gauge(self, name, value, labels=None, *, description=""):
        self._record("gauge", name, value, labels, description)

    def histogram(self, name, value, labels=None, *, description="", buckets=None):
        self._record("histogram", name, value, labels, description, buckets)

    def expose(self) -> str:
        """Prometheus text exposition of this backend's registry.

        Wired into the existing ``/api/metrics/`` endpoint by
        :func:`stapel_core.observability.exporter.register_prometheus_exporter`,
        so facade metrics appear on the scrape URL a Stapel service already
        serves — no second endpoint, no host wiring.
        """
        if self._client is None or self._registry is None:
            return ""
        try:
            return self._client.generate_latest(self._registry).decode("utf-8")
        except Exception:
            logger.warning(
                "stapel_core.observability: Prometheus exposition failed",
                exc_info=True,
            )
            return ""


class StatsdMetricsBackend(MetricsBackend):
    """Fire-and-forget statsd over UDP (DogStatsD label syntax).

    Second real backend, and the reason the seam is a seam rather than a
    Prometheus wrapper with extra steps: the facade's call sites do not
    change when a deployment already runs a statsd/Datadog agent.

    UDP by design — a metrics send must never block a request, and a dropped
    datagram must never be an error.
    """

    def __init__(self, host: str | None = None, port: int | None = None) -> None:
        from .conf import observability_settings as s

        self._addr = (
            host or s.STATSD_HOST,
            int(port or s.STATSD_PORT),
        )
        self._socket = None
        self._lock = threading.Lock()
        self._warned = False

    def _send(self, line: str) -> None:
        try:
            with self._lock:
                if self._socket is None:
                    self._socket = socket.socket(
                        socket.AF_INET, socket.SOCK_DGRAM
                    )
            self._socket.sendto(line.encode("utf-8"), self._addr)
        except Exception as exc:
            if not self._warned:
                self._warned = True
                logger.warning(
                    "stapel_core.observability: statsd send to %s failed (%s); "
                    "measurements are dropped.",
                    self._addr,
                    exc,
                )

    @staticmethod
    def _suffix(labels) -> str:
        items = _label_items(labels)
        if not items:
            return ""
        return "|#" + ",".join(f"{k}:{v}" for k, v in items)

    def counter(self, name, value=1.0, labels=None, *, description=""):
        self._send(f"{name}:{value}|c{self._suffix(labels)}")

    def gauge(self, name, value, labels=None, *, description=""):
        self._send(f"{name}:{value}|g{self._suffix(labels)}")

    def histogram(self, name, value, labels=None, *, description="", buckets=None):
        # statsd timers are milliseconds; the facade's histograms are
        # seconds (Prometheus convention), so convert at the boundary
        # instead of asking every call site to know which backend is on.
        self._send(f"{name}:{value * 1000.0}|ms{self._suffix(labels)}")
