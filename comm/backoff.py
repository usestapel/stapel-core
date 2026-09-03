"""Retry delay with jitter — one implementation, four call sites.

``2 ** retries`` appears verbatim in three bus backends and in a fourth
shape in the outbox relay, and not one of them adds jitter. That is not a
style complaint. Without jitter, N workers that fail against the same
provider at the same moment retry at the same moment, and keep doing so:
the herd stays synchronised for the whole backoff ladder, so the thing
that is already failing is hit by the full fleet at 1s, then the full
fleet at 2s, then at 4s. Backoff spreads the retries of ONE caller over
time; jitter is the only part that spreads the retries of MANY callers
over each other, and it is the part everybody leaves out.

The measured case this was written for: on a client fleet's stand, 215
``moderation.screen`` tasks are parked FAILED, every one of them with
``attempts=3``, and the mean time from created to failed is **0.87
seconds**. Three provider calls and a permanent give-up, inside one
second, because :func:`stapel_core.comm.tasks._requeue` re-announced the
task with no delay at all. The provider was an unreachable proxy; a
retry ladder with any real delay would have outlived most of those
blips. Instead the ladder ran to completion before the network noticed.

Full jitter (``random.uniform(0, ceiling)``) rather than the ceiling
itself, per AWS's "Exponential Backoff and Jitter": it has the lowest
contention of the common variants, and its worst case is the
undecorated ceiling. The cost is that a delay is not reproducible, so
:func:`retry_delay` takes the bound apart into :func:`retry_ceiling`,
which is pure and is what tests assert on.
"""
from __future__ import annotations

import random

#: Default first-retry ceiling, in seconds.
DEFAULT_BASE_SECONDS = 2.0

#: Default upper bound. Past this the ladder is flat: a caller that has
#: waited five minutes is not helped by waiting eighty.
DEFAULT_CAP_SECONDS = 300.0


def retry_ceiling(
    attempt: int,
    *,
    base: float = DEFAULT_BASE_SECONDS,
    cap: float = DEFAULT_CAP_SECONDS,
) -> float:
    """The un-jittered ceiling for *attempt* — ``base * 2**(attempt-1)``, capped.

    *attempt* is 1-based and counts attempts ALREADY MADE, so the delay
    after the first failure is ``base``. Pure, so a test can assert the
    ladder without reasoning about a random draw.
    """
    if attempt < 1:
        attempt = 1
    if base <= 0:
        return 0.0
    # Exponent guarded: attempt is an attempt counter, but it arrives from
    # a DB column, and 2 ** 10_000 is a several-thousand-digit int before
    # min() ever sees it.
    if attempt > 64:
        return float(cap)
    return float(min(cap, base * (2 ** (attempt - 1))))


def retry_delay(
    attempt: int,
    *,
    base: float = DEFAULT_BASE_SECONDS,
    cap: float = DEFAULT_CAP_SECONDS,
) -> float:
    """Seconds to wait before retry number *attempt*, with full jitter.

    Returns a value in ``[0, retry_ceiling(attempt))``. ``base=0``
    disables the wait entirely and returns 0.0 — the configuration a
    test or a single-process script wants, and the reason the ladder is
    a setting rather than a constant.
    """
    ceiling = retry_ceiling(attempt, base=base, cap=cap)
    if ceiling <= 0:
        return 0.0
    return random.uniform(0.0, ceiling)


__all__ = [
    "DEFAULT_BASE_SECONDS",
    "DEFAULT_CAP_SECONDS",
    "retry_ceiling",
    "retry_delay",
]
