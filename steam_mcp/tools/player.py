"""Player identity/profile tools: vanity resolution, summaries, level, bans, badges, groups."""

from __future__ import annotations

import re

from pydantic import Field

from steam_mcp import errors, identity, render, transport
from steam_mcp.app import mcp
from steam_mcp.cache import CACHE_TTL_GROUP
from steam_mcp.constants import VISIBILITY_STATES
from steam_mcp.data import players
from steam_mcp.models import PlayerInput, PlayersInput
from steam_mcp.render import ResponseFormat


@mcp.tool(
    name="steam_resolve_vanity_url",
    annotations={
        "title": "Resolve Steam Vanity URL",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_resolve_vanity_url(params: PlayerInput) -> str:
    """Resolve a Steam vanity/custom-URL name (or profile URL) to a SteamID64.

    Most Steam Web API endpoints require a numeric 17-digit SteamID64, but people
    usually know their custom URL name (steamcommunity.com/id/<name>). Use this to
    convert one to the other. If given a SteamID64 already, it is returned as-is.

    Args:
        params (PlayerInput): steamid (vanity name, SteamID64, or profile URL).

    Returns:
        str: The resolved SteamID64, or an Error string if it cannot be resolved.
    """
    try:
        resolved = await identity._resolve_steamid(params.steamid)
        if params.response_format == ResponseFormat.JSON:
            return render._dump({"input": params.steamid, "steamid64": resolved})
        return f"SteamID64 for '{params.steamid}': {resolved}"
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)


@mcp.tool(
    name="steam_get_player_summary",
    annotations={
        "title": "Get Steam Player Summary",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_player_summary(params: PlayersInput) -> str:
    """Get profile + current status for one or more Steam users.

    Returns persona name, profile visibility, online status (Online / Away / Busy /
    Snooze / Offline), and the game they are currently playing (if any). This is the
    primary tool for "is X online" and "what is X playing right now".

    Args:
        params (PlayersInput): steamids (list of up to 100 IDs/vanity names/URLs).

    Returns:
        str: Markdown or JSON. Per player: steamid, name, status, current_game,
        visibility, profile_url, country (if public), last_logoff.
    """
    try:
        resolved = [await identity._resolve_steamid(s) for s in (params.steamids or [None])]
        summaries = await players._summaries_for(resolved)
        player_rows = [summaries[s] for s in resolved if s in summaries]
        if not player_rows:
            return ("No player data found — the profile may be private, or the SteamID "
                "is invalid. " + errors._privacy_hint("My profile"))

        if params.response_format == ResponseFormat.JSON:
            return render._dump({"count": len(player_rows), "players": player_rows})

        lines = [f"# Steam Players ({len(player_rows)})", ""]
        for p in player_rows:
            lines.append(f"## {p.get('personaname', 'Unknown')} ({p['steamid']})")
            lines.append(f"- **Status**: {render._persona_label(p)}")
            lines.append(
                f"- **Visibility**: "
                f"{VISIBILITY_STATES.get(p.get('communityvisibilitystate'), 'Unknown')}"
            )
            if p.get("loccountrycode"):
                lines.append(f"- **Country**: {p['loccountrycode']}")
            if p.get("profileurl"):
                lines.append(f"- **Profile**: {p['profileurl']}")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)


@mcp.tool(
    name="steam_get_steam_level",
    annotations={
        "title": "Get Steam Level",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_steam_level(params: PlayerInput) -> str:
    """Get a user's Steam community level (the XP-based account level).

    Args:
        params (PlayerInput): steamid.

    Returns:
        str: The Steam level, or an Error string.
    """
    try:
        sid = await identity._resolve_steamid(params.steamid)
        data = await transport._steam_get("IPlayerService/GetSteamLevel/v1/", {"steamid": sid})
        level = data.get("response", {}).get("player_level")
        if level is None:
            return ("No level data — the profile may not be public. "
                    + errors._privacy_hint("My profile"))
        if params.response_format == ResponseFormat.JSON:
            return render._dump({"steamid": sid, "steam_level": level})
        return f"Steam level for {sid}: {level}"
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)


@mcp.tool(
    name="steam_get_player_bans",
    annotations={
        "title": "Get Steam Player Bans",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_player_bans(params: PlayerInput) -> str:
    """Get VAC / game / community / economy ban status for a user.

    Args:
        params (PlayerInput): steamid.

    Returns:
        str: Ban summary (VACBanned, NumberOfVACBans, DaysSinceLastBan,
        CommunityBanned, EconomyBan, NumberOfGameBans), or an Error string.
    """
    try:
        sid = await identity._resolve_steamid(params.steamid)
        data = await transport._steam_get("ISteamUser/GetPlayerBans/v1/", {"steamids": sid})
        bans = data.get("players", [])
        if not bans:
            return "No ban data found for that user."
        b = bans[0]
        if params.response_format == ResponseFormat.JSON:
            return render._dump(b)
        return (
            f"# Ban status for {sid}\n"
            f"- **VAC banned**: {b.get('VACBanned')} "
            f"({b.get('NumberOfVACBans', 0)} VAC ban(s))\n"
            f"- **Days since last ban**: {b.get('DaysSinceLastBan', 0)}\n"
            f"- **Game bans**: {b.get('NumberOfGameBans', 0)}\n"
            f"- **Community banned**: {b.get('CommunityBanned')}\n"
            f"- **Economy ban**: {b.get('EconomyBan', 'none')}"
        )
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)


@mcp.tool(
    name="steam_get_player_badges",
    annotations={
        "title": "Get Steam Player Badges",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_player_badges(params: PlayerInput) -> str:
    """Get a user's badges and the XP breakdown behind their Steam level.

    Answers "what badges do I have" and "how is my Steam level made up". Reports
    the level, total XP, XP needed to reach the next level, badge count, and the
    highest-XP badges. Requires the profile to be Public. Needs an API key.

    Args:
        params (PlayerInput): steamid.

    Returns:
        str: Markdown or JSON. player_level, player_xp, xp_needed_to_level_up,
        badge_count, and top badges (badgeid, appid, level, xp, scarcity).
    """
    try:
        sid = await identity._resolve_steamid(params.steamid)
        data = await transport._steam_get("IPlayerService/GetBadges/v1/", {"steamid": sid})
        resp = data.get("response", {})
        badges = resp.get("badges", [])
        if not resp or resp.get("player_level") is None:
            return ("No badge data — the profile may not be public. "
                    + errors._privacy_hint("My profile"))
        level = resp.get("player_level")
        xp = resp.get("player_xp") or 0
        to_next = resp.get("player_xp_needed_to_level_up") or 0
        top = sorted(badges, key=lambda b: b.get("xp", 0), reverse=True)[:15]

        if params.response_format == ResponseFormat.JSON:
            return render._dump(
                {
                    "steamid": sid,
                    "player_level": level,
                    "player_xp": xp,
                    "xp_needed_to_level_up": to_next,
                    "badge_count": len(badges),
                    "badges": [
                        {
                            "badgeid": b.get("badgeid"),
                            "appid": b.get("appid"),
                            "level": b.get("level"),
                            "xp": b.get("xp"),
                            "scarcity": b.get("scarcity"),
                        }
                        for b in top
                    ],
                }
            )

        lines = [
            f"# Badges for {sid}",
            f"- **Steam level**: {level} (XP {xp:,}; {to_next:,} to next level)",
            f"- **Badges earned**: {len(badges)}",
        ]
        if top:
            lines += ["", "## Top badges by XP"]
            for b in top:
                what = f"game {b['appid']}" if b.get("appid") else f"badge {b.get('badgeid')}"
                lines.append(
                    f"- {what}: level {b.get('level')}, {b.get('xp')} XP "
                    f"(owned by {b.get('scarcity')} users)"
                )
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)


class UserGroupsInput(PlayerInput):
    limit: int = Field(
        default=20, ge=1, le=100,
        description="Max groups to return; each enriched group is one extra lookup.",
    )
    enrich: bool = Field(
        default=True,
        description="Fetch each group's name, URL, and member count. Set false for "
        "a fast group-ID-only list.",
    )


async def _group_details(gid: str) -> dict:
    """Fetch a Steam group's name/url/member-count from its community memberlist XML."""
    fallback = {"gid": gid, "name": None,
                "url": f"https://steamcommunity.com/gid/{gid}", "member_count": None}
    try:
        xml = await transport._raw_get_text(
            f"https://steamcommunity.com/gid/{gid}/memberslistxml/",
            {"xml": 1}, cache_ttl=CACHE_TTL_GROUP,
        )
    except Exception:  # noqa: BLE001
        return fallback

    def _cdata(tag):
        m = re.search(rf"<{tag}><!\[CDATA\[(.*?)\]\]></{tag}>", xml, re.S)
        return m.group(1).strip() if m else None

    vanity = _cdata("groupURL")
    mc = re.search(r"<memberCount>(\d+)</memberCount>", xml)
    return {
        "gid": gid,
        "name": _cdata("groupName"),
        "url": (f"https://steamcommunity.com/groups/{vanity}" if vanity
                else f"https://steamcommunity.com/gid/{gid}"),
        "member_count": int(mc.group(1)) if mc else None,
    }


@mcp.tool(
    name="steam_get_user_groups",
    annotations={
        "title": "Get Steam User Groups",
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def steam_get_user_groups(params: UserGroupsInput) -> str:
    """List the Steam groups (communities/clans) a user belongs to.

    GetUserGroupList returns only group IDs, so with enrich=true each is resolved to
    its name, community URL, and member count (sorted by size). Requires the
    profile's group list to be Public. Needs an API key.

    Args:
        params (UserGroupsInput): steamid, limit, enrich.

    Returns:
        str: Markdown or JSON. total group count plus, per group, gid and (when
        enriched) name, url, and member_count.
    """
    try:
        sid = await identity._resolve_steamid(params.steamid)
        data = await transport._steam_get("ISteamUser/GetUserGroupList/v1/", {"steamid": sid})
        resp = data.get("response", {})
        if not resp.get("success"):
            return ("No group data — the profile may not be public. "
                    + errors._privacy_hint("My profile"))
        gids = [g.get("gid") for g in resp.get("groups", []) if g.get("gid")]
        if not gids:
            return f"{sid} is not in any public Steam groups."
        total = len(gids)
        page = gids[: params.limit]
        if params.enrich:
            groups = await transport._gather_limited([_group_details(g) for g in page])
            groups.sort(key=lambda d: d.get("member_count") or 0, reverse=True)
        else:
            groups = [{"gid": g, "name": None,
                       "url": f"https://steamcommunity.com/gid/{g}",
                       "member_count": None} for g in page]

        if params.response_format == ResponseFormat.JSON:
            return render._dump({"steamid": sid, "total": total,
                          "count": len(groups), "groups": groups})

        lines = [f"# Steam groups for {sid}",
                 f"In {total} group(s); showing {len(groups)}.", ""]
        for d in groups:
            if d.get("name"):
                mc = f" ({d['member_count']:,} members)" if d.get("member_count") else ""
                lines.append(f"- **{d['name']}**{mc} — {d['url']}")
            else:
                lines.append(f"- gid {d['gid']} — {d['url']}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)
