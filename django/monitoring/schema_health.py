"""Schema-drift probe: is the running code's schema at head?

Motivating incident: a live stand ran for twelve hours against an unmigrated
database while every endpoint it had reported healthy. Nothing in the process
ever asked the question, so nothing could answer it. This module asks it once
per scrape and puts the answer where an operator already looks.

Lifted from a product-local copy (ironmemo's ``iron-*/core/schema_health.py``,
duplicated per service because there was no shared package to import from).
"Is my schema at head" is not product knowledge; it is the same question in
every Django service, so it lives here and a product deletes its copy on the
next core bump.

Three states, not two
---------------------
The first version of this returned a bool and mapped every exception to False,
so ``could not translate host name "db" to address`` — a connectivity fault —
came out as "the schema is behind". A boolean has two values, so an inability
to ask became a negative verdict: every service would report drift during a
database restart, and the drift alert would fire saying something untrue.

:func:`schema_state` returns :data:`AT_HEAD`, :data:`BEHIND` or
:data:`UNKNOWN`, and each consumer is told which of the three it has:

* ``/api/metrics/`` carries the full truth. ``<prefix>schema_at_head`` is
  emitted ONLY when the state was determined, so an unreachable database
  makes the series stop rather than drop to zero;
  ``<prefix>schema_probe_ok`` is emitted always, so "the probe cannot answer"
  is itself observable. Alert rules read these, and only these.
* ``/api/health/`` gets the three states directly, since
  ``register_dependency_check`` grew a third one for exactly this: the probe
  returns ``None`` for UNKNOWN and the body says ``"unknown"``, distinct from
  ``"error"``.
* The deploy gate does not read this at all. ``manage.py migrate --check``
  inside the container is the single authority at deploy time, and two
  mechanisms answering the same question is its own defect.

Registered as a NON-critical dependency check: ``/api/health/`` keeps
answering 200 while naming the state. A 503 on drift would pull every backend
out of rotation during a normal rolling migration — an outage caused by the
alarm — and under ``restart: unless-stopped`` plus an autoheal watcher it
would turn a failed migration into an unrecoverable restart loop. The status
code is a routing decision; the body and the metrics are the truth.
"""
import logging
import threading
import time

from django.conf import settings
from django.db import Error as DatabaseError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

logger = logging.getLogger(__name__)

AT_HEAD = "at_head"
BEHIND = "behind"
UNKNOWN = "unknown"

# A determined verdict is worth caching: the migration files cannot change
# inside a running process, and re-reading the applied set every scrape is
# waste. A non-answer is NOT cached — pinning "I could not tell" for thirty
# seconds makes a two-second blip outlive itself, and the retry costs one
# failed connection attempt, which is what the endpoint's own database check
# is already doing on the same request.
_TTL_SECONDS = 30

_lock = threading.Lock()
_checked_at = 0.0
_state = UNKNOWN
_registered = False


def unapplied_migrations():
    """Migrations on disk that the database has not applied.

    Same definition as ``manage.py migrate --check``, deliberately: the boot
    gate, the deploy gate and this probe must not disagree about what
    "behind" means.
    """
    executor = MigrationExecutor(connection)
    targets = executor.loader.graph.leaf_nodes()
    return [f"{m.app_label}.{m.name}" for m, _backwards in executor.migration_plan(targets)]


def schema_state():
    """:data:`AT_HEAD`, :data:`BEHIND` or :data:`UNKNOWN`. Never raises."""
    global _checked_at, _state
    now = time.monotonic()
    with _lock:
        if _state is not UNKNOWN and _checked_at and now - _checked_at < _TTL_SECONDS:
            return _state
        try:
            pending = unapplied_migrations()
        except DatabaseError as exc:
            # Routine in operation — a database restart, a DNS blip. No stack
            # trace: a traceback for something that is not exceptional is the
            # noise that teaches people to skip the log. It is not silent
            # either: <prefix>schema_probe_ok goes to 0 and has its own rule.
            logger.warning("schema probe could not reach the database: %s", exc)
            _state = UNKNOWN
            return _state
        except Exception:
            # Genuinely unexpected — a broken migration graph, an inconsistent
            # history. Still not a verdict of "behind", but worth the trace.
            logger.exception("schema probe failed unexpectedly")
            _state = UNKNOWN
            return _state
        if pending:
            logger.error("schema is behind the code, unapplied migrations: %s", ", ".join(pending))
        _checked_at = now
        _state = BEHIND if pending else AT_HEAD
        return _state


def reset_schema_state():
    """Drop the cached verdict. For tests and for a post-migrate hook."""
    global _checked_at, _state
    with _lock:
        _checked_at = 0.0
        _state = UNKNOWN


def schema_probe():
    """Probe for ``register_dependency_check``: True / False / None.

    ``None`` is UNKNOWN and the health body renders it as ``"unknown"``,
    which is the whole point — the two-valued predecessor of this function
    had to answer "nothing has been detected" and call an unreachable
    database healthy.
    """
    state = schema_state()
    if state is UNKNOWN:
        return None
    return state is AT_HEAD


def _metrics():
    """Prometheus fragment carrying all three states."""
    prefix = getattr(settings, "STAPEL_METRICS_PREFIX", "stapel_")
    service = getattr(settings, "SERVICE_NAME", "unknown").lower().replace(" ", "_")
    state = schema_state()
    lines = [
        f"# HELP {prefix}schema_probe_ok Whether the schema state could be determined",
        f"# TYPE {prefix}schema_probe_ok gauge",
        f'{prefix}schema_probe_ok{{service="{service}"}} {0 if state is UNKNOWN else 1}',
    ]
    if state is not UNKNOWN:
        # Deliberately absent when undetermined: a series that drops to 0
        # because the database was unreachable is the false verdict this
        # module exists to avoid.
        lines += [
            f"# HELP {prefix}schema_at_head Whether the schema is at the code's head",
            f"# TYPE {prefix}schema_at_head gauge",
            f'{prefix}schema_at_head{{service="{service}"}} {1 if state is AT_HEAD else 0}',
        ]
    return "\n".join(lines)


def register_schema_check():
    """Register the probe on ``/api/health/`` and ``/api/metrics/``. Idempotent."""
    global _registered
    if _registered:
        return
    from stapel_core.django.monitoring.health import (
        register_dependency_check,
        register_metrics_exporter,
    )

    register_dependency_check("schema", schema_probe, critical=False)
    register_metrics_exporter(_metrics)
    _registered = True


__all__ = [
    "AT_HEAD",
    "BEHIND",
    "UNKNOWN",
    "register_schema_check",
    "reset_schema_state",
    "schema_probe",
    "schema_state",
    "unapplied_migrations",
]
