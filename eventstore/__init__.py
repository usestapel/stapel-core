"""stapel_core.eventstore — append-only stream primitive as a core seam.

Several high-volume, append-only streams share one nature — LLM-call ledger,
gateway audit, analytics, delivery logs: written often, read as aggregates,
grow without bound, out of band with business transactions. So they are one
core primitive, not N bespoke tables (docs/data-storage-and-observability.md
§1; docs/studio-design.md §3, three storage contours).

Usage — modules write through the facade, never touching a backend::

    from stapel_core import eventstore

    eventstore.append("llm.call", {"model": "opus", "output": 1200},
                      project="brave-falcon-1042", task="T-001")

    page = eventstore.query("llm.call", limit=100)
    for event in page:
        ...
    if page.has_more:
        page = eventstore.query("llm.call", after=page.cursor)

    rows = eventstore.rollup("llm.call", group_by=["model"],
                             sum_fields=["output"], into="llm_cost")

Design rules:

- **Backend seam** — ``STAPEL_EVENTSTORE["BACKEND"]`` (dotted path); default
  :class:`~stapel_core.eventstore.backends.postgres.PostgresEventStore`.
  Per-stream override via ``STAPEL_EVENTSTORE["ROUTES"]`` (merge-routing by
  stream name). ClickHouse is the documented evolution point — the ABC
  already permits it; it is not implemented here.
- **Buffered writes** — appends batch through a :class:`WriteBuffer`; reads
  flush first, so a caller always sees its own writes.
- **Retention/rollup are settings**, applied by ``manage.py sweep_eventstore``.
"""
from __future__ import annotations

import atexit
import threading
from datetime import datetime
from typing import Mapping, Sequence

from .base import (
    IDENTITY_FIELDS,
    Cursor,
    Event,
    EventPage,
    EventStore,
    RollupRow,
)
from .buffer import WriteBuffer

_lock = threading.Lock()
_buffer: WriteBuffer | None = None
_backends: dict[str, EventStore] = {}
_default_backend: EventStore | None = None


def _reset_state(*, setting=None, **kwargs) -> None:
    """Drop the buffer and cached backends on a config change / test override.

    Flushes any buffered events first so a settings swap never silently drops
    writes. Only reacts to eventstore-relevant settings.
    """
    from .conf import eventstore_settings

    if setting is not None and setting != eventstore_settings.namespace \
            and setting not in eventstore_settings.defaults:
        return
    global _buffer, _default_backend
    with _lock:
        buf, _buffer = _buffer, None
        _backends.clear()
        _default_backend = None
    if buf is not None:
        buf.flush()


try:  # keep the singletons honest across override_settings in tests
    from django.test.signals import setting_changed

    setting_changed.connect(_reset_state, weak=False)
except Exception:  # pragma: no cover - Django not importable at import time
    pass


def _load_backend(dotted_or_obj) -> EventStore:
    value = dotted_or_obj
    if isinstance(value, str):
        from django.utils.module_loading import import_string

        value = import_string(value)
    if isinstance(value, type):
        value = value()
    if not isinstance(value, EventStore):
        raise TypeError(
            f"STAPEL_EVENTSTORE backend resolved to {value!r}, "
            "which is not an EventStore"
        )
    return value


def _get_default_backend() -> EventStore:
    global _default_backend
    backend = _default_backend
    if backend is not None:
        return backend
    with _lock:
        if _default_backend is None:
            from .conf import eventstore_settings

            _default_backend = _load_backend(eventstore_settings.BACKEND)
        return _default_backend


def resolve_backend(stream: str) -> EventStore:
    """The backend a *stream* routes to (``ROUTES`` override → default)."""
    from .conf import eventstore_settings

    routes = eventstore_settings.ROUTES or {}
    dotted = routes.get(stream)
    if not dotted:
        return _get_default_backend()
    with _lock:
        backend = _backends.get(stream)
        if backend is None:
            backend = _load_backend(dotted)
            _backends[stream] = backend
        return backend


def _flush_batch(events: Sequence[Event]) -> None:
    """Group a flushed batch by resolved backend and append per backend."""
    by_backend: dict[int, tuple[EventStore, list[Event]]] = {}
    for event in events:
        backend = resolve_backend(event.stream)
        bucket = by_backend.setdefault(id(backend), (backend, []))
        bucket[1].append(event)
    for backend, batch in by_backend.values():
        backend.append_batch(batch)


def _get_buffer() -> WriteBuffer:
    global _buffer
    buffer = _buffer
    if buffer is not None:
        return buffer
    with _lock:
        if _buffer is None:
            from .conf import eventstore_settings

            _buffer = WriteBuffer(
                _flush_batch,
                size=int(eventstore_settings.BUFFER_SIZE),
                interval=float(eventstore_settings.BUFFER_INTERVAL),
                sync=bool(eventstore_settings.BUFFER_SYNC),
            )
        return _buffer


def append(
    stream: str,
    payload: Mapping[str, object] | None = None,
    *,
    ts: datetime | None = None,
    project: str | None = None,
    task: str | None = None,
    container: str | None = None,
) -> None:
    """Buffer one event for *stream*. Flushed by size/interval or ``flush()``."""
    _get_buffer().add(
        Event(
            stream=stream,
            payload=dict(payload or {}),
            ts=ts,
            project=project,
            task=task,
            container=container,
        )
    )


def append_batch(events: Sequence[Event]) -> None:
    """Buffer a batch of pre-built :class:`Event` objects."""
    _get_buffer().extend(events)


def flush() -> None:
    """Force-persist everything buffered (also runs at interpreter exit)."""
    buffer = _buffer
    if buffer is not None:
        buffer.flush()


def query(
    stream: str,
    *,
    after: Cursor | None = None,
    limit: int = 100,
    time_range: tuple[datetime | None, datetime | None] | None = None,
    filters: Mapping[str, object] | None = None,
) -> EventPage:
    """Read a page of *stream*. Flushes buffered writes first (read-your-writes)."""
    flush()
    return resolve_backend(stream).query(
        stream, after=after, limit=limit, time_range=time_range, filters=filters
    )


def rollup(
    stream: str,
    *,
    group_by: Sequence[str],
    sum_fields: Sequence[str],
    time_range: tuple[datetime | None, datetime | None] | None = None,
    filters: Mapping[str, object] | None = None,
    into: str | None = None,
) -> list[RollupRow]:
    """Aggregate *stream* by ``group_by``, summing ``sum_fields``."""
    flush()
    return resolve_backend(stream).rollup(
        stream,
        group_by=group_by,
        sum_fields=sum_fields,
        time_range=time_range,
        filters=filters,
        into=into,
    )


def purge(stream: str, *, older_than: datetime) -> int:
    """Delete raw events of *stream* older than *older_than*; return count."""
    flush()
    return resolve_backend(stream).purge(stream, older_than=older_than)


atexit.register(flush)


__all__ = [
    "IDENTITY_FIELDS",
    "Cursor",
    "Event",
    "EventPage",
    "EventStore",
    "RollupRow",
    "WriteBuffer",
    "append",
    "append_batch",
    "flush",
    "purge",
    "query",
    "resolve_backend",
    "rollup",
]
