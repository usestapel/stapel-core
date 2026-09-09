"""
Health check and metrics endpoints for monitoring.

Provides:
- /api/health/ - Basic health check with metrics for Prometheus
- /api/health/ready/ - Readiness probe (checks DB connection)
- /api/health/live/ - Liveness probe (always returns OK)
"""
import logging
import re
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

# The three answers a dependency probe can give. UNKNOWN is a first-class
# state, not an absence: "could not ask" is neither "reachable" nor "down",
# and collapsing it into either one is how a health endpoint lies.
DEP_OK = 'ok'
DEP_ERROR = 'error'
DEP_UNKNOWN = 'unknown'


def register_metrics_exporter(exporter):
    """
    Register a callable that returns a Prometheus-formatted string.

    Usage:
        from stapel_core.django.monitoring.health import register_metrics_exporter
        register_metrics_exporter(my_export_func)

    Registering the same callable twice is a no-op. That only covers the easy
    half: two *different* callables emitting the same series (a library
    collector plus a product's leftover copy of it) still register, and the
    exposition is what refuses to carry both — see
    :func:`_dedupe_exposition`.
    """
    if exporter in _custom_metrics_exporters:
        return
    _custom_metrics_exporters.append(exporter)


# name{labels} — everything up to the whitespace before the value.
_SAMPLE_RE = re.compile(
    r'^(?P<series>[a-zA-Z_:][a-zA-Z0-9_:]*(?:\{.*\})?)[ \t]+(?P<value>.*)$'
)
_META_RE = re.compile(r'^#\s+(?:HELP|TYPE)\s+(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)\b')


def _exporter_label(exporter):
    """Where a fragment came from, so a conflict names both collectors."""
    module = getattr(exporter, '__module__', '?')
    name = getattr(exporter, '__qualname__', None) or repr(exporter)
    return f'{module}.{name}'


def _dedupe_exposition(fragments):
    """One sample per series, whatever registered a collector twice.

    Motivating incident: every service in a fleet exported
    ``stapel_schema_probe_ok`` and ``stapel_schema_at_head`` twice — core's
    AppConfig registered the collector, and each product still carried the
    per-service copy it was lifted from, which registered a second one. Both
    read 1, so nothing looked wrong; Prometheus logged
    ``Error on ingesting samples with different value but same timestamp``
    and dropped one of each pair, ~164 times in two hours.

    Dropping "one of each pair" is the part that matters. Which copy survives
    is Prometheus' choice, not ours, so the day the two collectors disagree —
    one probing live, one serving a cached answer — the value that reaches an
    alert rule is a coin flip. Deduplicating at the registration seam would
    only have caught callables registered twice; these were two *different*
    functions emitting the same series, which no registry can tell apart. The
    exposition can, because a series identity is the thing Prometheus keys on.

    So: first writer of a series wins, later copies are dropped, and a copy
    that disagrees is logged with both values and both sources instead of
    silently deciding an alert. ``# HELP``/``# TYPE`` are kept once per metric
    name for the same reason — repeating them is malformed exposition.

    *fragments* is an iterable of ``(source, text)``; returns
    ``(lines, conflicts)``.
    """
    lines = []
    seen_series = {}   # series -> (value, source)
    seen_meta = set()  # metric name, per HELP/TYPE
    conflicts = 0

    for source, text in fragments:
        if not text:
            continue
        for line in text.split('\n'):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith('#'):
                meta = _META_RE.match(stripped)
                if meta:
                    key = (stripped.split()[1], meta.group('name'))
                    if key in seen_meta:
                        continue
                    seen_meta.add(key)
                lines.append(line)
                continue
            sample = _SAMPLE_RE.match(stripped)
            if sample is None:
                lines.append(line)
                continue
            series = sample.group('series')
            value = sample.group('value')
            if series in seen_series:
                kept_value, kept_source = seen_series[series]
                if kept_value != value:
                    conflicts += 1
                    logger.error(
                        "Two metrics collectors disagree about %s: %s says %s, "
                        "%s says %s. Keeping the first; one of them is a stale "
                        "duplicate and an alert reading this series cannot tell "
                        "which answer it got.",
                        series, kept_source, kept_value, source, value,
                    )
                continue
            seen_series[series] = (value, source)
            lines.append(line)

    return lines, conflicts


