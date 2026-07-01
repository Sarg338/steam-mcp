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

# asyncio stays importable as an attribute of this module: tests patch
# `S.asyncio.sleep`.
import asyncio  # noqa: F401

# httpx stays importable as an attribute of this module: the HTTP layer moved to
# steam_mcp.transport, but tests still reach httpx via `S.httpx`.
import httpx  # noqa: F401

# ---------------------------------------------------------------------------
# TEMPORARY SCAFFOLDING (package split, Phase 1). Every name that moved out of
# this module is re-imported here so that (a) the remaining tool code below
# resolves it unchanged and (b) `import steam_mcp.server as S` still exposes it
# for the tests. Delete this block in Phase 3/4 once callers and tests import
# the new modules directly.
# ---------------------------------------------------------------------------
from steam_mcp.app import mcp  # noqa: F401
from steam_mcp.cache import (  # noqa: F401
    CACHE_TTL_APPDETAILS,
    CACHE_TTL_DECK,
    CACHE_TTL_DISCOVER,
    CACHE_TTL_FEATURED,
    CACHE_TTL_GLOBAL_ACH,
    CACHE_TTL_GROUP,
    CACHE_TTL_MARKET,
    CACHE_TTL_NEWS,
    CACHE_TTL_PACKAGE,
    CACHE_TTL_REVIEWS,
    CACHE_TTL_SCHEMA,
    CACHE_TTL_TAGMAP,
    CACHE_TTL_TAGS,
    CACHE_TTL_WORKSHOP,
    _CACHE,
    _cache_key,
    _TTLCache,
)
from steam_mcp.config import (  # noqa: F401
    ENV_KEY,
    ENV_USER,
    _dotenv_value,
    _get_api_key,
    _get_default_user,
    _load_key_from_dotenv,
)
from steam_mcp.constants import (  # noqa: F401
    COOP_CATEGORY_IDS,
    CS_EXTERIORS,
    CURRENCY_SYMBOLS,
    DECK_CATEGORIES,
    DECK_COMPAT_URL,
    DECK_ITEM_STATUS,
    MAX_RECENT_PAGES,
    PERSONA_STATES,
    PROFILE_URL_RE,
    RECENT_PAGE_SIZE,
    STEAMID64_RE,
    VISIBILITY_STATES,
)
from steam_mcp.data.catalog import (  # noqa: F401
    SEARCH_URL,
    _deck_compat,
    _discover_appids,
    _is_temp_client,
    _items_coop,
    _TEMP_PHRASE_RE,
    _TEMP_SUFFIX_RE,
)
from steam_mcp.data.players import (  # noqa: F401
    _is_online,
    _owned_set,
    _summaries_for,
)
from steam_mcp.data.pricing import (  # noqa: F401
    _app_price,
    _app_prices,
    _fetch_featured,
)
from steam_mcp.data.tags import (  # noqa: F401
    _items_tags,
    _resolve_tag_ids,
    _tag_name_map,
    _taste_profile,
)
from steam_mcp.errors import (  # noqa: F401
    PRIVACY_SETTINGS_URL,
    SteamApiError,
    _handle_error,
    _privacy_hint,
    _scrub,
)
from steam_mcp.identity import _resolve_steamid  # noqa: F401
from steam_mcp.render import (  # noqa: F401
    _STRIP_HTML_MAX,
    _dump,
    _fmt_amount,
    _hours_str,
    _minutes_to_hours,
    _parse_languages,
    _persona_label,
    _strip_html,
    _ts_to_date,
    ResponseFormat,
)
from steam_mcp.transport import (  # noqa: F401
    ALLOWED_HOSTS,
    API_BASE,
    FANOUT_LIMIT,
    HTTP_TIMEOUT,
    MAX_RETRIES,
    RATE_LIMITS,
    RETRY_BASE_DELAY,
    RETRY_MAX_DELAY,
    RETRYABLE_STATUS,
    STORE_BASE,
    _Bucket,
    _BUCKETS,
    _check_host,
    # _CLIENT / _CLIENT_LOOP deliberately NOT re-imported: they are rebindable
    # globals, so a from-import would be a stale snapshot — read them via
    # steam_mcp.transport instead.
    _enforce_host,
    _gather_limited,
    _get_with_retry,
    _http_client,
    _rate_limit,
    _raw_get,
    _raw_get_text,
    _retry_delay,
    _steam_get,
    _steam_post,
    _store_get,
)

from steam_mcp.models import (  # noqa: F401
    AppOnlyInput,
    FriendListInput,
    PlayerGameInput,
    PlayerInput,
    PlayersInput,
)

# ---------------------------------------------------------------------------
# Tool registration + re-exports (also TEMPORARY SCAFFOLDING, deleted in
# Phase 5). Importing steam_mcp.tools registers every @mcp.tool on the shared
# FastMCP app; the from-imports below keep `S.steam_x`, `S.<XInput>`, and the
# co-located helpers reachable for the tests, prompts, resources, and scripts.
# ---------------------------------------------------------------------------
import steam_mcp.tools  # noqa: F401  (registration side effects)

