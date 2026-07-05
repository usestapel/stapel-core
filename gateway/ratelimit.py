"""Rate limiting for verbs — fixed window, counted per ``(verb, project)``.

Granularity rationale: the actor behind a gateway call is the project (its
container gets one scope token; internal callers act *about* a project),
so the fair unit of quota is the project, not the token or the IP — a
rotated token or a re-provisioned container must not reset the budget.
Calls with no project count in a shared ``-`` bucket. Finer slicing
(per-subject, per-container) is a custom :class:`RateLimiter` away.

The default limiter uses the Django cache (atomic ``incr``); deployments
point ``STAPEL_GATEWAY["RATE_LIMITER"]`` elsewhere for other stores. The
limiter answers allow/deny only — the *denial is still audited* by the
service layer (S6).
"""
from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod

from .base import CallerContext
from .exceptions import GatewayConfigError

_PERIODS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_RATE_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+|[smhd])\s*$")


def parse_rate(rate: str) -> tuple[int, int]:
    """``"30/m"`` → ``(30, 60)``; also ``"100/h"``, ``"5/s"``, ``"10/900"``
    (N per 900 seconds). Malformed strings are a configuration error —
    fail closed, never "no limit"."""
    match = _RATE_RE.match(rate or "")
    if not match:
        raise GatewayConfigError(
            f"rate_limit {rate!r} is malformed (expected 'N/s|m|h|d' or 'N/SECONDS')"
        )
    limit = int(match.group(1))
    period = match.group(2)
    window = _PERIODS.get(period) or int(period)
    if limit <= 0 or window <= 0:
        raise GatewayConfigError(f"rate_limit {rate!r} must be positive")
    return limit, window


class RateLimiter(ABC):
    @abstractmethod
    def allow(self, verb: str, caller: CallerContext, *, limit: int, window: int) -> bool:
        """One call attempt: consume quota and answer whether it fits."""


class CacheRateLimiter(RateLimiter):
    """Fixed-window counter in the Django cache.

    Key: ``stapel:gateway:rate:{verb}:{project}:{window_index}``. ``add``
    then ``incr`` keeps the count atomic on cache backends with atomic
    increments (Redis, memcached, locmem).
    """

    def allow(self, verb: str, caller: CallerContext, *, limit: int, window: int) -> bool:
        from django.core.cache import cache

        bucket = int(time.time() // window)
        key = f"stapel:gateway:rate:{verb}:{caller.project or '-'}:{bucket}"
        # Expire one window after the bucket closes — clock-skew slack.
        cache.add(key, 0, timeout=window * 2)
        try:
            count = cache.incr(key)
        except ValueError:  # add/incr race with expiry: recreate
            cache.add(key, 0, timeout=window * 2)
            count = cache.incr(key)
        return count <= limit


__all__ = ["CacheRateLimiter", "RateLimiter", "parse_rate"]
