"""Player data helpers (summaries, ownership, presence) for the Steam MCP server."""

from __future__ import annotations

from typing import Optional

from steam_mcp import transport


async def _summaries_for(steamids: list[str]) -> dict[str, dict]:
    """Fetch player summaries for many SteamIDs, chunked at 100 per call.

    Returns a dict keyed by SteamID64.
    """
    out: dict[str, dict] = {}
    for i in range(0, len(steamids), 100):
        chunk = steamids[i : i + 100]
        data = await transport._steam_get(
            "ISteamUser/GetPlayerSummaries/v2/",
            {"steamids": ",".join(chunk)},
        )
        for p in data.get("response", {}).get("players", []):
            out[p["steamid"]] = p
    return out


async def _owned_set(sid: str) -> Optional[set]:
    """Return a user's owned appids as a set, or None if their library is private."""
    d = await transport._steam_get(
        "IPlayerService/GetOwnedGames/v1/",
        {"steamid": sid, "include_appinfo": 0, "include_played_free_games": 1},
    )
    resp = d.get("response", {})
    if not resp:
        return None
    return {g.get("appid") for g in resp.get("games", []) if g.get("appid")}


def _is_online(p: dict) -> bool:
    """True if a player summary indicates online or in-game (not Offline)."""
    return bool(p) and (p.get("personastate", 0) != 0 or bool(p.get("gameextrainfo")))
