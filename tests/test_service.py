"""Unit tests for the Databento service -- no network is ever touched."""

import datetime as dt
import json

import pytest

from databento_discovery.ratelimit import RateLimited, RateLimiter
from databento_discovery.service import (
    DEFAULT_CONDITION_DAYS,
    DEFAULT_SYMBOL_LIMIT,
    MAX_SYMBOL_LIMIT,
    DatabentoQueryError,
    DatabentoService,
)


class FakeMetadata:
    def __init__(self, calls: list) -> None:
        self._calls = calls

    def list_datasets(self, start_date=None, end_date=None):
        self._calls.append(("list_datasets", start_date, end_date))
        return ["XNAS.ITCH", "MEMEX.MEMOIR"]

    def list_publishers(self):
        self._calls.append(("list_publishers",))
        return [{"publisher": "Nasdaq"}]

    def list_schemas(self, dataset):
        self._calls.append(("list_schemas", dataset))
        return ["mbo", "trades"]

    def list_fields(self, schema, encoding, dataset=None):
        self._calls.append(("list_fields", schema, encoding, dataset))
        return [{"name": "price", "type": "int64"}]

    def get_dataset_range(self, dataset):
        # The real API nests a {start, end} block per entitled schema alongside
        # the dataset-level start/end, so the fake must nest too.
        self._calls.append(("get_dataset_range", dataset))
        return {
            "start": "2020-01-01",
            "end": "2025-01-01",
            "mbo": {"start": "2020-01-01", "end": "2025-01-01"},
        }

    def list_unit_prices(self, dataset):
        self._calls.append(("list_unit_prices", dataset))
        return [{"schema": "mbo", "unit_price": 2.5}]

    def get_dataset_condition(self, dataset, start_date=None, end_date=None):
        self._calls.append(("get_dataset_condition", dataset, start_date, end_date))
        return [{"date": str(start_date), "condition": "full"}]

    def get_cost(self, dataset, **kwargs):
        self._calls.append(("get_cost", dataset, kwargs))
        return 11.325

    def get_record_count(self, dataset, **kwargs):
        self._calls.append(("get_record_count", dataset, kwargs))
        return 201_045

    def get_billable_size(self, dataset, **kwargs):
        self._calls.append(("get_billable_size", dataset, kwargs))
        return 4_424_625


class FakeSymbology:
    """Mimics ``symbology.resolve``: a dict of symbol -> interval list.

    ``bulk_size`` controls how many symbols a bulk ``ALL_SYMBOLS`` request
    matches, so tests can reproduce the ~12.6k-symbol / ~1.4 MB production case.
    """

    def __init__(self, calls: list, bulk_size: int = 0) -> None:
        self._calls = calls
        self._bulk_size = bulk_size

    def resolve(self, dataset, symbols, stype_in, stype_out, start_date, end_date=None):
        self._calls.append(("resolve", dataset, symbols, stype_in, stype_out, start_date))
        bulk = symbols == "ALL_SYMBOLS" or (
            isinstance(symbols, (list, tuple)) and "ALL_SYMBOLS" in symbols
        )
        if bulk:
            keys = [f"SYM{i:05d}" for i in range(self._bulk_size or 5000)]
        elif isinstance(symbols, str):
            keys = [symbols]
        else:
            keys = [str(s) for s in symbols]
        return {
            "result": {
                k: [{"d0": "2024-08-05", "d1": "2024-09-05", "s": "38"}] for k in keys
            },
            "symbols": [symbols] if isinstance(symbols, str) else list(symbols),
            "stype_in": stype_in,
            "stype_out": stype_out,
            "start_date": str(start_date),
            "end_date": str(end_date) if end_date is not None else None,
            "partial": [],
            "not_found": [],
            "message": "OK",
            "status": 0,
        }


class FakeClient:
    def __init__(self, bulk_size: int = 0) -> None:
        self.calls: list = []
        self.metadata = FakeMetadata(self.calls)
        self.symbology = FakeSymbology(self.calls, bulk_size=bulk_size)


def make_service(*, bulk_size: int = 0, **kwargs) -> tuple[DatabentoService, FakeClient]:
    client = FakeClient(bulk_size=bulk_size)
    return DatabentoService(client=client, **kwargs), client


