"""Tools: friends — friend list, friends-who-own, and player comparison."""

from __future__ import annotations

import asyncio
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from steam_mcp import errors, identity, render, transport
from steam_mcp.app import mcp
from steam_mcp.data import players, pricing
from steam_mcp.models import FriendListInput
from steam_mcp.render import ResponseFormat


@mcp.tool(
    name="steam_get_friend_list",
    annotations={
        "title": "Get Steam Friend List",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_friend_list(params: FriendListInput) -> str:
    """List a user's Steam friends, enriched with name and current status.

    Combines GetFriendList (which returns only IDs) with GetPlayerSummaries so each
    friend includes their persona name and live status (Online / Away / In-Game /
    Offline). Requires the target profile's friend list to be PUBLIC.

    Args:
        params (FriendListInput): steamid, limit, offset, online_only.

    Returns:
        str: Markdown or JSON list. Per friend: steamid, name, status,
        current_game (if any), friends_since. Includes pagination metadata in JSON.
    """
    try:
        sid = await identity._resolve_steamid(params.steamid)
        data = await transport._steam_get(
            "ISteamUser/GetFriendList/v1/",
            {"steamid": sid, "relationship": "friend"},
        )
        friends = data.get("friendslist", {}).get("friends", [])
        if not friends:
            return (
                "No friends returned — the friend list isn't public (or the user "
                "has none). " + errors._privacy_hint("Friends List")
            )

        ids = [f["steamid"] for f in friends]
        since = {f["steamid"]: f.get("friend_since", 0) for f in friends}
        summaries = await players._summaries_for(ids)

        enriched = []
        for fid in ids:
            p = summaries.get(fid, {})
            status = render._persona_label(p) if p else "Unknown"
            if params.online_only and (
                not p or (p.get("personastate", 0) == 0 and not p.get("gameextrainfo"))
            ):
                continue
            enriched.append(
                {
                    "steamid": fid,
                    "name": p.get("personaname", "Unknown"),
                    "status": status,
                    "current_game": p.get("gameextrainfo"),
                    "friends_since": since.get(fid, 0),
                }
            )

        total = len(enriched)
        page = enriched[params.offset : params.offset + params.limit]

        if params.response_format == ResponseFormat.JSON:
            return render._dump(
                {
                    "steamid": sid,
                    "total": total,
                    "count": len(page),
                    "offset": params.offset,
                    "has_more": params.offset + len(page) < total,
                    "friends": page,
                }
            )

        lines = [f"# Friends of {sid}", f"Showing {len(page)} of {total}.", ""]
        for f in page:
            tail = f" — {f['current_game']}" if f["current_game"] else ""
            lines.append(f"- **{f['name']}** ({f['steamid']}): {f['status']}{tail}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)


class FriendsWhoOwnInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    steamid: Optional[str] = Field(
        default=None, max_length=200,
        description="The user whose friends to check: SteamID64, vanity, or URL. "
        "Omit to use the configured STEAM_USER, if set.",
    )
    appid: int = Field(
        ..., description="The game (appid) to check friends' ownership of.", ge=1
    )
    max_friends: int = Field(
        default=50,
        description="How many friends to check for ownership (1-250). Each is one "
        "concurrent owned-games lookup; raise for completeness, lower for speed.",
        ge=1, le=250,
    )
    playing_now: bool = Field(
        default=False,
        description="If true, list only friends currently in-game in this title now.",
    )
    limit: int = Field(
        default=30, description="Max owners to list (1-100).", ge=1, le=100
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


async def _friend_owns_app(fid: str, appid: int) -> dict:
    """Check whether one user owns `appid` via their owned-games list.

    Returns {fid, owns, private, playtime_min}. A private/hidden game list yields
    private=True (we can't tell), distinct from owns=False (public, doesn't own).
    """
    try:
        d = await transport._steam_get(
            "IPlayerService/GetOwnedGames/v1/",
            {"steamid": fid, "include_appinfo": 0, "include_played_free_games": 1},
        )
        resp = d.get("response", {})
        if not resp:  # empty {} -> game details private/hidden
            return {"fid": fid, "owns": False, "private": True, "playtime_min": 0}
        g = next((x for x in resp.get("games", []) if x.get("appid") == appid), None)
        if g is None:
            return {"fid": fid, "owns": False, "private": False, "playtime_min": 0}
        return {
            "fid": fid, "owns": True, "private": False,
            "playtime_min": g.get("playtime_forever", 0),
        }
    except Exception:  # noqa: BLE001
        return {"fid": fid, "owns": False, "private": True, "playtime_min": 0}


@mcp.tool(
    name="steam_find_friends_who_own",
    annotations={
        "title": "Find Friends Who Own a Game",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_find_friends_who_own(params: FriendsWhoOwnInput) -> str:
    """List which of a user's friends own (or are playing) a specific game — "who can I play X with" (about friends' libraries, not store search).

    Answers "who can I play X with". Cross-references the user's friend list with each
    friend's owned games, then annotates owners with their playtime and whether they
    are in the game right now (use playing_now=true to filter to just those).
    Requires the USER's friend list to be Public AND each FRIEND's game details to be
    Public — friends with private libraries can't be determined and are reported
    separately. Checks up to max_friends friends concurrently. Needs an API key.

    Args:
        params (FriendsWhoOwnInput): steamid, appid, max_friends, playing_now, limit.

    Returns:
        str: Markdown or JSON. game name, counts (total_friends, checked, owners,
        private_or_unknown), and the owners (name, playtime_hours, status,
        playing_now), sorted by playtime.
    """
    try:
        sid = await identity._resolve_steamid(params.steamid)
        fdata = await transport._steam_get(
            "ISteamUser/GetFriendList/v1/",
            {"steamid": sid, "relationship": "friend"},
        )
        friends = fdata.get("friendslist", {}).get("friends", [])
        if not friends:
            return (
                "No friends returned — the user's friend list isn't public (or they "
                "have none). " + errors._privacy_hint("Friends List")
            )
        all_ids = [f["steamid"] for f in friends]
        check_ids = all_ids[: params.max_friends]
        results = await transport._gather_limited(
            [_friend_owns_app(fid, params.appid) for fid in check_ids]
        )
        owners = [r for r in results if r["owns"]]
        private = sum(1 for r in results if r["private"])

        owner_ids = [r["fid"] for r in owners]
        summaries = await players._summaries_for(owner_ids) if owner_ids else {}
        info = await pricing._app_price(params.appid, "us")
        game_name = info.get("name") or f"app {params.appid}"

        rows = []
        for r in owners:
            p = summaries.get(r["fid"], {})
            playing = bool(p.get("gameid")) and str(p.get("gameid")) == str(params.appid)
            rows.append(
                {
                    "steamid": r["fid"],
                    "name": p.get("personaname", "Unknown"),
                    "playtime_hours": render._minutes_to_hours(r["playtime_min"]),
                    "status": render._persona_label(p) if p else "Unknown",
                    "playing_now": playing,
                }
            )
        owners_count = len(rows)
        if params.playing_now:
            rows = [r for r in rows if r["playing_now"]]
        rows.sort(key=lambda r: r["playtime_hours"], reverse=True)
        page = rows[: params.limit]

        if params.response_format == ResponseFormat.JSON:
            return render._dump(
                {
                    "steamid": sid,
                    "appid": params.appid,
                    "game": game_name,
                    "total_friends": len(all_ids),
                    "checked": len(check_ids),
                    "owners": owners_count,
                    "private_or_unknown": private,
                    "friends": page,
                }
            )

        checked_note = (
            f" (checked first {len(check_ids)})" if len(check_ids) < len(all_ids) else ""
        )
        lines = [
            f"# Friends who own {game_name} (appid {params.appid})",
            f"{owners_count} of {len(all_ids)} friends own it{checked_note}; "
            f"{private} had private game libraries.",
        ]
        if params.playing_now:
            lines.append(f"Showing only those playing right now ({len(page)}).")
        lines.append("")
        for r in page:
            tail = " — ▶️ playing now" if r["playing_now"] else ""
            lines.append(f"- **{r['name']}** — {r['playtime_hours']}h{tail}")
        if not page:
            lines.append("(none)")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)


class ComparePlayersInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    steamid_a: Optional[str] = Field(
        default=None,
        description="First user: SteamID64, vanity name, or profile URL. Omit to "
        "use the configured STEAM_USER, if set.",
        max_length=200,
    )
    steamid_b: str = Field(
        ...,
        description="Second user: SteamID64, vanity name, or profile URL.",
        min_length=1,
        max_length=200,
    )
    limit: int = Field(
        default=20,
        description="Max shared games to list, ordered by combined playtime (1-100).",
        ge=1,
        le=100,
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


@mcp.tool(
    name="steam_compare_players",
    annotations={
        "title": "Compare Two Steam Players",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_compare_players(params: ComparePlayersInput) -> str:
    """Compare two users' libraries: shared games and who has played each more.

    Answers "what games do we both own" and "who has more hours in the games we
    share". Built on each user's owned-games list. Requires BOTH profiles' game
    details to be Public. Needs an API key.

    Args:
        params (ComparePlayersInput): steamid_a, steamid_b, limit.

    Returns:
        str: Markdown or JSON. each user's game count, the shared-game count, and
        the top shared games with each player's hours.
    """
    try:
        sid_a = await identity._resolve_steamid(params.steamid_a)
        sid_b = await identity._resolve_steamid(params.steamid_b)

        async def _owned(sid: str) -> dict:
            d = await transport._steam_get(
                "IPlayerService/GetOwnedGames/v1/",
                {"steamid": sid, "include_appinfo": 1, "include_played_free_games": 1},
            )
            return {g["appid"]: g for g in d.get("response", {}).get("games", [])}

        games_a, games_b = await asyncio.gather(_owned(sid_a), _owned(sid_b))
        if not games_a or not games_b:
            return (
                "Could not compare — one or both profiles' Game details aren't "
                "public (or own no games). " + errors._privacy_hint("Game details")
            )

        shared_ids = set(games_a) & set(games_b)
        shared = []
        for aid in shared_ids:
            ga, gb = games_a[aid], games_b[aid]
            ha = render._minutes_to_hours(ga.get("playtime_forever"))
            hb = render._minutes_to_hours(gb.get("playtime_forever"))
            shared.append(
                {
                    "appid": aid,
                    "name": ga.get("name") or gb.get("name"),
                    "hours_a": ha,
                    "hours_b": hb,
                    "combined": round(ha + hb, 1),
                }
            )
        shared.sort(key=lambda s: s["combined"], reverse=True)
        page = shared[: params.limit]

        if params.response_format == ResponseFormat.JSON:
            return render._dump(
                {
                    "a": {"steamid": sid_a, "game_count": len(games_a)},
                    "b": {"steamid": sid_b, "game_count": len(games_b)},
                    "shared_count": len(shared_ids),
                    "shared": page,
                }
            )

        lines = [
            f"# Comparing {sid_a} (A) vs {sid_b} (B)",
            f"- A owns {len(games_a)} games; B owns {len(games_b)}.",
            f"- **Shared games**: {len(shared_ids)}",
            "",
            "## Top shared games by combined playtime",
        ]
        for s in page:
            if s["hours_a"] > s["hours_b"]:
                who = "A ahead"
            elif s["hours_b"] > s["hours_a"]:
                who = "B ahead"
            else:
                who = "tied"
            lines.append(
                f"- **{s['name']}** (appid {s['appid']}): "
                f"A {s['hours_a']}h / B {s['hours_b']}h → {who}"
            )
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)
