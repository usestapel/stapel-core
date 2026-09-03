"""The beat entry that makes the Task ladder actually climb.

``sweep_tasks`` has existed since the Task primitive shipped, and on the
fleet this was written for it had **never been scheduled** — not in any
service, not in any beat file, not in cron. Nothing was obviously broken,
because until 0.60 a failed task re-announced itself instantly and the
ladder ran without help; the only casualty was ``deadline_seconds``, which
quietly meant nothing at all in every deployment.

From 0.60 that changes, and it changes in a direction that would be a
REGRESSION without this module. A retry is now held on ``not_before``, and
the thing that wakes a held retry is this sweep. Ship the backoff without
the sweep and every transient failure becomes a task that says "retrying"
forever and never retries — strictly worse than the storm it replaced.

So the schedule is shipped BY THE LIBRARY that owns the column, as a
splat a host merges, and a system check (``stapel_taskstore.W001``) fires
in any process that runs beat without it. A retry mechanism whose driver
is documented in a README is a retry mechanism that some deployment will
not have.

Wire it in::

    from stapel_core.django.taskstore.beat import get_taskstore_beat_schedule

    CELERY_BEAT_SCHEDULE = {
        **get_taskstore_beat_schedule(),
        ...
    }

Celery is OPTIONAL. ``manage.py sweep_tasks`` is a plain management
command any scheduler (cron, systemd timer, k8s CronJob) can run.
"""
from __future__ import annotations

#: The task name a beat schedule must reference (stable across refactors).
SWEEP_TASK_NAME = "stapel_core.django.taskstore.sweep_tasks"

#: Key of the shipped entry, so a host overrides the cadence by writing the
#: same key after the splat.
SWEEP_BEAT_KEY = "comm-task-sweep"

#: Every 60 seconds, and the number is a floor rather than a preference: it
#: is the granularity of the retry ladder. A task whose backoff expires one
#: second after a sweep waits until the next one, so the sweep interval is
#: added to every retry delay. At 60s a first retry nominally due in ~2s
#: actually happens within ~62s, which is fine for asynchronous screening
#: and is why the seller-facing draft does not depend on it (the composer
#: bounds its own wait and hands over the manual form).
SWEEP_INTERVAL_SECONDS = 60


def sweep_tasks() -> None:
    """Re-announce due retries and fail tasks past their deadline.

    A plain callable, so a host with no Celery can schedule it directly.
    """
    from django.core.management import call_command

    call_command("sweep_tasks")


def get_taskstore_beat_schedule(*, seconds: int = SWEEP_INTERVAL_SECONDS) -> dict:
    """Beat entry for the Task sweep. Add to ``CELERY_BEAT_SCHEDULE``."""
    return {
        SWEEP_BEAT_KEY: {
            "task": SWEEP_TASK_NAME,
            "schedule": float(seconds),
        },
    }


try:  # pragma: no cover — exercised by whichever profile the host installs
    from celery import shared_task
except ImportError:
    pass
else:
    sweep_tasks = shared_task(name=SWEEP_TASK_NAME)(sweep_tasks)


__all__ = [
    "SWEEP_BEAT_KEY",
    "SWEEP_INTERVAL_SECONDS",
    "SWEEP_TASK_NAME",
    "get_taskstore_beat_schedule",
    "sweep_tasks",
]
