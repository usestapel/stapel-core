"""Join the metrics facade to the scrape endpoint the fleet already serves.

``stapel_core.django.monitoring.health.prometheus_metrics`` has always
exposed ``/api/metrics/`` (uptime, database reachability, registered
dependency probes) and a ``register_metrics_exporter`` seam for anything
else that wants to appear there.

So facade metrics go there too, rather than to a second endpoint on a second
port that every deployment's scrape config would have to learn about.
Registered from ``stapel_core.django``'s ``AppConfig.ready()``: a service
that records a counter through the facade has it on its metrics URL with no
wiring of its own.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_registered = False

__all__ = ["register_prometheus_exporter", "facade_exposition", "serve_metrics"]


def facade_exposition() -> str:
    """The configured backend's exposition text (``""`` if it has none)."""
    from . import metrics

    try:
        return metrics.get_backend().expose() or ""
    except Exception:
        logger.warning(
            "stapel_core.observability: metrics exposition failed", exc_info=True
        )
        return ""


def register_prometheus_exporter() -> bool:
    """Add :func:`facade_exposition` to the ``/api/metrics/`` exporters.

    Idempotent — ``ready()`` can run more than once in a test process.
    Returns True when the registration happened here.
    """
    global _registered
    if _registered:
        return False
    from ..django.monitoring.health import register_metrics_exporter

    register_metrics_exporter(facade_exposition)
    _registered = True
    return True


_server = None


def serve_metrics(port: int | None = None, addr: str | None = None) -> bool:
    """Expose the facade's metrics from a process that serves no HTTP.

    A web process gets ``/api/metrics/`` for free. A consumer, an outbox
    worker or the function server does not — and those are exactly the
    processes that record the numbers worth alarming on. Until this existed
    the fleet could record ``bus_dlq_total`` in a consumer and no scrape could
    ever reach it: the counter incremented into a process nothing was pointed
    at, which is monitoring that cannot report, and that is indistinguishable
    from healthy.

    Off unless ``STAPEL_OBSERVABILITY["EXPORTER_PORT"]`` is set — a worker
    that starts listening on a port nobody asked for is a surprise, and in
    some deployments a security finding.

    Never raises. A worker must not fail to start because its metrics port is
    taken; it logs and carries on doing the job it exists for.

    Returns True when a listener is now running.
    """
    global _server
    if _server is not None:
        return True

    from .conf import observability_settings

    port = port if port is not None else observability_settings.EXPORTER_PORT
    # `is None`, not falsiness: port 0 is a real value meaning "any free port"
    # (the shape a test or an ephemeral sidecar wants), and treating it as
    # "off" would be a listener that silently never starts.
    if port is None:
        return False
    addr = addr if addr is not None else observability_settings.EXPORTER_ADDR

    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib's spelling
            body = facade_exposition().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            """Silence per-scrape access lines.

            A scrape every 15 seconds forever would bury the log this process
            exists to produce.
            """

    try:
        _server = ThreadingHTTPServer((addr, int(port)), _Handler)
    except OSError:
        logger.error(
            "stapel_core.observability: metrics listener could not bind %s:%s — "
            "this worker's metrics will not be scrapable, but it is starting anyway",
            addr, port, exc_info=True,
        )
        _server = None
        return False

    threading.Thread(
        target=_server.serve_forever, name="stapel-metrics-exporter", daemon=True
    ).start()
    logger.info(
        "stapel_core.observability: metrics listener on %s:%s", addr, port
    )
    return True
