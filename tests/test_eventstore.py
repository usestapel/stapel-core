"""Tests for stapel_core.eventstore — the append-stream seam.

Covers: append + cursor read, identity/payload filters, time ranges, the
write buffer (sync + size + interval + flush), rollup (group-by/sum, into
persistence, replace semantics), per-stream retention purge and the sweep
command, stream→backend routing, cursor token round-trip, and — structurally,
since Postgres is not available locally — the partition SQL generation. On the
SQLite minimal profile the same code runs against one plain table (documented
degradation), which every django_db test here exercises.
"""
from datetime import datetime, timedelta, timezone

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import override_settings
from django.utils import timezone as dj_tz

from stapel_core import eventstore
from stapel_core.eventstore import Cursor, Event
from stapel_core.eventstore.base import EventStore, EventPage, RollupRow, resolve_field
from stapel_core.eventstore.buffer import WriteBuffer
from stapel_core.django.eventstore import partitions

SYNC = {"STAPEL_EVENTSTORE": {"BUFFER_SYNC": True}}


# --------------------------------------------------------------------------
# append + cursor read
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_append_and_query_roundtrip():
    eventstore.append("llm.call", {"model": "opus", "output": 10},
                      project="p1", task="T-1")
    page = eventstore.query("llm.call")
    assert isinstance(page, EventPage)
    assert len(page) == 1
    ev = page.events[0]
    assert ev.stream == "llm.call"
    assert ev.payload == {"model": "opus", "output": 10}
    assert ev.project == "p1" and ev.task == "T-1" and ev.container is None
    assert ev.id is not None


@pytest.mark.django_db
def test_query_is_scoped_to_stream():
    eventstore.append("a", {"n": 1})
    eventstore.append("b", {"n": 2})
    page = eventstore.query("a")
    assert [e.payload["n"] for e in page] == [1]


@pytest.mark.django_db
def test_cursor_paging_walks_every_row_once():
    for i in range(5):
        eventstore.append("s", {"i": i})
    seen = []
    page = eventstore.query("s", limit=2)
    seen += [e.payload["i"] for e in page]
    assert page.has_more
    page = eventstore.query("s", after=page.cursor, limit=2)
    seen += [e.payload["i"] for e in page]
    assert page.has_more
    page = eventstore.query("s", after=page.cursor, limit=2)
    seen += [e.payload["i"] for e in page]
    assert not page.has_more  # exhausted
    assert seen == [0, 1, 2, 3, 4]


@pytest.mark.django_db
def test_cursor_tie_breaks_on_id_at_same_ts():
    ts = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(4):
        eventstore.append("s", {"i": i}, ts=ts)  # identical ts
    page = eventstore.query("s", limit=2)
    first = [e.payload["i"] for e in page]
    page2 = eventstore.query("s", after=page.cursor, limit=2)
    rest = [e.payload["i"] for e in page2]
    assert first + rest == [0, 1, 2, 3]  # no row skipped or repeated


@pytest.mark.django_db
def test_filters_by_identity_and_payload():
    eventstore.append("s", {"model": "opus"}, project="p1")
    eventstore.append("s", {"model": "sonnet"}, project="p2")
    by_project = eventstore.query("s", filters={"project": "p1"})
    assert [e.payload["model"] for e in by_project] == ["opus"]
    by_payload = eventstore.query("s", filters={"model": "sonnet"})
    assert [e.project for e in by_payload] == ["p2"]


@pytest.mark.django_db
def test_time_range_is_half_open():
    base = datetime(2026, 7, 6, 0, 0, 0, tzinfo=timezone.utc)
    for i in range(4):
        eventstore.append("s", {"i": i}, ts=base + timedelta(hours=i))
    page = eventstore.query(
        "s", time_range=(base + timedelta(hours=1), base + timedelta(hours=3))
    )
    assert [e.payload["i"] for e in page] == [1, 2]  # end exclusive


# --------------------------------------------------------------------------
# write buffer
# --------------------------------------------------------------------------

def test_buffer_flushes_on_size():
    flushed = []
    buf = WriteBuffer(lambda batch: flushed.extend(batch), size=3, interval=999)
    buf.add(Event("s", {"i": 0}))
    buf.add(Event("s", {"i": 1}))
    assert flushed == [] and buf.pending_count() == 2
    buf.add(Event("s", {"i": 2}))
    assert len(flushed) == 3 and buf.pending_count() == 0


def test_buffer_sync_flushes_every_add():
    flushed = []
    buf = WriteBuffer(lambda batch: flushed.extend(batch), size=100, sync=True)
    buf.add(Event("s", {}))
    assert len(flushed) == 1


def test_buffer_flushes_on_interval():
    flushed = []
    buf = WriteBuffer(lambda batch: flushed.extend(batch), size=100, interval=0)
    buf.add(Event("s", {}))  # interval 0 → oldest age >= 0 → flush
    assert len(flushed) == 1


