#!/usr/bin/env python3
"""Measure real response sizes from the deployed databento-discovery MCP server.

Drives the live MCP endpoint over Streamable HTTP and records, per tool call,
the byte size / char count of the JSON payload the model would actually receive.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

URL = sys.argv[1] if len(sys.argv) > 1 else "http://nas:8772/mcp"

CALLS = [
    ("list_datasets", {}),
    ("list_publishers", {}),
    # (dataset, schema) pairs that exercise the field surface
    ("list_schemas", {"dataset": "XNAS.ITCH"}),
    ("list_fields", {"schema": "mbo", "encoding": "dbn"}),
    ("list_fields", {"schema": "mbp-10", "encoding": "dbn"}),
    ("list_fields", {"schema": "mbp-1", "encoding": "dbn"}),
    ("list_fields", {"schema": "trades", "encoding": "dbn"}),
    ("list_fields", {"schema": "ohlcv-1m", "encoding": "dbn"}),
    ("list_fields", {"schema": "definition", "encoding": "dbn"}),
    ("list_fields", {"schema": "statistics", "encoding": "dbn"}),
    ("get_dataset_range", {"dataset": "XNAS.ITCH"}),
    ("get_dataset_range", {"dataset": "GLBX.MDP3"}),
    ("list_unit_prices", {"dataset": "XNAS.ITCH"}),
]


async def main() -> None:
    rows = []
    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            schema_bytes = len(json.dumps([t.model_dump() for t in tools.tools]))
            print(f"# server {URL}")
            print(f"# declared tools: {len(tools.tools)}; tool-schema JSON: {schema_bytes} bytes")
            for name, args in CALLS:
                t0 = time.monotonic()
                try:
                    res = await session.call_tool(name, args)
                except Exception as exc:  # noqa: BLE001
                    print(f"{name:16} {json.dumps(args):40} ERROR {exc!r}")
                    continue
                elapsed = time.monotonic() - t0
                texts = [c.text for c in res.content if getattr(c, "type", None) == "text"]
                payload = "\n".join(texts)
                raw = payload.encode("utf-8")
                # try to parse the JSON payload for a record count
                recs = ""
                try:
                    parsed = json.loads(payload)
                    if isinstance(parsed, list):
                        recs = f" list_len={len(parsed)}"
                    elif isinstance(parsed, dict):
                        recs = f" dict_keys={len(parsed)}"
                except Exception:  # noqa: BLE001
                    pass
                is_err = getattr(res, "isError", False)
                rows.append((name, json.dumps(args), len(raw), recs, elapsed, is_err))
                print(
                    f"{name:16} {json.dumps(args):40} bytes={len(raw):>9} "
                    f"~tokens={len(raw)//4:>8}{recs} t={elapsed:.1f}s err={is_err}"
                )
    print("\n# summary (sorted by bytes)")
    for name, args, n, recs, _el, is_err in sorted(rows, key=lambda r: -r[2]):
        print(f"{n:>9}  {name} {args}")


if __name__ == "__main__":
    asyncio.run(main())
