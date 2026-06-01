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

import asyncio
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

# Currency code -> display symbol. Steam's storefront list endpoints (storesearch,
# featuredcategories, packagedetails) return prices in the requested country's
# currency as integer minor units plus a currency code, but no preformatted
# string -- so we format them ourselves. Unknown codes fall back to
# "<amount> <CODE>", and a missing code falls back to "$".
CURRENCY_SYMBOLS = {
    "USD": "$", "GBP": "£", "EUR": "€", "JPY": "¥", "CNY": "¥",
    "KRW": "₩", "INR": "₹", "RUB": "₽", "BRL": "R$", "CAD": "CA$",
    "AUD": "A$", "NZD": "NZ$", "MXN": "MX$", "ARS": "ARS$", "CLP": "CLP$",
    "COP": "COL$", "PEN": "S/.", "ZAR": "R", "TRY": "₺", "UAH": "₴",
    "PLN": "zł", "CHF": "CHF", "SEK": "kr", "NOK": "kr", "DKK": "kr",
    "HKD": "HK$", "TWD": "NT$", "SGD": "S$", "THB": "฿", "VND": "₫",
    "IDR": "Rp", "MYR": "RM", "PHP": "₱", "AED": "AED", "SAR": "SAR",
    "ILS": "₪", "KZT": "₸", "CRC": "₡",
}

# Steam store "supported player" category IDs that indicate co-op play, used to
# detect co-op games from IStoreBrowseService/GetItems. 9=Co-op, 24=Shared/Split
# Screen, 38=Online Co-op, 39=LAN Co-op.
COOP_CATEGORY_IDS = {9, 24, 38, 39}

PROFILE_URL_RE = re.compile(r"steamcommunity\.com/(profiles|id)/([^/?#]+)", re.IGNORECASE)
STEAMID64_RE = re.compile(r"^7656\d{13}$")  # 17-digit SteamID64 starting 7656


# --- Static-response cache (per-process, opt-in) -----------------------------
CACHE_TTL_APPDETAILS = 600      # 10 min (price can change on sales)
CACHE_TTL_PACKAGE = 3600
CACHE_TTL_FEATURED = 300        # 5 min
CACHE_TTL_SCHEMA = 86400        # achievement/stat definitions are static
CACHE_TTL_GLOBAL_ACH = 3600
CACHE_TTL_TAGS = 3600           # community tag weights (slow-changing)
CACHE_TTL_TAGMAP = 86400        # tagid -> name dictionary is effectively static
CACHE_TTL_DISCOVER = 300        # storefront search results (5 min)


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


_CLIENT: Optional[httpx.AsyncClient] = None
_CLIENT_LOOP: Optional[asyncio.AbstractEventLoop] = None


def _http_client() -> httpx.AsyncClient:
    """Return a shared AsyncClient bound to the *current* event loop.

    Reusing one client avoids a fresh TCP/TLS handshake per request and lets the
    fan-out tools (wishlist, DLC, comparisons) run many concurrent lookups over
    pooled connections; an AsyncClient is safe for concurrent use. An AsyncClient
    binds to the loop it first runs on, so if the running loop has changed (e.g. a
    fresh asyncio.run() in a script or test) we recreate it — otherwise reuse would
    raise "RuntimeError: Event loop is closed". The long-lived MCP server uses a
    single loop, so in normal operation the client is created exactly once.
    """
    global _CLIENT, _CLIENT_LOOP
    loop = asyncio.get_running_loop()
    if _CLIENT is None or _CLIENT.is_closed or _CLIENT_LOOP is not loop:
        _CLIENT = httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
            headers={"Accept": "application/json"},
        )
        _CLIENT_LOOP = loop
    return _CLIENT


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
    client = _http_client()
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
    client = _http_client()
    resp = await client.get(url, params=params, timeout=HTTP_TIMEOUT)
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


def _fmt_amount(amount: Optional[float], currency: Optional[str] = None) -> Optional[str]:
    """Format a price with the right currency symbol.

    `amount` is in major units (e.g. dollars — already divided by 100). Falls back
    to "<amount> <CODE>" for currencies without a known symbol, and to "$" only
    when no currency code is available at all.
    """
    if amount is None:
        return None
    if currency:
        sym = CURRENCY_SYMBOLS.get(currency.upper())
        if sym:
            return f"{sym}{amount:,.2f}"
        return f"{amount:,.2f} {currency.upper()}"
    return f"${amount:,.2f}"


FANOUT_LIMIT = 8  # max concurrent storefront lookups for fan-out tools


async def _gather_limited(coros, limit: int = FANOUT_LIMIT):
    """Await many coroutines with bounded concurrency, preserving input order.

    Keeps fan-out tools (wishlist / DLC enrichment) fast without hammering the
    storefront: at most `limit` requests are in flight at once.
    """
    sem = asyncio.Semaphore(limit)

    async def _run(coro):
        async with sem:
            return await coro

    return await asyncio.gather(*(_run(c) for c in coros))


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


class FriendsWhoOwnInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    steamid: str = Field(
        ...,
        description="The user whose friends to check: SteamID64, vanity, or URL.",
        min_length=1, max_length=200,
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
        d = await _steam_get(
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
    """Find which of a user's friends own (or are right now playing) a given game.

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
        sid = await _resolve_steamid(params.steamid)
        fdata = await _steam_get(
            "ISteamUser/GetFriendList/v1/",
            {"steamid": sid, "relationship": "friend"},
        )
        friends = fdata.get("friendslist", {}).get("friends", [])
        if not friends:
            return (
                "No friends returned. The user's friend list is likely private "
                "(set Friends List to Public in Steam privacy settings)."
            )
        all_ids = [f["steamid"] for f in friends]
        check_ids = all_ids[: params.max_friends]
        results = await _gather_limited(
            [_friend_owns_app(fid, params.appid) for fid in check_ids]
        )
        owners = [r for r in results if r["owns"]]
        private = sum(1 for r in results if r["private"])

        owner_ids = [r["fid"] for r in owners]
        summaries = await _summaries_for(owner_ids) if owner_ids else {}
        info = await _app_price(params.appid, "us")
        game_name = info.get("name") or f"app {params.appid}"

        rows = []
        for r in owners:
            p = summaries.get(r["fid"], {})
            playing = bool(p.get("gameid")) and str(p.get("gameid")) == str(params.appid)
            rows.append(
                {
                    "steamid": r["fid"],
                    "name": p.get("personaname", "Unknown"),
                    "playtime_hours": _minutes_to_hours(r["playtime_min"]),
                    "status": _persona_label(p) if p else "Unknown",
                    "playing_now": playing,
                }
            )
        owners_count = len(rows)
        if params.playing_now:
            rows = [r for r in rows if r["playing_now"]]
        rows.sort(key=lambda r: r["playtime_hours"], reverse=True)
        page = rows[: params.limit]

        if params.response_format == ResponseFormat.JSON:
            return _dump(
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
        sid = await _resolve_steamid(params.steamid)
        data = await _steam_get(
            "ISteamUserStats/GetUserStatsForGame/v2/",
            {"steamid": sid, "appid": params.appid},
        )
        stats_obj = data.get("playerstats", {})
        stats = stats_obj.get("stats", []) or []
        game_name = stats_obj.get("gameName") or str(params.appid)
        if not stats:
            return (
                f"No stats available for app {params.appid}. The game may define no "
                f"stats, or the profile's Game Details are private."
            )
        rows = [{"name": s.get("name"), "value": s.get("value")} for s in stats]

        if params.response_format == ResponseFormat.JSON:
            return _dump(
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
        return _handle_error(e)


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
        sid = await _resolve_steamid(params.steamid)
        ach_data, glob_data = await asyncio.gather(
            _steam_get(
                "ISteamUserStats/GetPlayerAchievements/v1/",
                {"steamid": sid, "appid": params.appid, "l": "english"},
            ),
            _steam_get(
                "ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/",
                {"gameid": params.appid},
                with_key=False,
                cache_ttl=CACHE_TTL_GLOBAL_ACH,
            ),
        )
        stats = ach_data.get("playerstats", {})
        if not stats.get("success", False):
            return (
                f"Error: {stats.get('error', 'No achievement data')}. The profile "
                f"may be private, or app {params.appid} has no achievements."
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
                    "unlocked_at": _ts_to_date(a.get("unlocktime")),
                }
            )
        rows.sort(key=lambda r: (r["global_pct"] is None, r["global_pct"] or 0.0))
        game_name = stats.get("gameName", str(params.appid))
        page = rows[: params.limit]

        if params.response_format == ResponseFormat.JSON:
            return _dump(
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
                "currency": (it.get("price") or {}).get("currency"),
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
                price = f" — {_fmt_amount(r['price'] / 100, r['currency'])}"
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


class DlcInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    appid: int = Field(
        ...,
        description="Steam application (game) ID of the BASE game whose DLC to list.",
        ge=1,
    )
    limit: int = Field(
        default=25,
        description="Max DLC entries to return (1-100). Big franchises list "
        "hundreds of DLC, so keep this modest when enriching.",
        ge=1,
        le=100,
    )
    enrich: bool = Field(
        default=True,
        description="Fetch each DLC's name + current price/discount (one store "
        "lookup per DLC, run concurrently). Set false for a fast appid-only list.",
    )
    on_sale_only: bool = Field(
        default=False,
        description="If true (requires enrich=true), return only DLC currently "
        "discounted.",
    )
    country_code: str = Field(default="us", min_length=2, max_length=2)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


@mcp.tool(
    name="steam_get_dlc",
    annotations={
        "title": "Get Steam Game DLC",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_dlc(params: DlcInput) -> str:
    """List a game's DLC (add-ons), optionally with live prices and sale status.

    Answers "what DLC does X have", "how much is all the X DLC", and "is any X DLC
    on sale". steam_get_app_details exposes only bare DLC appids; this resolves them
    to names + current prices (concurrently) and can filter to just the discounts
    via on_sale_only. Prices are returned in the country_code's local currency. No
    API key required.

    Args:
        params (DlcInput): appid (the base game), limit, enrich, on_sale_only,
            country_code.

    Returns:
        str: Markdown or JSON. base game name, total DLC count, and per entry:
        appid and (when enriched) name, price, discount_pct, on_sale.
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
        base_name = d.get("name") or f"app {params.appid}"
        dlc_ids = d.get("dlc", []) or []
        if not dlc_ids:
            return f"{base_name} (appid {params.appid}) has no listed DLC."

        total = len(dlc_ids)
        page_ids = dlc_ids[: params.limit]
        if params.enrich:
            infos = await _gather_limited(
                [_app_price(i, params.country_code) for i in page_ids]
            )
        else:
            infos = [None] * len(page_ids)

        rows = []
        for appid, info in zip(page_ids, infos, strict=True):
            row = {"appid": appid}
            if info is not None:
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
                    "appid": params.appid,
                    "base_game": base_name,
                    "dlc_total": total,
                    "count": len(rows),
                    "enriched": params.enrich,
                    "dlc": rows,
                }
            )

        header = f"{total} DLC total; showing {len(rows)}"
        if params.on_sale_only:
            header += " (on sale only)"
        lines = [f"# DLC for {base_name} (appid {params.appid})", header + ".", ""]
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
                lines.append(f"- appid {r['appid']}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


class AppTagsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    appid: int = Field(..., description="Steam application (game) ID.", ge=1)
    limit: int = Field(
        default=20,
        description="Max tags to return, ordered by community weight (1-50).",
        ge=1, le=50,
    )
    country_code: str = Field(default="us", min_length=2, max_length=2)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


async def _tag_name_map() -> dict:
    """Map Steam community tagid -> display name (cached; static-ish, no key).

    GetItems returns only tagids + weights; this storefront dictionary supplies the
    human names (e.g. 29482 -> 'Souls-like').
    """
    data = await _raw_get(
        "https://store.steampowered.com/tagdata/populartags/english",
        {}, cache_ttl=CACHE_TTL_TAGMAP,
    )
    out: dict = {}
    if isinstance(data, list):
        for t in data:
            try:
                out[int(t.get("tagid"))] = t.get("name")
            except (TypeError, ValueError):
                continue
    return out


@mcp.tool(
    name="steam_get_app_tags",
    annotations={
        "title": "Get Steam Community Tags",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_app_tags(params: AppTagsInput) -> str:
    """Get a game's top community tags (Souls-like, Roguelike, Cozy, …) by weight.

    Community tags are player-applied descriptors that capture sub-genres and vibes
    Steam's official `genres` miss — the best signal for "is this a soulslike / cozy
    / bullet-hell". Returns the most-weighted tags for the app. Built from the
    storefront's modern item API plus its public tag dictionary; no API key required.

    Args:
        params (AppTagsInput): appid, limit, country_code.

    Returns:
        str: Markdown (comma-separated tag list) or JSON (per tag: tag, tagid,
        weight), ordered most-weighted first.
    """
    try:
        body = {
            "ids": [{"appid": params.appid}],
            "context": {
                "language": "english",
                "country_code": params.country_code.upper(),
                "steam_realm": 1,
            },
            "data_request": {"include_tag_count": 50, "include_basic_info": True},
        }
        data = await _steam_get(
            "IStoreBrowseService/GetItems/v1/",
            {"input_json": json.dumps(body, separators=(",", ":"))},
            with_key=False,
            cache_ttl=CACHE_TTL_TAGS,
        )
        items = (data.get("response") or {}).get("store_items") or []
        if not items:
            return f"No store data found for app {params.appid}."
        item = items[0]
        name = item.get("name") or str(params.appid)
        raw_tags = item.get("tags") or []
        if not raw_tags:
            return f"No community tags found for {name} (appid {params.appid})."
        name_map = await _tag_name_map()
        rows = []
        for t in raw_tags:
            try:
                tid = int(t.get("tagid"))
            except (TypeError, ValueError):
                continue
            tname = name_map.get(tid)
            if not tname:
                continue
            rows.append({"tag": tname, "tagid": tid, "weight": t.get("weight", 0)})
        rows = rows[: params.limit]
        if not rows:
            return (
                f"Found {len(raw_tags)} tags for {name} but could not resolve their "
                f"names from the tag dictionary."
            )
        if params.response_format == ResponseFormat.JSON:
            return _dump(
                {"appid": params.appid, "name": name, "count": len(rows), "tags": rows}
            )
        return "\n".join(
            [
                f"# Community tags: {name} (appid {params.appid})",
                "",
                ", ".join(r["tag"] for r in rows),
            ]
        )
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


# --- Discovery: filtered search + optional personalization ------------------

SEARCH_URL = "https://store.steampowered.com/search/results/"

# Friendly sort name -> Steam search sort_by value ("" = let Steam default).
_SORT_MAP = {
    "reviews": "Reviews_DESC",
    "release": "Released_DESC",
    "price_asc": "Price_ASC",
    "price_desc": "Price_DESC",
    "relevance": "",
}


async def _resolve_tag_ids(names: list[str]) -> tuple[list[int], list[str]]:
    """Resolve community tag NAMES to Steam tag IDs via the cached dictionary.

    Returns (ids, unresolved_names); case-insensitive.
    """
    if not names:
        return [], []
    name_map = await _tag_name_map()  # {tagid: name}
    rev = {(nm or "").lower(): tid for tid, nm in name_map.items()}
    ids, missing = [], []
    for n in names:
        tid = rev.get(n.strip().lower())
        if tid is not None:
            ids.append(tid)
        else:
            missing.append(n)
    return ids, missing


async def _items_tags(appids: list[int]) -> dict:
    """One GetItems call -> {appid: [{tagid, weight}, ...]} for many apps (no key)."""
    if not appids:
        return {}
    body = {
        "ids": [{"appid": a} for a in appids],
        "context": {"language": "english", "country_code": "US", "steam_realm": 1},
        "data_request": {"include_tag_count": 20},
    }
    data = await _steam_get(
        "IStoreBrowseService/GetItems/v1/",
        {"input_json": json.dumps(body, separators=(",", ":"))},
        with_key=False,
        cache_ttl=CACHE_TTL_TAGS,
    )
    out = {}
    for it in (data.get("response") or {}).get("store_items", []):
        out[it.get("appid")] = it.get("tags") or []
    return out


async def _taste_profile(sid: str, max_seed: int = 12, top_tags: int = 5) -> dict:
    """Build a taste profile from a user's recent + most-played games.

    Returns {owned_ids, tag_ids, tag_names, seed_games}: the games the user owns
    (for exclusion), and the top community tags aggregated by weight across their
    seed games (one batched GetItems call).
    """
    owned_d, recent_d = await asyncio.gather(
        _steam_get(
            "IPlayerService/GetOwnedGames/v1/",
            {"steamid": sid, "include_appinfo": 1, "include_played_free_games": 1},
        ),
        _steam_get("IPlayerService/GetRecentlyPlayedGames/v1/", {"steamid": sid}),
    )
    games = owned_d.get("response", {}).get("games", []) or []
    owned_ids = {g.get("appid") for g in games}
    name_by_id = {g.get("appid"): g.get("name") for g in games}
    by_play = sorted(
        (g for g in games if g.get("playtime_forever", 0) > 0),
        key=lambda g: g.get("playtime_forever", 0), reverse=True,
    )
    recent = recent_d.get("response", {}).get("games", []) or []
    for g in recent:
        name_by_id.setdefault(g.get("appid"), g.get("name"))

    # Seed from recent games (current taste) first, then most-played.
    seed: list[int] = []
    for g in recent + by_play:
        a = g.get("appid")
        if a and a not in seed:
            seed.append(a)
        if len(seed) >= max_seed:
            break
    if not seed:
        return {"owned_ids": owned_ids, "tag_ids": [], "tag_names": [], "seed_games": []}

    tags_by_app = await _items_tags(seed)
    weights: dict[int, float] = {}
    for a in seed:
        for t in tags_by_app.get(a, []):
            try:
                tid = int(t.get("tagid"))
            except (TypeError, ValueError):
                continue
            weights[tid] = weights.get(tid, 0) + (t.get("weight") or 1)
    top = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)[:top_tags]
    name_map = await _tag_name_map()
    tag_ids = [tid for tid, _ in top]
    tag_names = [name_map[tid] for tid, _ in top if name_map.get(tid)]
    display = [g.get("name") for g in by_play[:5]] or [name_by_id.get(a) for a in seed[:5]]
    return {
        "owned_ids": owned_ids,
        "tag_ids": tag_ids,
        "tag_names": tag_names,
        "seed_games": [n for n in display if n],
    }


async def _discover_appids(query: dict) -> tuple[list[int], int]:
    """Run the storefront search; return (ranked_appids, total_count).

    The store search returns rendered HTML, so we pull the ranked app IDs from the
    stable `data-ds-appid` attribute on each result row. Guarded: an empty/garbled
    response simply yields no IDs.
    """
    data = await _raw_get(SEARCH_URL, query, cache_ttl=CACHE_TTL_DISCOVER)
    if not isinstance(data, dict):
        return [], 0
    html = data.get("results_html") or ""
    ids: list[int] = []
    seen = set()
    for m in re.finditer(r'data-ds-appid="(\d+)', html):
        a = int(m.group(1))
        if a not in seen:
            seen.add(a)
            ids.append(a)
    return ids, data.get("total_count", len(ids))


class DiscoverInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    term: Optional[str] = Field(
        default=None, description="Optional free-text title/keyword to search.",
        max_length=200,
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Community tag names to require (AND), e.g. "
        "['Roguelike', 'Co-op']. Resolved to Steam tag IDs; unknown names are "
        "reported and ignored.",
        max_length=10,
    )
    max_price: Optional[int] = Field(
        default=None,
        description="Maximum price in the country's currency units (e.g. 30 = $30 "
        "for country_code='us'). Omit for any price.",
        ge=0, le=1000,
    )
    on_sale: bool = Field(default=False, description="Only games currently on sale.")
    platform: Optional[str] = Field(
        default=None, description="Filter by OS: 'win', 'mac', or 'linux'.",
    )
    sort: str = Field(
        default="reviews",
        description="Order: 'reviews' (best-reviewed first, default), 'release' "
        "(newest), 'price_asc', 'price_desc', or 'relevance'.",
    )
    steamid: Optional[str] = Field(
        default=None,
        description="Optional. If set, personalize: seed tags from this user's "
        "most-played + recently-played games and (by default) exclude games they "
        "own. SteamID64, vanity name, or profile URL.",
        max_length=200,
    )
    exclude_owned: bool = Field(
        default=True,
        description="When steamid is set, hide games the user already owns.",
    )
    limit: int = Field(
        default=15, description="Max results to return (1-50).", ge=1, le=50
    )
    country_code: str = Field(default="us", min_length=2, max_length=2)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)

    @field_validator("platform")
    @classmethod
    def _check_platform(cls, v):
        if v is None:
            return v
        v = v.lower().strip()
        if v not in {"win", "mac", "linux"}:
            raise ValueError("platform must be 'win', 'mac', or 'linux'")
        return v

    @field_validator("sort")
    @classmethod
    def _check_sort(cls, v):
        v = v.lower().strip()
        allowed = {"reviews", "release", "price_asc", "price_desc", "relevance"}
        if v not in allowed:
            raise ValueError(f"sort must be one of {sorted(allowed)}")
        return v


@mcp.tool(
    name="steam_discover",
    annotations={
        "title": "Discover / Recommend Steam Games",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_discover(params: DiscoverInput) -> str:
    """Find games by filters (tags, price, sale, platform) — and optionally recommend.

    The discovery/recommendation tool. Filters the whole store by community tags
    (by name), max price, on-sale, platform, and free text, sorted by review score
    (default), recency, or price. Pass a steamid to PERSONALIZE: it seeds the tag
    filter from that user's most-played + recently-played games and, by default,
    excludes games they already own — so it recommends NEW games matching their
    taste. Answers "find co-op roguelikes under $20" and "what should I play next".
    The search needs no API key; personalization needs one and a public profile.

    Args:
        params (DiscoverInput): term, tags, max_price, on_sale, platform, sort,
            steamid, exclude_owned, limit, country_code.

    Returns:
        str: Markdown or JSON. The applied filters (incl. any derived taste tags),
        the match total_count, and a ranked list (appid, name, price, on_sale).
    """
    try:
        cc = params.country_code
        tag_ids, missing = await _resolve_tag_ids(params.tags)

        owned_ids: set = set()
        taste_tags: list[str] = []
        seed_games: list[str] = []
        if params.steamid:
            sid = await _resolve_steamid(params.steamid)
            taste = await _taste_profile(sid)
            if params.exclude_owned:
                owned_ids = {a for a in taste["owned_ids"] if a}
            seed_games = taste["seed_games"]
            if not tag_ids and taste["tag_ids"]:   # seed tags only if none given
                tag_ids = taste["tag_ids"]
                taste_tags = taste["tag_names"]

        query = {
            "json": 1, "infinite": 1, "cc": cc, "l": "english",
            "category1": 998,                       # games only
            "start": 0, "count": 100,
        }
        if params.term:
            query["term"] = params.term
        if tag_ids:
            query["tags"] = ",".join(str(t) for t in tag_ids)
        if params.max_price is not None:
            query["maxprice"] = str(params.max_price)
        if params.on_sale:
            query["specials"] = 1
        if params.platform:
            query["os"] = params.platform
        sort_by = _SORT_MAP.get(params.sort, "Reviews_DESC")
        if sort_by:
            query["sort_by"] = sort_by

        appids, total = await _discover_appids(query)
        appids = [a for a in appids if a not in owned_ids]
        page = appids[: params.limit]
        infos = await _gather_limited([_app_price(a, cc) for a in page]) if page else []
        rows = []
        for a, info in zip(page, infos, strict=True):
            rows.append({
                "appid": a,
                "name": info.get("name") or f"app {a}",
                "price": info.get("price"),
                "discount_pct": info.get("discount_pct", 0),
                "on_sale": info.get("on_sale", False),
            })

        excluded = len(owned_ids) if (params.steamid and params.exclude_owned) else 0
        if params.response_format == ResponseFormat.JSON:
            return _dump({
                "filters": {
                    "term": params.term,
                    "tags": params.tags,
                    "resolved_tag_ids": tag_ids,
                    "unresolved_tags": missing,
                    "max_price": params.max_price,
                    "on_sale": params.on_sale,
                    "platform": params.platform,
                    "sort": params.sort,
                },
                "personalized": bool(params.steamid),
                "seed_games": seed_games,
                "taste_tags": taste_tags,
                "excluded_owned": excluded,
                "total_count": total,
                "count": len(rows),
                "results": rows,
            })

        bits = []
        if params.term:
            bits.append(f"'{params.term}'")
        if params.tags:
            bits.append("tags: " + ", ".join(params.tags))
        if params.max_price is not None:
            bits.append(f"<= {params.max_price} {cc.upper()}")
        if params.on_sale:
            bits.append("on sale")
        if params.platform:
            bits.append(params.platform)
        lines = [
            f"# Discover: {', '.join(bits) if bits else 'top games'}",
            f"Matched {total:,} games; showing {len(rows)} (sorted by {params.sort}).",
        ]
        if params.steamid and seed_games:
            extra = f" -> tags: {', '.join(taste_tags)}" if taste_tags else ""
            lines.append(
                f"Personalized from your most-played ({', '.join(seed_games)}){extra}."
            )
            if excluded:
                lines.append(f"Excluding {excluded:,} games you own.")
        if missing:
            lines.append(f"(couldn't resolve tags: {', '.join(missing)})")
        lines.append("")
        for r in rows:
            if r["on_sale"]:
                tail = f" - 🔖 {r['price']} (-{r['discount_pct']}%)"
            elif r["price"]:
                tail = f" - {r['price']}"
            else:
                tail = ""
            lines.append(f"- **{r['name']}** (appid {r['appid']}){tail}")
        if not rows:
            lines.append("(no matches — try loosening the filters)")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


# ---------------------------------------------------------------------------
# Tools: market intelligence (sales, reviews, ratings, popularity, news)
# These are NOT tied to any user account and need no SteamID.
# ---------------------------------------------------------------------------

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
                "currency": it.get("currency"),
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
            final = _fmt_amount(r["final_price"], r["currency"])
            orig = _fmt_amount(r["original_price"], r["currency"])
            lines.append(
                f"- **{r['name']}** (appid {r['appid']}): "
                f"{final} (was {orig}, -{r['discount_pct']}%)"
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
                    f"{_fmt_amount(r['final_price'], r['currency'])} (was "
                    f"{_fmt_amount(r['original_price'], r['currency'])}, "
                    f"-{r['discount_pct']}%)"
                )
            elif r["final_price"]:
                price = _fmt_amount(r["final_price"], r["currency"])
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

        if params.enrich:
            infos = await _gather_limited(
                [_app_price(it.get("appid"), params.country_code) for it in page]
            )
        else:
            infos = [None] * len(page)

        rows = []
        for it, info in zip(page, infos, strict=True):
            row = {"appid": it.get("appid"), "priority": it.get("priority")}
            if info is not None:
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
        currency = price.get("currency") if price else None
        summary = {
            "packageid": params.packageid,
            "name": d.get("name"),
            "final_price": (price.get("final", 0) / 100) if price else None,
            "initial_price": (price.get("initial", 0) / 100) if price else None,
            "discount_pct": price.get("discount_percent", 0) if price else 0,
            "currency": currency,
            "release_date": (d.get("release_date") or {}).get("date"),
            "apps": apps,
        }
        if params.response_format == ResponseFormat.JSON:
            return _dump(summary)

        lines = [f"# {summary['name']} (package {params.packageid})"]
        if price:
            if summary["discount_pct"]:
                lines.append(
                    f"- **Price**: {_fmt_amount(summary['final_price'], currency)} "
                    f"(was {_fmt_amount(summary['initial_price'], currency)}, "
                    f"-{summary['discount_pct']}%)"
                )
            else:
                lines.append(
                    f"- **Price**: {_fmt_amount(summary['final_price'], currency)}"
                )
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

        games_a, games_b = await asyncio.gather(_owned(sid_a), _owned(sid_b))
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


# ---------------------------------------------------------------------------
# Tools: intelligence (composite decision + recommendation helpers)
# ---------------------------------------------------------------------------

class ShouldIBuyInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    appid: int = Field(..., description="Steam application (game) ID to evaluate.", ge=1)
    steamid: Optional[str] = Field(
        default=None, max_length=200,
        description="Optional: personalize — whether you already own it and how its "
        "tags match your most-played games. SteamID64, vanity, or profile URL.",
    )
    country_code: str = Field(default="us", min_length=2, max_length=2)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


@mcp.tool(
    name="steam_should_i_buy",
    annotations={
        "title": "Steam Buying Brief (Should I Buy?)",
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def steam_should_i_buy(params: ShouldIBuyInput) -> str:
    """Gather everything needed to decide whether to buy a game, in one call.

    Fuses the decision-relevant signals: current price/discount, lifetime AND
    last-30-days review scores (the divergence shows whether a game is improving or
    declining), top community tags, Metacritic, and release status. Pass a steamid
    to personalize — whether you already own it and which of its tags match your
    most-played games. Returns the facts for a reasoned call (it does not hard-code
    a yes/no). The store data needs no API key; personalization does.

    Args:
        params (ShouldIBuyInput): appid, steamid, country_code.

    Returns:
        str: Markdown brief or JSON — price, reviews (lifetime + recent + trend),
        tags, metacritic, and (if steamid) ownership + taste match.
    """
    try:
        cc = params.country_code
        details, rev, tags_map = await asyncio.gather(
            _store_get("appdetails", {"appids": params.appid, "cc": cc, "l": "english"},
                       cache_ttl=CACHE_TTL_APPDETAILS),
            _raw_get(f"https://store.steampowered.com/appreviews/{params.appid}",
                     {"json": 1, "filter": "all", "language": "english",
                      "review_type": "all", "purchase_type": "all",
                      "num_per_page": 0, "cc": cc}),
            _items_tags([params.appid]),
        )
        entry = details.get(str(params.appid), {}) if isinstance(details, dict) else {}
        if not entry.get("success"):
            return f"No store details found for app {params.appid}."
        d = entry.get("data", {})
        name = d.get("name") or str(params.appid)
        price = d.get("price_overview") or {}
        is_free = d.get("is_free", False)
        rel = d.get("release_date") or {}

        summ = rev.get("query_summary", {}) if isinstance(rev, dict) else {}
        l_pos, l_neg = summ.get("total_positive", 0), summ.get("total_negative", 0)
        l_pct = round(100 * l_pos / (l_pos + l_neg), 1) if (l_pos + l_neg) else None
        window, capped = await _collect_recent_reviews(params.appid, 30, cc)
        r_n = len(window)
        r_pct = round(100 * sum(1 for r in window if r.get("voted_up")) / r_n, 1) if r_n else None
        trend = round(r_pct - l_pct, 1) if (r_pct is not None and l_pct is not None) else None

        name_map = await _tag_name_map()
        top_tag_ids, top_tags = [], []
        for t in (tags_map.get(params.appid, []) or [])[:8]:
            try:
                tid = int(t.get("tagid"))
            except (TypeError, ValueError):
                continue
            top_tag_ids.append(tid)
            if name_map.get(tid):
                top_tags.append(name_map[tid])

        personal = None
        if params.steamid:
            sid = await _resolve_steamid(params.steamid)
            taste = await _taste_profile(sid)
            taste_set = set(taste["tag_ids"])
            personal = {
                "already_owns": params.appid in taste["owned_ids"],
                "taste_match_tags": [name_map[t] for t in top_tag_ids
                                     if t in taste_set and name_map.get(t)],
                "your_top_tags": taste["tag_names"],
            }

        summary = {
            "appid": params.appid, "name": name, "is_free": is_free,
            "price": price.get("final_formatted") or ("Free" if is_free else None),
            "initial_price": price.get("initial_formatted") or None,
            "discount_pct": price.get("discount_percent", 0),
            "released": rel.get("date"), "coming_soon": rel.get("coming_soon", False),
            "genres": [g.get("description") for g in d.get("genres", [])],
            "metacritic": (d.get("metacritic") or {}).get("score"),
            "review_lifetime": {"desc": summ.get("review_score_desc"),
                                "positive_pct": l_pct,
                                "total": summ.get("total_reviews", 0)},
            "review_recent_30d": {"positive_pct": r_pct, "reviews_counted": r_n,
                                  "sampled": capped},
            "review_trend_pts": trend,
            "top_tags": top_tags,
            "personal": personal,
        }
        if params.response_format == ResponseFormat.JSON:
            return _dump(summary)

        price_str = summary["price"] or "Unknown"
        if summary["discount_pct"]:
            price_str = (f"{summary['price']} (was {summary['initial_price']}, "
                         f"-{summary['discount_pct']}%)")
        lines = [
            f"# Should I buy: {name} (appid {params.appid})",
            f"- **Price**: {price_str}"
            + (" — coming soon" if summary["coming_soon"] else ""),
            f"- **Released**: {summary['released'] or 'n/a'}  |  "
            f"**Genres**: {', '.join(g for g in summary['genres'] if g) or 'n/a'}",
        ]
        if summary["metacritic"]:
            lines.append(f"- **Metacritic**: {summary['metacritic']}")
        lt = summary["review_lifetime"]
        lines.append(
            f"- **Reviews (lifetime)**: {lt['desc'] or 'n/a'} — "
            f"{lt['positive_pct']}% of {lt['total']:,}"
        )
        rc = summary["review_recent_30d"]
        if rc["positive_pct"] is not None:
            tnote = f" ({'+' if (trend or 0) >= 0 else ''}{trend} pts vs lifetime)" \
                if trend is not None else ""
            samp = " [sampled]" if rc["sampled"] else ""
            lines.append(
                f"- **Reviews (last 30d)**: {rc['positive_pct']}% of "
                f"{rc['reviews_counted']}{samp}{tnote}"
            )
        if top_tags:
            lines.append(f"- **Tags**: {', '.join(top_tags)}")
        if personal:
            if personal["already_owns"]:
                lines.append("- ⚠️ **You already own this.**")
            if personal["taste_match_tags"]:
                lines.append(
                    f"- **Matches your taste**: shares "
                    f"{', '.join(personal['taste_match_tags'])} with your most-played"
                )
            elif personal["your_top_tags"]:
                lines.append(
                    f"- Your taste leans {', '.join(personal['your_top_tags'])} "
                    f"(little overlap here)"
                )
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


class RecommendInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    seed_appid: Optional[int] = Field(
        default=None, ge=1,
        description="Recommend games similar to THIS game (by community tags).",
    )
    steamid: Optional[str] = Field(
        default=None, max_length=200,
        description="Recommend from this user's taste (most-played + recent); also "
        "excludes games they already own. SteamID64, vanity, or profile URL.",
    )
    tags: list[str] = Field(
        default_factory=list, max_length=10,
        description="Explicit tag names to base recommendations on. Takes precedence "
        "over seed_appid/steamid tags if given.",
    )
    max_price: Optional[int] = Field(
        default=None, ge=0, le=1000,
        description="Optional max price (country's currency units).",
    )
    limit: int = Field(default=10, ge=1, le=30, description="Max recommendations (1-30).")
    country_code: str = Field(default="us", min_length=2, max_length=2)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


@mcp.tool(
    name="steam_recommend",
    annotations={
        "title": "Recommend Steam Games (with reasons)",
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def steam_recommend(params: RecommendInput) -> str:
    """Recommend games similar to a game you love, or to your taste — with reasons.

    Pick a basis: a seed_appid ("games like Hades"), a steamid (your most-played +
    recent taste), or explicit tags. Finds well-reviewed games that share those
    tags — excluding the seed game and (with steamid) games you already own — and
    explains WHY each matches (the shared tags). The store search needs no key;
    steamid personalization does.

    Args:
        params (RecommendInput): seed_appid, steamid, tags, max_price, limit, cc.

    Returns:
        str: Markdown or JSON — the basis plus ranked recommendations (appid, name,
        price, matching_tags), best tag-overlap first.
    """
    try:
        cc = params.country_code
        seed_ids: list[int] = []     # full tag set, for scoring overlap
        filter_ids: list[int] = []   # the AND filter for the store search
        basis = None
        exclude: set = set()
        owned_ids: set = set()

        if params.tags:
            seed_ids, _ = await _resolve_tag_ids(params.tags)
            filter_ids = seed_ids[:]
            basis = "tags: " + ", ".join(params.tags)
        if params.steamid:
            sid = await _resolve_steamid(params.steamid)
            taste = await _taste_profile(sid)
            owned_ids = {a for a in taste["owned_ids"] if a}
            if not seed_ids and taste["tag_ids"]:
                seed_ids = taste["tag_ids"]
                filter_ids = seed_ids[:3]
                basis = "your taste (" + ", ".join(taste["seed_games"][:3]) + ")"
        if not seed_ids and params.seed_appid:
            tmap = await _items_tags([params.seed_appid])
            for t in (tmap.get(params.seed_appid, []) or [])[:10]:
                try:
                    seed_ids.append(int(t.get("tagid")))
                except (TypeError, ValueError):
                    continue
            filter_ids = seed_ids[:3]
            info = await _app_price(params.seed_appid, cc)
            basis = "like " + (info.get("name") or f"app {params.seed_appid}")
            exclude.add(params.seed_appid)

        if not seed_ids:
            return ("Provide a basis: seed_appid (games like X), steamid (your "
                    "taste), or tags.")
        exclude |= owned_ids

        query = {
            "json": 1, "infinite": 1, "cc": cc, "l": "english", "category1": 998,
            "start": 0, "count": 100, "sort_by": "Reviews_DESC",
            "tags": ",".join(str(t) for t in (filter_ids or seed_ids)),
        }
        if params.max_price is not None:
            query["maxprice"] = str(params.max_price)
        cand, _ = await _discover_appids(query)
        cand = [a for a in cand if a not in exclude][:40]
        if not cand:
            return "No recommendations found — try fewer/different tags or a higher price."

        cand_tags = await _items_tags(cand)
        name_map = await _tag_name_map()
        seed_set = set(seed_ids)
        scored = []
        for a in cand:
            shared = []
            for t in cand_tags.get(a, []) or []:
                try:
                    tid = int(t.get("tagid"))
                except (TypeError, ValueError):
                    continue
                if tid in seed_set and name_map.get(tid):
                    shared.append(name_map[tid])
            scored.append((a, shared))
        scored.sort(key=lambda x: len(x[1]), reverse=True)  # stable: review rank on ties
        page = scored[: params.limit]
        infos = await _gather_limited([_app_price(a, cc) for a, _ in page])
        rows = []
        for (a, shared), info in zip(page, infos, strict=True):
            rows.append({
                "appid": a, "name": info.get("name") or f"app {a}",
                "price": info.get("price"), "on_sale": info.get("on_sale", False),
                "discount_pct": info.get("discount_pct", 0),
                "matching_tags": shared,
            })

        if params.response_format == ResponseFormat.JSON:
            return _dump({"basis": basis, "excluded_owned": len(owned_ids),
                          "count": len(rows), "recommendations": rows})

        owned_note = f", excluding {len(owned_ids)} you own" if owned_ids else ""
        lines = [f"# Recommendations — {basis}", f"{len(rows)} games{owned_note}:", ""]
        for r in rows:
            why = f" — matches: {', '.join(r['matching_tags'])}" if r["matching_tags"] else ""
            if r["on_sale"]:
                price = f" [{r['price']} -{r['discount_pct']}%]"
            elif r["price"]:
                price = f" [{r['price']}]"
            else:
                price = ""
            lines.append(f"- **{r['name']}** (appid {r['appid']}){price}{why}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


async def _owned_set(sid: str) -> Optional[set]:
    """Return a user's owned appids as a set, or None if their library is private."""
    d = await _steam_get(
        "IPlayerService/GetOwnedGames/v1/",
        {"steamid": sid, "include_appinfo": 0, "include_played_free_games": 1},
    )
    resp = d.get("response", {})
    if not resp:
        return None
    return {g.get("appid") for g in resp.get("games", []) if g.get("appid")}


async def _items_coop(appids: list[int]) -> dict:
    """Batched GetItems -> {appid: {"name": str, "coop": bool}} (no key).

    Co-op is read from `categories.supported_player_categoryids` against the known
    co-op category IDs. Chunked so request URLs stay reasonable.
    """
    if not appids:
        return {}

    async def _chunk(ids):
        body = {
            "ids": [{"appid": a} for a in ids],
            "context": {"language": "english", "country_code": "US", "steam_realm": 1},
            "data_request": {"include_basic_info": True, "include_categories": True},
        }
        data = await _steam_get(
            "IStoreBrowseService/GetItems/v1/",
            {"input_json": json.dumps(body, separators=(",", ":"))},
            with_key=False, cache_ttl=CACHE_TTL_TAGS,
        )
        out = {}
        for it in (data.get("response") or {}).get("store_items", []):
            cats = ((it.get("categories") or {}).get("supported_player_categoryids")) or []
            out[it.get("appid")] = {
                "name": it.get("name"),
                "coop": bool(set(cats) & COOP_CATEGORY_IDS),
            }
        return out

    chunks = [appids[i:i + 50] for i in range(0, len(appids), 50)]
    merged = {}
    for part in await _gather_limited([_chunk(c) for c in chunks]):
        merged.update(part)
    return merged


def _is_online(p: dict) -> bool:
    """True if a player summary indicates online or in-game (not Offline)."""
    return bool(p) and (p.get("personastate", 0) != 0 or bool(p.get("gameextrainfo")))


class PlanCoopNightInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    steamid: str = Field(
        ..., min_length=1, max_length=200,
        description="The host whose library to match against friends. SteamID64, "
        "vanity, or profile URL.",
    )
    friends: list[str] = Field(
        default_factory=list, max_length=50,
        description="Optional explicit group (SteamID64s / vanity names). If omitted, "
        "uses the host's friends (online ones by default).",
    )
    online_only: bool = Field(
        default=True,
        description="When the group is derived from the friend list, include only "
        "friends online right now. Ignored when 'friends' is given.",
    )
    max_friends: int = Field(
        default=20, ge=1, le=100,
        description="Max friends to check when deriving the group (bounds lookups).",
    )
    min_friends_owning: int = Field(
        default=1, ge=1, le=50,
        description="A game must be owned by the host AND at least this many group "
        "members to be suggested.",
    )
    limit: int = Field(default=20, ge=1, le=50, description="Max co-op games to list.")
    country_code: str = Field(default="us", min_length=2, max_length=2)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


@mcp.tool(
    name="steam_plan_coop_night",
    annotations={
        "title": "Plan a Steam Co-op Night",
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def steam_plan_coop_night(params: PlanCoopNightInput) -> str:
    """Find co-op games the host and their friends all own — for game night.

    Cross-references the host's library with friends' libraries, keeps games that
    support co-op, and ranks them by how many of the group own each. By default the
    group is the host's friends who are ONLINE right now (the "tonight" framing);
    pass an explicit `friends` list to plan with specific people, or
    online_only=false for everyone. Requires the host's friend list + Game Details
    Public, and each friend's Game Details Public (private ones are skipped and
    counted). Needs an API key.

    Args:
        params (PlanCoopNightInput): steamid (host), friends, online_only,
            max_friends, min_friends_owning, limit, country_code.

    Returns:
        str: Markdown or JSON. the group (+ who's online), how many libraries were
        checked, and co-op games ranked by how many of the group own each.
    """
    try:
        host = await _resolve_steamid(params.steamid)

        if params.friends:
            group, seen = [], set()
            for f in params.friends:
                try:
                    g = await _resolve_steamid(f)
                except Exception:  # noqa: BLE001
                    continue
                if g != host and g not in seen:
                    seen.add(g)
                    group.append(g)
            if not group:
                return "Couldn't resolve any of the given friends."
            summaries = await _summaries_for(group)
            derived = False
        else:
            fdata = await _steam_get(
                "ISteamUser/GetFriendList/v1/",
                {"steamid": host, "relationship": "friend"},
            )
            fids = [f["steamid"] for f in fdata.get("friendslist", {}).get("friends", [])]
            if not fids:
                return ("No friends returned — the host's friend list is likely "
                        "private (set Friends List to Public).")
            summaries = await _summaries_for(fids)
            group = ([g for g in fids if _is_online(summaries.get(g, {}))]
                     if params.online_only else fids)
            if params.online_only and not group:
                return ("None of the host's friends are online right now — try "
                        "online_only=false, or pass an explicit friends list.")
            group = group[: params.max_friends]
            derived = True

        host_owned = await _owned_set(host)
        if host_owned is None:
            return ("Can't plan — the host's Game Details are private (set them to "
                    "Public).")

        member_sets = await _gather_limited([_owned_set(g) for g in group])
        owners_by_app: dict = {}
        private = 0
        checked = []
        for g, s in zip(group, member_sets, strict=True):
            if s is None:
                private += 1
                continue
            checked.append(g)
            for a in (s & host_owned):
                owners_by_app.setdefault(a, []).append(g)

        candidates = [(a, owners) for a, owners in owners_by_app.items()
                      if len(owners) >= params.min_friends_owning]
        if not candidates:
            return ("No shared games among the host and the selected friends "
                    "(with public libraries). Try more friends or online_only=false.")
        candidates.sort(key=lambda x: len(x[1]), reverse=True)
        coop_info = await _items_coop([a for a, _ in candidates[:150]])

        rows = []
        for a, owners in candidates[:150]:
            ci = coop_info.get(a)
            if not ci or not ci.get("coop"):
                continue
            rows.append({
                "appid": a, "name": ci.get("name") or f"app {a}",
                "owner_count": len(owners),
                "owners": [summaries.get(o, {}).get("personaname", "Unknown")
                           for o in owners],
            })
            if len(rows) >= params.limit:
                break

        online_names = [summaries.get(g, {}).get("personaname", "Unknown")
                        for g in checked if _is_online(summaries.get(g, {}))]

        if params.response_format == ResponseFormat.JSON:
            return _dump({
                "host": host,
                "group_size": len(group),
                "checked": len(checked),
                "private_or_unknown": private,
                "online_now": online_names,
                "count": len(rows),
                "games": rows,
            })

        if derived:
            grp_desc = (f"your {len(group)} online friends" if params.online_only
                        else f"{len(group)} friends")
        else:
            grp_desc = ", ".join(summaries.get(g, {}).get("personaname", g)
                                 for g in checked) or "your group"
        lines = [
            f"# Co-op night for {host}",
            f"Group: {grp_desc}."
            + (f" Online now: {', '.join(online_names)}." if online_names else ""),
            f"Checked {len(checked)} libraries ({private} private/unknown).",
            "",
        ]
        if rows:
            lines.append("Co-op games you can play together (most-owned first):")
            for r in rows:
                shown = r["owners"][:5]
                more = f" +{len(r['owners']) - 5} more" if len(r["owners"]) > 5 else ""
                lines.append(
                    f"- **{r['name']}** (appid {r['appid']}) — you + "
                    f"{r['owner_count']} ({', '.join(shown)}{more})"
                )
        else:
            lines.append("No co-op games shared across the group "
                         "(everyone owns different things, or libraries are private).")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


def main() -> None:
    """Run the server over stdio (default MCP transport for local clients)."""
    mcp.run()


if __name__ == "__main__":
    main()
