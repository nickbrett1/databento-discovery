"""The FastMCP surface: nine read-only Databento discovery tools over HTTP.

Every tool here is free and read-only, so the group that exposes them has
nothing to gate. The spend boundary is enforced by what this file does *not*
register (no batch submission, no timeseries fetch) and by the name it carries.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .service import DatabentoService

INSTRUCTIONS = (
    "Read-only discovery over Databento's metadata. Answers what datasets, "
    "schemas, fields and coverage exist and what a slice would cost. Cannot "
    "download data or submit batch jobs."
)

TOOL_NAMES = (
    "list_datasets",
    "list_publishers",
    "list_schemas",
    "list_fields",
    "get_dataset_range",
    "get_dataset_condition",
    "list_unit_prices",
    "estimate_cost",
    "resolve_symbols",
)


def build_server(
    service: DatabentoService | None = None,
    *,
    key: str | None = None,
    host: str = "0.0.0.0",
    port: int = 8772,
) -> FastMCP:
    """Construct the server. Pass ``service`` in tests to avoid any network."""
    svc = service if service is not None else DatabentoService(key=key)
    mcp = FastMCP(
        "databento-discovery",
        instructions=INSTRUCTIONS,
        host=host,
        port=port,
    )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "databento-discovery"})

    @mcp.tool()
    def list_datasets(start_date: str | None = None, end_date: str | None = None) -> list[str]:
        """List Databento dataset codes (e.g. XNAS.ITCH), optionally for a date range."""
        return svc.list_datasets(start_date=start_date, end_date=end_date)

    @mcp.tool()
    def list_publishers() -> list[dict[str, Any]]:
        """List publishers and the datasets and venues each one covers."""
        return svc.list_publishers()

    @mcp.tool()
    def list_schemas(dataset: str) -> list[str]:
        """List the schemas available for a dataset (mbo, mbp-10, trades, ohlcv-1m, ...)."""
        return svc.list_schemas(dataset)

    @mcp.tool()
    def list_fields(schema: str, encoding: str = "dbn", dataset: str | None = None) -> list[dict[str, str]]:
        """List the fields of a schema: name, type, description and enum values."""
        return svc.list_fields(schema, encoding, dataset)

    @mcp.tool()
    def get_dataset_range(dataset: str) -> dict[str, str]:
        """Return a dataset's available {start_date, end_date}, as your key is entitled."""
        return svc.get_dataset_range(dataset)

    @mcp.tool()
    def get_dataset_condition(
        dataset: str, start_date: str | None = None, end_date: str | None = None
    ) -> list[dict[str, Any]]:
        """Per-date coverage (full | partial | missing | untested).

        Defaults to the last 30 days; the range is capped at 366 days.
        """
        return svc.get_dataset_condition(dataset, start_date, end_date)

    @mcp.tool()
    def list_unit_prices(dataset: str) -> list[dict[str, Any]]:
        """List the $/GB unit prices for a dataset, per feed mode and schema."""
        return svc.list_unit_prices(dataset)

    @mcp.tool()
    def estimate_cost(
        dataset: str,
        start: str,
        end: str | None = None,
        symbols: list[str] | None = None,
        schema: str = "trades",
        stype_in: str = "raw_symbol",
        mode: str | None = None,
    ) -> dict[str, Any]:
        """Estimate the cost of a slice: cost_usd, record_count and billable_bytes.

        Answers "what would a month of AAPL MBO cost?" without spending anything.
        """
        return svc.estimate_cost(
            dataset,
            start,
            end=end,
            symbols=symbols,
            schema=schema,
            stype_in=stype_in,
            mode=mode,
        )

    @mcp.tool()
    def resolve_symbols(
        dataset: str,
        symbols: list[str] | str,
        start_date: str,
        end_date: str | None = None,
        stype_in: str = "raw_symbol",
        stype_out: str = "instrument_id",
    ) -> dict[str, Any]:
        """Resolve symbols to instrument ids over a date range (Symbology API)."""
        return svc.resolve_symbols(
            dataset,
            symbols,
            start_date=start_date,
            end_date=end_date,
            stype_in=stype_in,
            stype_out=stype_out,
        )

    return mcp
