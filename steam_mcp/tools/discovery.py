"""Discovery tool: filtered store search with optional personalization."""

from __future__ import annotations

import time
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from steam_mcp import errors, identity, render
from steam_mcp.app import mcp
from steam_mcp.data import catalog, pricing, tags
from steam_mcp.render import ResponseFormat

# --- Discovery: filtered search + optional personalization ------------------

# Friendly sort name -> Steam search sort_by value ("" = let Steam default).
_SORT_MAP = {
    "reviews": "Reviews_DESC",
    "release": "Released_DESC",
    "price_asc": "Price_ASC",
    "price_desc": "Price_DESC",
    "relevance": "",
}


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
    released_within_days: Optional[int] = Field(
        default=None, ge=1, le=3650,
        description="Only include games released in the last N days (forces "
        "newest-first). Use for 'what came out recently'. Omit for any release date.",
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
    """Discover games by criteria — tags, max price, on-sale, platform — optionally personalized to a user's taste; for filter-based search, not "games like X" (use steam_recommend for that).

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
        tag_ids, missing = await tags._resolve_tag_ids(params.tags)

        owned_ids: set = set()
        taste_tags: list[str] = []
        seed_games: list[str] = []
        if params.steamid:
            sid = await identity._resolve_steamid(params.steamid)
            taste = await tags._taste_profile(sid)
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
        if params.released_within_days:
            sort_by = "Released_DESC"  # a release window is inherently newest-first
        if sort_by:
            query["sort_by"] = sort_by

        appids, total = await catalog._discover_appids(query)
        appids = [a for a in appids if a not in owned_ids]
        page = appids[: params.limit]
        pm = await pricing._app_prices(page, cc) if page else {}
        infos = [pm.get(a, {}) for a in page]
        cutoff = (time.time() - params.released_within_days * 86400
                  if params.released_within_days else None)
        rows = []
        for a, info in zip(page, infos, strict=True):
            if cutoff is not None:
                rts = info.get("release_ts")
                if not rts or rts < cutoff:
                    continue  # released before the window, or release date unknown
            rows.append({
                "appid": a,
                "name": info.get("name") or f"app {a}",
                "price": info.get("price"),
                "discount_pct": info.get("discount_pct", 0),
                "on_sale": info.get("on_sale", False),
            })

        excluded = len(owned_ids) if (params.steamid and params.exclude_owned) else 0
        if params.response_format == ResponseFormat.JSON:
            return render._dump({
                "filters": {
                    "term": params.term,
                    "tags": params.tags,
                    "resolved_tag_ids": tag_ids,
                    "unresolved_tags": missing,
                    "max_price": params.max_price,
                    "on_sale": params.on_sale,
                    "platform": params.platform,
                    "sort": params.sort,
                    "released_within_days": params.released_within_days,
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
            f"Matched {total:,} games; showing {len(rows)}"
            + (f" released in the last {params.released_within_days} days "
               "(newest first)." if params.released_within_days
               else f" (sorted by {params.sort})."),
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
        return errors._handle_error(e)