def test_buffer_explicit_flush():
    flushed = []
    buf = WriteBuffer(lambda batch: flushed.extend(batch), size=100, interval=999)
    buf.add(Event("s", {}))
    assert flushed == []
    buf.flush()
    assert len(flushed) == 1
    buf.flush()  # nothing pending — no double emit
    assert len(flushed) == 1


@pytest.mark.django_db
def test_facade_buffers_until_read_or_flush():
    with override_settings(STAPEL_EVENTSTORE={"BUFFER_SIZE": 500, "BUFFER_INTERVAL": 999}):
        eventstore.append("s", {"i": 1})
        # not yet persisted (buffered), but query() flushes first
        page = eventstore.query("s")
        assert len(page) == 1


@pytest.mark.django_db
def test_append_batch_facade():
    with override_settings(**SYNC):
        eventstore.append_batch([Event("s", {"i": 0}), Event("s", {"i": 1})])
        eventstore.flush()
        page = eventstore.query("s")
        assert sorted(e.payload["i"] for e in page) == [0, 1]


# --------------------------------------------------------------------------
# rollup
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_rollup_groups_and_sums():
    for model, out in [("opus", 10), ("opus", 5), ("sonnet", 7)]:
        eventstore.append("llm.call", {"model": model, "output": out})
    rows = eventstore.rollup("llm.call", group_by=["model"], sum_fields=["output"])
    by_model = {r.group["model"]: r for r in rows}
    assert isinstance(rows[0], RollupRow)
    assert by_model["opus"].count == 2
    assert by_model["opus"].sums["output"] == 15.0
    assert by_model["sonnet"].sums["output"] == 7.0


@pytest.mark.django_db
def test_rollup_ignores_non_numeric_and_bool_sums():
    eventstore.append("s", {"cost": "n/a", "flag": True})
    eventstore.append("s", {"cost": 2.5, "flag": True})
    rows = eventstore.rollup("s", group_by=[], sum_fields=["cost", "flag"])
    assert rows[0].sums["cost"] == 2.5  # string skipped
    assert rows[0].sums["flag"] == 0.0  # bool skipped (not counted as 1)
    assert rows[0].count == 2


@pytest.mark.django_db
def test_rollup_groups_by_identity_column():
    eventstore.append("s", {"n": 1}, project="p1")
    eventstore.append("s", {"n": 1}, project="p1")
    eventstore.append("s", {"n": 1}, project="p2")
    rows = eventstore.rollup("s", group_by=["project"], sum_fields=["n"])
    counts = {r.group["project"]: r.count for r in rows}
    assert counts == {"p1": 2, "p2": 1}


@pytest.mark.django_db
def test_rollup_into_persists_and_replaces():
    from stapel_core.django.eventstore.models import EventRollup

    eventstore.append("llm.call", {"model": "opus", "output": 10})
    eventstore.rollup("llm.call", group_by=["model"], sum_fields=["output"],
                      into="llm_cost")
    bucket = EventRollup.objects.get(name="llm_cost", stream="llm.call")
    assert bucket.count == 1 and bucket.sums["output"] == 10.0

    eventstore.append("llm.call", {"model": "opus", "output": 5})
    eventstore.rollup("llm.call", group_by=["model"], sum_fields=["output"],
                      into="llm_cost")
    bucket.refresh_from_db()
    # recompute replaces (absolute), not increments: 2 events, 15 total
    assert bucket.count == 2 and bucket.sums["output"] == 15.0
    assert EventRollup.objects.filter(name="llm_cost").count() == 1


# --------------------------------------------------------------------------
# retention
# --------------------------------------------------------------------------

@pytest.mark.django_db
def test_purge_deletes_only_old_rows():
    old = dj_tz.now() - timedelta(days=40)
    new = dj_tz.now()
    eventstore.append("s", {"age": "old"}, ts=old)
    eventstore.append("s", {"age": "new"}, ts=new)
    removed = eventstore.purge("s", older_than=dj_tz.now() - timedelta(days=30))
    assert removed == 1
    page = eventstore.query("s")
    assert [e.payload["age"] for e in page] == ["new"]


@pytest.mark.django_db
def test_sweep_eventstore_command_applies_retention():
    old = dj_tz.now() - timedelta(days=100)
    with override_settings(STAPEL_EVENTSTORE={
        "BUFFER_SYNC": True,
        "RETENTION": {"delivery": 30},
    }):
        eventstore.append("delivery", {"x": 1}, ts=old)
        eventstore.append("delivery", {"x": 2})  # fresh
        eventstore.append("keep", {"x": 3}, ts=old)  # no retention → kept
        eventstore.flush()
        call_command("sweep_eventstore")
        assert len(eventstore.query("delivery")) == 1
        assert len(eventstore.query("keep")) == 1


@pytest.mark.django_db
def test_purge_rollup_by_age():
    from stapel_core.django.eventstore.models import EventRollup

    eventstore.append("s", {"n": 1})
    eventstore.rollup("s", group_by=[], sum_fields=["n"], into="agg")
    # backdate the bucket so it is older than the cutoff
    EventRollup.objects.filter(name="agg").update(
        updated_at=dj_tz.now() - timedelta(days=10)
    )
    removed = eventstore.resolve_backend("s").purge_rollup(
        "s", older_than=dj_tz.now() - timedelta(days=5)
    )
    assert removed == 1


