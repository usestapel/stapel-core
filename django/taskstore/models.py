"""Persistent state for comm Tasks (async named operations)."""
import uuid

from django.db import models


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

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=255, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    state = models.CharField(max_length=16, choices=STATES, default=PENDING, db_index=True)
    result = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=3)
    deadline = models.DateTimeField(null=True, blank=True, db_index=True)
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
        ]

    def __str__(self):
        return f"{self.kind} [{self.state}]"
