"""Settings namespace for the event store (``STAPEL_EVENTSTORE``)."""
from stapel_core.conf import AppSettings

eventstore_settings = AppSettings(
    "STAPEL_EVENTSTORE",
    defaults={
        # Backend seam (replace-style): dotted path to an EventStore subclass
        # (or instance). Default is the zero-infrastructure Postgres backend —
        # a partitionable table that also degrades to a plain table on the
        # SQLite minimal profile. ClickHouse is the documented scale-out point.
        "BACKEND": "stapel_core.eventstore.backends.postgres.PostgresEventStore",
        # Per-stream backend routing (merge-routing by stream name, like
        # bus-routing): {"analytics": "…ClickHouseEventStore", "audit": "…"}.
        # A stream not listed falls back to BACKEND. Values are dotted paths.
        "ROUTES": {},
        # Write buffer: never INSERT per event. Flush when the buffer reaches
        # BUFFER_SIZE rows or when BUFFER_INTERVAL seconds have elapsed since
        # the oldest buffered event (checked on append and on flush()).
        "BUFFER_SIZE": 500,
        "BUFFER_INTERVAL": 5.0,
        # Write-through mode: flush every append immediately. Turn on for
        # tests and low-volume streams where read-your-writes matters more
        # than batching. (Reads always flush first regardless.)
        "BUFFER_SYNC": False,
        # Per-stream raw retention in days: {"delivery": 30, "analytics": 90}.
        # A stream absent here is kept forever. Applied by `manage.py
        # sweep_eventstore`.
        "RETENTION": {},
        # Per-stream rollup retention in days (raw retention ≠ rollup
        # retention: rollups are small and usually kept much longer).
        "RETENTION_ROLLUP": {},
        # Time-partition granularity for the Postgres backend: "month" or
        # "day". Structural only outside PostgreSQL (SQLite stays one table).
        "PARTITION_PERIOD": "month",
    },
    # BACKEND/ROUTES pick which store code runs and where a stream lands —
    # generic names that must never be silently overridden by a stray env var.
    no_env=("BACKEND", "ROUTES"),
)

__all__ = ["eventstore_settings"]
