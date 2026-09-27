"""A client-side sliding-window limiter.

Databento allows roughly 60 requests/minute. The limit is enforced here rather
than trusted to the model, so a burst of tool calls produces a clean
"rate limited, retry in Ns" instead of a raw 429 from upstream.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable


class RateLimited(Exception):
    """Raised when the local limiter (or upstream) refuses a call."""

    def __init__(self, retry_after: float) -> None:
        self.retry_after = max(0.0, float(retry_after))
        super().__init__(f"rate limited, retry in {self.retry_after:.0f}s")


class RateLimiter:
    """Allow at most ``max_calls`` in any rolling ``period_seconds`` window."""

    def __init__(
        self,
        max_calls: int = 60,
        period_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_calls
        self._period = period_seconds
        self._clock = clock
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Record a call, or raise :class:`RateLimited` if the window is full."""
        with self._lock:
            now = self._clock()
            while self._calls and self._calls[0] <= now - self._period:
                self._calls.popleft()
            if len(self._calls) >= self._max:
                retry_after = self._calls[0] + self._period - now
                raise RateLimited(retry_after)
            self._calls.append(now)
