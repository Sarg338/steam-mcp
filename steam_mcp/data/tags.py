"""Community-tag data helpers (tag dictionary, per-app tags, taste profile) for the Steam MCP server."""

from __future__ import annotations

import asyncio
import json

from steam_mcp import transport
from steam_mcp.cache import CACHE_TTL_TAGMAP, CACHE_TTL_TAGS
from steam_mcp.data import catalog


async def _tag_name_map() -> dict:
    """Map Steam community tagid -> display name (cached; static-ish, no key).

    GetItems returns only tagids + weights; this storefront dictionary supplies the
    human names (e.g. 29482 -> 'Souls-like').
    """
    data = await transport._raw_get(
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
    data = await transport._steam_get(
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
        transport._steam_get(
            "IPlayerService/GetOwnedGames/v1/",
            {"steamid": sid, "include_appinfo": 1, "include_played_free_games": 1},
        ),
        transport._steam_get("IPlayerService/GetRecentlyPlayedGames/v1/", {"steamid": sid}),
    )
    games = owned_d.get("response", {}).get("games", []) or []
    owned_ids = {g.get("appid") for g in games}
    name_by_id = {g.get("appid"): g.get("name") for g in games}
    # Don't let beta/playtest/demo/test clients seed taste — a 165h playtest would
    # otherwise dominate the tag profile (same _is_temp_client filter the library
    # analysis uses). owned_ids stays full, since it's only used to exclude games
    # the user already owns from recommendations.
    by_play = sorted(
        (g for g in games
         if g.get("playtime_forever", 0) > 0
         and not catalog._is_temp_client(g.get("name", ""))),
        key=lambda g: g.get("playtime_forever", 0), reverse=True,
    )
    recent = [
        g for g in (recent_d.get("response", {}).get("games", []) or [])
        if not catalog._is_temp_client(g.get("name", ""))
    ]
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
