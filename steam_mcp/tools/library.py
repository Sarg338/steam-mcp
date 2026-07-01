"""Library tools: owned games, recently played games, and whole-library analysis."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from steam_mcp import errors, identity, render, transport
from steam_mcp.app import mcp
from steam_mcp.data import catalog, players
from steam_mcp.models import PlayerInput
from steam_mcp.render import ResponseFormat


class OwnedGamesInput(PlayerInput):
    limit: int = Field(
        default=25,
        description="Maximum games to return after sorting (1-200).",
        ge=1,
        le=200,
    )
    offset: int = Field(default=0, description="Games to skip for pagination.", ge=0)
    sort_by: str = Field(
        default="playtime",
        description="Sort order: 'playtime' (most played first) or 'name' (A-Z).",
    )
    include_free_games: bool = Field(
        default=True,
        description="Include free-to-play games the user has played.",
    )

    @field_validator("sort_by")
    @classmethod
    def _check_sort(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in {"playtime", "name"}:
            raise ValueError("sort_by must be 'playtime' or 'name'")
        return v


@mcp.tool(
    name="steam_get_owned_games",
    annotations={
        "title": "Get Steam Owned Games",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_owned_games(params: OwnedGamesInput) -> str:
    """List the games a user owns, with total and recent hours played.

    Use this for "how many hours have I played X", "what are my most-played games",
    and "how many games do I own". Requires the target's Game Details to be PUBLIC.

    Args:
        params (OwnedGamesInput): steamid, limit, offset, sort_by ('playtime'|'name'),
            include_free_games.

    Returns:
        str: Markdown or JSON. Per game: appid, name, playtime_hours,
        playtime_2weeks_hours. JSON includes game_count and pagination metadata.
    """
    try:
        sid = await identity._resolve_steamid(params.steamid)
        data = await transport._steam_get(
            "IPlayerService/GetOwnedGames/v1/",
            {
                "steamid": sid,
                "include_appinfo": 1,
                "include_played_free_games": 1 if params.include_free_games else 0,
            },
        )
        resp = data.get("response", {})
        games = resp.get("games", [])
        if not games:
            return (
                "No games returned — the profile's Game details aren't public (or "
                "it owns no games). " + errors._privacy_hint("Game details")
            )

        for g in games:
            g["playtime_hours"] = render._minutes_to_hours(g.get("playtime_forever"))
            g["playtime_2weeks_hours"] = render._minutes_to_hours(g.get("playtime_2weeks"))

        if params.sort_by == "name":
            games.sort(key=lambda g: g.get("name", "").lower())
        else:
            games.sort(key=lambda g: g.get("playtime_forever", 0), reverse=True)

        total = resp.get("game_count", len(games))
        page = games[params.offset : params.offset + params.limit]

        if params.response_format == ResponseFormat.JSON:
            slim = [
                {
                    "appid": g.get("appid"),
                    "name": g.get("name"),
                    "playtime_hours": g["playtime_hours"],
                    "playtime_2weeks_hours": g["playtime_2weeks_hours"],
                }
                for g in page
            ]
            return render._dump(
                {
                    "steamid": sid,
                    "game_count": total,
                    "count": len(page),
                    "offset": params.offset,
                    "has_more": params.offset + len(page) < len(games),
                    "games": slim,
                }
            )

        lines = [
            f"# Owned games for {sid}",
            f"Owns {total} games. Showing {len(page)} (sorted by {params.sort_by}).",
            "",
        ]
        for g in page:
            recent = (
                f" (recent {g['playtime_2weeks_hours']}h)"
                if g["playtime_2weeks_hours"]
                else ""
            )
            lines.append(
                f"- **{g.get('name', 'Unknown')}** (appid {g.get('appid')}): "
                f"{g['playtime_hours']}h total{recent}"
            )
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)


@mcp.tool(
    name="steam_get_recently_played_games",
    annotations={
        "title": "Get Steam Recently Played Games",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_recently_played_games(params: PlayerInput) -> str:
    """List games a user has played in the last two weeks, with hours.

    Args:
        params (PlayerInput): steamid.

    Returns:
        str: Markdown or JSON. Per game: appid, name, playtime_2weeks_hours,
        playtime_hours (total).
    """
    try:
        sid = await identity._resolve_steamid(params.steamid)
        data = await transport._steam_get(
            "IPlayerService/GetRecentlyPlayedGames/v1/", {"steamid": sid}
        )
        games = data.get("response", {}).get("games", [])
        if not games:
            return ("No recently played games — none in the last 2 weeks, or Game "
                    "details aren't public. " + errors._privacy_hint("Game details"))

        rows = [
            {
                "appid": g.get("appid"),
                "name": g.get("name"),
                "playtime_2weeks_hours": render._minutes_to_hours(g.get("playtime_2weeks")),
                "playtime_hours": render._minutes_to_hours(g.get("playtime_forever")),
            }
            for g in games
        ]
        if params.response_format == ResponseFormat.JSON:
            return render._dump({"steamid": sid, "count": len(rows), "games": rows})

        lines = [f"# Recently played (last 2 weeks) — {sid}", ""]
        for r in rows:
            lines.append(
                f"- **{r['name']}** (appid {r['appid']}): "
                f"{r['playtime_2weeks_hours']}h recently / {r['playtime_hours']}h total"
            )
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)


class LibraryAnalysisInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    steamid: Optional[str] = Field(
        default=None,
        description="SteamID64, vanity name, or profile URL of the library owner. "
        "Omit to use the configured STEAM_USER, if set.",
        max_length=200,
    )
    top_limit: int = Field(
        default=10, description="How many most-played games to list (1-50).",
        ge=1, le=50,
    )
    backlog_limit: int = Field(
        default=100,
        description="How many never-played games to list (0-100). Defaults to the "
        "max so 'what should I play' sees the whole backlog, not an alphabetical "
        "slice of it.",
        ge=0, le=100,
    )
    abandoned_limit: int = Field(
        default=25,
        description="How many 'abandoned' games to list (0-100). Independent of "
        "backlog_limit; ordered by abandoned_sort.",
        ge=0, le=100,
    )
    abandoned_sort: str = Field(
        default="recent",
        description="Order for the abandoned list: 'recent' (most recently dropped "
        "first — the most actionable to resume), 'oldest' (longest-dropped first), "
        "or 'playtime' (most hours sunk first).",
    )
    stale_days: int = Field(
        default=365,
        description="A played game untouched for at least this many days is "
        "counted as 'abandoned' (30-3650).",
        ge=30, le=3650,
    )
    exclude_temp_clients: bool = Field(
        default=True,
        description="Exclude non-retail clients (betas, playtests, demos, trials, "
        "test servers, staging branches, prototypes) — they're often unlaunchable, "
        "so they pollute 'what to play next'. Detected by name; the excluded count "
        "is always reported. Set false to include them in every stat and list.",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)

    @field_validator("abandoned_sort")
    @classmethod
    def _check_abandoned_sort(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in {"recent", "oldest", "playtime"}:
            raise ValueError(
                "abandoned_sort must be 'recent', 'oldest', or 'playtime'"
            )
        return v


@mcp.tool(
    name="steam_analyze_library",
    annotations={
        "title": "Analyze Steam Library / Backlog",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_analyze_library(params: LibraryAnalysisInput) -> str:
    """Analyze a whole game library: backlog, playtime distribution, abandoned games.

    Answers "what should I play", "what have I never touched", "where do my hours
    go", and "what did I love but abandon". Computed from one owned-games call
    (plus a small persona lookup for the header), so it spans the entire library
    cheaply. Requires the profile's Game Details to be Public. Needs an API key.

    Reports total games and hours; the never-played backlog; a playtime histogram
    (0h / <1h / 1-5h / 5-20h / 20-100h / 100h+); most-played games; recently active
    games; and 'abandoned' games (played, but not launched within `stale_days`).
    Steam only began recording last-played dates ~2019, so games last played before
    then show 'last played: unknown' rather than a date.

    The backlog is listed **alphabetically** and `backlog_limit` defaults to the
    100-game maximum, so a recommendation sees the whole backlog rather than an
    alphabetical slice. If a library has more never-played games than the limit,
    the output is flagged truncated (`backlog_truncated`) — see the full set before
    recommending, not just the early letters of the alphabet.

    By default, non-retail clients (betas, playtests, demos, trials, test servers,
    staging branches, prototypes) are excluded from every stat and list, since
    they're often unlaunchable and pollute "what to play next"; they're detected by
    name (best-effort) and the excluded count is always reported. Pass
    `exclude_temp_clients=false` to include them.

    Args:
        params (LibraryAnalysisInput): steamid, top_limit, backlog_limit,
            abandoned_limit, abandoned_sort, stale_days, exclude_temp_clients.

    Returns:
        str: Markdown or JSON with summary stats, playtime_buckets, top_played,
        recently_played, backlog_never_played, and abandoned lists.
    """
    try:
        import time as _time
        sid = await identity._resolve_steamid(params.steamid)
        data = await transport._steam_get(
            "IPlayerService/GetOwnedGames/v1/",
            {"steamid": sid, "include_appinfo": 1, "include_played_free_games": 1},
        )
        resp = data.get("response", {})
        all_games = resp.get("games", [])
        if not all_games:
            return (
                "No games returned — the profile's Game details aren't public, or "
                "it owns no games. " + errors._privacy_hint("Game details")
            )
        # Best-effort persona (display) name for the header — the resolver only
        # yields a SteamID64, so this is a separate, cheap lookup; failure is fine.
        try:
            persona = (await players._summaries_for([sid])).get(sid, {}).get("personaname")
        except Exception:  # noqa: BLE001
            persona = None
        if params.exclude_temp_clients:
            temp_clients = [
                g for g in all_games if catalog._is_temp_client(g.get("name", ""))
            ]
            games = [
                g for g in all_games if not catalog._is_temp_client(g.get("name", ""))
            ]
        else:
            temp_clients = []
            games = all_games
        temp_excluded = len(temp_clients)
        game_count = len(games)
        cutoff = _time.time() - params.stale_days * 86400

        # Authoritative never-vs-played predicate, used everywhere below (the split,
        # the buckets, the backlog/abandoned builders): played := playtime_forever
        # (minutes) > 0. A game launched only briefly has a tiny positive playtime
        # that *rounds* to 0.0h — it is still 'played'/abandonable, and renders as
        # '<0.1h' (see _hours_str), never a contradictory '0.0h'. The '0h' bucket
        # below is exactly minutes == 0, i.e. the never-played set.
        total_min = sum(g.get("playtime_forever", 0) for g in games)
        played = [g for g in games if g.get("playtime_forever", 0) > 0]
        never = [g for g in games if g.get("playtime_forever", 0) == 0]

        buckets = {"0h": 0, "under_1h": 0, "1_5h": 0, "5_20h": 0,
                   "20_100h": 0, "over_100h": 0}
        for g in games:
            h = g.get("playtime_forever", 0) / 60
            if h == 0:
                buckets["0h"] += 1
            elif h < 1:
                buckets["under_1h"] += 1
            elif h < 5:
                buckets["1_5h"] += 1
            elif h < 20:
                buckets["5_20h"] += 1
            elif h < 100:
                buckets["20_100h"] += 1
            else:
                buckets["over_100h"] += 1

        def _row(g):
            mins = g.get("playtime_forever")
            return {
                "appid": g.get("appid"),
                "name": g.get("name"),
                "hours": render._minutes_to_hours(mins),
                "hours_str": render._hours_str(mins),
                "last_played": render._ts_to_date(g.get("rtime_last_played")),
            }

        top_played = [
            _row(g) for g in sorted(
                played, key=lambda g: g.get("playtime_forever", 0), reverse=True
            )[: params.top_limit]
        ]
        recent = sorted(
            [g for g in games if g.get("playtime_2weeks")],
            key=lambda g: g.get("playtime_2weeks", 0), reverse=True,
        )
        recently_played = [
            {"appid": g.get("appid"), "name": g.get("name"),
             "hours_2weeks": render._minutes_to_hours(g.get("playtime_2weeks"))}
            for g in recent[:10]
        ]
        abandoned_src = [
            g for g in played
            if 1_000_000_000 < g.get("rtime_last_played", 0) < cutoff
        ]
        if params.abandoned_sort == "oldest":
            _akey, _arev = (lambda g: g.get("rtime_last_played", 0)), False
        elif params.abandoned_sort == "playtime":
            _akey, _arev = (lambda g: g.get("playtime_forever", 0)), True
        else:  # 'recent' (default) — most recently dropped first
            _akey, _arev = (lambda g: g.get("rtime_last_played", 0)), True
        abandoned = [
            _row(g)
            for g in sorted(abandoned_src, key=_akey, reverse=_arev)[
                : params.abandoned_limit
            ]
        ]
        # limit==0 is intentional suppression, not truncation — don't nag.
        abandoned_truncated = (
            params.abandoned_limit > 0 and len(abandoned) < len(abandoned_src)
        )
        backlog = [
            {"appid": g.get("appid"), "name": g.get("name")}
            for g in sorted(never, key=lambda g: (g.get("name") or "").lower())[
                : params.backlog_limit
            ]
        ]
        # limit==0 is intentional suppression, not truncation — don't nag.
        backlog_truncated = params.backlog_limit > 0 and len(backlog) < len(never)

        total_hours = round(total_min / 60, 1)
        summary = {
            "game_count": game_count,
            "total_hours": total_hours,
            "played_count": len(played),
            "never_played_count": len(never),
            "never_played_pct": round(100 * len(never) / game_count, 1) if game_count else 0,
            "avg_hours_per_owned_game": round(total_hours / game_count, 1) if game_count else 0,
            "avg_hours_per_played_game": round(total_hours / len(played), 1) if played else 0,
            "temp_clients_excluded": temp_excluded,
        }

        if params.response_format == ResponseFormat.JSON:
            return render._dump({
                "steamid": sid,
                "persona_name": persona,
                "summary": summary,
                "playtime_buckets": buckets,
                "top_played": top_played,
                "recently_played": recently_played,
                "backlog_never_played": backlog,
                "backlog_truncated": backlog_truncated,
                "abandoned": abandoned,
                "abandoned_truncated": abandoned_truncated,
                "temp_clients_excluded_names": [
                    g.get("name") for g in temp_clients[:50]
                ],
            })

        who = f"{persona} ({sid})" if persona else sid
        lines = [
            f"# Library analysis for {who}",
            f"- **Games owned**: {game_count}  |  **Total played**: "
            f"{total_hours:,.1f}h",
            f"- **Never played**: {len(never)} "
            f"({summary['never_played_pct']}% of library)",
        ]
        if temp_excluded:
            _ex = ", ".join(g.get("name", "?") for g in temp_clients[:3])
            _more_ex = "…" if temp_excluded > 3 else ""
            lines.append(
                f"- Excluded **{temp_excluded}** non-retail client(s) "
                f"(beta/playtest/demo/test), e.g. {_ex}{_more_ex}. Set "
                f"exclude_temp_clients=false to include them."
            )
        if backlog_truncated:
            if params.backlog_limit < 100:
                more = "call again with backlog_limit=100 to see more"
            else:
                more = ("this is the 100-game max — page the rest via "
                        "steam_get_owned_games (sort_by=name, offset=...)")
            lines.append(
                f"- ⚠️ **Backlog truncated**: showing {len(backlog)} of "
                f"{len(never)} never-played (alphabetical); {more}."
            )
        lines += [
            f"- **Avg hours/game**: {summary['avg_hours_per_owned_game']} across "
            f"all owned, {summary['avg_hours_per_played_game']} across played games",
            "",
            "## Playtime distribution",
            f"- never: {buckets['0h']} · <1h: {buckets['under_1h']} · "
            f"1-5h: {buckets['1_5h']} · 5-20h: {buckets['5_20h']} · "
            f"20-100h: {buckets['20_100h']} · 100h+: {buckets['over_100h']}",
            "",
            "## Most played",
        ]
        for g in top_played:
            lp = f", last played {g['last_played']}" if g["last_played"] else ""
            lines.append(f"- **{g['name']}** — {g['hours_str']}h{lp}")
        if abandoned:
            lines += [
                "",
                f"## Abandoned — played, untouched {params.stale_days}+ days "
                f"({len(abandoned_src)} total, showing {len(abandoned)})",
            ]
            for g in abandoned:
                lines.append(
                    f"- **{g['name']}** — {g['hours_str']}h, last played {g['last_played']}"
                )
        if backlog:
            lines += [
                "",
                f"## Backlog — never played ({len(never)} total, showing {len(backlog)})",
            ]
            for g in backlog:
                lines.append(f"- {g['name']} (appid {g['appid']})")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)
