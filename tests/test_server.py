"""Tests for the MCP surface -- no network is touched."""

import asyncio
import json

from databento_discovery.server import TOOL_NAMES, build_server
from databento_discovery.service import DatabentoService
from test_service import FakeClient


def test_all_nine_tools_are_registered():
    server = build_server(service=DatabentoService(client=FakeClient()))
    tools = asyncio.run(server.list_tools())
    assert sorted(t.name for t in tools) == sorted(TOOL_NAMES)
    assert len(TOOL_NAMES) == 9


def test_no_spending_tool_is_exposed():
    server = build_server(service=DatabentoService(client=FakeClient()))
    names = {t.name for t in asyncio.run(server.list_tools())}
    for forbidden in ("batch_submit_job", "timeseries_get_range", "get_range"):
        assert forbidden not in names


def test_get_dataset_range_tool_survives_nested_schema_blocks():
    """Regression: the API nests {start, end} per schema; the declared output
    model must not reject that shape."""
    server = build_server(service=DatabentoService(client=FakeClient()))
    content, structured = asyncio.run(
        server.call_tool("get_dataset_range", {"dataset": "XNAS.ITCH"})
    )
    payload = structured or json.loads("".join(getattr(c, "text", "") for c in content))
    assert payload["mbo"] == {"start": "2020-01-01", "end": "2025-01-01"}


def test_resolve_symbols_tool_declares_limit_and_offset():
    server = build_server(service=DatabentoService(client=FakeClient()))
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    props = tools["resolve_symbols"].inputSchema["properties"]
    assert "limit" in props
    assert "offset" in props


def test_resolve_symbols_tool_returns_a_bounded_paged_envelope():
    server = build_server(service=DatabentoService(client=FakeClient(bulk_size=12593)))
    content, structured = asyncio.run(
        server.call_tool(
            "resolve_symbols",
            {
                "dataset": "XNAS.ITCH",
                "symbols": "ALL_SYMBOLS",
                "start_date": "2026-01-01",
                "end_date": "2026-02-01",
            },
        )
    )
    payload = structured or json.loads("".join(getattr(c, "text", "") for c in content))
    assert {"total", "returned", "offset", "next_offset", "truncated", "result"} <= set(payload)
    assert payload["total"] == 12593
    assert payload["truncated"] is True
    assert payload["returned"] <= 1000
    # The tool result the model receives is a bounded page, not a 1.4 MB blob.
    assert len(json.dumps(payload).encode()) < 50_000


def test_list_fields_docstring_matches_actual_response():
    """The API returns {name, type} only -- the tool description must not promise
    descriptions or enum values it cannot deliver."""
    server = build_server(service=DatabentoService(client=FakeClient()))
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    description = (tools["list_fields"].description or "").lower()
    assert "enum" not in description
    assert "description" not in description
    assert "name" in description and "type" in description
