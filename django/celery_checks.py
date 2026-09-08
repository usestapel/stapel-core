"""A service's Celery worker must consume its OWN queue (tag ``stapel_celery``).

Every stapel service in a fleet gets its own Celery app (``Celery("<slug>")``
in ``config/celery.py``) and its own default queue
(``CELERY_TASK_DEFAULT_QUEUE`` in ``config/settings/base.py``), and they share
one broker. That arrangement is correct and needs no routing table: a bare
``celery -A config worker`` consumes exactly ``task_default_queue``, and a
service's beat publishes onto the same one, so a scheduled task can only ever
reach the worker that has its code.

It holds only while the two names agree, and nothing checked that they did.
Measured on a client stand, 2026-09-08: one service's settings carried a
neighbour's queue name — one word, copied with the settings file — and the
consequences were entirely silent to everyone except whoever opened a log.

- Its beat published ~360 sweeps an hour onto the neighbour's queue.
- Both workers consumed that queue, so ~63% of them reached the worker with
  no such task registered: 226 tracebacks an hour, and the task ran at 37% of
  its schedule while reporting nothing wrong.
- Worse, and invisible: tasks BOTH services have registered — this library's
  own ``stapel_core.django.taskstore.sweep_tasks`` — were executed by the
  wrong process against the wrong database. In that hour one service's task
  store was swept sixty times by a schedule it does not own, and the
  publishing service's own store was not swept at all. No traceback, no
  metric, no 5xx.

Fixing the one word repairs the deployment; it does not close the class,
because the next copied settings file reopens it. This check closes it: the
mismatch is refused at ``manage.py check``, which is boot smoke and CI for
every service in every fleet, so the wrong thing cannot reach a broker.

E-level, deliberately. The damage is silent by construction — a worker
running a neighbour's task against its own database is a correct-looking
process — so a warning would join the noise it is meant to interrupt, which
is exactly how this survived. A deployment that really does name its queue
something other than its app puts the id in ``SILENCED_SYSTEM_CHECKS``; the
hint says so.
"""
from __future__ import annotations

from django.core import checks

E001_QUEUE_NAMES_ANOTHER_APP = "stapel_core.celery.E001"
W002_QUEUE_IS_THE_SHARED_DEFAULT = "stapel_core.celery.W002"

#: Celery's own out-of-the-box queue name. Every app that never set one lands
#: here, so on a shared broker they all consume each other's work.
FACTORY_DEFAULT_QUEUE = "celery"

#: Celery's unconfigured module-level app. ``main`` is this when no project
#: ``config/celery.py`` has been imported — nothing to compare against.
UNBOUND_APP_NAME = "default"


def _normalise(name: str) -> str:
    """Fold the one difference that is spelling, not identity.

    A service slug is hyphenated (``classified-core``) and its Python module
    is underscored (``classified_core``); ``stapel-tools`` renders the app
    name from the module and the queue from the slug, so the two legitimately
    differ by that character in every generated service.
    """
    return name.strip().lower().replace("_", "-")


@checks.register("stapel_celery")
def check_task_default_queue(app_configs=None, **kwargs):
    try:
        from celery import current_app
    except ImportError:  # celery is optional — a service without it has no queue
        return []

    app_name = getattr(current_app, "main", None)
    if not app_name or app_name == UNBOUND_APP_NAME:
        # No project app is bound (celery installed as a transitive dep, or
        # config/celery.py absent). Reading a queue off Celery's own default
        # app would report the library's default as this service's choice.
        return []

    try:
        queue = current_app.conf.task_default_queue
    except Exception:  # pragma: no cover - a conf that cannot be read is not ours to judge
        return []
    if not queue:
        return []

    if _normalise(queue) == FACTORY_DEFAULT_QUEUE:
        return [checks.Warning(
            f'This service\'s Celery app is "{app_name}" but its default '
            f'queue is Celery\'s own "{FACTORY_DEFAULT_QUEUE}". Every service '
            "on a shared broker that also left the default consumes every "
            "other one's scheduled tasks: the ones that do not have the code "
            "raise, and the ones that do run it against the wrong database.",
            hint="Set CELERY_TASK_DEFAULT_QUEUE to this service's own name "
                 f'(CELERY_TASK_DEFAULT_QUEUE = "{_normalise(app_name)}"). '
                 "Warning rather than error because a single-service "
                 "deployment with a broker of its own is entitled to the "
                 "default.",
            id=W002_QUEUE_IS_THE_SHARED_DEFAULT,
        )]

    if _normalise(queue) != _normalise(app_name):
        return [checks.Error(
            f'This service\'s Celery app is "{app_name}" but its default '
            f'queue is "{queue}". On a shared broker that hands this '
            "service's scheduled work to whichever worker consumes "
            f'"{queue}" — its beat publishes there and a worker that does '
            "not have the code refuses the task, while one that does have it "
            "(any task both services register, including "
            "stapel_core.django.taskstore.sweep_tasks) runs it against the "
            "wrong database, silently.",
            hint="Name the queue after the service: "
                 f'CELERY_TASK_DEFAULT_QUEUE = "{_normalise(app_name)}". A '
                 "deployment that really does route this app's work onto "
                 f'"{queue}" — one worker serving two apps, a dedicated '
                 "broker — states that by putting "
                 f'"{E001_QUEUE_NAMES_ANOTHER_APP}" in '
                 "SILENCED_SYSTEM_CHECKS.",
            id=E001_QUEUE_NAMES_ANOTHER_APP,
        )]

    return []
