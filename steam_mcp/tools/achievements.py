"""Achievement & stat tools: player achievements, game schema, global rarity, user stats, rarest unlocks."""

from __future__ import annotations

import asyncio

from pydantic import Field

from steam_mcp import errors, identity, render, transport
from steam_mcp.app import mcp
from steam_mcp.cache import CACHE_TTL_GLOBAL_ACH, CACHE_TTL_SCHEMA
from steam_mcp.models import AppOnlyInput, PlayerGameInput
from steam_mcp.render import ResponseFormat


@mcp.tool(
    name="steam_get_player_achievements",
    annotations={
        "title": "Get Steam Player Achievements",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_player_achievements(params: PlayerGameInput) -> str:
    """Get a user's achievement progress for a specific game.

    Reports how many achievements are unlocked vs total, and lists locked ones.
    Use steam_search_apps or steam_get_owned_games first if you only know the game
    name and need its appid. Requires the profile's game details to be PUBLIC and
    the game to have achievements.

    Args:
        params (PlayerGameInput): steamid, appid.

    Returns:
        str: Markdown or JSON. Includes game name, unlocked count, total count,
        completion percentage, and a list of locked achievements.
    """
    try:
        sid = await identity._resolve_steamid(params.steamid)
        data = await transport._steam_get(
            "ISteamUserStats/GetPlayerAchievements/v1/",
            {"steamid": sid, "appid": params.appid, "l": params.language},
        )
        stats = data.get("playerstats", {})
        if not stats.get("success", False):
            return (
                f"Error: {stats.get('error', 'No achievement data')}. Game details "
                f"may not be public, or app {params.appid} has no achievements. "
                + errors._privacy_hint("Game details")
            )
        achievements = stats.get("achievements", [])
        total = len(achievements)
        unlocked = [a for a in achievements if a.get("achieved") == 1]
        locked = [a for a in achievements if a.get("achieved") != 1]
        pct = round(100.0 * len(unlocked) / total, 1) if total else 0.0
        game_name = stats.get("gameName", str(params.appid))

        if params.response_format == ResponseFormat.JSON:
            return render._dump(
                {
                    "steamid": sid,
                    "appid": params.appid,
                    "game": game_name,
                    "unlocked": len(unlocked),
                    "total": total,
                    "completion_pct": pct,
                    "locked": [
                        {"api_name": a.get("apiname"), "name": a.get("name")}
                        for a in locked
                    ],
                }
            )

        lines = [
            f"# Achievements: {game_name} (appid {params.appid})",
            f"Unlocked **{len(unlocked)} / {total}** ({pct}%) for {sid}.",
            "",
        ]
        if locked:
            lines.append(f"## Still locked ({len(locked)})")
            for a in locked[:50]:
                name = a.get("name") or a.get("apiname")
                desc = f" — {a['description']}" if a.get("description") else ""
                lines.append(f"- {name}{desc}")
            if len(locked) > 50:
                lines.append(f"- …and {len(locked) - 50} more")
        else:
            lines.append("🏆 All achievements unlocked!")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)


@mcp.tool(
    name="steam_get_game_schema",
    annotations={
        "title": "Get Steam Game Schema",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_game_schema(params: AppOnlyInput) -> str:
    """Get the achievement and stat definitions for a game (not user-specific).

    Useful to see the full list of achievements a game offers, with display names
    and descriptions, independent of any player.

    Args:
        params (AppOnlyInput): appid.

    Returns:
        str: Markdown or JSON. game name plus achievement definitions
        (api_name, display_name, description, hidden).
    """
    try:
        data = await transport._steam_get(
            "ISteamUserStats/GetSchemaForGame/v2/",
            {"appid": params.appid},
            cache_ttl=CACHE_TTL_SCHEMA,
        )
        game = data.get("game", {})
        ach = game.get("availableGameStats", {}).get("achievements", [])
        rows = [
            {
                "api_name": a.get("name"),
                "display_name": a.get("displayName"),
                "description": a.get("description", ""),
                "hidden": bool(a.get("hidden", 0)),
            }
            for a in ach
        ]
        name = game.get("gameName", str(params.appid))
        if params.response_format == ResponseFormat.JSON:
            return render._dump({"appid": params.appid, "game": name, "achievements": rows})

        lines = [
            f"# Schema: {name} (appid {params.appid})",
            f"{len(rows)} achievements defined.",
            "",
        ]
        for r in rows[:100]:
            hidden = " [hidden]" if r["hidden"] else ""
            lines.append(f"- **{r['display_name']}**{hidden}: {r['description']}")
        if len(rows) > 100:
            lines.append(f"- …and {len(rows) - 100} more")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)


@mcp.tool(
    name="steam_get_global_achievement_percentages",
    annotations={
        "title": "Get Global Achievement Rarity",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_global_achievement_percentages(params: AppOnlyInput) -> str:
    """Get the global unlock percentage (rarity) of each achievement in a game.

    Lower percentages mean rarer achievements. Pair with
    steam_get_player_achievements to tell a user which of their unlocks are rarest.

    Args:
        params (AppOnlyInput): appid.

    Returns:
        str: Markdown or JSON. Per achievement: api_name, global_pct
        (sorted rarest first).
    """
    try:
        data = await transport._steam_get(
            "ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/",
            {"gameid": params.appid},
            with_key=False,  # this endpoint does not require a key
            cache_ttl=CACHE_TTL_GLOBAL_ACH,
        )
        ach = data.get("achievementpercentages", {}).get("achievements", [])
        rows = sorted(
            (
                {"api_name": a.get("name"), "global_pct": round(a.get("percent", 0), 2)}
                for a in ach
            ),
            key=lambda r: r["global_pct"],
        )
        if not rows:
            return f"No global achievement data for app {params.appid}."
        if params.response_format == ResponseFormat.JSON:
            return render._dump({"appid": params.appid, "achievements": rows})

        lines = [f"# Achievement rarity for app {params.appid} (rarest first)", ""]
        for r in rows[:50]:
            lines.append(f"- {r['api_name']}: {r['global_pct']}% of players")
        if len(rows) > 50:
            lines.append(f"- …and {len(rows) - 50} more")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)


@mcp.tool(
    name="steam_get_user_game_stats",
    annotations={
        "title": "Get Steam User Game Stats",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_user_game_stats(params: PlayerGameInput) -> str:
    """Get a user's in-game STATS for a specific game (kills, wins, distance, etc.).

    Complements steam_get_player_achievements: where that lists achievement
    unlocks, this returns the numeric gameplay stats a game tracks — whatever the
    developer defined (e.g. total kills, matches won, distance travelled). Use
    steam_search_apps or steam_get_owned_games first if you only have a game name.
    Requires the profile's Game Details to be PUBLIC and the game to define stats;
    many games define none (then this returns an empty result). Needs an API key.

    Args:
        params (PlayerGameInput): steamid, appid.

    Returns:
        str: Markdown or JSON. game name plus each tracked stat (name, value).
    """
    try:
        sid = await identity._resolve_steamid(params.steamid)
        data = await transport._steam_get(
            "ISteamUserStats/GetUserStatsForGame/v2/",
            {"steamid": sid, "appid": params.appid, "l": params.language},
        )
        stats_obj = data.get("playerstats", {})
        stats = stats_obj.get("stats", []) or []
        game_name = stats_obj.get("gameName") or str(params.appid)
        if not stats:
            return (
                f"No stats available for app {params.appid}. The game may define no "
                f"stats, or Game details aren't public. " + errors._privacy_hint("Game details")
            )
        rows = [{"name": s.get("name"), "value": s.get("value")} for s in stats]

        if params.response_format == ResponseFormat.JSON:
            return render._dump(
                {
                    "steamid": sid,
                    "appid": params.appid,
                    "game": game_name,
                    "stat_count": len(rows),
                    "stats": rows,
                }
            )

        lines = [
            f"# Stats: {game_name} (appid {params.appid})",
            f"{len(rows)} stats tracked for {sid}.",
            "",
        ]
        for r in rows[:100]:
            lines.append(f"- **{r['name']}**: {r['value']}")
        if len(rows) > 100:
            lines.append(f"- …and {len(rows) - 100} more")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)


class RarestUnlocksInput(PlayerGameInput):
    limit: int = Field(
        default=10,
        description="How many of the rarest unlocked achievements to list (1-50).",
        ge=1, le=50,
    )


@mcp.tool(
    name="steam_get_rarest_unlocks",
    annotations={
        "title": "Get Player's Rarest Achievement Unlocks",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_rarest_unlocks(params: RarestUnlocksInput) -> str:
    """Show a player's RAREST unlocked achievements in a game (by global unlock %).

    Joins the player's unlocked achievements with each one's global unlock rarity to
    surface their most impressive "flexes" — achievements few players ever earn. Does
    in one step what pairing steam_get_player_achievements with
    steam_get_global_achievement_percentages would. Requires the profile's game
    details to be PUBLIC and the game to have achievements. Needs an API key.

    Args:
        params (RarestUnlocksInput): steamid, appid, limit.

    Returns:
        str: Markdown or JSON. game name, total unlocked count, and the rarest
        unlocked achievements (name, global_pct, unlocked_at), rarest first.
    """
    try:
        sid = await identity._resolve_steamid(params.steamid)
        ach_data, glob_data = await asyncio.gather(
            transport._steam_get(
                "ISteamUserStats/GetPlayerAchievements/v1/",
                {"steamid": sid, "appid": params.appid, "l": params.language},
            ),
            transport._steam_get(
                "ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/",
                {"gameid": params.appid},
                with_key=False,
                cache_ttl=CACHE_TTL_GLOBAL_ACH,
            ),
        )
        stats = ach_data.get("playerstats", {})
        if not stats.get("success", False):
            return (
                f"Error: {stats.get('error', 'No achievement data')}. Game details "
                f"may not be public, or app {params.appid} has no achievements. "
                + errors._privacy_hint("Game details")
            )
        unlocked = [a for a in stats.get("achievements", []) if a.get("achieved") == 1]
        if not unlocked:
            return f"{sid} has no unlocked achievements in app {params.appid}."
        pct_map = {
            g.get("name"): g.get("percent", 0.0)
            for g in glob_data.get("achievementpercentages", {}).get("achievements", [])
        }
        rows = []
        for a in unlocked:
            api = a.get("apiname")
            pct = round(pct_map[api], 2) if api in pct_map else None
            rows.append(
                {
                    "name": a.get("name") or api,
                    "api_name": api,
                    "global_pct": pct,
                    "unlocked_at": render._ts_to_date(a.get("unlocktime")),
                }
            )
        rows.sort(key=lambda r: (r["global_pct"] is None, r["global_pct"] or 0.0))
        game_name = stats.get("gameName", str(params.appid))
        page = rows[: params.limit]

        if params.response_format == ResponseFormat.JSON:
            return render._dump(
                {
                    "steamid": sid,
                    "appid": params.appid,
                    "game": game_name,
                    "unlocked_count": len(unlocked),
                    "rarest": page,
                }
            )

        lines = [
            f"# Rarest unlocks: {game_name} (appid {params.appid})",
            f"{sid} has unlocked {len(unlocked)} achievements — rarest first:",
            "",
        ]
        for r in page:
            pct = f"{r['global_pct']}%" if r["global_pct"] is not None else "rarity n/a"
            when = f" (unlocked {r['unlocked_at']})" if r["unlocked_at"] else ""
            lines.append(f"- **{r['name']}** — {pct} of players{when}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)
