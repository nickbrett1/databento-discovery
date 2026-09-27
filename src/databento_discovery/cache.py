"""A tiny thread-safe TTL cache for the slow-moving Databento metadata calls.

Dataset lists, schemas, fields and unit prices change rarely and are expensive
to ask for on every turn, so they are cached for an hour. Request-specific
answers (coverage condition, cost estimates) are deliberately *not* cached.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable
from typing import Any, TypeVar

T = TypeVar("T")


class TTLCache:
    """Map a hashable key to a value that expires after ``ttl_seconds``."""

    def __init__(
        self,
        ttl_seconds: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._store: dict[Hashable, tuple[float, Any]] = {}

    def get_or_set(self, key: Hashable, factory: Callable[[], T]) -> T:
        """Return a live cached value, or compute, store and return a fresh one."""
        now = self._clock()
        with self._lock:
            hit = self._store.get(key)
            if hit is not None and hit[0] > now:
                return hit[1]

        value = factory()

        with self._lock:
            self._store[key] = (self._clock() + self._ttl, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)
