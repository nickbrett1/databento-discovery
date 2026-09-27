"""Unit tests for the sliding-window rate limiter."""

import pytest

from databento_discovery.ratelimit import RateLimited, RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_the_limit_then_refuses():
    clock = FakeClock()
    limiter = RateLimiter(max_calls=3, period_seconds=60.0, clock=clock)

    for _ in range(3):
        limiter.acquire()

    with pytest.raises(RateLimited) as excinfo:
        limiter.acquire()
    assert excinfo.value.retry_after == pytest.approx(60.0)


def test_window_slides():
    clock = FakeClock()
    limiter = RateLimiter(max_calls=2, period_seconds=60.0, clock=clock)
    limiter.acquire()
    limiter.acquire()

    clock.now = 61.0
    limiter.acquire()  # both earlier calls have aged out


def test_retry_after_shrinks_as_time_passes():
    clock = FakeClock()
    limiter = RateLimiter(max_calls=1, period_seconds=60.0, clock=clock)
    limiter.acquire()

    clock.now = 45.0
    with pytest.raises(RateLimited) as excinfo:
        limiter.acquire()
    assert excinfo.value.retry_after == pytest.approx(15.0)
    assert "retry in 15s" in str(excinfo.value)
