"""Contract of the event-store seam — the ABC and its value types.

An event store is an **append-only** sink for high-volume streams that are
written often, read as aggregates, grow without bound, and do not belong in
a transaction with business data (ledger of LLM calls, gateway audit,
analytics, delivery logs — see docs/data-storage-and-observability.md §1).

The contract is deliberately small: append (single/batch), cursor read,
group-by rollup, and per-stream retention purge. Backends map it onto their
engine (Postgres time-partitions by default; ClickHouse is the documented
scale-out evolution point). Callers go through the module facade
(:mod:`stapel_core.eventstore`), never a backend directly.
"""
from __future__ import annotations

import base64
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Sequence

#: Identity columns promoted out of the JSON payload into indexed columns.
#: They are generic on purpose — the framework does not know what a
#: "project"/"task"/"container" means, only that consumers (Studio ledger,
#: gateway audit, delivery logs) want to slice by them cheaply. Every one is
#: nullable: a stream that has no notion of a task simply leaves it unset.
IDENTITY_FIELDS: tuple[str, ...] = ("project", "task", "container")


@dataclass(slots=True)
class Event:
    """One append-only row.

    ``stream`` is the indexed logical name (``llm.call``, ``audit``,
    ``delivery`` …). ``payload`` is the free-form JSON body. ``ts`` defaults
    to append time when omitted. The identity fields are the generic,
    indexed, nullable slice columns. ``id`` is assigned by the backend on
    read and is ``None`` for events on their way in.
    """

    stream: str
    payload: dict = field(default_factory=dict)
    ts: datetime | None = None
    project: str | None = None
    task: str | None = None
    container: str | None = None
    id: int | None = None

    def identity(self) -> dict[str, str | None]:
        return {f: getattr(self, f) for f in IDENTITY_FIELDS}


@dataclass(frozen=True, slots=True)
class Cursor:
    """Stable position in a stream: ``(ts, id)``.

    Read pages advance strictly past this point. ``ts`` alone is not unique
    (many events share a millisecond), so the row ``id`` is the tie-break —
    this is what makes cursor paging exact rather than lossy under bursts.
    Cursors serialize to an opaque URL-safe token for API transport.
    """

    ts: datetime
    id: int

    def encode(self) -> str:
        raw = json.dumps([self.ts.isoformat(), self.id]).encode()
        return base64.urlsafe_b64encode(raw).decode()

    @classmethod
    def decode(cls, token: str | None) -> "Cursor | None":
        if not token:
            return None
        raw = base64.urlsafe_b64decode(token.encode())
        ts_iso, row_id = json.loads(raw)
        return cls(ts=datetime.fromisoformat(ts_iso), id=int(row_id))


@dataclass(slots=True)
class EventPage:
    """A page of events plus the cursor to fetch the next page.

    ``cursor`` is ``None`` when the page exhausted the stream (no more rows
    matched); otherwise pass it back as ``after=`` to continue.
    """

    events: list[Event]
    cursor: Cursor | None

    @property
    def has_more(self) -> bool:
        return self.cursor is not None

    def __iter__(self):
        return iter(self.events)

    def __len__(self) -> int:
        return len(self.events)


@dataclass(slots=True)
class RollupRow:
    """One aggregated bucket produced by :meth:`EventStore.rollup`."""

    stream: str
    group: dict[str, str | None]
    count: int
    sums: dict[str, float]


class EventStore(ABC):
    """Append-only stream store — the swappable backend contract.

    Implementations: :class:`~stapel_core.eventstore.backends.postgres.PostgresEventStore`
    (default) and, as a documented evolution point, a ClickHouse backend.
    A backend must be safe to share across threads.
    """

    @abstractmethod
    def append_batch(self, events: Sequence[Event]) -> None:
        """Persist a batch of events. The buffer calls this, not per-event."""

    def append(self, event: Event) -> None:
        """Persist one event (default: a one-element batch)."""
        self.append_batch([event])

    @abstractmethod
    def query(
        self,
        stream: str,
        *,
        after: Cursor | None = None,
        limit: int = 100,
        time_range: tuple[datetime | None, datetime | None] | None = None,
        filters: Mapping[str, object] | None = None,
    ) -> EventPage:
        """Cursor read: rows of *stream* strictly after *after*, in ``(ts, id)``
        order, at most *limit*. ``time_range`` bounds ``ts`` (half-open
        ``[start, end)``); ``filters`` matches identity columns or payload
        keys (``{"project": "p1", "model": "opus"}``)."""

    @abstractmethod
    def rollup(
        self,
        stream: str,
        *,
        group_by: Sequence[str],
        sum_fields: Sequence[str],
        time_range: tuple[datetime | None, datetime | None] | None = None,
        filters: Mapping[str, object] | None = None,
        into: str | None = None,
    ) -> list[RollupRow]:
        """Aggregate *stream* into ``group_by`` buckets, summing ``sum_fields``
        (both resolve against identity columns first, then payload keys).
        When *into* is given the rows are also upserted into that rollup
        table; the concrete meaning of a rollup is the consumer's business."""

    @abstractmethod
    def purge(self, stream: str, *, older_than: datetime) -> int:
        """Delete raw events of *stream* with ``ts < older_than``; return the
        number removed. Retention policy lives in settings and is applied by
        the sweep command — this is the mechanism."""

    def purge_rollup(self, stream: str, *, older_than: datetime) -> int:
        """Delete rollup buckets older than *older_than* (raw retention ≠
        rollup retention). Default: nothing to purge."""
        return 0


def resolve_field(event: Event, name: str):
    """Read *name* from an event: identity column first, then payload key."""
    if name in IDENTITY_FIELDS:
        return getattr(event, name)
    return event.payload.get(name)


__all__ = [
    "IDENTITY_FIELDS",
    "Cursor",
    "Event",
    "EventPage",
    "EventStore",
    "RollupRow",
    "resolve_field",
]
