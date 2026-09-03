"""Keep the comm Task table moving (run via cron / celery beat).

    python manage.py sweep_tasks

Two jobs, both of which are "nobody else will do this":

**Re-announce due retries.** A failed attempt sets ``not_before`` and
re-announces, but the announcement is the part that can be lost — a
consumer that crashes between the outbox relay and the claim, a
redelivery that arrives while the backoff is still running and is
correctly declined. Either way the row sits PENDING with its backoff long
expired and nothing ever looks at it again. Before this sweep re-announced
them, a Task's retry ladder depended on a message surviving; now the ladder
is a column and the message is an optimisation.

**Fail tasks past their deadline.** Unchanged, except that the row now
records WHY it was failed (``deadline_exceeded``) rather than only a
sentence in a text column.
"""
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from stapel_core.comm.actions import mutate_and_emit
from stapel_core.comm.tasks import TASK_FAILED, TASK_REQUESTED, _park_in_dlq, _metric
from stapel_core.comm.tasks import TASK_FAILED_METRIC
from stapel_core.django.taskstore.models import TaskRecord

#: Cap on re-announcements per run. A sweep that tries to wake ten thousand
#: rows in one pass is itself the incident: it would republish the whole
#: backlog into a broker in a burst, which is the load spike the backoff
#: exists to prevent. The remainder is picked up next run.
DEFAULT_BATCH = 500


class Command(BaseCommand):
    help = (
        "Re-announce comm Tasks whose retry backoff has expired, and fail "
        "those past their deadline."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch",
            type=int,
            default=DEFAULT_BATCH,
            help=f"Max retries re-announced per run (default {DEFAULT_BATCH}).",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        expired = TaskRecord.objects.filter(
            state__in=[TaskRecord.PENDING, TaskRecord.RUNNING],
            deadline__isnull=False,
            deadline__lte=now,
        )
        count = 0
        for record in expired:
            # FAILED state + task.failed event commit together, per record —
            # a crash mid-sweep leaves the rest expired (next run catches
            # them), never failed-but-unannounced.
            with mutate_and_emit() as emit_event:
                record.state = TaskRecord.FAILED
                record.error = "deadline exceeded"
                record.failure_reason = TaskRecord.REASON_DEADLINE
                record.finished_at = now
                record.save(
                    update_fields=[
                        "state", "error", "failure_reason", "finished_at"
                    ]
                )
                emit_event(
                    TASK_FAILED,
                    {
                        "task_id": str(record.pk),
                        "kind": record.kind,
                        "error": "deadline exceeded",
                        "reason": TaskRecord.REASON_DEADLINE,
                        "correlation_id": record.correlation_id,
                    },
                )
            _metric(
                "counter",
                TASK_FAILED_METRIC,
                labels={"kind": record.kind, "reason": TaskRecord.REASON_DEADLINE},
            )
            _park_in_dlq(record.kind, TaskRecord.REASON_DEADLINE)
            count += 1

        # Retries whose hold has expired. `attempts__gt=0` keeps first
        # attempts out of it: a brand-new task's announcement is somebody
        # else's job, and re-announcing it here would double-dispatch every
        # task created between two sweeps.
        due = TaskRecord.objects.filter(
            state=TaskRecord.PENDING,
            attempts__gt=0,
        ).filter(Q(not_before__isnull=True) | Q(not_before__lte=now))[
            : max(1, int(options["batch"]))
        ]
        woken = 0
        for record in due:
            with mutate_and_emit() as emit_event:
                emit_event(
                    TASK_REQUESTED,
                    {"task_id": str(record.pk), "kind": record.kind},
                    key=record.correlation_id or str(record.pk),
                )
            woken += 1

        self.stdout.write(
            f"sweep_tasks: failed {count} expired task(s), "
            f"re-announced {woken} due retry(ies)"
        )
