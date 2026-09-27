"""Container entry point: python -m databento_discovery.

Serves the FastMCP app over Streamable HTTP on port 8772, with a GET /health
endpoint for the Docker healthcheck and the Homepage widget.
"""

from __future__ import annotations

import os
import sys

DEFAULT_PORT = 8772


def main() -> int:
    if not os.environ.get("DATABENTO_API_KEY"):
        print(
            "DATABENTO_API_KEY is not set; refusing to start without a key.",
            file=sys.stderr,
        )
        return 2

    from .server import build_server

    port = int(os.environ.get("MCP_PORT", DEFAULT_PORT))
    server = build_server(key=os.environ["DATABENTO_API_KEY"], port=port)
    server.run(transport="streamable-http")
    return 0


if __name__ == "__main__":
    sys.exit(main())
