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
# TEMPORARY SCAFFOLDING (package split, Phases 1-4). Every name that moved out
# of this module is re-imported here so `import steam_mcp.server as S` still
# exposes it for the tests. Delete this block in Phase 5 once the tests import
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

# Registering the prompts and resources AFTER the tools keeps the wire surface
# ordered tools -> prompts -> resources.
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
