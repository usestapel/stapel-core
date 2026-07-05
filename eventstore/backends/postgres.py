"""Default event-store backend: Django ORM over a Postgres (or SQLite) table.

Zero infrastructure — reuses the platform database. On PostgreSQL the raw
table is time-partitioned (see ``django/eventstore/partitions.py``); on the
SQLite minimal profile it degrades to a single plain table with the same
rows and no partitions (documented). Aggregation for :meth:`rollup` runs in
Python so it is identical on every engine — correct first; pushing the
GROUP BY into SQL/ClickHouse is the scale-out optimization.

The Django model is imported lazily so importing the seam never requires
Django to be configured.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Mapping, Sequence

from ..base import (
    IDENTITY_FIELDS,
    Cursor,
    Event,
    EventPage,
    EventStore,
    RollupRow,
    resolve_field,
)


def _model():
    from stapel_core.django.eventstore.models import EventRecord

    return EventRecord


def _rollup_model():
    from stapel_core.django.eventstore.models import EventRollup

    return EventRollup


def _apply_filters(queryset, filters: Mapping[str, object] | None):
    """Translate a filter map to ORM lookups: identity columns direct,
    everything else as a JSON payload-key lookup."""
    if not filters:
        return queryset
    lookups: dict[str, object] = {}
    for key, value in filters.items():
        if key in IDENTITY_FIELDS:
            lookups[key] = value
        else:
            lookups[f"payload__{key}"] = value
    return queryset.filter(**lookups)


def _apply_time_range(queryset, time_range):
    if not time_range:
        return queryset
    start, end = time_range
    if start is not None:
        queryset = queryset.filter(ts__gte=start)
    if end is not None:
        queryset = queryset.filter(ts__lt=end)
    return queryset


class PostgresEventStore(EventStore):
    def append_batch(self, events: Sequence[Event]) -> None:
        if not events:
            return
        from django.utils import timezone

        model = _model()
        now = timezone.now()
        rows = [
            model(
                stream=e.stream,
                ts=e.ts or now,
                payload=e.payload or {},
                project=e.project,
                task=e.task,
                container=e.container,
            )
            for e in events
        ]
        model.objects.bulk_create(rows)

    def query(
        self,
        stream: str,
        *,
        after: Cursor | None = None,
        limit: int = 100,
        time_range: tuple[datetime | None, datetime | None] | None = None,
        filters: Mapping[str, object] | None = None,
    ) -> EventPage:
        from django.db.models import Q

        limit = max(1, int(limit))
        queryset = _model().objects.filter(stream=stream)
        queryset = _apply_time_range(queryset, time_range)
        queryset = _apply_filters(queryset, filters)
        if after is not None:
            queryset = queryset.filter(
                Q(ts__gt=after.ts) | Q(ts=after.ts, id__gt=after.id)
            )
        # Fetch one extra to learn whether a further page exists.
        rows = list(queryset.order_by("ts", "id")[: limit + 1])
        has_more = len(rows) > limit
        rows = rows[:limit]
        events = [
            Event(
                stream=r.stream,
                payload=r.payload or {},
                ts=r.ts,
                project=r.project,
                task=r.task,
                container=r.container,
                id=r.id,
            )
            for r in rows
        ]
        cursor = None
        if has_more and rows:
            last = rows[-1]
            cursor = Cursor(ts=last.ts, id=last.id)
        return EventPage(events=events, cursor=cursor)

    def _iter_events(self, stream, time_range, filters):
        queryset = _model().objects.filter(stream=stream)
        queryset = _apply_time_range(queryset, time_range)
        queryset = _apply_filters(queryset, filters)
        return queryset.order_by("ts", "id").iterator()

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
        group_by = list(group_by)
        sum_fields = list(sum_fields)
        buckets: dict[str, RollupRow] = {}
        for row in self._iter_events(stream, time_range, filters):
            event = Event(
                stream=row.stream,
                payload=row.payload or {},
                ts=row.ts,
                project=row.project,
                task=row.task,
                container=row.container,
                id=row.id,
            )
            group = {f: resolve_field(event, f) for f in group_by}
            key = json.dumps(group, sort_keys=True, default=str)
            bucket = buckets.get(key)
            if bucket is None:
                bucket = RollupRow(
                    stream=stream,
                    group=group,
                    count=0,
                    sums={f: 0.0 for f in sum_fields},
                )
                buckets[key] = bucket
            bucket.count += 1
            for f in sum_fields:
                value = resolve_field(event, f)
                if isinstance(value, bool):  # bool is an int subclass — skip
                    continue
                if isinstance(value, (int, float)):
                    bucket.sums[f] += float(value)
        result = list(buckets.values())
        if into:
            self._persist_rollup(into, stream, buckets)
        return result

    def _persist_rollup(self, name, stream, buckets):
        model = _rollup_model()
        for key, bucket in buckets.items():
            model.objects.update_or_create(
                name=name,
                stream=stream,
                group_key=key,
                defaults={
                    "group": bucket.group,
                    "count": bucket.count,
                    "sums": bucket.sums,
                },
            )

    def purge(self, stream: str, *, older_than: datetime) -> int:
        deleted, _ = (
            _model().objects.filter(stream=stream, ts__lt=older_than).delete()
        )
        return deleted

    def purge_rollup(self, stream: str, *, older_than: datetime) -> int:
        deleted, _ = (
            _rollup_model()
            .objects.filter(stream=stream, updated_at__lt=older_than)
            .delete()
        )
        return deleted


__all__ = ["PostgresEventStore"]