def test_slow_moving_reads_are_cached():
    service, client = make_service()

    assert service.list_datasets() == ["XNAS.ITCH", "MEMEX.MEMOIR"]
    assert service.list_datasets() == ["XNAS.ITCH", "MEMEX.MEMOIR"]
    service.list_schemas("XNAS.ITCH")
    service.list_schemas("XNAS.ITCH")
    service.list_publishers()
    service.list_publishers()

    assert [c[0] for c in client.calls] == [
        "list_datasets",
        "list_schemas",
        "list_publishers",
    ]


def test_condition_defaults_to_a_bounded_window():
    service, client = make_service()

    service.get_dataset_condition("XNAS.ITCH")

    _, _, start, end = client.calls[-1]
    assert (end - start).days == DEFAULT_CONDITION_DAYS
    assert end == dt.datetime.now(dt.UTC).date()


def test_condition_range_is_capped():
    service, _ = make_service()

    with pytest.raises(DatabentoQueryError, match="wider than"):
        service.get_dataset_condition(
            "XNAS.ITCH", start_date="2020-01-01", end_date="2024-01-01"
        )


def test_condition_rejects_inverted_range():
    service, _ = make_service()

    with pytest.raises(DatabentoQueryError, match="after"):
        service.get_dataset_condition(
            "XNAS.ITCH", start_date="2024-08-10", end_date="2024-08-05"
        )


def test_condition_is_not_cached():
    service, client = make_service()

    service.get_dataset_condition("XNAS.ITCH", start_date="2024-08-01", end_date="2024-08-05")
    service.get_dataset_condition("XNAS.ITCH", start_date="2024-08-01", end_date="2024-08-05")

    assert len([c for c in client.calls if c[0] == "get_dataset_condition"]) == 2


def test_estimate_cost_folds_three_calls_into_one_result():
    service, client = make_service()

    result = service.estimate_cost(
        "XNAS.ITCH",
        start="2024-08-05T00:00:00",
        end="2024-08-05T10:00:00",
        symbols=["SPY", "QQQ"],
        schema="mbo",
    )

    assert result == {
        "dataset": "XNAS.ITCH",
        "start": "2024-08-05T00:00:00",
        "end": "2024-08-05T10:00:00",
        "symbols": ["SPY", "QQQ"],
        "schema": "mbo",
        "cost_usd": 11.325,
        "record_count": 201_045,
        "billable_bytes": 4_424_625,
    }
    assert [c[0] for c in client.calls] == [
        "get_cost",
        "get_record_count",
        "get_billable_size",
    ]


def test_estimate_cost_omits_optional_arguments_when_absent():
    service, client = make_service()

    service.estimate_cost("XNAS.ITCH", start="2024-08-05", schema="trades")

    _, _, kwargs = client.calls[0]
    assert "end" not in kwargs
    assert "symbols" not in kwargs
    assert "mode" not in kwargs


def test_resolve_symbols_passes_through_to_symbology():
    service, client = make_service()

    result = service.resolve_symbols("XNAS.ITCH", ["AAPL"], start_date="2024-08-05")

    assert result["result"] == {"AAPL": [{"d0": "2024-08-05", "d1": "2024-09-05", "s": "38"}]}
    assert client.calls[-1][0] == "resolve"


def test_resolve_envelope_is_self_describing():
    service, _ = make_service()

    out = service.resolve_symbols("XNAS.ITCH", ["AAPL"], start_date="2024-08-05")

    assert {"total", "returned", "offset", "next_offset", "truncated", "result"} <= set(out)
    assert out["total"] == 1
    assert out["returned"] == 1
    assert out["offset"] == 0
    assert out["next_offset"] is None
    assert out["truncated"] is False
    # Upstream metadata is preserved verbatim.
    assert out["stype_in"] == "raw_symbol"
    assert out["stype_out"] == "instrument_id"
    assert out["status"] == 0
    assert out["partial"] == []
    assert out["not_found"] == []


def test_resolve_default_limit_is_bounded():
    service, _ = make_service(bulk_size=5000)

    out = service.resolve_symbols(
        "XNAS.ITCH", "ALL_SYMBOLS", start_date="2024-08-05", end_date="2024-09-05"
    )

    assert out["total"] == 5000
    assert out["returned"] == DEFAULT_SYMBOL_LIMIT
    assert out["limit"] == DEFAULT_SYMBOL_LIMIT
    assert len(out["result"]) == DEFAULT_SYMBOL_LIMIT
    assert out["truncated"] is True
    assert out["next_offset"] == DEFAULT_SYMBOL_LIMIT
    # A default page stays in the low thousands of tokens (~11 KB of JSON).
    assert len(json.dumps(out).encode()) < 20_000


