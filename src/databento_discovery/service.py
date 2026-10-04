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

# `resolve_symbols` with a bulk spec (e.g. ``ALL_SYMBOLS`` on a US-equities
# dataset) matches ~12.6k symbols and used to return ~1.4 MB of JSON in one
# result. That blob lands in the caller's context and is re-sent whole every
# turn, which is how a single call produced 400k-token prompts. Cap it the same
# way the condition window is capped: page the *response* rather than truncate
# it, so nothing the caller asked for is lost. A page of ~100 mappings measures
# ~11 KB / ~3k tokens.
DEFAULT_SYMBOL_LIMIT = 100
MAX_SYMBOL_LIMIT = 1000


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

    def get_dataset_range(self, dataset: str) -> dict[str, Any]:
        return self._cached(
            ("get_dataset_range", dataset), self._client.metadata.get_dataset_range, dataset
        )

    def list_unit_prices(self, dataset: str) -> list[dict[str, Any]]:
        return self._cached(
            ("list_unit_prices", dataset), self._client.metadata.list_unit_prices, dataset
        )

    # -- request-specific (never cached) ------------------------------------
    # Exception: `resolve_symbols` caches its full upstream snapshot so a caller
    # can page one consistent result without re-fetching the whole universe per
    # page. See its docstring.

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
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Map symbols to instrument ids over a date range (Symbology API).

        The upstream response maps one entry per symbol, and a bulk spec such as
        ``ALL_SYMBOLS`` matches thousands of them (~1.4 MB / ~358k tokens for a
        US-equities dataset). Returning that whole would blow the caller's
        context, so the result is **paged**: at most ``limit`` mappings (default
        :data:`DEFAULT_SYMBOL_LIMIT`, hard maximum :data:`MAX_SYMBOL_LIMIT`) are
        returned per call, with ``offset`` / ``next_offset`` / ``truncated`` /
        ``total`` so the caller can page through every mapping losslessly.

        The full upstream snapshot is cached per request so that paging is cheap
        and every page is sliced from one consistent, stable-ordered result.
        """
        page_limit = _symbol_limit(limit)
        page_offset = _symbol_offset(offset)
        raw = self._cached(
            (
                "resolve_symbols",
                dataset,
                _symbols_key(symbols),
                stype_in,
                stype_out,
                str(start_date),
                None if end_date is None else str(end_date),
            ),
            self._client.symbology.resolve,
            dataset,
            symbols,
            stype_in=stype_in,
            stype_out=stype_out,
            start_date=start_date,
            end_date=end_date,
        )
        return _page_resolve(raw, page_limit, page_offset)


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


def _symbols_key(symbols: Sequence[str | int] | str | int) -> Any:
    """Normalise the ``symbols`` argument into something hashable for caching."""
    if isinstance(symbols, (list, tuple, set)):
        return tuple(str(s) for s in symbols)
    return str(symbols)


def _symbol_limit(limit: int | None) -> int:
    """Resolve a requested page size to an effective one, bounded by the max."""
    if limit is None:
        return DEFAULT_SYMBOL_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise DatabentoQueryError("limit must be a positive integer")
    return min(limit, MAX_SYMBOL_LIMIT)


def _symbol_offset(offset: int) -> int:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise DatabentoQueryError("offset must be a non-negative integer")
    return offset


def _page_resolve(raw: Any, limit: int, offset: int) -> dict[str, Any]:
    """Slice one upstream ``symbology.resolve`` response into a paged envelope.

    The upstream ``result`` is a mapping of symbol -> interval list. Keys are
    sorted before slicing so that paging stays stable and lossless even across
    separate upstream fetches. Everything else the API returns is preserved
    (``symbols``/``stype_in``/``stype_out``/``start_date``/``end_date``/
    ``partial``/``not_found``/``message``/``status``) so no information is lost.
    """
    envelope: dict[str, Any] = {
        "result": None,
        "total": None,
        "returned": None,
        "offset": offset,
        "next_offset": None,
        "truncated": False,
        "limit": limit,
    }

    mapping = raw.get("result") if isinstance(raw, dict) else None
    if isinstance(mapping, dict):
        keys = sorted(mapping)
        total = len(keys)
        page_keys = keys[offset : offset + limit]
        page = {key: mapping[key] for key in page_keys}
        returned = len(page)
        consumed = offset + returned
        next_offset = consumed if consumed < total else None
        envelope.update(
            result=page,
            total=total,
            returned=returned,
            next_offset=next_offset,
            truncated=next_offset is not None,
        )
    else:
        # Unexpected shape (or a non-paged payload): surface it verbatim rather
        # than inventing a page, so a future upstream change degrades loudly.
        envelope["result"] = mapping if mapping is not None else raw

    if isinstance(raw, dict):
        for key in (
            "symbols",
            "stype_in",
            "stype_out",
            "start_date",
            "end_date",
            "partial",
            "not_found",
            "message",
            "status",
        ):
            if key in raw:
                envelope[key] = raw[key]
    return envelope
