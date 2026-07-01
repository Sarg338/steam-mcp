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

# Release-window search: Steam has no server-side "released after" filter, so the
# window is enumerated newest-first (Released_DESC) page by page and then re-ranked
# client-side by the requested sort. These bound that enumeration so a broad
# window over a popular tag pool can't trigger unbounded requests.
_WINDOW_PAGE_SIZE = 100
_WINDOW_MAX_PAGES = 3          # up to 300 newest matches considered
# Review ranking guard: a fresh release with a handful of glowing reviews should
# not outrank an established 90%-positive game, and unreviewed games rank last.
_MIN_RANKED_REVIEWS = 10


def _rank_window(rows: list[dict], sort: str) -> list[dict]:
    """Order window candidates by the requested sort, client-side.

    'reviews': games with >= _MIN_RANKED_REVIEWS reviews first (by percent, then
    volume), then thinly-reviewed ones, then unreviewed. 'release' keeps
    newest-first. Price sorts use the numeric price when known (unknown last);
    'relevance' keeps Steam's newest-first enumeration order.
    """
    if sort == "reviews":
        def key(r):
            n = r.get("review_count") or 0
            tier = 0 if n >= _MIN_RANKED_REVIEWS else (1 if n else 2)
            return (tier, -(r.get("review_pct") or 0), -n)
        return sorted(rows, key=key)
    if sort == "release":
        return sorted(rows, key=lambda r: -(r.get("release_ts") or 0))
    if sort in ("price_asc", "price_desc"):
        sign = 1 if sort == "price_asc" else -1
        return sorted(
            rows,
            key=lambda r: (r.get("price_cents") is None,
                           sign * (r.get("price_cents") or 0)),
        )
    return rows  # relevance: keep enumeration order


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
        description="Only include games released in the last N days, ranked by "
        "your chosen `sort` (default: best-reviewed). Use for 'what came out "
        "recently'. Omit for any release date.",
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
        def _row(meta: dict, info: dict) -> dict:
            a = meta["appid"]
            return {
                "appid": a,
                "name": info.get("name") or f"app {a}",
                "price": info.get("price"),
                "discount_pct": info.get("discount_pct", 0),
                "on_sale": info.get("on_sale", False),
                "review_pct": meta.get("review_pct"),
                "review_count": meta.get("review_count") or 0,
            }

        window = params.released_within_days
        excluded = 0
        coverage = None
        if not window:
            # No release window: Steam's own server-side ordering is authoritative.
            sort_by = _SORT_MAP.get(params.sort, "Reviews_DESC")
            if sort_by:
                query["sort_by"] = sort_by
            found, total = await catalog._discover_search(query)
            excluded = sum(1 for r in found if r["appid"] in owned_ids)
            kept = [r for r in found if r["appid"] not in owned_ids]
            page = kept[: params.limit]
            pm = await pricing._app_prices([r["appid"] for r in page], cc) if page else {}
            rows = [_row(r, pm.get(r["appid"], {})) for r in page]
        else:
            # Release window: Steam can't filter by date server-side, so enumerate
            # the window newest-first (Released_DESC guarantees everything inside
            # the window precedes everything outside it), then re-rank client-side
            # by the requested sort — "well-reviewed AND recent" must not collapse
            # into "newest".
            query["sort_by"] = "Released_DESC"
            cutoff = time.time() - window * 86400
            candidates: list[dict] = []
            seen: set[int] = set()   # pages can drift/overlap while Steam re-ranks
            total = 0
            coverage = "full"
            for page_no in range(_WINDOW_MAX_PAGES):
                q = dict(query, start=page_no * _WINDOW_PAGE_SIZE,
                         count=_WINDOW_PAGE_SIZE)
                found, page_total = await catalog._discover_search(q)
                if page_total:
                    total = page_total
                if not found:
                    break
                pm = await pricing._app_prices([r["appid"] for r in found], cc)
                past_window = False
                for r in found:
                    if r["appid"] in seen:
                        continue
                    seen.add(r["appid"])
                    info = pm.get(r["appid"], {})
                    rts = info.get("release_ts")
                    if not rts:
                        continue  # release date unknown: can't confirm the window
                    if rts < cutoff:
                        past_window = True  # newest-first => the rest are older
                        continue
                    if r["appid"] in owned_ids:
                        excluded += 1
                        continue
                    candidates.append({**_row(r, info),
                                       "price_cents": info.get("price_cents"),
                                       "release_ts": rts})
                if past_window or len(found) < _WINDOW_PAGE_SIZE:
                    break
            else:
                coverage = "partial"  # page cap hit while still inside the window
            ranked = _rank_window(candidates, params.sort)
            rows = [{k: v for k, v in r.items()
                     if k not in ("price_cents", "release_ts")}
                    for r in ranked[: params.limit]]
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
                **({"window_coverage": coverage} if window else {}),
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
            + (f" released in the last {window} days" if window else "")
            + f" (sorted by {params.sort}).",
        ]
        if coverage == "partial":
            lines.append(
                f"(ranked the {_WINDOW_MAX_PAGES * _WINDOW_PAGE_SIZE} newest "
                "matches — the window may contain more; narrow the filters or "
                "the window for full coverage)"
            )
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
            if r["review_count"]:
                tail += f" - {r['review_pct']}% positive ({r['review_count']:,} reviews)"
            lines.append(f"- **{r['name']}** (appid {r['appid']}){tail}")
        if not rows:
            lines.append("(no matches — try loosening the filters)")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)
