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
