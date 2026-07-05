"""Write buffer — batch-flush so we never INSERT per event.

High-volume streams (LLM ledger, audit, delivery logs) would hammer the DB
with one INSERT each. The buffer accumulates events and flushes a batch when
it fills (``size``) or ages out (``interval`` seconds since the oldest
buffered event). A ``sync`` buffer flushes every append immediately — the
test/low-volume fallback.

Concurrency: appends from many worker threads share one buffer. The batch is
swapped out under a lock, but the flush callback (which does DB I/O) runs
*outside* the lock so a slow write never blocks other producers.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Sequence

from .base import Event


class WriteBuffer:
    def __init__(
        self,
        flush_fn: Callable[[Sequence[Event]], None],
        *,
        size: int = 500,
        interval: float = 5.0,
        sync: bool = False,
    ) -> None:
        self._flush_fn = flush_fn
        self._size = max(1, int(size))
        self._interval = float(interval)
        self._sync = bool(sync)
        self._lock = threading.Lock()
        self._pending: list[Event] = []
        self._oldest_at: float | None = None

    def add(self, event: Event) -> None:
        self.extend((event,))

    def extend(self, events: Sequence[Event]) -> None:
        to_flush: list[Event] | None = None
        with self._lock:
            if not self._pending:
                self._oldest_at = time.monotonic()
            self._pending.extend(events)
            if self._should_flush_locked():
                to_flush = self._take_locked()
        if to_flush:
            self._flush_fn(to_flush)

    def flush(self) -> None:
        with self._lock:
            to_flush = self._take_locked()
        if to_flush:
            self._flush_fn(to_flush)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def _should_flush_locked(self) -> bool:
        if self._sync or len(self._pending) >= self._size:
            return True
        if self._oldest_at is not None and self._interval >= 0:
            return (time.monotonic() - self._oldest_at) >= self._interval
        return False

    def _take_locked(self) -> list[Event]:
        batch = self._pending
        self._pending = []
        self._oldest_at = None
        return batch


__all__ = ["WriteBuffer"]
