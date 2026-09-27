"""Tests for the MCP surface -- no network is touched."""

import asyncio

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
