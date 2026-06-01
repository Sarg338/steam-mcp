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

import json
import os
import re
import time
from enum import Enum
from typing import Any, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Server + constants
# ---------------------------------------------------------------------------

mcp = FastMCP("steam_mcp")

API_BASE = "https://api.steampowered.com"
STORE_BASE = "https://store.steampowered.com/api"
HTTP_TIMEOUT = 30.0
ENV_KEY = "STEAM_API_KEY"

# Recent-reviews computation: Steam's query_summary is always lifetime, so the
# recent (last-N-days) score is computed by paginating the newest reviews. These
# bound that work so a hugely-reviewed game can't trigger unbounded requests.
RECENT_PAGE_SIZE = 100
MAX_RECENT_PAGES = 6  # up to 600 most-recent reviews considered

# Steam persona (online) states -> human-readable label.
PERSONA_STATES = {
    0: "Offline",
    1: "Online",
    2: "Busy",
    3: "Away",
    4: "Snooze",
    5: "Looking to trade",
    6: "Looking to play",
}

# Community visibility states from GetPlayerSummaries.
VISIBILITY_STATES = {
    1: "Private",
    2: "Friends only",
    3: "Public",
}

PROFILE_URL_RE = re.compile(r"steamcommunity\.com/(profiles|id)/([^/?#]+)", re.IGNORECASE)
STEAMID64_RE = re.compile(r"^7656\d{13}$")  # 17-digit SteamID64 starting 7656


# --- Static-response cache (per-process, opt-in) -----------------------------
CACHE_TTL_APPDETAILS = 600      # 10 min (price can change on sales)
CACHE_TTL_PACKAGE = 3600
CACHE_TTL_FEATURED = 300        # 5 min
CACHE_TTL_SCHEMA = 86400        # achievement/stat definitions are static
CACHE_TTL_GLOBAL_ACH = 3600


class _TTLCache:
    """Tiny in-memory TTL cache for static GET responses.

    Keeps the server gentle on Steam's rate limit and speeds up tools that fan
    out many lookups (wishlist enrichment, library/app detail comparisons). Only
    static endpoints opt in via a positive cache_ttl; live data (player status,
    current players, wishlists, friends) is never cached.
    """

    def __init__(self, maxsize: int = 256):
        self._d: dict[str, tuple[float, Any]] = {}
        self._max = maxsize

    def get(self, key: str):
        item = self._d.get(key)
        if not item:
            return None
        expiry, value = item
        if expiry < time.time():
            self._d.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        if len(self._d) >= self._max:
            now = time.time()
            for k in [k for k, (e, _) in self._d.items() if e < now]:
                self._d.pop(k, None)
            if len(self._d) >= self._max:
                self._d.clear()
        self._d[key] = (time.time() + ttl, value)

    def clear(self) -> None:
        self._d.clear()


_CACHE = _TTLCache()


def _cache_key(prefix: str, params: dict) -> str:
    """Stable cache key from a path/URL + params, excluding the secret API key."""
    items = sorted((k, v) for k, v in params.items() if k != "key")
    return prefix + "?" + "&".join(f"{k}={v}" for k, v in items)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

class ResponseFormat(str, Enum):
    """Output format for tool responses."""

    MARKDOWN = "markdown"
    JSON = "json"


class SteamApiError(Exception):
    """Raised for Steam-specific (non-HTTP) problems with an actionable message."""


def _load_key_from_dotenv() -> str:
    """Fallback: read STEAM_API_KEY from a .env file in the project root.

    This lets the key live only in .env (which is gitignored) instead of being
    placed in the MCP client config. The project root is the parent directory of
    this package, resolved from __file__ so it works regardless of cwd.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, ".env"), "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{ENV_KEY}="):
                    return line.split("=", 1)[1].strip().strip('"').strip()
    except OSError:
        pass
    return ""


def _get_api_key() -> str:
    """Read the Steam Web API key from the environment or .env, or raise."""
    key = os.environ.get(ENV_KEY, "").strip() or _load_key_from_dotenv()
    if not key:
        raise SteamApiError(
            f"No Steam Web API key configured. Set the {ENV_KEY} environment "
            f"variable in your MCP client config, or put it in a .env file next to "
            f"the project. Get a free key at https://steamcommunity.com/dev/apikey"
        )
    return key


async def _steam_get(path: str, params: dict[str, Any], *, with_key: bool = True,
                     cache_ttl: float = 0) -> dict:
    """GET a Steam Web API endpoint and return parsed JSON.

    Args:
        path: Path after the host, e.g. "ISteamUser/GetFriendList/v1/".
        params: Query parameters (the API key is injected automatically).
        with_key: Whether to attach the configured API key.
        cache_ttl: If > 0, cache the response for this many seconds. Use only for
            static endpoints (e.g. game schemas); never for live/user data.
    """
    ck = _cache_key(API_BASE + "/" + path, params) if cache_ttl else None
    if ck is not None:
        hit = _CACHE.get(ck)
        if hit is not None:
            return hit
    query = dict(params)
    if with_key:
        query["key"] = _get_api_key()
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{API_BASE}/{path}", params=query, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    if ck is not None:
        _CACHE.set(ck, data, cache_ttl)
    return data


async def _store_get(path: str, params: dict[str, Any], cache_ttl: float = 0) -> Any:
    """GET a public storefront API endpoint (no key required)."""
    return await _raw_get(f"{STORE_BASE}/{path}", params, cache_ttl=cache_ttl)


async def _raw_get(url: str, params: dict[str, Any], cache_ttl: float = 0) -> Any:
    """GET an arbitrary public Steam JSON endpoint (no key required)."""
    ck = _cache_key(url, params) if cache_ttl else None
    if ck is not None:
        hit = _CACHE.get(ck)
        if hit is not None:
            return hit
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(
            url,
            params=params,
            timeout=HTTP_TIMEOUT,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
    if ck is not None:
        _CACHE.set(ck, data, cache_ttl)
    return data


def _handle_error(e: Exception) -> str:
    """Consistent, actionable error formatting across all tools."""
    if isinstance(e, SteamApiError):
        return f"Error: {e}"
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code == 401 or code == 403:
            return (
                "Error: Steam rejected the request (401/403). Your API key may be "
                "invalid, or the target profile is private. Verify STEAM_API_KEY."
            )
        if code == 404:
            return "Error: Not found (404). Check the SteamID / app ID is correct."
        if code == 429:
            return (
                "Error: Rate limited by Steam (429). The Web API allows ~100,000 "
                "calls/day per key. Wait and retry, or reduce request volume."
            )
        if code == 500:
            return (
                "Error: Steam returned 500. This often means the SteamID is invalid "
                "or the profile/app has no data for this endpoint."
            )
        return f"Error: Steam API request failed with HTTP {code}."
    if isinstance(e, httpx.TimeoutException):
        return "Error: Request to Steam timed out. Please try again."
    return f"Error: Unexpected {type(e).__name__}: {e}"


async def _resolve_steamid(identifier: str) -> str:
    """Resolve a flexible identifier to a 17-digit SteamID64.

    Accepts:
        - A raw SteamID64 (e.g. "76561197960287930")
        - A vanity / custom-URL name (e.g. "gabelogannewell")
        - A full profile URL (steamcommunity.com/id/<name> or /profiles/<id>)

    Raises SteamApiError if a vanity name cannot be resolved.
    """
    raw = identifier.strip()

    # Full profile URL?
    m = PROFILE_URL_RE.search(raw)
    if m:
        kind, value = m.group(1).lower(), m.group(2)
        if kind == "profiles":
            return value
        raw = value  # /id/<vanity> -> resolve the vanity below

    # Already a SteamID64?
    if STEAMID64_RE.match(raw):
        return raw

    # Otherwise treat as a vanity name and resolve it.
    data = await _steam_get(
        "ISteamUser/ResolveVanityURL/v1/", {"vanityurl": raw}
    )
    resp = data.get("response", {})
    if resp.get("success") == 1 and resp.get("steamid"):
        return resp["steamid"]
    raise SteamApiError(
        f"Could not resolve '{identifier}' to a SteamID. Provide a 17-digit "
        f"SteamID64, an exact vanity name, or a full profile URL."
    )


async def _summaries_for(steamids: list[str]) -> dict[str, dict]:
    """Fetch player summaries for many SteamIDs, chunked at 100 per call.

    Returns a dict keyed by SteamID64.
    """
    out: dict[str, dict] = {}
    for i in range(0, len(steamids), 100):
        chunk = steamids[i : i + 100]
        data = await _steam_get(
            "ISteamUser/GetPlayerSummaries/v2/",
            {"steamids": ",".join(chunk)},
        )
        for p in data.get("response", {}).get("players", []):
            out[p["steamid"]] = p
    return out


def _persona_label(player: dict) -> str:
    """Human label for a player's current status, including current game."""
    game = player.get("gameextrainfo")
    if game:
        return f"In-Game: {game}"
    return PERSONA_STATES.get(player.get("personastate", 0), "Unknown")


