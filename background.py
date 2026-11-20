"""Background jobs with celery optional.

A library that has background work (thumbnails, exports, sweeps) should not
force celery on a host that has no broker. :func:`background_task` declares
such a job once:

    from stapel_core.background import background_task

    @background_task(name="stapel_cdn.tasks.generate_thumbnails")
    def generate_thumbnails(media_id): ...

    generate_thumbnails.delay(42)

* celery importable -> ``celery.shared_task(name=..., **options)``: the job is
  an ordinary celery task under a stable name a beat schedule, a
  ``task_routes`` map or a worker's ``-Q`` can reference;
* celery absent -> an :class:`InlineTask`: ``delay``/``apply_async`` run the
  function in the calling process before returning (what celery's eager mode
  does). Routing and retry options are accepted and ignored, there is no
  broker; ``bind=True`` passes the :class:`InlineTask` as ``self`` and
  ``self.retry(exc=...)`` raises ``exc`` to the caller.

The choice is made when the decorator runs (module import), so a test proves
the no-celery face by importing the job module in a process where ``celery``
cannot be imported.
"""
from __future__ import annotations

import functools
from typing import Any, Callable, Optional

__all__ = ["InlineTask", "background_task"]


def _celery_shared_task() -> Optional[Callable]:
    """``celery.shared_task`` when celery is importable, else ``None``."""
    try:
        from celery import shared_task
    except ImportError:
        return None
    return shared_task


class InlineTask:
    """A job without celery: ``delay``/``apply_async`` call it now.

    Keeps the celery calling convention so call sites do not branch:
    ``job.delay(*a, **kw)``, ``job.apply_async(args=..., kwargs=..., **options)``
    and a plain ``job(*a, **kw)``. Each returns the function's own result.
    """

    def __init__(self, fn: Callable, name: str, *, bind: bool = False):
        functools.update_wrapper(self, fn)
        self.run = fn
        self.name = name
        self.bind = bind

    def __repr__(self) -> str:
        return f"<InlineTask {self.name}>"

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self.bind:
            return self.run(self, *args, **kwargs)
        return self.run(*args, **kwargs)

    def delay(self, *args: Any, **kwargs: Any) -> Any:
        return self(*args, **kwargs)

    def apply_async(self, args=None, kwargs=None, **options: Any) -> Any:
        return self(*(args or ()), **(kwargs or {}))

    def retry(self, exc: Optional[BaseException] = None, **options: Any):
        """Inline there is no retry ladder: the failure goes to the caller."""
        if exc is not None:
            raise exc
        raise RuntimeError(f"{self.name}: retry requested and there is no broker")


def background_task(name: Optional[str] = None, **options: Any) -> Callable:
    """Decorate a job: a celery task when celery is installed, :class:`InlineTask` otherwise.

    *name* defaults to ``<module>.<function>`` — the name celery itself would
    give the task, so adopting this decorator does not rename a job a
    deployment already routes on. *options* go to ``shared_task`` unchanged
    (``bind``, ``max_retries``, ``queue`` ...); inline only ``bind`` matters.
    """

    def decorate(fn: Callable):
        task_name = name or f"{fn.__module__}.{fn.__name__}"
        shared_task = _celery_shared_task()
        if shared_task is not None:
            return shared_task(name=task_name, **options)(fn)
        return InlineTask(fn, task_name, bind=bool(options.get("bind")))

    return decorate
