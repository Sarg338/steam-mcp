"""Catalog data helpers (Deck compatibility, store search, co-op flags) for the Steam MCP server."""

from __future__ import annotations

import json
import re
from typing import Optional

from steam_mcp import transport
from steam_mcp.cache import CACHE_TTL_DECK, CACHE_TTL_DISCOVER, CACHE_TTL_TAGS
from steam_mcp.constants import (
    COOP_CATEGORY_IDS,
    DECK_CATEGORIES,
    DECK_COMPAT_URL,
    DECK_ITEM_STATUS,
)


async def _deck_compat(appid: int, language: str = "english") -> Optional[dict]:
    """Steam Deck compatibility report for an app (no key, cached 24h).

    Returns {category, label, items:[{status, text}], blog_url} or None if the app
    has no published rating. `resolved_category` 0/1/2/3 = Unknown/Unsupported/
    Playable/Verified; `resolved_items` carry a loc_token (no localized string, so
    we humanize the CamelCase) and a display_type glyph. Verified live 2026-06.
    """
    data = await transport._raw_get(
        DECK_COMPAT_URL, {"nAppID": appid, "l": language}, cache_ttl=CACHE_TTL_DECK
    )
    if not isinstance(data, dict) or not data.get("success"):
        return None
    res = data.get("results") or {}
    cat = res.get("resolved_category")
    if cat is None:
        return None
    items = []
    for it in res.get("resolved_items") or []:
        token = (it.get("loc_token") or "")[:200].replace(
            "#SteamDeckVerified_TestResult_", ""
        )
        text = re.sub(r"(?<!^)(?=[A-Z])", " ", token).strip()
        items.append({
            "status": DECK_ITEM_STATUS.get(it.get("display_type"), "•"),
            "text": text,
        })
    return {
        "category": cat,
        "label": DECK_CATEGORIES.get(cat, "Unknown"),
        "items": items,
        "blog_url": res.get("steam_deck_blog_url") or None,
    }


# Beta/playtest/demo/test clients show up in GetOwnedGames as ordinary "games"
# (often with real accrued playtime) but are frequently unlaunchable, so
# recommending them as "play next" is dead on arrival. We detect them by name
# (GetOwnedGames carries no type/metadata) — best-effort, tuned for precision so
# real games aren't hidden. Matching is CASE-INSENSITIVE (re.IGNORECASE), so
# all-caps names like "REMATCH BETA TEST" are caught.
#
# Standalone "beta" (anywhere in the name) + unambiguous multi-word markers. A
# bare "beta" is safe — a standalone "Beta" word in a retail title is vanishingly
# rare — and matching it anywhere (not just as a trailing qualifier) is what flags
# "REMATCH BETA TEST", "Game BETA Weekend", "Open Beta", etc. Do NOT add bare
# "test" or "alpha" here: they collide with real titles ("The Turing Test", "Test
# Drive", "Alpha Protocol"), so those only appear in multi-word phrases.
_TEMP_PHRASE_RE = re.compile(
    r"\b(?:beta|playtest|play test|public test|test server|test client|"
    r"test build|alpha test|alpha build|closed alpha|open alpha|staging branch|"
    r"dev build|developer build|press build|preview build|pts|ptr)\b",
    re.IGNORECASE,
)
# Risky single tokens that also occur in real titles ("Prototype", "Prototype 2",
# "Trials Rising") — only a signal when they TRAIL a real title word (e.g.
# "Knockout City Trial", "Spacebase DF-9 Prototype"), never as the whole or
# leading title. ("beta" is handled above as a standalone word, anywhere.)
_TEMP_SUFFIX_RE = re.compile(
    r"\w[\w'’.]*[\s_]*[-:–—]?\s*(?:demo|trial|prototype)\s*$",
    re.IGNORECASE,
)


def _is_temp_client(name: str) -> bool:
    """Heuristic: does this name look like a non-retail client (beta, playtest,
    demo, trial, test server, staging branch, prototype) rather than a shipped
    game? Name-based and best-effort, tuned to avoid hiding real games."""
    # Cap length: _TEMP_SUFFIX_RE is O(n^2) worst-case, and this runs on every
    # owned-game name. Real Steam app names are well under 200 chars.
    n = (name or "").strip()[:200]
    return bool(_TEMP_PHRASE_RE.search(n) or _TEMP_SUFFIX_RE.search(n))


SEARCH_URL = "https://store.steampowered.com/search/results/"


async def _discover_appids(query: dict) -> tuple[list[int], int]:
    """Run the storefront search; return (ranked_appids, total_count).

    The store search returns rendered HTML, so we pull the ranked app IDs from the
    stable `data-ds-appid` attribute on each result row. Guarded: an empty/garbled
    response simply yields no IDs.
    """
    data = await transport._raw_get(SEARCH_URL, query, cache_ttl=CACHE_TTL_DISCOVER)
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
        data = await transport._steam_get(
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
    for part in await transport._gather_limited([_chunk(c) for c in chunks]):
        merged.update(part)
    return merged
