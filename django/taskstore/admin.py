"""Read-only ops admin for comm Task records (admin-suite AS-3 §1.3)."""
from django.contrib import admin

from stapel_core.django.admin.base import StapelModelAdmin

from .models import TaskRecord


@admin.register(TaskRecord)
class TaskRecordAdmin(StapelModelAdmin):
    list_display = (
        "id", "kind", "state", "attempts", "max_attempts",
        "created_at", "started_at", "finished_at",
    )
    list_filter = ("state",)
    search_fields = ("kind", "correlation_id", "error")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
