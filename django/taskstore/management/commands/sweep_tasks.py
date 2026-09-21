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

**Fail tasks past their deadline.** The row records WHY it was failed
(``deadline_exceeded``) rather than only a sentence in a text column — and
a task is never failed while a finished answer is sitting unread.

    A PAID RESULT THAT EXISTS MUST NOT EXPIRE UNREAD.

A deadline says "we waited long enough", not "nothing was produced". On
2026-09-20, on a client host, two tasks reached their deadline holding
nothing while the provider's answers — one transcription of a 148-minute
meeting, one six-call summary, both charged — sat in the shared store,
persisted before a publish that no process was left to hear. Both were
buried as ``deadline_exceeded``; both had to be bought again by hand.

So before failing, the sweep asks the store whether any of this task's
calls has already been answered (``recoverable_replies``). If one has, the
task gets its attempt back and a grace window instead of an epitaph: the
re-run's ``call()`` reads the stored reply and returns it without touching
the provider (comm/nats.py), so the recovery costs a read, not a bill.
"""
import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from stapel_core.comm.actions import mutate_and_emit
from stapel_core.comm.config import comm_setting
from stapel_core.comm.tasks import (
    DEFAULT_RECOVERY_GRACE_SECONDS,
    TASK_FAILED,
    TASK_FAILED_METRIC,
    TASK_REQUESTED,
    _metric,
    _park_in_dlq,
    recoverable_replies,
)
from stapel_core.django.taskstore.models import TaskRecord

logger = logging.getLogger(__name__)

#: Tasks whose deadline was extended because a finished reply was waiting.
#: A rising line here is a transport dropping answers — invisible from the
#: task table alone, where a recovery looks like an ordinary retry.
RECOVERED_METRIC = "comm_task_reply_recovered_total"

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
        recovered = 0
        for record in expired:
            waiting = recoverable_replies(record)
            if waiting and record.attempts < record.max_attempts:
                # NOT A FAILURE. The work exists; only its delivery was
                # lost. Give the row its attempt back and re-announce it —
                # the next run resolves these calls from the store.
                grace = float(
                    comm_setting(
                        "TASK_RECOVERY_GRACE_SECONDS",
                        DEFAULT_RECOVERY_GRACE_SECONDS,
                    )
                )
                logger.error(
                    "task %s (%s) reached its deadline with %d finished "
                    "reply(ies) waiting unread in the overflow store (%s) — "
                    "re-announcing with %.0fs of grace instead of failing "
                    "work that has already been done and paid for. A reply "
                    "was produced and never delivered: check the transport "
                    "and whether the caller's process was restarted mid-call.",
                    record.pk, record.kind, len(waiting),
                    ", ".join(fn for fn, _ in waiting), grace,
                )
                with mutate_and_emit() as emit_event:
                    record.state = TaskRecord.PENDING
                    record.not_before = None
                    record.deadline = now + timedelta(seconds=grace)
                    record.save(
                        update_fields=["state", "not_before", "deadline"]
                    )
                    emit_event(
                        TASK_REQUESTED,
                        {"task_id": str(record.pk), "kind": record.kind},
                        key=record.correlation_id or str(record.pk),
                    )
                _metric(
                    "counter", RECOVERED_METRIC, labels={"kind": record.kind}
                )
                recovered += 1
                continue

            if waiting:
                logger.error(
                    "task %s (%s) has %d finished reply(ies) in the overflow "
                    "store but has used all %d attempt(s) — failing it, and "
                    "the answers stay on the shelf until the TTL for a "
                    "manual re-run",
                    record.pk, record.kind, len(waiting), record.max_attempts,
                )
            else:
                logger.error(
                    "task %s (%s) failed: deadline exceeded after %d "
                    "attempt(s); no finished reply was waiting in the "
                    "overflow store",
                    record.pk, record.kind, record.attempts,
                )
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
            f"recovered {recovered} with a reply waiting, "
            f"re-announced {woken} due retry(ies)"
        )
