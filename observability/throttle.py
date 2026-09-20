"""One alert per fact per window — per SERVICE, not per worker.

THE DEFECT
    "At most one ERROR per provider per hour" is a rule every alerting path
    in the fleet writes the same way: a module-level dict holding the last
    time it was loud. In a single-process service that is exactly right. In
    a container running ``gunicorn --workers 2``, or a Celery prefork pool
    with four children, the dict exists once PER WORKER — so the hour is
    per worker, and a provider refusing every upload produces one page per
    worker per hour. Six workers across two services is twelve pages about
    one fact, which is how a real alert gets muted.

    The same arithmetic runs the other way and is worse: a throttle sized
    for one process ("one line an hour is fine") becomes N lines an hour,
    and the operator's answer to that is a filter rule, after which the
    N+1st alert — the real one — is also filtered.

THE SLOT
    A claim on the shared cache: ``cache.add(key, 1, ttl)`` is atomic on
    every backend worth deploying (Redis ``SET NX EX``, memcached ``add``),
    so exactly one process in the fleet wins the window and every other one
    counts itself as suppressed. The suppressed count rides on a second key
    incremented by the losers and handed to the winner of the NEXT window,
    so the loud line can say how many occurrences it stands for — the shape
    :mod:`stapel_notifications.contact_gap` already proved in production.

THE FALLBACK IS PROCESS-LOCAL, AND SAYS SO
    No cache configured, a cache that is per-process anyway (locmem), a
    cache that stores nothing (dummy), or a cache that raised: this falls
    back to the in-process dict the call sites used before. That is the
    honest degradation — it is exactly today's behaviour — and it is chosen
    deliberately over "no throttle at all", because losing the rate limit
    turns one true alert into a storm, while losing the ALERT is the defect
    the throttle exists inside of.

    ``locmem`` and ``dummy`` are detected rather than used: dummy stores
    nothing, so ``add()`` always succeeds and EVERY occurrence would be
    loud — a throttle that silently stops throttling.
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

__all__ = [
    "claim_slot",
    "release_slot",
    "clear_slots",
    "slot_is_shared",
]

#: Cache key prefix. One namespace so a deployment can see (and flush) every
#: alert slot the fleet holds.
KEY_PREFIX = "stapel:throttle:"

#: Process-local fallback: key -> (monotonic time of the last loud one,
#: suppressed since).
_slots: dict[str, tuple[float, int]] = {}
_lock = threading.Lock()

#: Backends that are not shared between processes, whatever their alias
#: says. Matched on the backend's dotted path.
_UNSHARED_BACKENDS = ("locmem", "dummy")


def _cache(alias: str | None):
    """The Django cache to claim slots in, or None if there is no usable one."""
    try:
        from django.core.cache import caches
    except Exception:
        return None
    try:
        cache = caches[alias or "default"]
    except Exception:
        return None
    backend = type(cache).__module__.lower()
    if any(token in backend for token in _UNSHARED_BACKENDS):
        return None
    return cache


def slot_is_shared(alias: str | None = None) -> bool:
    """Whether slots claimed here hold across processes.

    Exposed so a caller can say which of the two regimes it is in — a log
    line that claims "one alert an hour" while the throttle is per worker is
    the misleading half of this whole module.
    """
    return _cache(alias) is not None


def claim_slot(
    key: str, interval: float, *, alias: str | None = None
) -> tuple[bool, int]:
    """``(be_loud, suppressed_since_the_last_loud_one)`` for one occurrence.

    The first caller in a window gets ``(True, n)`` where *n* is how many
    occurrences were swallowed since the previous loud one; everyone else
    gets ``(False, n)`` and has counted itself into the next one.

    ``interval <= 0`` disables the throttle — a test's setting, not a
    deployment's.

    Never raises: an alerting path that can end a request is worse than the
    thing it is alerting about.
    """
    if interval is None or interval <= 0:
        # The throttle is off, but occurrences already swallowed must still
        # be reported. Returning a flat 0 made the first loud line after a
        # window was disabled claim it stood for nothing — the count is the
        # whole reason the suppressed figure is carried.
        return True, _drain_suppressed(key, alias)
    cache = _cache(alias)
    if cache is None:
        return _claim_local(key, interval)
    try:
        return _claim_shared(cache, key, interval)
    except Exception:
        logger.debug(
            "stapel_core.observability: the shared alert slot for %r is "
            "unavailable; falling back to a per-process throttle",
            key, exc_info=True,
        )
        return _claim_local(key, interval)


def _claim_shared(cache, key: str, interval: float) -> tuple[bool, int]:
    slot_key = f"{KEY_PREFIX}{key}"
    count_key = f"{KEY_PREFIX}{key}:suppressed"
    ttl = int(interval)
    # A window shorter than a second still has to hold for SOME time, or the
    # claim expires before the next caller reads it and nothing is throttled.
    ttl = ttl if ttl > 0 else 1
    if cache.add(slot_key, 1, ttl):
        suppressed = cache.get(count_key) or 0
        try:
            cache.delete(count_key)
        except Exception:  # pragma: no cover - delete is best effort
            pass
        return True, int(suppressed)
    try:
        suppressed = cache.incr(count_key)
    except ValueError:
        # incr on a key that expired between the add() above and here.
        cache.set(count_key, 1, ttl * 2)
        suppressed = 1
    return False, int(suppressed)


def _drain_suppressed(key: str, alias: str | None) -> int:
    """Take (and forget) what has been swallowed for *key* so far.

    Reads both halves — the shared counter and this process's own — and
    answers with the larger: a deployment that switched a cache on or off
    between two occurrences must not be told fewer than actually happened.
    """
    with _lock:
        _, local = _slots.pop(key, (None, 0))
    suppressed = int(local)
    cache = _cache(alias)
    if cache is None:
        return suppressed
    try:
        shared = cache.get(f"{KEY_PREFIX}{key}:suppressed") or 0
        cache.delete(f"{KEY_PREFIX}{key}:suppressed")
        cache.delete(f"{KEY_PREFIX}{key}")
        suppressed = max(suppressed, int(shared))
    except Exception:
        logger.debug(
            "stapel_core.observability: could not read the suppressed count "
            "for %r", key, exc_info=True,
        )
    return suppressed


def _claim_local(key: str, interval: float) -> tuple[bool, int]:
    now = time.monotonic()
    with _lock:
        last, suppressed = _slots.get(key, (None, 0))
        if last is None or (now - last) >= interval:
            _slots[key] = (now, 0)
            return True, suppressed
        _slots[key] = (last, suppressed + 1)
        return False, suppressed + 1


def release_slot(key: str, *, alias: str | None = None) -> None:
    """Forget *key*, so the next occurrence is as loud as the first was.

    Called when the condition clears (the provider served a call again): a
    recurrence after a recovery is NEW news, and making it wait out the
    remainder of the old window would delay the second outage by up to an
    hour.
    """
    with _lock:
        _slots.pop(key, None)
    cache = _cache(alias)
    if cache is None:
        return
    try:
        cache.delete(f"{KEY_PREFIX}{key}")
        cache.delete(f"{KEY_PREFIX}{key}:suppressed")
    except Exception:
        logger.debug(
            "stapel_core.observability: could not release the alert slot %r",
            key, exc_info=True,
        )


def clear_slots(*, alias: str | None = None) -> None:
    """Forget every process-local slot. For tests."""
    with _lock:
        _slots.clear()
