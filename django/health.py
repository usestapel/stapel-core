"""
Health check and metrics endpoints for monitoring.

Provides:
- /api/health/ - Basic health check with metrics for Prometheus
- /api/health/ready/ - Readiness probe (checks DB connection)
- /api/health/live/ - Liveness probe (always returns OK)
"""
import logging
import time
from django.http import JsonResponse, HttpResponse
from django.db import connection
from django.conf import settings
from django.urls import path

logger = logging.getLogger(__name__)


# Track service start time for uptime calculation
_start_time = time.time()

# Custom metrics exporters: list of callables returning Prometheus text fragments
_custom_metrics_exporters = []


def register_metrics_exporter(exporter):
    """
    Register a callable that returns a Prometheus-formatted string.

    Usage:
        from stapel_core.django.health import register_metrics_exporter
        register_metrics_exporter(my_export_func)
    """
    _custom_metrics_exporters.append(exporter)


def health_check(request):
    """
    Health check endpoint with basic metrics.

    Returns JSON with service status and metrics.
    Can be scraped by Prometheus (use metrics endpoint for proper format).
    """
    uptime = time.time() - _start_time

    # Check database connection
    db_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        db_ok = False

    service_name = getattr(settings, 'SERVICE_NAME', 'unknown')
    version = getattr(settings, 'APP_VERSION_NUMBER', 'unknown')

    status = 'healthy' if db_ok else 'degraded'

    return JsonResponse({
        'status': status,
        'service': service_name,
        'version': version,
        'uptime_seconds': round(uptime, 2),
        'checks': {
            'database': 'ok' if db_ok else 'error'
        }
    }, status=200 if db_ok else 503)


def readiness_probe(request):
    """
    Kubernetes readiness probe.

    Returns 200 if service is ready to accept traffic.
    Checks database connectivity.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return HttpResponse("OK", status=200)
    except Exception as e:
        return HttpResponse(f"Not Ready: {e}", status=503)


def liveness_probe(request):
    """
    Kubernetes liveness probe.

    Returns 200 if service is alive.
    Does not check dependencies (that's what readiness is for).
    """
    return HttpResponse("OK", status=200)


def prometheus_metrics(request):
    """
    Prometheus metrics endpoint.

    Returns metrics in Prometheus text format.
    """
    uptime = time.time() - _start_time
    service_name = getattr(settings, 'SERVICE_NAME', 'unknown').lower().replace(' ', '_')
    version = getattr(settings, 'APP_VERSION_NUMBER', 'unknown')

    # Check database
    db_ok = 1
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        db_ok = 0

    metrics = []

    # Service info
    metrics.append('# HELP iron_service_info Service information')
    metrics.append('# TYPE iron_service_info gauge')
    metrics.append(f'iron_service_info{{service="{service_name}",version="{version}"}} 1')

    # Uptime
    metrics.append('# HELP iron_uptime_seconds Service uptime in seconds')
    metrics.append('# TYPE iron_uptime_seconds gauge')
    metrics.append(f'iron_uptime_seconds{{service="{service_name}"}} {uptime:.2f}')

    # Database health
    metrics.append('# HELP iron_database_up Database connection status')
    metrics.append('# TYPE iron_database_up gauge')
    metrics.append(f'iron_database_up{{service="{service_name}"}} {db_ok}')

    # Service up
    metrics.append('# HELP iron_up Service is up')
    metrics.append('# TYPE iron_up gauge')
    metrics.append(f'iron_up{{service="{service_name}"}} 1')

    # Append custom metrics from registered exporters
    for exporter in _custom_metrics_exporters:
        try:
            extra = exporter()
            if extra:
                metrics.append(extra)
        except Exception:
            logger.exception("Metrics exporter %s failed", exporter)

    return HttpResponse(
        '\n'.join(metrics) + '\n',
        content_type='text/plain; version=0.0.4; charset=utf-8'
    )


def get_health_urls(prefix: str = ''):
    """
    Get URL patterns for health endpoints.

    Usage in urls.py:
        from stapel_core.django.health import get_health_urls

        urlpatterns = [
            ...
            *get_health_urls('myservice/'),
        ]

    This adds:
        - /{prefix}api/health/
        - /{prefix}api/health/ready/
        - /{prefix}api/health/live/
        - /{prefix}api/metrics/
    """
    return [
        path(f'{prefix}api/health/', health_check, name='health-check'),
        path(f'{prefix}api/health/ready/', readiness_probe, name='readiness-probe'),
        path(f'{prefix}api/health/live/', liveness_probe, name='liveness-probe'),
        path(f'{prefix}api/metrics/', prometheus_metrics, name='prometheus-metrics'),
    ]