from steam_mcp.tools.achievements import (  # noqa: F401
    RarestUnlocksInput,
    steam_get_game_schema,
    steam_get_global_achievement_percentages,
    steam_get_player_achievements,
    steam_get_rarest_unlocks,
    steam_get_user_game_stats,
)
from steam_mcp.tools.deals import (  # noqa: F401
    FeaturedInput,
    StoreHighlightsInput,
    WishlistInput,
    _featured_rows,
    steam_get_featured_specials,
    steam_get_store_highlights,
    steam_get_wishlist,
)
from steam_mcp.tools.discovery import (  # noqa: F401
    DiscoverInput,
    _SORT_MAP,
    steam_discover,
)
from steam_mcp.tools.friends import (  # noqa: F401
    ComparePlayersInput,
    FriendsWhoOwnInput,
    _friend_owns_app,
    steam_compare_players,
    steam_find_friends_who_own,
    steam_get_friend_list,
)
from steam_mcp.tools.intelligence import (  # noqa: F401
    PlanCoopNightInput,
    RecommendInput,
    ShouldIBuyInput,
    steam_plan_coop_night,
    steam_recommend,
    steam_should_i_buy,
)
from steam_mcp.tools.library import (  # noqa: F401
    LibraryAnalysisInput,
    OwnedGamesInput,
    steam_analyze_library,
    steam_get_owned_games,
    steam_get_recently_played_games,
)
from steam_mcp.tools.market import (  # noqa: F401
    InventoryInput,
    MarketPriceInput,
    WorkshopItemInput,
    _parse_cs_attributes,
    steam_get_inventory,
    steam_get_market_price,
    steam_get_workshop_item,
)
from steam_mcp.tools.player import (  # noqa: F401
    UserGroupsInput,
    _group_details,
    steam_get_player_badges,
    steam_get_player_bans,
    steam_get_player_summary,
    steam_get_steam_level,
    steam_get_user_groups,
    steam_resolve_vanity_url,
)
from steam_mcp.tools.reviews import (  # noqa: F401
    AppNewsInput,
    AppReviewsInput,
    _collect_recent_reviews,
    _fmt_review,
    steam_get_app_news,
    steam_get_app_reviews,
    steam_get_current_players,
)
from steam_mcp.tools.store import (  # noqa: F401
    AppDetailsInput,
    AppSearchInput,
    AppTagsInput,
    DeckCompatInput,
    DlcInput,
    PackageDetailsInput,
    RegionalPricingInput,
    steam_get_app_details,
    steam_get_app_regional_pricing,
    steam_get_app_tags,
    steam_get_deck_compatibility,
    steam_get_dlc,
    steam_get_package_details,
    steam_search_apps,
)


# ---------------------------------------------------------------------------
# Prompts — guided, one-shot flows that orchestrate the tools
# ---------------------------------------------------------------------------

@mcp.prompt(
    name="what_should_i_play",
    description="Recommend what to play next from a user's library and taste.",
)
def prompt_what_should_i_play(steamid: str) -> str:
    return (
        f"Recommend what the Steam user '{steamid}' should play next. "
        f"(1) Call steam_analyze_library(steamid='{steamid}') to surface their "
        f"backlog and abandoned games they already own. (2) Call "
        f"steam_recommend(steamid='{steamid}') for NEW games matching their taste. "
        f"Then give a short, friendly shortlist: a couple of owned-but-unplayed "
        f"games worth finishing AND a couple of new games to consider — one line on "
        f"why each fits their taste."
    )


@mcp.prompt(
    name="is_it_worth_buying",
    description="Decide whether a game is worth buying right now.",
)
def prompt_is_it_worth_buying(game: str, steamid: str = "") -> str:
    base = (
        f"Help decide whether to buy '{game}' on Steam right now. If '{game}' is a "
        f"title rather than an appid, resolve it with steam_search_apps first, then "
        f"call steam_should_i_buy with that appid. Weigh the price/discount, the "
        f"LIFETIME vs RECENT review trend, the tags, and Metacritic, then give a "
        f"clear recommendation with the reasoning."
    )
    if steamid:
        base += (
            f" Personalize it: pass steamid='{steamid}' to steam_should_i_buy to "
            f"check whether they already own it and how its tags match their "
            f"most-played games."
        )
    return base


@mcp.prompt(
    name="plan_game_night",
    description="Plan a co-op game night with a user's online friends.",
)
def prompt_plan_game_night(steamid: str) -> str:
    return (
        f"Plan a co-op game night for Steam user '{steamid}'. Call "
        f"steam_plan_coop_night(steamid='{steamid}') to find co-op games the user "
        f"and their online friends all own. Present the top options — noting who's "
        f"online now and how many of the group own each — and suggest one to start."
    )


@mcp.prompt(
    name="steam_deals",
    description="Find Steam deals worth buying right now.",
)
def prompt_steam_deals(max_price: str = "") -> str:
    extra = f" Focus on games at or under {max_price} (pass max_price)." if max_price else ""
    return (
        "Find good Steam deals right now. Use steam_get_featured_specials and/or "
        "steam_discover(on_sale=true, sort='reviews') to get discounted games, prefer "
        "well-reviewed ones (check steam_get_app_reviews for anything promising), and "
        f"summarize the best 5-10 with price, discount, and review score.{extra}"
    )


@mcp.prompt(
    name="game_overview",
    description="Give a comprehensive overview of a game.",
)
def prompt_game_overview(game: str) -> str:
    return (
        f"Give a comprehensive overview of '{game}' on Steam. Resolve the appid with "
        f"steam_search_apps if needed, then combine steam_get_app_details, "
        f"steam_get_app_tags, steam_get_app_reviews (lifetime + recent), and "
        f"steam_get_current_players into a tight summary: what it is, price, how it "
        f"reviews, its vibe (tags), and how alive it is right now."
    )


# ---------------------------------------------------------------------------
# Resources — reference Steam entities by URI (steam://app/{id}, steam://user/{id})
# ---------------------------------------------------------------------------

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
