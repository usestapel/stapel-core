"""Read-only ops admin for the transactional outbox (admin-suite AS-3 §1.3).

Registered always, visible per the ``ops`` declaration (clearance HIGH /
superuser; ``SHOW_OPS_MODELS`` for dev) — outbox debugging no longer needs
dbshell.
"""
from django.contrib import admin

from stapel_core.django.admin.base import StapelModelAdmin

from .models import OutboxEvent


@admin.register(OutboxEvent)
class OutboxEventAdmin(StapelModelAdmin):
    list_display = (
        "id", "topic", "created_at", "dispatched_at", "attempts", "next_attempt_at",
    )
    list_filter = ("topic",)
    search_fields = ("topic", "event_json", "last_error")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
