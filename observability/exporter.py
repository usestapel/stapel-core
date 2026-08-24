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

__all__ = ["register_prometheus_exporter", "facade_exposition"]


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
