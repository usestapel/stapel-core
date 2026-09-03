"""Persistent state for comm Tasks (async named operations)."""
import uuid

from django.db import models

from stapel_core.access.declaration import access  # light module — see outbox


@access.ops  # task journal: hidden below HIGH, read-only in admin (AS-3)
class TaskRecord(models.Model):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    STATES = [
        (PENDING, "Pending"),
        (RUNNING, "Running"),
        (DONE, "Done"),
        (FAILED, "Failed"),
    ]

    # ── Why a parked task needs a REASON and not just an error string ──
    #
    # `error` holds `repr(exc)` — the right thing to read once you are
    # already looking at the row, and useless for everything you do before
    # that. It cannot be a metric label (unbounded), it cannot be grouped in
    # SQL without LIKE patterns that rot, and it cannot answer the one
    # question an operator has at 3am: is this the provider being down, a
    # payload nothing will ever accept, or a task routed to a service that
    # does not own its kind? Those three need different people to do
    # different things, and until now they were one indistinguishable pile
    # of FAILED rows.
    #
    # The values mirror the bus DLQ's `REASONS` where they mean the same
    # thing, because they end up in the same metric (`bus_dlq_total`) and a
    # split vocabulary across two producers of one series is a worse
    # problem than a slightly loose fit.
    REASON_HANDLER = "handler"  # handler raised; attempts exhausted
    REASON_UNPROCESSABLE = "unprocessable"  # refused the VALUES — never retried
    REASON_NO_HANDLER = "no_handler"  # announced to a process that does not own it
    REASON_DEADLINE = "deadline_exceeded"  # the sweep gave up on it
    FAILURE_REASONS = [
        (REASON_HANDLER, "Handler error"),
        (REASON_UNPROCESSABLE, "Unprocessable payload"),
        (REASON_NO_HANDLER, "No local handler"),
        (REASON_DEADLINE, "Deadline exceeded"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=255, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    state = models.CharField(max_length=16, choices=STATES, default=PENDING, db_index=True)
    result = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=3)
    deadline = models.DateTimeField(null=True, blank=True, db_index=True)
    # Named cause of a FAILED row. Blank on every other state, and blank on
    # rows parked before this column existed — an empty reason means "not
    # recorded", never "no reason".
    failure_reason = models.CharField(
        max_length=32, blank=True, default="", choices=FAILURE_REASONS, db_index=True
    )
    # Earliest time this task may be CLAIMED. NULL means "now" — the shape a
    # first attempt has, so nothing about the initial dispatch changes.
    # A retry sets it forward by the jittered backoff, and `execute()`
    # declines to claim before it: the delay lives in the RECORD rather than
    # in a sleeping worker, so it survives a crash, a redeploy, and a
    # redelivery of the announcement.
    not_before = models.DateTimeField(null=True, blank=True)
    # Caller-supplied idempotency key. A second start() with a key that is
    # already live returns the FIRST task's id instead of creating a second
    # one — the difference between a retried publish costing one provider
    # call and costing two. Empty (the default) opts out, which is why the
    # uniqueness below is a partial index and not a plain unique=True.
    dedupe_key = models.CharField(max_length=255, blank=True, default="")
    correlation_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    # comm Function name invoked with the outcome (best-effort)
    callback = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        # Pinned to the historical name so the 0.8.0 app-label rename
        # (stapel_tasks -> stapel_taskstore) leaves the physical table
        # untouched. Table names are internal (not a public contract); the
        # label is what collided with the stapel-tasks module.
        db_table = "stapel_tasks_taskrecord"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["state", "deadline"], name="taskrec_deadline_idx"),
            # The sweep's other query: PENDING rows whose backoff has expired.
            models.Index(fields=["state", "not_before"], name="taskrec_notbefore_idx"),
        ]
        constraints = [
            # Partial: only rows that opted in. A plain unique=True would
            # make the empty string a value and let exactly one task in the
            # whole table have no key.
            models.UniqueConstraint(
                fields=["dedupe_key"],
                condition=models.Q(state__in=["pending", "running"])
                & ~models.Q(dedupe_key=""),
                name="taskrec_live_dedupe_key",
            ),
        ]

    def __str__(self):
        return f"{self.kind} [{self.state}]"
