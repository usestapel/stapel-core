"""Database-connection hygiene for long-lived, non-request workers.

Django opens and closes database connections around every HTTP request via
``request_started``/``request_finished``. A consumer loop has no requests, so
whatever connection the first event opened is the one every later event
reuses — for days.

``close_old_connections()`` alone is not enough for that shape. It closes a
connection when the app already recorded an error on it, or when
``CONN_MAX_AGE`` expired. A connection the DATABASE SERVER dropped while the
loop sat idle looks healthy to Django until something tries to use it, and by
then the event is already in flight. That is how ironmemo lost 46 hours of
notifications (2026-08-26 21:58 UTC → 2026-08-28): one idle drop, then
``InterfaceError: connection already closed`` on every event after it,
forever, because nothing in the loop ever reset the connection. The retry
loop around the handler could not help — all four attempts reused the same
dead connection, so retrying was structurally incapable of changing the
condition it was retrying.

So this MEASURES rather than assumes: probe the live connections and close the
ones that answer wrong. ``is_usable()`` costs one round trip per event, which
is the price of not dropping the event.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

__all__ = ["close_stale_connections", "worker_db_lifecycle"]


def close_stale_connections() -> None:
    """Close connections that are old, errored, or no longer answering.

    Safe to call when Django is not configured (no-op) — the bus backends run
    in deployments that may not have loaded settings.
    """
    try:
        from django.db import close_old_connections, connections
    except Exception:  # pragma: no cover - Django absent
        return

    try:
        close_old_connections()
    except Exception:  # pragma: no cover - settings not configured
        return

    for conn in connections.all(initialized_only=True):
        # `initialized_only` keeps this from opening a connection just to ask
        # whether it is healthy; an alias nobody touched has nothing stale.
        if conn.connection is None or conn.in_atomic_block:
            continue
        try:
            usable = conn.is_usable()
        except Exception:
            # A probe that raises is the answer, not an error to propagate:
            # the connection is unusable.
            usable = False
        if not usable:
            conn.close()


@contextmanager
def worker_db_lifecycle() -> Iterator[None]:
    """Wrap one unit of work in a non-request worker.

    Mirrors what the request lifecycle does for a view: start from a
    connection known to answer, and release it afterwards so a connection
    poisoned by the handler cannot be inherited by the next unit of work.
    """
    close_stale_connections()
    try:
        yield
    finally:
        try:
            from django.db import close_old_connections

            close_old_connections()
        except Exception:  # pragma: no cover - Django absent/unconfigured
            pass
