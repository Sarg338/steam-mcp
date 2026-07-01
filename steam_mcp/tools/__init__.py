"""Tool modules for the Steam MCP server.

Importing this package imports every tool module, whose @mcp.tool decorators
register the tools on the shared FastMCP app as a side effect. The list is
explicit on purpose — no dynamic discovery.
"""

from steam_mcp.tools import achievements, deals, discovery, friends, intelligence, library, market, player, reviews, store  # noqa: F401
