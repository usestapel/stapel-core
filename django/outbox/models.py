"""Transactional outbox for Action events (docs: module-communication.md).

A row is inserted in the same transaction as the business mutation; the
event therefore exists iff the transaction committed. Delivery marks
dispatched_at; failures accumulate attempts with exponential backoff and
are retried by the relay until they succeed.
"""
from django.db import models


class OutboxEvent(models.Model):
    topic = models.CharField(max_length=255, db_index=True)
    event_json = models.TextField(help_text="Serialized stapel_core.bus.Event")
    created_at = models.DateTimeField(auto_now_add=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(auto_now_add=True, db_index=True)
    last_error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(
                fields=["dispatched_at", "next_attempt_at"],
                name="outbox_pending_idx",
            ),
        ]

    def __str__(self):
        state = "dispatched" if self.dispatched_at else f"pending({self.attempts})"
        return f"{self.topic} [{state}]"