def test_resolve_rejects_bad_limit_and_offset():
    service, _ = make_service(bulk_size=10)

    with pytest.raises(DatabentoQueryError, match="limit"):
        service.resolve_symbols("XNAS.ITCH", "AAPL", start_date="2024-08-05", limit=0)
    with pytest.raises(DatabentoQueryError, match="offset"):
        service.resolve_symbols("XNAS.ITCH", "AAPL", start_date="2024-08-05", offset=-1)


def test_resolve_over_max_limit_is_clamped_not_rejected():
    service, _ = make_service(bulk_size=5000)

    out = service.resolve_symbols(
        "XNAS.ITCH", "ALL_SYMBOLS", start_date="2024-08-05", limit=10_000
    )

    assert out["limit"] == MAX_SYMBOL_LIMIT
    assert out["returned"] == MAX_SYMBOL_LIMIT
    assert out["truncated"] is True


def test_resolve_truncated_flag_is_accurate():
    service, _ = make_service(bulk_size=250)

    first = service.resolve_symbols("XNAS.ITCH", "ALL_SYMBOLS", start_date="2024-08-05", limit=100)
    assert (first["returned"], first["truncated"], first["next_offset"]) == (100, True, 100)

    middle = service.resolve_symbols(
        "XNAS.ITCH", "ALL_SYMBOLS", start_date="2024-08-05", limit=100, offset=100
    )
    assert (middle["returned"], middle["truncated"], middle["next_offset"]) == (100, True, 200)

    last = service.resolve_symbols(
        "XNAS.ITCH", "ALL_SYMBOLS", start_date="2024-08-05", limit=100, offset=200
    )
    assert (last["returned"], last["truncated"], last["next_offset"]) == (50, False, None)

    # Reading exactly the full result is not "truncated".
    whole = service.resolve_symbols(
        "XNAS.ITCH", "ALL_SYMBOLS", start_date="2024-08-05", limit=250
    )
    assert (whole["returned"], whole["truncated"], whole["next_offset"]) == (250, False, None)


def test_resolve_paging_is_lossless_across_the_full_result():
    service, _ = make_service(bulk_size=5000)

    seen: dict[str, object] = {}
    offset = 0
    pages = 0
    while True:
        out = service.resolve_symbols(
            "XNAS.ITCH", "ALL_SYMBOLS", start_date="2024-08-05", limit=MAX_SYMBOL_LIMIT, offset=offset
        )
        assert out["offset"] == offset
        assert out["truncated"] == (out["next_offset"] is not None)
        assert out["total"] == 5000
        for key, value in out["result"].items():
            assert key not in seen, f"duplicate {key} while paging"
            seen[key] = value
        if out["next_offset"] is None:
            break
        offset = out["next_offset"]
        pages += 1
        assert pages < 20, "paging did not converge"

    assert len(seen) == 5000
    assert set(seen) == {f"SYM{i:05d}" for i in range(5000)}


def test_resolve_pages_share_one_cached_upstream_snapshot():
    service, client = make_service(bulk_size=5000)

    for offset in (0, 1000, 2000):
        service.resolve_symbols(
            "XNAS.ITCH", "ALL_SYMBOLS", start_date="2024-08-05", limit=1000, offset=offset
        )

    assert len([c for c in client.calls if c[0] == "resolve"]) == 1


def test_bulk_all_symbols_is_no_longer_megabytes():
    """Regression: this call used to return ~1.43 MB / ~358k tokens in one go."""
    service, _ = make_service(bulk_size=12593)

    out = service.resolve_symbols(
        "XNAS.ITCH", "ALL_SYMBOLS", start_date="2024-01-01", end_date="2024-03-01"
    )

    payload = len(json.dumps(out).encode())
    assert out["total"] == 12593
    assert out["returned"] == DEFAULT_SYMBOL_LIMIT
    assert payload < 50_000, f"bulk page should be bounded, got {payload} bytes"


def test_rate_limited_client_error_is_translated():
    class BoomMetadata(FakeMetadata):
        def list_datasets(self, start_date=None, end_date=None):
            from databento.common.error import BentoClientError

            raise BentoClientError(429, message="slow down")

    client = FakeClient()
    client.metadata = BoomMetadata([])
    service = DatabentoService(client=client)

    with pytest.raises(RateLimited):
        service.list_datasets()


def test_local_limiter_refuses_before_upstream():
    service, _ = make_service(limiter=RateLimiter(max_calls=1, period_seconds=60.0))

    service.list_datasets()
    with pytest.raises(RateLimited):
        service.list_publishers()
