"""Unit tests for the Databento service -- no network is ever touched."""

import datetime as dt

import pytest

from databento_discovery.ratelimit import RateLimited, RateLimiter
from databento_discovery.service import (
    DEFAULT_CONDITION_DAYS,
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
    def __init__(self, calls: list) -> None:
        self._calls = calls

    def resolve(self, dataset, symbols, stype_in, stype_out, start_date, end_date=None):
        self._calls.append(("resolve", dataset, symbols, stype_in, stype_out, start_date))
        return {"result": {"AAPL": [{"instrument_id": 1, "start_date": "2024-08-05"}]}}


class FakeClient:
    def __init__(self) -> None:
        self.calls: list = []
        self.metadata = FakeMetadata(self.calls)
        self.symbology = FakeSymbology(self.calls)


def make_service(**kwargs) -> tuple[DatabentoService, FakeClient]:
    client = FakeClient()
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

    assert "result" in result
    assert client.calls[-1][0] == "resolve"


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
