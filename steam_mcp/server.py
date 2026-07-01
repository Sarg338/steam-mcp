#!/usr/bin/env python3
"""
Steam MCP Server (read-only, bring-your-own-key).

Exposes the public Steam Web API and storefront API as MCP tools so an LLM can
answer natural questions like "who are my Steam friends", "how many hours have I
played in X", "what achievements am I missing", and "what is this game about".

Authentication model (IMPORTANT):
    This server uses a single Steam Web API key supplied by whoever RUNS the
    server, via the STEAM_API_KEY environment variable. The key is the *caller's*
    credential -- with it you can look up ANY user's PUBLIC profile data by their
    SteamID. End users do not log in. Private / friends-only profiles return no
    data regardless of the key. There is no OAuth flow that unlocks another user's
    private data.

Get a key (free): https://steamcommunity.com/dev/apikey
"""

from __future__ import annotations

from steam_mcp.app import mcp

# Importing steam_mcp.tools imports every tool module, whose @mcp.tool decorators
# register the tools on the shared FastMCP app. Registering the prompts and
# resources AFTER the tools keeps the wire surface ordered
# tools -> prompts -> resources.
import steam_mcp.tools  # noqa: F401  (registration side effects)
from steam_mcp import prompts, resources  # noqa: F401  (registration side effects)


def _compact_descriptions() -> None:
    """Trim each tool's *wire* description to its one-line summary.

    FastMCP sends a tool's full docstring as its MCP description, so the model pays
    for all of them on every request (~5k tokens across our tools). The first line
    of each docstring is already a complete summary, so the description sent over
    the wire is trimmed to that — the full docstrings stay in source for humans and
    IDEs. Best-effort: if the SDK internals change, descriptions simply stay full.
    """
    try:
        tools = list(mcp._tool_manager._tools.values())
    except Exception:  # noqa: BLE001
        return
    for tool in tools:
        desc = (getattr(tool, "description", None) or "").strip()
        if not desc:
            continue
        summary = desc.split("\n\n", 1)[0].split("\n", 1)[0].strip()
        if summary and len(summary) < len(desc):
            try:
                tool.description = summary
            except Exception:  # noqa: BLE001
                pass


_compact_descriptions()


def main() -> None:
    """Run the server over stdio (default MCP transport for local clients)."""
    mcp.run()


if __name__ == "__main__":
    main()
