"""The sweep has to be SCHEDULED, and a deployment has to be told when it isn't.

Found the hard way: on the fleet this was written for, `sweep_tasks` was
scheduled in no service, no beat file and no crontab, and had therefore
never run once. Nothing looked wrong — until 0.60, when the sweep became
the thing that wakes a held retry, at which point its absence would have
turned every transient failure into a task that says "retrying" and never
does. A driver nobody wired is the failure mode this check exists for.
"""
import pytest

from stapel_core.django.taskstore.beat import (
    SWEEP_BEAT_KEY,
    SWEEP_TASK_NAME,
    get_taskstore_beat_schedule,
)
from stapel_core.django.taskstore.checks import check_task_sweep_is_scheduled


def test_schedule_names_the_stable_task_name():
    entry = get_taskstore_beat_schedule()[SWEEP_BEAT_KEY]
    assert entry["task"] == SWEEP_TASK_NAME


def test_cadence_is_overridable():
    assert get_taskstore_beat_schedule(seconds=15)[SWEEP_BEAT_KEY]["schedule"] == 15


def test_no_beat_in_this_process_is_not_a_finding(settings):
    """The sweep only has to run SOMEWHERE. Warning in every web container of
    a correctly configured fleet is how a check becomes noise, and then
    becomes ignored."""
    settings.CELERY_BEAT_SCHEDULE = {}
    assert check_task_sweep_is_scheduled(None) == []


def test_beat_without_the_sweep_warns(settings):
    settings.CELERY_BEAT_SCHEDULE = {"something-else": {"task": "app.other"}}
    issues = check_task_sweep_is_scheduled(None)
    assert [i.id for i in issues] == ["stapel_taskstore.W001"]
    # The hint has to say what to add, not that something is missing.
    assert "get_taskstore_beat_schedule" in issues[0].hint


def test_beat_with_the_sweep_is_silent(settings):
    settings.CELERY_BEAT_SCHEDULE = {
        **get_taskstore_beat_schedule(),
        "something-else": {"task": "app.other"},
    }
    assert check_task_sweep_is_scheduled(None) == []


def test_a_host_that_renamed_the_entry_is_not_nagged(settings):
    """Matched on the task NAME, not on our key. A check that only
    recognises its own spelling nags deployments that did the right thing
    — the fastest way to teach people to silence checks."""
    settings.CELERY_BEAT_SCHEDULE = {
        "my-own-name-for-it": {"task": SWEEP_TASK_NAME, "schedule": 30.0}
    }
    assert check_task_sweep_is_scheduled(None) == []


@pytest.mark.django_db(transaction=True)
def test_the_callable_runs_the_command():
    """A host without Celery schedules the plain callable from cron; it must
    actually be a working entry point, not a name."""
    from stapel_core.django.taskstore.beat import sweep_tasks

    # shared_task-wrapped when celery is installed; both shapes are callable.
    runner = getattr(sweep_tasks, "run", sweep_tasks)
    runner()