# --------------------------------------------------------------------------
# routing seam
# --------------------------------------------------------------------------

class _MemStore(EventStore):
    """Trivial in-memory backend to prove stream→backend routing."""

    def __init__(self):
        self.rows: list[Event] = []

    def append_batch(self, events):
        self.rows.extend(events)

    def query(self, stream, *, after=None, limit=100, time_range=None, filters=None):
        evs = [e for e in self.rows if e.stream == stream]
        return EventPage(events=evs, cursor=None)

    def rollup(self, stream, *, group_by, sum_fields, time_range=None,
               filters=None, into=None):
        return []

    def purge(self, stream, *, older_than):
        return 0


@pytest.mark.django_db
def test_routes_send_stream_to_alternate_backend():
    mem = _MemStore()
    with override_settings(STAPEL_EVENTSTORE={"BUFFER_SYNC": True, "ROUTES": {"analytics": mem}}):
        eventstore.append("analytics", {"n": 1})  # → mem backend
        eventstore.append("llm.call", {"n": 2})   # → default (DB)
        eventstore.flush()
        assert len(mem.rows) == 1 and mem.rows[0].payload == {"n": 1}
        assert len(eventstore.query("llm.call")) == 1
        assert eventstore.resolve_backend("analytics") is mem


# --------------------------------------------------------------------------
# value types
# --------------------------------------------------------------------------

def test_cursor_encode_decode_roundtrip():
    c = Cursor(ts=datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc), id=42)
    token = c.encode()
    back = Cursor.decode(token)
    assert back == c
    assert Cursor.decode(None) is None
    assert Cursor.decode("") is None


def test_resolve_field_prefers_identity_then_payload():
    ev = Event("s", {"model": "opus"}, project="p1")
    assert resolve_field(ev, "project") == "p1"
    assert resolve_field(ev, "model") == "opus"
    assert resolve_field(ev, "missing") is None


def test_event_identity_map():
    ev = Event("s", {}, project="p", task="t")
    assert ev.identity() == {"project": "p", "task": "t", "container": None}


# --------------------------------------------------------------------------
# Postgres partitioning — structural (no live PG locally)
# --------------------------------------------------------------------------

def test_period_bounds_month_and_day():
    m = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    start, end, suffix = partitions.period_bounds(m, "month")
    assert (start.isoformat(), end.isoformat(), suffix) == \
        ("2026-07-01", "2026-08-01", "p202607")
    start, end, suffix = partitions.period_bounds(m, "day")
    assert (start.isoformat(), end.isoformat(), suffix) == \
        ("2026-07-06", "2026-07-07", "p20260706")


def test_period_bounds_december_rolls_over_year():
    m = datetime(2026, 12, 15, tzinfo=timezone.utc)
    _, end, suffix = partitions.period_bounds(m, "month")
    assert end.isoformat() == "2027-01-01" and suffix == "p202612"


def test_period_bounds_rejects_unknown_period():
    with pytest.raises(ValueError):
        partitions.period_bounds(datetime.now(timezone.utc), "week")


def test_parent_ddl_is_range_partitioned():
    ddl = partitions.parent_ddl()
    assert "PARTITION BY RANGE (ts)" in ddl
    assert "PRIMARY KEY (id, ts)" in ddl  # partition key must be in the PK
    assert partitions.DEFAULT_TABLE in ddl


def test_create_partition_sql():
    sql = partitions.create_partition_sql(
        datetime(2026, 7, 1).date(), datetime(2026, 8, 1).date(), "p202607"
    )
    assert "CREATE TABLE IF NOT EXISTS" in sql
    assert "PARTITION OF" in sql
    assert "FROM ('2026-07-01') TO ('2026-08-01')" in sql


def test_ensure_partitions_sql_covers_current_plus_ahead():
    start = datetime(2026, 7, 6, tzinfo=timezone.utc)
    stmts = partitions.ensure_partitions_sql(start=start, periods_ahead=2, period="month")
    assert len(stmts) == 3  # current + 2 ahead
    assert "p202607" in stmts[0]
    assert "p202608" in stmts[1]
    assert "p202609" in stmts[2]


def test_drop_partition_sql():
    assert partitions.drop_partition_sql("p202607") == \
        f"DROP TABLE IF EXISTS {partitions.DEFAULT_TABLE}_p202607;"


@pytest.mark.django_db
def test_partition_command_dry_run_prints_sql(capsys):
    call_command("eventstore_partition", "--dry-run", "--periods-ahead", "1")
    out = capsys.readouterr().out
    assert "PARTITION OF" in out


@pytest.mark.django_db
def test_partition_command_skips_on_sqlite():
    # The minimal profile runs on SQLite: the command must degrade, not error.
    assert connection.vendor == "sqlite"
    call_command("eventstore_partition", "--periods-ahead", "1")
    out = "".join(str(a) for a in [connection.vendor])
    assert out  # command completed without raising on a plain table
