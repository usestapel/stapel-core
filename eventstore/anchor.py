"""Anchor-paginated reads over an event stream.

The fleet's list endpoints speak one wire contract —
``stapel_core.django.api.pagination.AnchorPagination``: ``{items,
next_anchor, prev_anchor, has_next, has_prev, count}``, newest first, with
an ISO-timestamp anchor that pages *past* strictly. That contract predates
the event store, so every journal that moved from a bespoke table into a
stream faced the same translation: anchor → time bound, display order →
``reverse`` read. Writing that translation once here is what makes "move
your journal into the store" a mechanical step instead of a re-derivation —
the released HTTP shape survives the storage change byte-for-byte.

Only the wire semantics live here. Who may read the stream, which filters a
request may set, how an item serializes — those stay in the owning module;
this adapter answers with :class:`~stapel_core.eventstore.base.Event`
objects and the envelope flags, nothing more.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping


@dataclass(slots=True)
class AnchorPage:
    """One page in display order (newest first) plus the envelope flags.

    ``next_anchor``/``prev_anchor`` are ISO timestamps (or ``None``), exactly
    what ``AnchorPagination.get_paginated_response`` reports for a model
    queryset — a client cannot tell which storage served it.
    """

    events: list
    next_anchor: str | None
    prev_anchor: str | None
    has_next: bool
    has_prev: bool

    @property
    def count(self) -> int:
        return len(self.events)


def _parse_anchor(anchor) -> datetime:
    """ISO string (or datetime) → aware datetime. Raises ValueError loudly:
    a malformed anchor must not degrade into "first page again"."""
    if isinstance(anchor, datetime):
        parsed = anchor
    else:
        parsed = datetime.fromisoformat(str(anchor))
    if parsed.tzinfo is None:
        from django.utils import timezone

        parsed = timezone.make_aware(parsed)
    return parsed


#: One microsecond — the resolution Django's DateTimeField stores. The store's
#: time_range is half-open ``[start, end)``; adding one tick to the start turns
#: "at or after the anchor" into the strict "newer than the anchor" the anchor
#: contract requires, with no epsilon guesswork: at microsecond resolution the
#: two predicates are identical.
_TICK = timedelta(microseconds=1)


def anchor_page(
    stream: str,
    *,
    filters: Mapping[str, object] | None = None,
    anchor=None,
    direction: str = "next",
    limit: int = 100,
) -> AnchorPage:
    """Read one anchor page of *stream*, newest first.

    ``direction`` mirrors AnchorPagination: ``next`` (older than the anchor —
    the default and the only direction most clients ever send), ``prev`` (the
    page of newer rows adjacent to the anchor), ``center`` (rows around the
    anchor, anchor-timestamped rows included). Anything else falls back to
    ``next``, as the queryset paginator's does.

    Boundary ties share the paginator's known limitation: the anchor is a
    bare timestamp, so rows sharing one microsecond can straddle a page
    edge. Journals write at human cadence; the parity is deliberate —
    fixing it would change the released anchor format.
    """
    from stapel_core import eventstore

    limit = max(1, int(limit))
    at = _parse_anchor(anchor) if anchor else None

    if at is not None and direction == "prev":
        page = eventstore.query(
            stream,
            time_range=(at + _TICK, None),
            filters=filters,
            limit=limit + 1,
        )
        rows = list(page.events)  # ascending: adjacent-to-anchor rows first
        has_prev = len(rows) > limit
        rows = rows[:limit][::-1]  # display order is newest first
        return AnchorPage(
            events=rows,
            next_anchor=_iso(rows[-1]) if rows else None,
            prev_anchor=_iso(rows[0]) if (rows and has_prev) else None,
            # The page we paged back from is still below the anchor.
            has_next=True,
            has_prev=has_prev,
        )

    if at is not None and direction == "center":
        half = limit // 2
        newer = eventstore.query(
            stream, time_range=(at + _TICK, None), filters=filters, limit=half + 1
        )
        before = list(newer.events)
        has_prev = len(before) > half
        before = before[:half][::-1]
        pinned = list(
            eventstore.query(
                stream, time_range=(at, at + _TICK), filters=filters, limit=limit
            ).events
        )[::-1]
        older = eventstore.query(
            stream,
            time_range=(None, at),
            filters=filters,
            limit=half + 1,
            reverse=True,
        )
        after = list(older.events)
        has_next = len(after) > half
        after = after[:half]
        rows = before + pinned + after
        return AnchorPage(
            events=rows,
            next_anchor=_iso(rows[-1]) if (rows and has_next) else None,
            prev_anchor=_iso(rows[0]) if (rows and has_prev) else None,
            has_next=has_next,
            has_prev=has_prev,
        )

    # "next" — and the unanchored first page, which is the same read with no
    # upper bound.
    page = eventstore.query(
        stream,
        time_range=(None, at) if at is not None else None,
        filters=filters,
        limit=limit + 1,
        reverse=True,
    )
    rows = list(page.events)
    has_next = len(rows) > limit
    rows = rows[:limit]
    return AnchorPage(
        events=rows,
        next_anchor=_iso(rows[-1]) if (rows and has_next) else None,
        prev_anchor=_iso(rows[0]) if (rows and at is not None) else None,
        has_next=has_next,
        has_prev=at is not None,
    )


def _iso(event) -> str | None:
    return event.ts.isoformat() if event.ts else None


__all__ = ["AnchorPage", "anchor_page"]