def _minutes_to_hours(minutes: Optional[int]) -> float:
    return round((minutes or 0) / 60.0, 1)


def _dump(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------

class PlayerInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    steamid: str = Field(
        ...,
        description="SteamID64 (17 digits), vanity name, or full profile URL "
        "(e.g. '76561197960287930', 'gabelogannewell', "
        "'https://steamcommunity.com/id/gabelogannewell').",
        min_length=1,
        max_length=200,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="'markdown' for human-readable, 'json' for machine-readable.",
    )


class PlayerGameInput(PlayerInput):
    appid: int = Field(
        ...,
        description="Steam application (game) ID, e.g. 730 for CS2, 570 for Dota 2.",
        ge=1,
    )


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


class FriendListInput(PlayerInput):
    limit: int = Field(
        default=50,
        description="Maximum friends to return (1-200). Each is enriched with "
        "name and current status.",
        ge=1,
        le=200,
    )
    offset: int = Field(default=0, description="Friends to skip for pagination.", ge=0)
    online_only: bool = Field(
        default=False,
        description="If true, return only friends who are not Offline.",
    )


class PlayersInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    steamids: list[str] = Field(
        ...,
        description="List of SteamID64 / vanity names / profile URLs (max 100).",
        min_length=1,
        max_length=100,
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class AppDetailsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    appid: int = Field(..., description="Steam application (game) ID.", ge=1)
    country_code: str = Field(
        default="us",
        description="ISO country code for pricing/availability (e.g. 'us', 'gb').",
        min_length=2,
        max_length=2,
    )
    include_requirements: bool = Field(
        default=True,
        description="Include a short PC system-requirements summary "
        "(minimum + recommended).",
    )
    include_long_description: bool = Field(
        default=False,
        description="Include the full 'about the game' text (large). Off by "
        "default; the short description is always included.",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class AppSearchInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(
        ..., description="Game title (or partial title) to search for.",
        min_length=1, max_length=200,
    )
    limit: int = Field(default=10, description="Max results (1-25).", ge=1, le=25)
    country_code: str = Field(default="us", min_length=2, max_length=2)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class AppOnlyInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    appid: int = Field(..., description="Steam application (game) ID.", ge=1)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class AppReviewsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    appid: int = Field(..., description="Steam application (game) ID.", ge=1)
    review_filter: str = Field(
        default="all",
        description="Scoring window: 'all' returns Steam's lifetime summary; "
        "'recent' additionally computes the last-N-days score (the store page's "
        "'Recent Reviews' box) by tallying the newest reviews. Default 'all'.",
    )
    day_range: int = Field(
        default=30,
        description="Window in days for review_filter='recent' (1-365). Ignored "
        "when review_filter='all'. Default 30 to match Steam's store page.",
        ge=1,
        le=365,
    )
    review_type: str = Field(
        default="all",
        description="Which reviews to sample for excerpts: 'all', 'positive', "
        "or 'negative'.",
    )
    limit: int = Field(
        default=5,
        description="Number of individual review excerpts to include (0-20). The "
        "score summary is always returned.",
        ge=0,
        le=20,
    )
    country_code: str = Field(default="us", min_length=2, max_length=2)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)

    @field_validator("review_type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in {"all", "positive", "negative"}:
            raise ValueError("review_type must be 'all', 'positive', or 'negative'")
        return v

    @field_validator("review_filter")
    @classmethod
    def _check_filter(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in {"all", "recent"}:
            raise ValueError("review_filter must be 'all' or 'recent'")
        return v


class FeaturedInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    limit: int = Field(
        default=15, description="Max games on sale to return (1-50).", ge=1, le=50
    )
    country_code: str = Field(
        default="us",
        description="ISO country code for regional pricing (e.g. 'us', 'gb', 'de').",
        min_length=2,
        max_length=2,
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class StoreHighlightsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    section: str = Field(
        default="top_sellers",
        description="Which storefront list to return: 'top_sellers', "
        "'new_releases', 'coming_soon', or 'specials'.",
    )
    limit: int = Field(default=15, description="Max items to return (1-50).", ge=1, le=50)
    country_code: str = Field(
        default="us",
        description="ISO country code for regional pricing (e.g. 'us', 'gb').",
        min_length=2,
        max_length=2,
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)

    @field_validator("section")
    @classmethod
    def _check_section(cls, v: str) -> str:
        v = v.lower().strip()
        allowed = {"top_sellers", "new_releases", "coming_soon", "specials"}
        if v not in allowed:
            raise ValueError(f"section must be one of {sorted(allowed)}")
        return v


class WishlistInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    steamid: str = Field(
        ...,
        description="SteamID64, vanity name, or profile URL of the wishlist owner.",
        min_length=1,
        max_length=200,
    )
    limit: int = Field(
        default=15,
        description="Max wishlist entries to return, ordered by wishlist priority "
        "(1-50). Enriched entries each cost one store lookup, so keep this modest.",
        ge=1,
        le=50,
    )
    enrich: bool = Field(
        default=True,
        description="Fetch each game's name + current price/discount (one store "
        "lookup per game). Set false for a fast appid-only list.",
    )
    on_sale_only: bool = Field(
        default=False,
        description="If true (requires enrich=true), return only wishlist games that "
        "are currently discounted.",
    )
    country_code: str = Field(default="us", min_length=2, max_length=2)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class AppNewsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    appid: int = Field(..., description="Steam application (game) ID.", ge=1)
    count: int = Field(default=5, description="Number of news items (1-20).", ge=1, le=20)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


# ---------------------------------------------------------------------------
# Tools: identity & profile
# ---------------------------------------------------------------------------

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
        resolved = await _resolve_steamid(params.steamid)
        if params.response_format == ResponseFormat.JSON:
            return _dump({"input": params.steamid, "steamid64": resolved})
        return f"SteamID64 for '{params.steamid}': {resolved}"
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


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
        resolved = [await _resolve_steamid(s) for s in params.steamids]
        summaries = await _summaries_for(resolved)
        players = [summaries[s] for s in resolved if s in summaries]
        if not players:
            return "No player data found (profiles may be private or IDs invalid)."

        if params.response_format == ResponseFormat.JSON:
            return _dump({"count": len(players), "players": players})

        lines = [f"# Steam Players ({len(players)})", ""]
        for p in players:
            lines.append(f"## {p.get('personaname', 'Unknown')} ({p['steamid']})")
            lines.append(f"- **Status**: {_persona_label(p)}")
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
        return _handle_error(e)


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
        sid = await _resolve_steamid(params.steamid)
        data = await _steam_get("IPlayerService/GetSteamLevel/v1/", {"steamid": sid})
        level = data.get("response", {}).get("player_level")
        if level is None:
            return "No level data (profile may be private)."
        if params.response_format == ResponseFormat.JSON:
            return _dump({"steamid": sid, "steam_level": level})
        return f"Steam level for {sid}: {level}"
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


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
        sid = await _resolve_steamid(params.steamid)
        data = await _steam_get("ISteamUser/GetPlayerBans/v1/", {"steamids": sid})
        bans = data.get("players", [])
        if not bans:
            return "No ban data found for that user."
        b = bans[0]
        if params.response_format == ResponseFormat.JSON:
            return _dump(b)
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
        return _handle_error(e)


# ---------------------------------------------------------------------------
# Tools: friends
# ---------------------------------------------------------------------------

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
        sid = await _resolve_steamid(params.steamid)
        data = await _steam_get(
            "ISteamUser/GetFriendList/v1/",
            {"steamid": sid, "relationship": "friend"},
        )
        friends = data.get("friendslist", {}).get("friends", [])
        if not friends:
            return (
                "No friends returned. The friend list is likely private "
                "(set Friends List to Public in Steam privacy settings)."
            )

        ids = [f["steamid"] for f in friends]
        since = {f["steamid"]: f.get("friend_since", 0) for f in friends}
        summaries = await _summaries_for(ids)

        enriched = []
        for fid in ids:
            p = summaries.get(fid, {})
            status = _persona_label(p) if p else "Unknown"
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
            return _dump(
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
        return _handle_error(e)


# ---------------------------------------------------------------------------
# Tools: games & playtime
# ---------------------------------------------------------------------------

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
        sid = await _resolve_steamid(params.steamid)
        data = await _steam_get(
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
                "No games returned. Game details are likely private, or the user "
                "owns no games."
            )

        for g in games:
            g["playtime_hours"] = _minutes_to_hours(g.get("playtime_forever"))
            g["playtime_2weeks_hours"] = _minutes_to_hours(g.get("playtime_2weeks"))

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
            return _dump(
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
        return _handle_error(e)


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
        sid = await _resolve_steamid(params.steamid)
        data = await _steam_get(
            "IPlayerService/GetRecentlyPlayedGames/v1/", {"steamid": sid}
        )
        games = data.get("response", {}).get("games", [])
        if not games:
            return "No recently played games (none in the last 2 weeks, or private)."

        rows = [
            {
                "appid": g.get("appid"),
                "name": g.get("name"),
                "playtime_2weeks_hours": _minutes_to_hours(g.get("playtime_2weeks")),
                "playtime_hours": _minutes_to_hours(g.get("playtime_forever")),
            }
            for g in games
        ]
        if params.response_format == ResponseFormat.JSON:
            return _dump({"steamid": sid, "count": len(rows), "games": rows})

        lines = [f"# Recently played (last 2 weeks) — {sid}", ""]
        for r in rows:
            lines.append(
                f"- **{r['name']}** (appid {r['appid']}): "
                f"{r['playtime_2weeks_hours']}h recently / {r['playtime_hours']}h total"
            )
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


# ---------------------------------------------------------------------------
# Tools: achievements & stats
# ---------------------------------------------------------------------------

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
        sid = await _resolve_steamid(params.steamid)
        data = await _steam_get(
            "ISteamUserStats/GetPlayerAchievements/v1/",
            {"steamid": sid, "appid": params.appid, "l": "english"},
        )
        stats = data.get("playerstats", {})
        if not stats.get("success", False):
            return (
                f"Error: {stats.get('error', 'No achievement data')}. The profile "
                f"may be private, or app {params.appid} has no achievements."
            )
        achievements = stats.get("achievements", [])
        total = len(achievements)
        unlocked = [a for a in achievements if a.get("achieved") == 1]
        locked = [a for a in achievements if a.get("achieved") != 1]
        pct = round(100.0 * len(unlocked) / total, 1) if total else 0.0
        game_name = stats.get("gameName", str(params.appid))

        if params.response_format == ResponseFormat.JSON:
            return _dump(
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
        return _handle_error(e)


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
        data = await _steam_get(
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
            return _dump({"appid": params.appid, "game": name, "achievements": rows})

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
        return _handle_error(e)


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
        data = await _steam_get(
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
            return _dump({"appid": params.appid, "achievements": rows})

        lines = [f"# Achievement rarity for app {params.appid} (rarest first)", ""]
        for r in rows[:50]:
            lines.append(f"- {r['api_name']}: {r['global_pct']}% of players")
        if len(rows) > 50:
            lines.append(f"- …and {len(rows) - 50} more")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


# ---------------------------------------------------------------------------
# Tools: store (no API key required)
# ---------------------------------------------------------------------------

@mcp.tool(
    name="steam_search_apps",
    annotations={
        "title": "Search Steam Store Apps",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_search_apps(params: AppSearchInput) -> str:
    """Search the Steam store for games by title and return their appids.

    Use this to turn a game name into an appid for the achievement/details tools.
    Does not require an API key.

    Args:
        params (AppSearchInput): query, limit, country_code.

    Returns:
        str: Markdown or JSON list of matches: appid, name, price (if any).
    """
    try:
        data = await _store_get(
            "storesearch/",
            {"term": params.query, "l": "english", "cc": params.country_code},
        )
        items = data.get("items", [])[: params.limit]
        rows = [
            {
                "appid": it.get("id"),
                "name": it.get("name"),
                "price": (it.get("price") or {}).get("final"),
            }
            for it in items
        ]
        if not rows:
            return f"No store results for '{params.query}'."
        if params.response_format == ResponseFormat.JSON:
            return _dump({"query": params.query, "count": len(rows), "results": rows})

        lines = [f"# Store search: '{params.query}'", ""]
        for r in rows:
            price = ""
            if r["price"]:
                price = f" — ${r['price'] / 100:.2f}"
            lines.append(f"- **{r['name']}** (appid {r['appid']}){price}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


@mcp.tool(
    name="steam_get_app_details",
    annotations={
        "title": "Get Steam App Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_app_details(params: AppDetailsInput) -> str:
    """Get comprehensive store details for a game — the best 'tell me about X' tool.

    Returns name, type, price/discount, developers & publishers, genres, release
    date, Metacritic, review count, achievement count, supported languages (and
    which have full audio), platforms, DLC, mature-content flags, and — most
    usefully — play modes and features derived from Steam's category list. Also
    exposes a `features` object of boolean flags so an LLM can filter directly
    (is_singleplayer, is_coop, is_online_coop, is_local_coop,
    has_controller_support, has_cloud_saves, has_trading_cards,
    remote_play_together, family_sharing, vr_support, anti_cheat). Optionally
    includes PC system requirements. No API key required.

    Args:
        params (AppDetailsInput): appid, country_code, include_requirements,
            include_long_description.

    Returns:
        str: Markdown or JSON containing all of the above.
    """
    try:
        data = await _store_get(
            "appdetails",
            {"appids": params.appid, "cc": params.country_code, "l": "english"},
            cache_ttl=CACHE_TTL_APPDETAILS,
        )
        entry = data.get(str(params.appid), {})
        if not entry.get("success"):
            return f"No store details found for app {params.appid}."
        d = entry.get("data", {})

        cats = [c.get("description", "") for c in d.get("categories", [])]
        cats_l = [c.lower() for c in cats]

        def _has(*subs):
            return any(any(sub in c for c in cats_l) for sub in subs)

        price = d.get("price_overview") or {}
        platforms = [k for k, v in (d.get("platforms") or {}).items() if v]
        langs, audio_langs = _parse_languages(d.get("supported_languages", ""))
        try:
            req_age = int(d.get("required_age") or 0)
        except (TypeError, ValueError):
            req_age = 0
        cd = d.get("content_descriptors") or {}
        pcr = d.get("pc_requirements")
        pcr = pcr if isinstance(pcr, dict) else {}

        features = {
            "is_singleplayer": _has("single-player"),
            "is_multiplayer": _has("multi-player", "pvp", "mmo"),
            "is_coop": _has("co-op"),
            "is_online_coop": _has("online co-op"),
            "is_local_coop": _has("shared/split screen co-op", "local co-op"),
            "has_controller_support": d.get("controller_support") in ("full", "partial")
            or _has("controller support"),
            "has_cloud_saves": _has("steam cloud"),
            "has_trading_cards": _has("trading cards"),
            "has_achievements": _has("steam achievements")
            or bool((d.get("achievements") or {}).get("total")),
            "remote_play_together": _has("remote play together"),
            "family_sharing": _has("family sharing"),
            "vr_support": _has("vr "),
            "anti_cheat": _has("anti-cheat"),
        }

        summary = {
            "appid": params.appid,
            "name": d.get("name"),
            "type": d.get("type"),
            "is_free": d.get("is_free", False),
            "price": (price.get("final_formatted") or None)
            if price else ("Free" if d.get("is_free") else None),
            "initial_price": (price.get("initial_formatted") or None) if price else None,
            "discount_pct": price.get("discount_percent", 0) if price else 0,
            "developers": d.get("developers", []),
            "publishers": d.get("publishers", []),
            "release_date": (d.get("release_date") or {}).get("date"),
            "coming_soon": (d.get("release_date") or {}).get("coming_soon", False),
            "genres": [g.get("description") for g in d.get("genres", [])],
            "categories": cats,
            "features": features,
            "controller_support": d.get("controller_support"),
            "platforms": platforms,
            "metacritic": (d.get("metacritic") or {}).get("score"),
            "metacritic_url": (d.get("metacritic") or {}).get("url"),
            "recommendations_total": (d.get("recommendations") or {}).get("total"),
            "achievements_total": (d.get("achievements") or {}).get("total"),
            "dlc": d.get("dlc", []),
            "dlc_count": len(d.get("dlc", [])),
            "required_age": req_age,
            "mature_content": _strip_html(cd.get("notes")) if cd.get("notes") else None,
            "supported_languages": langs,
            "full_audio_languages": audio_langs,
            "website": d.get("website"),
            "short_description": _strip_html(d.get("short_description"), 600),
        }
        if params.include_requirements and pcr:
            def _req(v):
                v = _strip_html(v, 500)
                return re.sub(r"^(Minimum|Recommended)\s*:\s*", "", v, flags=re.I) if v else v
            summary["pc_requirements"] = {
                "minimum": _req(pcr.get("minimum")),
                "recommended": _req(pcr.get("recommended")),
            }
        if params.include_long_description:
            summary["about_the_game"] = _strip_html(d.get("about_the_game"), 2000)

        if params.response_format == ResponseFormat.JSON:
            return _dump(summary)

        mode_set = {
            "Single-player", "Multi-player", "Co-op", "Online Co-op", "Online PvP",
            "Shared/Split Screen Co-op", "Shared/Split Screen PvP", "MMO",
            "Cross-Platform Multiplayer", "LAN Co-op", "LAN PvP", "PvP",
        }
        modes = [c for c in cats if c in mode_set]
        price_str = summary["price"] or ("Free" if summary["is_free"] else "Unknown")
        if summary["discount_pct"]:
            price_str += f" ({summary['discount_pct']}% off)"

        lines = [
            f"# {summary['name']} (appid {params.appid})",
            f"- **Type / Price**: {summary['type']} · {price_str}",
            f"- **Developer / Publisher**: "
            f"{', '.join(summary['developers']) or 'n/a'} / "
            f"{', '.join(summary['publishers']) or 'n/a'}",
            f"- **Released**: {summary['release_date'] or 'n/a'}"
            + (" (coming soon)" if summary["coming_soon"] else ""),
            f"- **Genres**: {', '.join(summary['genres']) or 'n/a'}",
            f"- **Platforms**: {', '.join(platforms) or 'n/a'}",
            f"- **Play modes**: {', '.join(modes) or 'n/a'}",
            f"- **Controller**: {summary['controller_support'] or 'none'}",
        ]
        if summary["metacritic"]:
            lines.append(f"- **Metacritic**: {summary['metacritic']}")
        if summary["recommendations_total"]:
            lines.append(
                f"- **Reviews**: {summary['recommendations_total']:,} recommendations"
            )
        if summary["achievements_total"]:
            lines.append(f"- **Achievements**: {summary['achievements_total']}")
        if summary["dlc_count"]:
            lines.append(f"- **DLC**: {summary['dlc_count']}")
        if langs:
            audio = f" (full audio: {', '.join(audio_langs)})" if audio_langs else ""
            lines.append(f"- **Languages**: {', '.join(langs)}{audio}")
        if summary["mature_content"]:
            age = f"{req_age}+ — " if req_age else ""
            lines.append(f"- **Content notes**: {age}{summary['mature_content']}")
        flags = [k.replace("_", " ") for k, v in features.items() if v]
        if flags:
            lines.append(f"- **Features**: {', '.join(flags)}")
        if summary["short_description"]:
            lines += ["", summary["short_description"]]
        if summary.get("pc_requirements"):
            lines += ["", "## PC requirements"]
            if summary["pc_requirements"].get("minimum"):
                lines.append(f"**Minimum:** {summary['pc_requirements']['minimum']}")
            if summary["pc_requirements"].get("recommended"):
                lines.append(
                    f"**Recommended:** {summary['pc_requirements']['recommended']}"
                )
        if summary.get("about_the_game"):
            lines += ["", "## About", summary["about_the_game"]]
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


# ---------------------------------------------------------------------------
# Tools: market intelligence (sales, reviews, ratings, popularity, news)
# These are NOT tied to any user account and need no SteamID.
# ---------------------------------------------------------------------------

@mcp.tool(
    name="steam_get_app_reviews",
    annotations={
        "title": "Get Steam App Reviews & Rating",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
def _fmt_review(r: dict) -> dict:
    """Normalize one raw Steam review object into a compact dict."""
    text = (r.get("review") or "").strip().replace("\n", " ")
    return {
        "voted_up": r.get("voted_up"),
        "votes_up": r.get("votes_up", 0),
        "playtime_hours": _minutes_to_hours(
            (r.get("author") or {}).get("playtime_forever")
        ),
        "timestamp_created": r.get("timestamp_created"),
        "excerpt": (text[:280] + "…") if len(text) > 280 else text,
    }


async def _collect_recent_reviews(
    appid: int, day_range: int, cc: str
) -> tuple[list[dict], bool]:
    """Paginate the newest reviews (filter=recent) within the last `day_range` days.

    Steam's query_summary is always lifetime, so the recent score must be tallied
    from individual reviews. Returns (reviews_in_window, capped) where `capped` is
    True if the page budget was exhausted before reaching the window's edge (i.e.
    there may be more recent reviews than were counted).
    """
    import time

    cutoff = time.time() - day_range * 86400
    collected: list[dict] = []
    cursor = "*"
    seen: set[str] = set()
    for _ in range(MAX_RECENT_PAGES):
        data = await _raw_get(
            f"https://store.steampowered.com/appreviews/{appid}",
            {
                "json": 1,
                "filter": "recent",
                "language": "english",
                "review_type": "all",
                "purchase_type": "all",
                "num_per_page": RECENT_PAGE_SIZE,
                "cc": cc,
                "cursor": cursor,
            },
        )
        if data.get("success") != 1:
            return collected, False
        revs = data.get("reviews", [])
        if not revs:
            return collected, False
        for r in revs:
            if (r.get("timestamp_created") or 0) >= cutoff:
                collected.append(r)
            else:
                return collected, False  # reached the window edge: fully covered
        nxt = data.get("cursor")
        if not nxt or nxt in seen:
            return collected, False
        seen.add(nxt)
        cursor = nxt
    return collected, True  # exhausted page budget without reaching the edge


async def steam_get_app_reviews(params: AppReviewsInput) -> str:
    """Get the review score and sample reviews for a game (lifetime and/or recent).

    Answers "is X any good", "what's the rating of X", and "how are the RECENT
    reviews for X". Always returns Steam's lifetime verdict (e.g. 'Very Positive').
    With review_filter='recent', it ALSO computes the last-N-days positive % by
    tallying the newest reviews — Steam's API has no recent-summary field, so this
    is derived from individual reviews and is marked 'sampled' if the game has more
    recent reviews than the page budget (~600). No API key required.

    Args:
        params (AppReviewsInput): appid, review_filter ('all'|'recent'),
            day_range (window for 'recent'), review_type (excerpt sampling),
            limit (number of excerpts), country_code.

    Returns:
        str: Markdown or JSON. Always includes the lifetime summary
        (review_score_desc, total_positive/negative/reviews, positive_pct). When
        review_filter='recent', adds a 'recent' block (day_range, reviews_counted,
        positive, negative, positive_pct, sampled) and samples excerpts from the
        recent window; otherwise samples from the most-helpful lifetime reviews.
    """
    try:
        # Lifetime summary (always) + excerpt source for the 'all' path.
        base = await _raw_get(
            f"https://store.steampowered.com/appreviews/{params.appid}",
            {
                "json": 1,
                "filter": "all",
                "language": "english",
                "review_type": params.review_type,
                "purchase_type": "all",
                "num_per_page": params.limit if params.review_filter == "all" else 0,
                "cc": params.country_code,
            },
        )
        if base.get("success") != 1:
            return f"No review data available for app {params.appid}."
        summ = base.get("query_summary", {})
        total = summ.get("total_reviews", 0)
        pos = summ.get("total_positive", 0)
        neg = summ.get("total_negative", 0)
        pos_pct = round(100.0 * pos / (pos + neg), 1) if (pos + neg) else 0.0

        recent = None
        if params.review_filter == "recent":
            window, capped = await _collect_recent_reviews(
                params.appid, params.day_range, params.country_code
            )
            rpos = sum(1 for r in window if r.get("voted_up"))
            rneg = len(window) - rpos
            rpct = round(100.0 * rpos / len(window), 1) if window else 0.0
            recent = {
                "day_range": params.day_range,
                "reviews_counted": len(window),
                "positive": rpos,
                "negative": rneg,
                "positive_pct": rpct,
                "sampled": capped,
            }
            sample_src = window
        else:
            sample_src = base.get("reviews", [])

        if params.review_type == "positive":
            sample_src = [r for r in sample_src if r.get("voted_up")]
        elif params.review_type == "negative":
            sample_src = [r for r in sample_src if not r.get("voted_up")]
        reviews = [_fmt_review(r) for r in sample_src[: params.limit]]

        if params.response_format == ResponseFormat.JSON:
            out = {
                "appid": params.appid,
                "summary": {
                    "review_score_desc": summ.get("review_score_desc"),
                    "total_reviews": total,
                    "total_positive": pos,
                    "total_negative": neg,
                    "positive_pct": pos_pct,
                },
                "reviews": reviews,
            }
            if recent is not None:
                out["recent"] = recent
            return _dump(out)

        lines = [
            f"# Reviews for app {params.appid}",
            f"- **Overall (all-time)**: {summ.get('review_score_desc', 'n/a')} — "
            f"{pos:,}/{pos + neg:,} ({pos_pct}%)",
        ]
        if recent is not None:
            note = " (sampled — capped)" if recent["sampled"] else ""
            lines.append(
                f"- **Recent (last {recent['day_range']}d)**: "
                f"{recent['positive_pct']}% of {recent['reviews_counted']} "
                f"reviews{note}"
            )
        lines.append("")
        if reviews:
            scope = "recent" if params.review_filter == "recent" else params.review_type
            lines.append(f"## Sample {scope} reviews")
            for r in reviews:
                thumb = "👍" if r["voted_up"] else "👎"
                lines.append(
                    f"- {thumb} ({r['playtime_hours']}h played, "
                    f"{r['votes_up']} found helpful): {r['excerpt']}"
                )
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


async def _fetch_featured(cc: str) -> dict:
    """Fetch the storefront featuredcategories payload (no key required)."""
    return await _store_get("featuredcategories", {"cc": cc, "l": "english"},
                            cache_ttl=CACHE_TTL_FEATURED)


def _featured_rows(items: list, limit: int) -> list:
    """Normalize featuredcategories items into compact rows."""
    rows = []
    for it in items[:limit]:
        rows.append(
            {
                "appid": it.get("id"),
                "name": it.get("name"),
                "original_price": (it.get("original_price") or 0) / 100,
                "final_price": (it.get("final_price") or 0) / 100,
                "discount_pct": it.get("discount_percent", 0),
            }
        )
    return rows


async def _app_price(appid: int, cc: str) -> dict:
    """Fetch a single app's name + current price/discount via the store API."""
    try:
        data = await _store_get(
            "appdetails",
            {
                "appids": appid,
                "cc": cc,
                "l": "english",
                "filters": "basic,price_overview",
            },
            cache_ttl=CACHE_TTL_APPDETAILS,
        )
        entry = data.get(str(appid), {})
        if not entry.get("success"):
            return {"appid": appid, "name": None, "on_sale": False, "discount_pct": 0}
        d = entry.get("data", {})
        price = d.get("price_overview") or {}
        is_free = d.get("is_free", False)
        disc = price.get("discount_percent", 0) or 0
        return {
            "appid": appid,
            "name": d.get("name"),
            "is_free": is_free,
            "price": price.get("final_formatted") or ("Free" if is_free else None),
            "discount_pct": disc,
            "on_sale": disc > 0,
        }
    except Exception:  # noqa: BLE001
        return {"appid": appid, "name": None, "on_sale": False, "discount_pct": 0}


@mcp.tool(
    name="steam_get_featured_specials",
    annotations={
        "title": "Get Steam Featured Sales/Specials",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_featured_specials(params: FeaturedInput) -> str:
    """List games currently ON SALE (featured specials) on the Steam store.

    Answers "what's on sale right now" and "any good Steam deals". Returns the
    discounted price, original price, and discount percent for each. Regional via
    country_code. No API key required. For top sellers / new releases / coming
    soon, use steam_get_store_highlights.

    Args:
        params (FeaturedInput): limit, country_code.

    Returns:
        str: Markdown or JSON list: appid, name, original_price, final_price,
        discount_pct.
    """
    try:
        data = await _fetch_featured(params.country_code)
        rows = _featured_rows(data.get("specials", {}).get("items", []), params.limit)
        if not rows:
            return "No featured specials returned right now."
        if params.response_format == ResponseFormat.JSON:
            return _dump(
                {"country": params.country_code, "count": len(rows), "specials": rows}
            )

        lines = [f"# Steam specials on sale ({params.country_code.upper()})", ""]
        for r in rows:
            lines.append(
                f"- **{r['name']}** (appid {r['appid']}): "
                f"${r['final_price']:.2f} (was ${r['original_price']:.2f}, "
                f"-{r['discount_pct']}%)"
            )
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


@mcp.tool(
    name="steam_get_store_highlights",
    annotations={
        "title": "Get Steam Store Highlights",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_store_highlights(params: StoreHighlightsInput) -> str:
    """List a Steam storefront section: top sellers, new releases, or coming soon.

    Answers "what's popular on Steam right now", "what new games just came out",
    and "what's coming soon". Also supports 'specials' (same data as
    steam_get_featured_specials). No API key required.

    Args:
        params (StoreHighlightsInput): section ('top_sellers' | 'new_releases' |
            'coming_soon' | 'specials'), limit, country_code.

    Returns:
        str: Markdown or JSON list: appid, name, final_price, original_price,
        discount_pct.
    """
    try:
        data = await _fetch_featured(params.country_code)
        node = data.get(params.section, {})
        items = node.get("items", []) if isinstance(node, dict) else []
        rows = _featured_rows(items, params.limit)
        if not rows:
            return f"No items returned for section '{params.section}'."
        if params.response_format == ResponseFormat.JSON:
            return _dump(
                {
                    "section": params.section,
                    "country": params.country_code,
                    "count": len(rows),
                    "items": rows,
                }
            )

        titles = {
            "top_sellers": "Top sellers",
            "new_releases": "New releases",
            "coming_soon": "Coming soon",
            "specials": "Specials",
        }
        lines = [
            f"# {titles[params.section]} ({params.country_code.upper()})",
            "",
        ]
        for r in rows:
            if r["discount_pct"]:
                price = (
                    f"${r['final_price']:.2f} (was ${r['original_price']:.2f}, "
                    f"-{r['discount_pct']}%)"
                )
            elif r["final_price"]:
                price = f"${r['final_price']:.2f}"
            else:
                price = "Free / TBA"
            lines.append(f"- **{r['name']}** (appid {r['appid']}): {price}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


@mcp.tool(
    name="steam_get_wishlist",
    annotations={
        "title": "Get Steam Wishlist",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_wishlist(params: WishlistInput) -> str:
    """Get a user's Steam wishlist, optionally with live prices and sale status.

    Answers "what's on my wishlist" and "which of my wishlist games are on sale".
    Returns wishlist entries ordered by priority; with enrich=true each is
    annotated with its name, current price, and whether it's discounted (use
    on_sale_only=true to filter to just the deals). Requires the target's wishlist
    privacy to be Public. Needs an API key.

    Args:
        params (WishlistInput): steamid, limit, enrich, on_sale_only, country_code.

    Returns:
        str: Markdown or JSON. total wishlist size plus per entry: appid, priority,
        and (when enriched) name, price, discount_pct, on_sale.
    """
    try:
        sid = await _resolve_steamid(params.steamid)
        data = await _steam_get(
            "IWishlistService/GetWishlist/v1/", {"steamid": sid}
        )
        items = data.get("response", {}).get("items", [])
        if not items:
            return (
                "No wishlist items returned. The wishlist is empty, or its privacy "
                "is not set to Public."
            )
        items.sort(key=lambda x: x.get("priority", 0))
        total = len(items)
        page = items[: params.limit]

        rows = []
        for it in page:
            appid = it.get("appid")
            row = {"appid": appid, "priority": it.get("priority")}
            if params.enrich:
                info = await _app_price(appid, params.country_code)
                row.update(
                    {
                        "name": info.get("name"),
                        "price": info.get("price"),
                        "discount_pct": info.get("discount_pct", 0),
                        "on_sale": info.get("on_sale", False),
                    }
                )
            rows.append(row)

        if params.enrich and params.on_sale_only:
            rows = [r for r in rows if r.get("on_sale")]

        if params.response_format == ResponseFormat.JSON:
            return _dump(
                {
                    "steamid": sid,
                    "total": total,
                    "count": len(rows),
                    "enriched": params.enrich,
                    "items": rows,
                }
            )

        header = f"{total} items total; showing {len(rows)}"
        if params.on_sale_only:
            header += " (on sale only)"
        lines = [f"# Wishlist for {sid}", header + ".", ""]
        for r in rows:
            if params.enrich:
                name = r.get("name") or f"appid {r['appid']}"
                if r.get("on_sale"):
                    tail = f" — 🔖 {r.get('price')} (-{r.get('discount_pct')}%)"
                elif r.get("price"):
                    tail = f" — {r.get('price')}"
                else:
                    tail = ""
                lines.append(f"- **{name}** (appid {r['appid']}){tail}")
            else:
                lines.append(
                    f"- appid {r['appid']} (priority {r.get('priority')})"
                )
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


@mcp.tool(
    name="steam_get_current_players",
    annotations={
        "title": "Get Steam Live Player Count",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_current_players(params: AppOnlyInput) -> str:
    """Get the number of players currently in-game for a title (live concurrency).

    Answers "how many people are playing X right now" / "is X still popular". No API
    key required.

    Args:
        params (AppOnlyInput): appid.

    Returns:
        str: The current concurrent player count, or an Error string.
    """
    try:
        data = await _steam_get(
            "ISteamUserStats/GetNumberOfCurrentPlayers/v1/",
            {"appid": params.appid},
            with_key=False,
        )
        resp = data.get("response", {})
        if resp.get("result") != 1:
            return f"No live player count available for app {params.appid}."
        count = resp.get("player_count", 0)
        if params.response_format == ResponseFormat.JSON:
            return _dump({"appid": params.appid, "current_players": count})
        return f"App {params.appid} currently has {count:,} players in-game."
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


@mcp.tool(
    name="steam_get_app_news",
    annotations={
        "title": "Get Steam App News/Updates",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_app_news(params: AppNewsInput) -> str:
    """Get recent news/update posts for a game (patch notes, announcements).

    Answers "what's new in X" / "latest update for X". No API key required.

    Args:
        params (AppNewsInput): appid, count.

    Returns:
        str: Markdown or JSON. Per item: title, date, source feed, url, and a short
        excerpt of the contents.
    """
    try:
        data = await _steam_get(
            "ISteamNews/GetNewsForApp/v2/",
            {"appid": params.appid, "count": params.count, "maxlength": 300},
            with_key=False,
        )
        items = data.get("appnews", {}).get("newsitems", [])
        rows = []
        for it in items:
            body = (it.get("contents") or "").strip().replace("\n", " ")
            rows.append(
                {
                    "title": it.get("title"),
                    "date": it.get("date"),
                    "feed": it.get("feedlabel"),
                    "url": it.get("url"),
                    "excerpt": (body[:280] + "…") if len(body) > 280 else body,
                }
            )
        if not rows:
            return f"No news found for app {params.appid}."
        if params.response_format == ResponseFormat.JSON:
            return _dump({"appid": params.appid, "count": len(rows), "news": rows})

        import datetime as _dt

        lines = [f"# News for app {params.appid}", ""]
        for r in rows:
            when = (
                _dt.datetime.fromtimestamp(r["date"], _dt.timezone.utc).strftime("%Y-%m-%d")
                if r["date"]
                else "?"
            )
            lines.append(f"## {r['title']} ({when}, {r['feed']})")
            lines.append(r["excerpt"])
            lines.append(f"[Read more]({r['url']})")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


# ---------------------------------------------------------------------------
# Tools: badges, package details, and player comparison
# ---------------------------------------------------------------------------

class PackageDetailsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    packageid: int = Field(
        ...,
        description="Steam package (sub) ID. Package IDs appear in a game's "
        "store details under 'packages' (distinct from app IDs).",
        ge=1,
    )
    country_code: str = Field(
        default="us",
        description="ISO country code for regional pricing (e.g. 'us', 'gb').",
        min_length=2,
        max_length=2,
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class ComparePlayersInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    steamid_a: str = Field(
        ...,
        description="First user: SteamID64, vanity name, or profile URL.",
        min_length=1,
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
        sid = await _resolve_steamid(params.steamid)
        data = await _steam_get("IPlayerService/GetBadges/v1/", {"steamid": sid})
        resp = data.get("response", {})
        badges = resp.get("badges", [])
        if not resp or resp.get("player_level") is None:
            return "No badge data returned (the profile is likely private)."
        level = resp.get("player_level")
        xp = resp.get("player_xp") or 0
        to_next = resp.get("player_xp_needed_to_level_up") or 0
        top = sorted(badges, key=lambda b: b.get("xp", 0), reverse=True)[:15]

        if params.response_format == ResponseFormat.JSON:
            return _dump(
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
        return _handle_error(e)


@mcp.tool(
    name="steam_get_package_details",
    annotations={
        "title": "Get Steam Package/Bundle Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_package_details(params: PackageDetailsInput) -> str:
    """Get store details for a Steam package (a sub/bundle of one or more games).

    Answers "how much is the X package" and "what games are in this bundle".
    appdetails covers single games; this covers multi-game packages. No API key
    required.

    Args:
        params (PackageDetailsInput): packageid, country_code.

    Returns:
        str: Markdown or JSON. name, price, discount, release date, and the list
        of apps the package includes.
    """
    try:
        data = await _store_get(
            "packagedetails",
            {"packageids": params.packageid, "cc": params.country_code, "l": "english"},
            cache_ttl=CACHE_TTL_PACKAGE,
        )
        entry = data.get(str(params.packageid), {})
        if not entry.get("success"):
            return f"No package details found for package {params.packageid}."
        d = entry.get("data", {})
        price = d.get("price") or {}
        apps = [a.get("name") for a in d.get("apps", []) if a.get("name")]
        summary = {
            "packageid": params.packageid,
            "name": d.get("name"),
            "final_price": (price.get("final", 0) / 100) if price else None,
            "initial_price": (price.get("initial", 0) / 100) if price else None,
            "discount_pct": price.get("discount_percent", 0) if price else 0,
            "release_date": (d.get("release_date") or {}).get("date"),
            "apps": apps,
        }
        if params.response_format == ResponseFormat.JSON:
            return _dump(summary)

        lines = [f"# {summary['name']} (package {params.packageid})"]
        if price:
            if summary["discount_pct"]:
                lines.append(
                    f"- **Price**: ${summary['final_price']:.2f} "
                    f"(was ${summary['initial_price']:.2f}, -{summary['discount_pct']}%)"
                )
            else:
                lines.append(f"- **Price**: ${summary['final_price']:.2f}")
        if summary["release_date"]:
            lines.append(f"- **Released**: {summary['release_date']}")
        if apps:
            lines.append(f"- **Includes {len(apps)} app(s)**: " + ", ".join(apps[:20]))
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


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
        sid_a = await _resolve_steamid(params.steamid_a)
        sid_b = await _resolve_steamid(params.steamid_b)

        async def _owned(sid: str) -> dict:
            d = await _steam_get(
                "IPlayerService/GetOwnedGames/v1/",
                {"steamid": sid, "include_appinfo": 1, "include_played_free_games": 1},
            )
            return {g["appid"]: g for g in d.get("response", {}).get("games", [])}

        games_a = await _owned(sid_a)
        games_b = await _owned(sid_b)
        if not games_a or not games_b:
            return (
                "Could not compare — one or both profiles have private game details "
                "(or own no games)."
            )

        shared_ids = set(games_a) & set(games_b)
        shared = []
        for aid in shared_ids:
            ga, gb = games_a[aid], games_b[aid]
            ha = _minutes_to_hours(ga.get("playtime_forever"))
            hb = _minutes_to_hours(gb.get("playtime_forever"))
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
            return _dump(
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
        return _handle_error(e)


# ---------------------------------------------------------------------------
# Helpers + library analysis
# ---------------------------------------------------------------------------

def _strip_html(s, limit: int = 600):
    """Strip HTML tags/entities to readable plain text, truncated to `limit`."""
    if not s:
        return None
    import html as _html
    s = re.sub(r"<\s*br\s*/?>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return None
    return (s[: limit - 1] + "…") if len(s) > limit else s


def _parse_languages(html_str):
    """Parse Steam's supported_languages HTML into (all, full_audio) name lists.

    Steam marks full-audio languages with an asterisk, e.g.
    'English<strong>*</strong>, French, German<br><strong>*</strong>languages...'.
    """
    if not html_str:
        return [], []
    head = re.split(r"<\s*br\s*/?>", html_str)[0]
    out, audio = [], []
    for seg in head.split(","):
        full = "*" in seg
        name = re.sub(r"<[^>]+>", "", seg).replace("*", "").strip()
        if name:
            out.append(name)
            if full:
                audio.append(name)
    return out, audio


def _ts_to_date(ts):
    """Unix seconds -> 'YYYY-MM-DD'. None for missing/sentinel values (pre-2001).

    Steam only began recording last-played timestamps ~2019; older plays carry a
    tiny placeholder value, so anything before 2001 is treated as 'unknown'.
    """
    try:
        if not ts or ts < 1_000_000_000:
            return None
        import datetime as _dt
        return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return None


class LibraryAnalysisInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    steamid: str = Field(
        ...,
        description="SteamID64, vanity name, or profile URL of the library owner.",
        min_length=1,
        max_length=200,
    )
    top_limit: int = Field(
        default=10, description="How many most-played games to list (1-50).",
        ge=1, le=50,
    )
    backlog_limit: int = Field(
        default=25, description="How many never-played games to list (0-100).",
        ge=0, le=100,
    )
    stale_days: int = Field(
        default=365,
        description="A played game untouched for at least this many days is "
        "counted as 'abandoned' (30-3650).",
        ge=30, le=3650,
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


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
    go", and "what did I love but abandon". Computed from a single owned-games
    call, so it spans the entire library cheaply. Requires the profile's Game
    Details to be Public. Needs an API key.

    Reports total games and hours; the never-played backlog; a playtime histogram
    (0h / <1h / 1-5h / 5-20h / 20-100h / 100h+); most-played games; recently active
    games; and 'abandoned' games (played, but not launched within `stale_days`).
    Steam only began recording last-played dates ~2019, so games last played before
    then show 'last played: unknown' rather than a date.

    Args:
        params (LibraryAnalysisInput): steamid, top_limit, backlog_limit, stale_days.

    Returns:
        str: Markdown or JSON with summary stats, playtime_buckets, top_played,
        recently_played, backlog_never_played, and abandoned lists.
    """
    try:
        import time as _time
        sid = await _resolve_steamid(params.steamid)
        data = await _steam_get(
            "IPlayerService/GetOwnedGames/v1/",
            {"steamid": sid, "include_appinfo": 1, "include_played_free_games": 1},
        )
        resp = data.get("response", {})
        games = resp.get("games", [])
        if not games:
            return (
                "No games returned. The profile's Game Details are likely private, "
                "or it owns no games."
            )
        game_count = resp.get("game_count", len(games))
        cutoff = _time.time() - params.stale_days * 86400

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
            return {
                "appid": g.get("appid"),
                "name": g.get("name"),
                "hours": _minutes_to_hours(g.get("playtime_forever")),
                "last_played": _ts_to_date(g.get("rtime_last_played")),
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
             "hours_2weeks": _minutes_to_hours(g.get("playtime_2weeks"))}
            for g in recent[:10]
        ]
        abandoned_src = [
            g for g in played
            if 1_000_000_000 < g.get("rtime_last_played", 0) < cutoff
        ]
        abandoned = [
            _row(g) for g in sorted(
                abandoned_src, key=lambda g: g.get("rtime_last_played", 0)
            )[: params.backlog_limit]
        ]
        backlog = [
            {"appid": g.get("appid"), "name": g.get("name")}
            for g in sorted(never, key=lambda g: (g.get("name") or "").lower())[
                : params.backlog_limit
            ]
        ]

        total_hours = round(total_min / 60, 1)
        summary = {
            "game_count": game_count,
            "total_hours": total_hours,
            "played_count": len(played),
            "never_played_count": len(never),
            "never_played_pct": round(100 * len(never) / game_count, 1) if game_count else 0,
            "avg_hours_per_owned_game": round(total_hours / game_count, 1) if game_count else 0,
            "avg_hours_per_played_game": round(total_hours / len(played), 1) if played else 0,
        }

        if params.response_format == ResponseFormat.JSON:
            return _dump({
                "steamid": sid,
                "summary": summary,
                "playtime_buckets": buckets,
                "top_played": top_played,
                "recently_played": recently_played,
                "backlog_never_played": backlog,
                "abandoned": abandoned,
            })

        lines = [
            f"# Library analysis for {sid}",
            f"- **Games owned**: {game_count}  |  **Total played**: {total_hours:,.1f}h",
            f"- **Never played**: {len(never)} ({summary['never_played_pct']}% of library)",
            f"- **Avg hours/game**: {summary['avg_hours_per_owned_game']} owned, "
            f"{summary['avg_hours_per_played_game']} of played",
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
            lines.append(f"- **{g['name']}** — {g['hours']}h{lp}")
        if abandoned:
            lines += ["", f"## Abandoned (played, untouched {params.stale_days}+ days)"]
            for g in abandoned:
                lines.append(
                    f"- **{g['name']}** — {g['hours']}h, last played {g['last_played']}"
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
        return _handle_error(e)


def main() -> None:
    """Run the server over stdio (default MCP transport for local clients)."""
    mcp.run()


if __name__ == "__main__":
    main()
