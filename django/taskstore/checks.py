"""System checks for the comm Task journal.

- ``stapel_taskstore.W001`` — this process runs a beat schedule and the
  Task sweep is not in it. Retries are held on ``not_before`` from 0.60,
  and the sweep is what wakes them: without it, a transient failure
  produces a task that reports "retrying" forever and never retries.

Warning rather than Error, and the reason is the rollout: a fleet upgrades
service by service, and the first service on 0.60 must not refuse to start
because a sibling's settings have not been edited yet. But it says so at
every boot, because the failure it describes is silent — a stalled retry
looks exactly like a slow one, and the queue it stalls in has no depth
alarm of its own.
"""
from __future__ import annotations

from django.core import checks

from .beat import SWEEP_BEAT_KEY, SWEEP_TASK_NAME


def _beat_schedule():
    """The host's ``CELERY_BEAT_SCHEDULE``, or ``{}`` when it has none."""
    from django.conf import settings

    return getattr(settings, "CELERY_BEAT_SCHEDULE", None) or {}


def _sweep_is_scheduled(schedule) -> bool:
    """Whether any entry points at the sweep, however it was keyed.

    Matched on the TASK NAME rather than on our own key: a host is entitled
    to name the entry whatever it likes, and a check that only recognises
    its own key would nag a deployment that did the right thing under a
    different name — the fastest way to teach people to silence checks.
    """
    if SWEEP_BEAT_KEY in schedule:
        return True
    for entry in schedule.values():
        if isinstance(entry, dict) and entry.get("task") == SWEEP_TASK_NAME:
            return True
    return False


@checks.register("stapel_taskstore")
def check_task_sweep_is_scheduled(app_configs, **kwargs):
    schedule = _beat_schedule()
    if not schedule:
        # No beat in this process at all. That is the normal shape for a web
        # container, and the sweep only has to run SOMEWHERE in the
        # deployment — warning here would fire in every process of a fleet
        # that is correctly configured, which is how a check becomes noise
        # and then becomes ignored.
        return []
    if _sweep_is_scheduled(schedule):
        return []
    return [
        checks.Warning(
            "This process runs a Celery beat schedule and the comm Task "
            "sweep is not in it.",
            hint=(
                "Add it:\n\n"
                "    from stapel_core.django.taskstore.beat import "
                "get_taskstore_beat_schedule\n\n"
                "    CELERY_BEAT_SCHEDULE = {\n"
                "        **get_taskstore_beat_schedule(),\n"
                "        ...\n"
                "    }\n\n"
                "The sweep does two things nothing else does: it wakes a "
                "retry whose backoff has expired (since 0.60 a failed task "
                "waits on `not_before` instead of re-announcing instantly, "
                "so without the sweep it waits forever), and it fails tasks "
                "past their `deadline_seconds` — which, in a deployment that "
                "has never scheduled this, has never meant anything. Without "
                "Celery, run `manage.py sweep_tasks` from cron at the same "
                "cadence."
            ),
            id="stapel_taskstore.W001",
        )
    ]
