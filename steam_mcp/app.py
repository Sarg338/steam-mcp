"""FastMCP application instance for the Steam MCP server."""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("steam_mcp")

# Security: httpx/httpcore log full request URLs at INFO, and Steam requires the
# API key as a `?key=` query param — so quiet those loggers to keep the key out of
# any logs the host might capture.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
