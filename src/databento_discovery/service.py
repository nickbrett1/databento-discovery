"""The only place in this service that talks to Databento.

It exposes the read-only Metadata API (plus Symbology ``resolve``) and nothing
else. There is deliberately no batch submission and no timeseries fetch: those
are the operations that spend credit, and their absence from this module is
what makes the whole server safe to hand to any agent. The service is a thin,
faithful proxy over the remote API -- it knows nothing about local parquet.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Iterable, Sequence
from typing import Any

import databento as db
from databento.common.error import BentoClientError

from .cache import TTLCache
from .ratelimit import RateLimited, RateLimiter

# Dataset lists, schemas, fields and unit prices change rarely; an hour is safe.
CACHE_TTL_SECONDS = 3600.0
# Databento allows ~60 requests/minute. Pre-empt rather than surfacing a raw 429.
RATE_LIMIT_PER_MINUTE = 60
# `get_dataset_condition` returns one row per date. Default to a sane window and
# refuse anything wider, so an unbounded request cannot return a huge payload.
DEFAULT_CONDITION_DAYS = 30
MAX_CONDITION_DAYS = 366


class DatabentoQueryError(Exception):
    """A Databento call failed in a way worth surfacing verbatim to the model."""


class DatabentoService:
    """Metadata-only, read-only access to Databento, with caching and limiting."""

    def __init__(
        self,
        key: str | None = None,
        *,
        client: Any | None = None,
        cache: TTLCache | None = None,
        limiter: RateLimiter | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        # ``db.Historical`` does no I/O at construction, so passing a key that is
        # absent here fails later at call time with the SDK's own message.
        self._client = client if client is not None else db.Historical(key=key)
        self._cache = cache or TTLCache(CACHE_TTL_SECONDS, clock=clock)
        self._limiter = limiter or RateLimiter(RATE_LIMIT_PER_MINUTE, 60.0, clock=clock)

    # -- internals ---------------------------------------------------------

    def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run one Databento call through the limiter, translating failures."""
        self._limiter.acquire()
        try:
            return fn(*args, **kwargs)
        except BentoClientError as exc:  # pragma: no cover - exercised via fakes
            raise _translate(exc) from exc

    def _cached(self, key: tuple[Any, ...], fn: Any, *args: Any, **kwargs: Any) -> Any:
        return self._cache.get_or_set(key, lambda: self._call(fn, *args, **kwargs))

    # -- slow-moving metadata (cached) -------------------------------------

    def list_datasets(
        self, start_date: dt.date | str | None = None, end_date: dt.date | str | None = None
    ) -> list[str]:
        return self._cached(
            ("list_datasets", _key_date(start_date), _key_date(end_date)),
            self._client.metadata.list_datasets,
            start_date=start_date,
            end_date=end_date,
        )

    def list_publishers(self) -> list[dict[str, Any]]:
        return self._cached(("list_publishers",), self._client.metadata.list_publishers)

    def list_schemas(self, dataset: str) -> list[str]:
        return self._cached(
            ("list_schemas", dataset), self._client.metadata.list_schemas, dataset
        )

    def list_fields(
        self, schema: str, encoding: str = "dbn", dataset: str | None = None
    ) -> list[dict[str, str]]:
        return self._cached(
            ("list_fields", schema, encoding, dataset),
            self._client.metadata.list_fields,
            schema,
            encoding,
            dataset=dataset,
        )

    def get_dataset_range(self, dataset: str) -> dict[str, str]:
        return self._cached(
            ("get_dataset_range", dataset), self._client.metadata.get_dataset_range, dataset
        )

    def list_unit_prices(self, dataset: str) -> list[dict[str, Any]]:
        return self._cached(
            ("list_unit_prices", dataset), self._client.metadata.list_unit_prices, dataset
        )

    # -- request-specific (never cached) -----------------------------------

    def get_dataset_condition(
        self,
        dataset: str,
        start_date: dt.date | str | None = None,
        end_date: dt.date | str | None = None,
    ) -> list[dict[str, Any]]:
        """Per-date coverage for a dataset. Defaults to 30 days, capped at a year."""
        end = _to_date(end_date) or dt.datetime.now(dt.UTC).date()
        start = _to_date(start_date) or end - dt.timedelta(days=DEFAULT_CONDITION_DAYS)
        if start > end:
            raise DatabentoQueryError(f"start_date {start} is after end_date {end}")
        if (end - start).days > MAX_CONDITION_DAYS:
            raise DatabentoQueryError(
                f"range {start}..{end} is wider than the {MAX_CONDITION_DAYS}-day cap; "
                "narrow it and ask again"
            )
        return self._call(
            self._client.metadata.get_dataset_condition,
            dataset,
            start_date=start,
            end_date=end,
        )

    def estimate_cost(
        self,
        dataset: str,
        start: str | dt.date | dt.datetime,
        end: str | dt.date | dt.datetime | None = None,
        symbols: Iterable[str | int] | str | int | None = None,
        schema: str = "trades",
        stype_in: str = "raw_symbol",
        mode: str | None = None,
    ) -> dict[str, Any]:
        """Cost, record count and billable size for one slice, in one call.

        Folding the three queries together is the point: record count and
        billable size are exactly the context for a cost figure, and returning
        them together saves two round trips.
        """
        kwargs: dict[str, Any] = {"start": start, "schema": schema, "stype_in": stype_in}
        if end is not None:
            kwargs["end"] = end
        if symbols is not None:
            kwargs["symbols"] = symbols
        if mode is not None:
            kwargs["mode"] = mode

        cost = self._call(self._client.metadata.get_cost, dataset, **kwargs)
        record_count = self._call(self._client.metadata.get_record_count, dataset, **kwargs)
        billable_bytes = self._call(self._client.metadata.get_billable_size, dataset, **kwargs)

        return {
            "dataset": dataset,
            "start": str(start),
            "end": str(end) if end is not None else None,
            "symbols": list(symbols) if isinstance(symbols, (list, tuple, set)) else symbols,
            "schema": schema,
            "cost_usd": float(cost),
            "record_count": int(record_count),
            "billable_bytes": int(billable_bytes),
        }

    def resolve_symbols(
        self,
        dataset: str,
        symbols: Sequence[str | int] | str | int,
        start_date: str | dt.date,
        end_date: str | dt.date | None = None,
        stype_in: str = "raw_symbol",
        stype_out: str = "instrument_id",
    ) -> dict[str, Any]:
        """Map symbols to instrument ids over a date range (Symbology API)."""
        return self._call(
            self._client.symbology.resolve,
            dataset,
            symbols,
            stype_in=stype_in,
            stype_out=stype_out,
            start_date=start_date,
            end_date=end_date,
        )


def _translate(exc: BentoClientError) -> Exception:
    status = getattr(exc, "http_status", None)
    if status == 429:
        return RateLimited(_retry_after(exc))
    return DatabentoQueryError(f"Databento returned HTTP {status}: {exc}")


def _retry_after(exc: BentoClientError) -> float:
    headers = getattr(exc, "headers", None) or {}
    try:
        return float(headers.get("retry-after", 60))
    except (TypeError, ValueError):
        return 60.0


def _to_date(value: dt.date | str | None) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def _key_date(value: dt.date | str | None) -> str | None:
    as_date = _to_date(value)
    return as_date.isoformat() if as_date is not None else None
