"""Unit tests for the TTL cache."""

from databento_discovery.cache import TTLCache


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_value_is_cached_until_ttl_expires():
    clock = FakeClock()
    cache: TTLCache = TTLCache(ttl_seconds=60.0, clock=clock)
    calls = []

    def factory():
        calls.append(1)
        return len(calls)

    assert cache.get_or_set("k", factory) == 1
    assert cache.get_or_set("k", factory) == 1
    assert len(calls) == 1

    clock.now = 61.0
    assert cache.get_or_set("k", factory) == 2
    assert len(calls) == 2


def test_failed_factory_is_not_cached():
    cache: TTLCache = TTLCache(ttl_seconds=60.0)
    calls = []

    def factory():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return "ok"

    try:
        cache.get_or_set("k", factory)
    except RuntimeError:
        pass

    assert cache.get_or_set("k", factory) == "ok"
    assert len(cache) == 1