def register_dependency_check(name, probe, *, critical=False):
    """
    Register an outbound-dependency probe surfaced on ``/api/health/`` and
    ``/api/metrics/`` (docs/pending/env-address-class-v2.md §3.6).

    Motivating incident: a client's host-kick and room-PIN endpoints wrapped
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

    ``probe`` is a zero-argument callable returning one of THREE answers:

    * ``True``  — the dependency answered and is reachable/correct;
    * ``False`` — positively determined to be unreachable/wrong;
    * ``None``  — the probe could not ask (the database was restarting, DNS
      blipped, a timeout hit before any answer came back).

    The third one is not decoration. This function used to do
    ``ok = bool(probe())``, so a sentinel meaning "unknown" coerced to
    ``True`` and rendered as ``ok`` — a probe that could not ask reported a
    healthy dependency, which is not merely unsupported but silently wrong.
    An UNKNOWN is now carried through end to end: ``checks{}`` says
    ``"unknown"`` (distinct from ``"error"``), ``stapel_dependency_up`` is
    OMITTED for that dependency for the scrape (a series that drops to 0
    because nobody could ask is the false verdict this exists to avoid), and
    the always-emitted ``stapel_dependency_probe_ok`` goes to 0 so the
    inability to ask is itself something an alert can fire on.

    A probe that RAISES is a broken probe, not an unknown: it is logged with
    a stack and counted as unreachable, never allowed to break the health
    endpoint itself (the same "a broken exporter must not break
    ``/api/metrics/``" posture ``register_metrics_exporter`` already has).
    "I could not ask" is said by returning ``None``, deliberately, so it can
    never be confused with a bug in the probe. Any timeout is the probe's
    own responsibility.

    ``critical=True`` means this dependency is load-bearing for the WHOLE
    process's readiness: a DETERMINED failure flips ``/api/health/`` and
    ``readiness_probe()`` to HTTP 503 (an orchestrator should stop routing
    traffic here). An UNDETERMINED critical dependency does NOT: an inability
    to ask is not proof the dependency is down, and pulling the process out
    of rotation on it converts a two-second blip into an outage — every
    replica loses the same probe at the same moment, so the whole service
    leaves rotation together. ``critical=False`` (default) — the process
    stays healthy (200) but the failure is named in ``checks{}`` and in
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
    """``(checks dict, any_critical_determined_down)`` — never raises.

    Each entry is ``{"state": OK|ERROR|UNKNOWN, "critical": bool}``. Only a
    DETERMINED failure of a critical dependency sets the second element: see
    ``register_dependency_check`` on why an undetermined one must not take
    the process out of rotation.
    """
    checks = {}
    critical_down = False
    for name, probe, critical in _dependency_checks:
        try:
            answer = probe()
        except Exception:
            logger.exception("Dependency check %s failed", name)
            state = DEP_ERROR
        else:
            state = DEP_UNKNOWN if answer is None else (DEP_OK if answer else DEP_ERROR)
        checks[name] = {"state": state, "critical": critical}
        if state is DEP_ERROR and critical:
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
    any_down = any(c['state'] is DEP_ERROR for c in dep_checks.values())

    healthy = db_ok and not critical_down
    # 'degraded' is reserved for a DETERMINED failure. An undetermined
    # dependency is named in checks{} as 'unknown' and carried by
    # stapel_dependency_probe_ok; calling it degraded would make every
    # database restart look like a product outage.
    status = 'healthy' if (healthy and not any_down) else 'degraded'

    checks = {'database': DEP_OK if db_ok else DEP_ERROR}
    for name, c in dep_checks.items():
        checks[name] = c['state']

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
    dependency DETERMINED down means this process cannot serve its purpose,
    so an orchestrator should stop routing traffic here (same status code
    either way; unlike ``health_check`` this probe has no room for a body —
    it is ok/not-ready only).

    A critical dependency whose probe returned ``None`` (could not ask) does
    NOT take this process out of rotation. An inability to ask is not proof
    the dependency is down, and every replica loses the same probe at the
    same moment, so a 503 here turns a blip into a full outage — the exact
    "fail-closed on environment errors is the wrong answer" argument the rest
    of this module is built on.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception as e:
        return HttpResponse(f"Not Ready: {e}", status=503)

    _checks, critical_down = _run_dependency_checks()
    if critical_down:
        down = [
            name for name, c in _checks.items()
            if c['state'] is DEP_ERROR and c['critical']
        ]
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
    # client LiveKit twirp incident: kick/PIN silently no-op'd in prod)
    # lights up in monitoring instead of nowhere.
    #
    # Two series, because there are three states. `dependency_probe_ok` is
    # emitted ALWAYS (0 = the probe could not ask), so "nobody could tell" is
    # itself alertable. `dependency_up` carries the verdict and is OMITTED
    # for an undetermined dependency: a series that drops to 0 because the
    # network blipped is a false "it is down", and an alert would fire saying
    # something untrue.
    if _dependency_checks:
        dep_checks, _critical_down = _run_dependency_checks()
        metrics.append(
            f'# HELP {mp}dependency_probe_ok Whether the dependency state could be determined'
        )
        metrics.append(f'# TYPE {mp}dependency_probe_ok gauge')
        for name, c in dep_checks.items():
            metrics.append(
                f'{mp}dependency_probe_ok{{service="{service_name}",dependency="{name}"}} '
                f'{0 if c["state"] is DEP_UNKNOWN else 1}'
            )
        determined = {n: c for n, c in dep_checks.items() if c['state'] is not DEP_UNKNOWN}
        if determined:
            metrics.append(f'# HELP {mp}dependency_up Registered dependency reachability')
            metrics.append(f'# TYPE {mp}dependency_up gauge')
            for name, c in determined.items():
                metrics.append(
                    f'{mp}dependency_up{{service="{service_name}",dependency="{name}"}} '
                    f'{1 if c["state"] is DEP_OK else 0}'
                )

    # Append custom metrics from registered exporters
    fragments = [('this endpoint', '\n'.join(metrics))]
    for exporter in _custom_metrics_exporters:
        try:
            extra = exporter()
            if extra:
                fragments.append((_exporter_label(exporter), extra))
        except Exception:
            logger.exception("Metrics exporter %s failed", exporter)

    # A series appears once, whoever registered a collector for it twice.
    lines, conflicts = _dedupe_exposition(fragments)
    # The drop is itself a number: an exposition that quietly discarded a
    # disagreeing duplicate looks exactly like one that never had it.
    lines.append(f'# HELP {mp}metrics_series_conflicts '
                 f'Series two collectors disagreed about on this scrape')
    lines.append(f'# TYPE {mp}metrics_series_conflicts gauge')
    lines.append(f'{mp}metrics_series_conflicts{{service="{service_name}"}} {conflicts}')

    return HttpResponse(
        '\n'.join(lines) + '\n',
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
        - /{prefix}api/version/

    ``api/version/`` rides along deliberately rather than being a second
    thing to wire. The whole point of it is that an outside observer can ask
    a deployed service what it is running, and an endpoint each service has
    to remember to mount is one that some service will not have mounted on
    the day someone needs it. Every service that already reports its health
    now also reports its build — see monitoring/version.py.
    """
    from .version import get_version_urls

    return [
        path(f'{prefix}api/health/', health_check, name='health-check'),
        path(f'{prefix}api/health/ready/', readiness_probe, name='readiness-probe'),
        path(f'{prefix}api/health/live/', liveness_probe, name='liveness-probe'),
        path(f'{prefix}api/metrics/', prometheus_metrics, name='prometheus-metrics'),
        *get_version_urls(prefix),
    ]
