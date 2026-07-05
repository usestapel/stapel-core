"""Storage for the default Postgres event-store backend.

``EventRecord`` is the append-only stream table: an indexed ``stream`` name,
an event ``ts``, a JSON ``payload``, and the generic nullable identity
columns (``project``/``task``/``container``) promoted out of the payload for
cheap slicing. On PostgreSQL this table is partitioned by ``ts`` range (see
``partitions.py`` / the ``eventstore_partition`` command); on the SQLite
minimal profile it degrades to one plain table — same rows, no partitions.

``EventRollup`` holds pre-aggregated buckets so dashboards read summaries
instead of scanning raw events (raw retention ≠ rollup retention).
"""
from django.db import models


class EventRecord(models.Model):
    id = models.BigAutoField(primary_key=True)
    stream = models.CharField(max_length=255, db_index=True)
    ts = models.DateTimeField(db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    # Generic identity — nullable; a stream uses only what it has.
    project = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    task = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    container = models.CharField(max_length=255, null=True, blank=True, db_index=True)

    class Meta:
        # (stream, ts, id) is the read path: filter by stream, page by ts with
        # id as the tie-break — matches the cursor order exactly.
        indexes = [
            models.Index(fields=["stream", "ts", "id"], name="evt_stream_ts_idx"),
        ]

    def __str__(self):
        return f"{self.stream}@{self.ts:%Y-%m-%dT%H:%M:%S}"


class EventRollup(models.Model):
    id = models.BigAutoField(primary_key=True)
    # The rollup table name (``into=``) this bucket belongs to — one physical
    # table serves many named rollups, keyed by this column.
    name = models.CharField(max_length=255, db_index=True)
    stream = models.CharField(max_length=255, db_index=True)
    # Canonical JSON of the group-by field→value map (sorted keys) — the
    # uniqueness key of a bucket within (name, stream).
    group_key = models.CharField(max_length=1024)
    group = models.JSONField(default=dict, blank=True)
    count = models.BigIntegerField(default=0)
    sums = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["name", "stream", "group_key"],
                name="evt_rollup_bucket_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["name", "stream"], name="evt_rollup_name_idx"),
        ]

    def __str__(self):
        return f"{self.name}:{self.stream} {self.group_key}"
