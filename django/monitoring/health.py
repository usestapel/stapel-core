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

# Dependency checks: list of (name, probe, critical) — see
# register_dependency_check() below.
_dependency_checks = []


def register_metrics_exporter(exporter):
    """
    Register a callable that returns a Prometheus-formatted string.

    Usage:
        from stapel_core.django.monitoring.health import register_metrics_exporter
        register_metrics_exporter(my_export_func)
    """
    _custom_metrics_exporters.append(exporter)


def register_dependency_check(name, probe, *, critical=False):
    """
    Register an outbound-dependency probe surfaced on ``/api/health/`` and
    ``/api/metrics/`` (docs/pending/env-address-class-v2.md §3.6).

    Motivating incident: meettoday's host-kick and room-PIN endpoints wrapped
    every LiveKit twirp call in ``try/except`` + ``logger.warning`` (best
    effort, so an unreachable LiveKit never breaks the caller) — and then
    silently did nothing in production for as long as LiveKit was
    unreachable, with no signal anywhere an operator would look. A
    best-effort wrapper around a network call is fine; a best-effort wrapper
    with NO registered check on this endpoint is the same defect that took a
    day to notice. Canon (also stated in MODULE.md's Anti-patterns): a
    ``try/except`` around a network call is only correct paired with (a)
    ``logger.error`` (not ``.warning`` — a warning that fires for days in
    production is invisible by construction) and (b) a
    ``register_dependency_check`` on this same probe.

    ``probe`` is a zero-argument callable returning ``True`` (dependency
    reachable) or ``False`` (unreachable) — never raising; a probe that
    raises is treated as unreachable and logged, never allowed to break the
    health endpoint itself (the same "a broken exporter must not break
    ``/api/metrics/``" posture ``register_metrics_exporter`` already has).
    Any timeout is the probe's own responsibility.

    ``critical=True`` means this dependency is load-bearing for the WHOLE
    process's readiness: its failure flips ``/api/health/`` and
    ``readiness_probe()`` to HTTP 503 (an orchestrator should stop routing
    traffic here). ``critical=False`` (default) — the process stays healthy
    (200) but the failure is named in ``checks{}`` and in
    ``stapel_dependency_up`` so monitoring sees it without an LB pulling a
    backend that serves everything else fine. This is deliberately NOT a
    Django boot-time check: a dependency like LiveKit can go down long after
    boot, and refusing to serve the rest of the app because one downstream
    dependency is unreachable would widen the blast radius from one feature
    to the whole product (docs/pending/env-address-class-v2.md §2 — the same
    "fail-closed on environment errors is the wrong answer" argument the
    nginx upstream gate was rebuilt around).

    Usage:
        from stapel_core.django.monitoring.health import register_dependency_check

        def _livekit_reachable():
            try:
                requests.post(f"{base}/twirp/livekit.RoomService/ListRooms",
                               json={"names": []}, headers=..., timeout=3)
                return True
            except requests.RequestException:
                return False

        register_dependency_check("livekit", _livekit_reachable, critical=False)
    """
    _dependency_checks.append((name, probe, critical))


def _run_dependency_checks():
    """(checks dict, any_critical_down) — never raises; a probe exception is
    treated as a failed/unreachable dependency, same posture as a probe that
    plainly returns False."""
    checks = {}
    critical_down = False
    for name, probe, critical in _dependency_checks:
        try:
            ok = bool(probe())
        except Exception:
            logger.exception("Dependency check %s failed", name)
            ok = False
        checks[name] = {"ok": ok, "critical": critical}
        if not ok and critical:
            critical_down = True
    return checks, critical_down


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

    dep_checks, critical_down = _run_dependency_checks()
    non_critical_down = any(not c['ok'] and not c['critical'] for c in dep_checks.values())

    healthy = db_ok and not critical_down
    status = 'healthy' if (healthy and not non_critical_down) else 'degraded'

    checks = {'database': 'ok' if db_ok else 'error'}
    for name, c in dep_checks.items():
        checks[name] = 'ok' if c['ok'] else 'error'

    return JsonResponse({
        'status': status,
        'service': service_name,
        'version': version,
        'uptime_seconds': round(uptime, 2),
        'checks': checks,
    }, status=200 if healthy else 503)


def readiness_probe(request):
    """
    Kubernetes readiness probe.

    Returns 200 if service is ready to accept traffic. Checks database
    connectivity and every dependency registered with
    ``critical=True`` via ``register_dependency_check`` — a critical
    dependency down means this process cannot serve its purpose, so an
    orchestrator should stop routing traffic here (same status code either
    way; unlike ``health_check`` this probe has no room for a body — it is
    ok/not-ready only).
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception as e:
        return HttpResponse(f"Not Ready: {e}", status=503)

    _checks, critical_down = _run_dependency_checks()
    if critical_down:
        down = [name for name, c in _checks.items() if not c['ok'] and c['critical']]
        return HttpResponse(f"Not Ready: critical dependency down: {', '.join(down)}", status=503)

    return HttpResponse("OK", status=200)


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

    # Metric name prefix. Deployments can customise via STAPEL_METRICS_PREFIX
    # (e.g. the Iron product pins it to "iron_" to preserve dashboards).
    mp = getattr(settings, 'STAPEL_METRICS_PREFIX', 'stapel_')

    # Service info
    metrics.append(f'# HELP {mp}service_info Service information')
    metrics.append(f'# TYPE {mp}service_info gauge')
    metrics.append(f'{mp}service_info{{service="{service_name}",version="{version}"}} 1')

    # Uptime
    metrics.append(f'# HELP {mp}uptime_seconds Service uptime in seconds')
    metrics.append(f'# TYPE {mp}uptime_seconds gauge')
    metrics.append(f'{mp}uptime_seconds{{service="{service_name}"}} {uptime:.2f}')

    # Database health
    metrics.append(f'# HELP {mp}database_up Database connection status')
    metrics.append(f'# TYPE {mp}database_up gauge')
    metrics.append(f'{mp}database_up{{service="{service_name}"}} {db_ok}')

    # Service up
    metrics.append(f'# HELP {mp}up Service is up')
    metrics.append(f'# TYPE {mp}up gauge')
    metrics.append(f'{mp}up{{service="{service_name}"}} 1')

    # Registered dependency checks (register_dependency_check) — one gauge
    # per dependency so an unreachable best-effort-wrapped call (the
    # meettoday LiveKit twirp incident: kick/PIN silently no-op'd in prod)
    # lights up in monitoring instead of nowhere.
    if _dependency_checks:
        dep_checks, _critical_down = _run_dependency_checks()
        metrics.append(f'# HELP {mp}dependency_up Registered dependency reachability')
        metrics.append(f'# TYPE {mp}dependency_up gauge')
        for name, c in dep_checks.items():
            metrics.append(
                f'{mp}dependency_up{{service="{service_name}",dependency="{name}"}} '
                f'{1 if c["ok"] else 0}'
            )

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
        from stapel_core.django.monitoring.health import get_health_urls

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
