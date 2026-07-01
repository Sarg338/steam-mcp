"""Resources — reference Steam entities by URI (steam://app/{id}, steam://user/{id})."""

from __future__ import annotations

from steam_mcp.app import mcp
from steam_mcp.models import PlayersInput
from steam_mcp.tools.player import steam_get_player_summary
from steam_mcp.tools.store import AppDetailsInput, steam_get_app_details


@mcp.resource(
    "steam://app/{appid}",
    name="Steam app details",
    description="Store details for a Steam app by appid.",
    mime_type="text/markdown",
)
async def resource_app(appid: str) -> str:
    """Resolve steam://app/<appid> to the app's store details (markdown)."""
    try:
        aid = int(appid)
    except (TypeError, ValueError):
        return f"Invalid appid: {appid!r}"
    return await steam_get_app_details(AppDetailsInput(appid=aid))


@mcp.resource(
    "steam://user/{steamid}",
    name="Steam player summary",
    description="Profile + live status for a Steam user (SteamID64, vanity, or URL).",
    mime_type="text/markdown",
)
async def resource_user(steamid: str) -> str:
    """Resolve steam://user/<steamid> to the player's summary (markdown)."""
    return await steam_get_player_summary(PlayersInput(steamids=[steamid]))
