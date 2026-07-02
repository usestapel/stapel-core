"""Fail comm Tasks past their deadline (run via cron / celery beat).

    python manage.py sweep_tasks
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from stapel_core.comm.actions import emit
from stapel_core.comm.tasks import TASK_FAILED
from stapel_core.django.taskstore.models import TaskRecord


class Command(BaseCommand):
    help = "Mark comm Tasks past their deadline as failed and announce it."

    def handle(self, *args, **options):
        now = timezone.now()
        expired = TaskRecord.objects.filter(
            state__in=[TaskRecord.PENDING, TaskRecord.RUNNING],
            deadline__isnull=False,
            deadline__lte=now,
        )
        count = 0
        for record in expired:
            record.state = TaskRecord.FAILED
            record.error = "deadline exceeded"
            record.finished_at = now
            record.save(update_fields=["state", "error", "finished_at"])
            emit(
                TASK_FAILED,
                {
                    "task_id": str(record.pk),
                    "kind": record.kind,
                    "error": "deadline exceeded",
                    "correlation_id": record.correlation_id,
                },
            )
            count += 1
        self.stdout.write(f"sweep_tasks: failed {count} expired task(s)")
