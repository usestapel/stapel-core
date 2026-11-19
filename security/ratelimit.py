"""Rate strings — ``"30/m"``, ``"100/h"``, ``"10/900"`` — parsed in one place.

Any fixed-window limiter in the fleet (the gateway's verb limits, the cdn
import-from-URL quota) states its budget in this format. The parser is
kernel so a library can read a rate without importing an optional feature.
"""
from __future__ import annotations

import re

_PERIODS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_RATE_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+|[smhd])\s*$")


class InvalidRate(ValueError):
    """A rate string that is not ``N/s|m|h|d`` or ``N/SECONDS``, or not positive."""

    def __init__(self, rate, problem: str) -> None:
        self.rate = rate
        self.problem = problem
        super().__init__(f"rate {rate!r} {problem}")


def parse_rate(rate: str) -> tuple[int, int]:
    """``"30/m"`` → ``(30, 60)``; also ``"100/h"``, ``"5/s"``, ``"10/900"``
    (N per 900 seconds). Malformed strings raise :class:`InvalidRate` — a
    configuration error, fail closed, never "no limit"."""
    match = _RATE_RE.match(rate or "")
    if not match:
        raise InvalidRate(rate, "is malformed (expected 'N/s|m|h|d' or 'N/SECONDS')")
    limit = int(match.group(1))
    period = match.group(2)
    window = _PERIODS.get(period) or int(period)
    if limit <= 0 or window <= 0:
        raise InvalidRate(rate, "must be positive")
    return limit, window


__all__ = ["InvalidRate", "parse_rate"]
