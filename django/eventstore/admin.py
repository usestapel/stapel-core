"""Read-only ops admins for the event store (admin-suite AS-3 §1.3)."""
from django.contrib import admin

from stapel_core.django.admin.base import StapelModelAdmin

from .models import EventRecord, EventRollup


@admin.register(EventRecord)
class EventRecordAdmin(StapelModelAdmin):
    list_display = ("id", "stream", "ts", "project", "task", "container")
    search_fields = ("stream", "project", "task", "container")
    date_hierarchy = "ts"
    ordering = ("-ts", "-id")


@admin.register(EventRollup)
class EventRollupAdmin(StapelModelAdmin):
    list_display = ("id", "name", "stream", "group_key", "count", "updated_at")
    search_fields = ("name", "stream", "group_key")
    ordering = ("name", "stream")
