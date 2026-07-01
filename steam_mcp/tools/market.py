"""Market tools: inventories, Community Market prices, and Workshop items."""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from steam_mcp import errors, identity, render, transport
from steam_mcp.app import mcp
from steam_mcp.cache import CACHE_TTL_MARKET, CACHE_TTL_WORKSHOP
from steam_mcp.constants import CS_EXTERIORS
from steam_mcp.render import ResponseFormat


class WorkshopItemInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    published_file_id: int = Field(
        ..., ge=1,
        description="Steam Workshop published file ID (the ?id= number in the "
        "item's community URL).",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


@mcp.tool(
    name="steam_get_workshop_item",
    annotations={
        "title": "Get Steam Workshop Item",
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def steam_get_workshop_item(params: WorkshopItemInput) -> str:
    """Get metadata for a Steam Workshop item (mod, map, guide, collection, …).

    Answers "what is this workshop item" and "how popular is it". Returns the title,
    which game it's for, description, tags, and engagement (subscribers, favorites,
    views), plus created/updated dates. No API key required.

    Args:
        params (WorkshopItemInput): published_file_id.

    Returns:
        str: Markdown or JSON. title, app_id, creator, description, tags,
        subscriptions/favorited/views, created/updated, and the community link.
    """
    try:
        data = await transport._steam_post(
            "ISteamRemoteStorage/GetPublishedFileDetails/v1/",
            {"itemcount": 1, "publishedfileids[0]": params.published_file_id},
            cache_ttl=CACHE_TTL_WORKSHOP,
        )
        items = data.get("response", {}).get("publishedfiledetails", [])
        if not items or items[0].get("result") != 1:
            return f"No Workshop item found for id {params.published_file_id}."
        d = items[0]
        summary = {
            "published_file_id": params.published_file_id,
            "title": d.get("title"),
            "app_id": d.get("consumer_app_id"),
            "creator_steamid": d.get("creator"),
            "description": render._strip_html(d.get("description"), 600),
            "tags": [t.get("tag") for t in d.get("tags", []) if t.get("tag")],
            "subscriptions": int(d.get("subscriptions") or 0),
            "lifetime_subscriptions": int(d.get("lifetime_subscriptions") or 0),
            "favorited": int(d.get("favorited") or 0),
            "views": int(d.get("views") or 0),
            "file_size": int(d.get("file_size") or 0),
            "created": render._ts_to_date(d.get("time_created")),
            "updated": render._ts_to_date(d.get("time_updated")),
            "banned": bool(d.get("banned")),
            "preview_url": d.get("preview_url"),
            "url": "https://steamcommunity.com/sharedfiles/filedetails/?id="
                   f"{params.published_file_id}",
        }
        if params.response_format == ResponseFormat.JSON:
            return render._dump(summary)

        lines = [
            f"# Workshop: {summary['title'] or params.published_file_id} "
            f"(id {params.published_file_id})",
            f"- **For app**: {summary['app_id']}",
            f"- **Subscribers**: {summary['subscriptions']:,}"
            + (f" (lifetime {summary['lifetime_subscriptions']:,})"
               if summary['lifetime_subscriptions'] else ""),
            f"- **Favorited**: {summary['favorited']:,}  |  "
            f"**Views**: {summary['views']:,}",
        ]
        if summary["tags"]:
            lines.append(f"- **Tags**: {', '.join(summary['tags'])}")
        if summary["created"]:
            upd = f", updated {summary['updated']}" if summary["updated"] else ""
            lines.append(f"- **Created**: {summary['created']}{upd}")
        if summary["banned"]:
            lines.append("- ⚠️ This item is banned.")
        lines.append(f"- **Link**: {summary['url']}")
        if summary["description"]:
            lines += ["", summary["description"]]
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)


class InventoryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    steamid: Optional[str] = Field(
        default=None, max_length=200,
        description="SteamID64, vanity name, or profile URL of the inventory owner. "
        "Omit to use the configured STEAM_USER, if set.",
    )
    appid: int = Field(
        default=753, ge=1,
        description="App whose inventory to read. 753 = Steam Community items "
        "(trading cards, emoticons, backgrounds, gems); 730 = CS2; 440 = TF2; "
        "570 = Dota 2; etc.",
    )
    context_id: Optional[int] = Field(
        default=None, ge=1,
        description="Inventory context within the app. Leave unset to auto-pick "
        "(6 for app 753 / Community items, 2 for games).",
    )
    count: int = Field(
        default=100, ge=1, le=2000,
        description="Max item instances to fetch (a sample for very large "
        "inventories).",
    )
    language: str = Field(
        default="english", min_length=2, max_length=32,
        description="Steam language name for localized item names.",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


@mcp.tool(
    name="steam_get_inventory",
    annotations={
        "title": "Get Steam Inventory",
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def steam_get_inventory(params: InventoryInput) -> str:
    """List a user's Steam inventory — game items or generic Community items.

    Works for any app's inventory: a game (CS2 730, TF2 440, Dota 2 570 — items,
    skins, cosmetics) or the **Steam Community** inventory (app 753 — trading cards,
    emoticons, profile backgrounds, gems). Aggregates duplicate items by quantity
    and flags whether each is tradable/marketable. The context is auto-picked from
    the app unless you set context_id. Requires the target's **inventory privacy to
    be Public**; no API key required (use a SteamID64 or profile URL to skip vanity
    resolution, which does need a key).

    Args:
        params (InventoryInput): steamid, appid, context_id, count, language.

    Returns:
        str: Markdown or JSON. total_inventory_count plus items (name, type, count,
        tradable, marketable), most-numerous first.
    """
    try:
        sid = await identity._resolve_steamid(params.steamid)
        ctx = (params.context_id if params.context_id is not None
               else (6 if params.appid == 753 else 2))
        data = await transport._raw_get(
            f"https://steamcommunity.com/inventory/{sid}/{params.appid}/{ctx}",
            {"l": params.language, "count": params.count},
        )
        if not data or data.get("success") != 1:
            return (f"No inventory returned for app {params.appid} (context {ctx}). "
                    f"It's empty, the app/context is wrong, or the inventory isn't "
                    f"public. " + errors._privacy_hint("Inventory"))

        descs = {}
        for d in data.get("descriptions", []) or []:
            descs[(str(d.get("classid")), str(d.get("instanceid")))] = d
        counts: dict = {}
        for a in data.get("assets", []) or []:
            key = (str(a.get("classid")), str(a.get("instanceid")))
            counts[key] = counts.get(key, 0) + int(a.get("amount") or 1)

        rows = []
        for key, n in counts.items():
            d = descs.get(key) or descs.get((key[0], "0"))
            rows.append({
                "name": (d.get("market_name") or d.get("name")) if d else None,
                "type": d.get("type") if d else None,
                "count": n,
                "tradable": bool(d.get("tradable")) if d else None,
                "marketable": bool(d.get("marketable")) if d else None,
            })
        rows.sort(key=lambda r: r["count"], reverse=True)
        total = data.get("total_inventory_count", len(rows))
        fetched = len(data.get("assets", []) or [])

        if params.response_format == ResponseFormat.JSON:
            return render._dump({
                "steamid": sid, "appid": params.appid, "context_id": ctx,
                "total_inventory_count": total, "fetched": fetched,
                "distinct_items": len(rows), "items": rows,
            })

        partial = (f" (sampled {fetched} of {total:,})" if total and fetched < total
                   else "")
        lines = [
            f"# Inventory: {sid} — app {params.appid} (context {ctx})",
            f"{total:,} items total{partial}; {len(rows)} distinct shown.",
            "",
        ]
        for r in rows[:50]:
            flags = []
            if r["tradable"]:
                flags.append("tradable")
            if r["marketable"]:
                flags.append("marketable")
            flagstr = f" [{', '.join(flags)}]" if flags else ""
            qty = f" ×{r['count']}" if r["count"] > 1 else ""
            typ = f" — {r['type']}" if r["type"] else ""
            lines.append(f"- **{r['name'] or 'Unknown item'}**{qty}{typ}{flagstr}")
        if len(rows) > 50:
            lines.append(f"- …and {len(rows) - 50} more distinct items")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)


def _parse_cs_attributes(hash_name: str) -> dict:
    """Parse CS2/CSGO attributes encoded in a market_hash_name (no request).

    e.g. 'StatTrak™ AK-47 | Redline (Field-Tested)' or '★ Karambit | Doppler
    (Factory New)'. Rarity/type are NOT in the hash name — those come from the
    item's `type` (e.g. 'Classified Rifle') via the market lookup.
    """
    # Cap length: `\(([^)]+)\)\s*$` is O(n^2) on a flood of '(' (ReDoS guard). The
    # markers we read are all at the start/end of a normal-length hash name.
    hash_name = (hash_name or "")[:300]
    attrs = {"exterior": None, "stattrak": False, "souvenir": False, "star": False}
    m = re.search(r"\(([^)]+)\)\s*$", hash_name)
    if m and m.group(1) in CS_EXTERIORS:
        attrs["exterior"] = m.group(1)
    attrs["stattrak"] = "StatTrak" in hash_name      # StatTrak™
    attrs["souvenir"] = hash_name.startswith("Souvenir ")
    attrs["star"] = hash_name.startswith("★")        # knives / gloves
    return attrs


class MarketPriceInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    appid: int = Field(
        ..., ge=1,
        description="App the item belongs to: 730 = CS2, 440 = TF2, 570 = Dota 2, "
        "753 = Steam Community items.",
    )
    market_hash_name: str = Field(
        ..., min_length=1, max_length=300,
        description="The item's exact Market Hash Name — it encodes the variant, so "
        "include condition/quality prefixes, e.g. 'AK-47 | Redline (Field-Tested)', "
        "'StatTrak™ AWP | Asiimov (Field-Tested)', 'Souvenir ...'. Copy it from "
        "the item's Community Market page.",
    )
    currency: int = Field(
        default=1, ge=1, le=41,
        description="Steam currency code: 1=USD, 2=GBP, 3=EUR, 5=RUB, 9=JPY, "
        "20=BRL, 23=CNY, etc.",
    )
    include_item_details: bool = Field(
        default=True,
        description="Also look up the item's type/rarity and listing count (one "
        "extra request). Set false for price only.",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


@mcp.tool(
    name="steam_get_market_price",
    annotations={
        "title": "Get Steam Community Market Price",
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def steam_get_market_price(params: MarketPriceInput) -> str:
    """Get the Community Market price for a single item, with rarity and condition.

    Returns the current lowest and median sale price plus 24-hour volume, and (by
    default) the item's type/rarity (e.g. "Classified Rifle", "Mythical Bow") and
    listing count. For CS2 it also surfaces the wear/exterior, StatTrak™, Souvenir,
    and ★ flags parsed from the name. The item is identified by its exact Market
    Hash Name — which already encodes the variant (condition, StatTrak, etc.).

    No API key required. Uses Steam's Community Market endpoints, which are
    undocumented and tightly rate-limited; results are cached briefly, and an item
    with no current listings reports the price as unavailable.

    Args:
        params (MarketPriceInput): appid, market_hash_name, currency,
            include_item_details.

    Returns:
        str: Markdown or JSON. lowest_price, median_price, volume_24h, listings,
        type (rarity + category), CS2 attributes, and the market URL.
    """
    try:
        po = await transport._raw_get(
            "https://steamcommunity.com/market/priceoverview/",
            {"appid": params.appid, "currency": params.currency,
             "market_hash_name": params.market_hash_name},
            cache_ttl=CACHE_TTL_MARKET,
        )
        priced = bool(po) and po.get("success") and (
            po.get("lowest_price") or po.get("median_price"))

        item_type = None
        listings = None
        if params.include_item_details:
            try:
                sr = await transport._raw_get(
                    "https://steamcommunity.com/market/search/render/",
                    {"appid": params.appid, "norender": 1, "count": 10,
                     "currency": params.currency, "query": params.market_hash_name},
                    cache_ttl=CACHE_TTL_MARKET,
                )
                hit = next(
                    (r for r in (sr.get("results") or [])
                     if r.get("hash_name") == params.market_hash_name), None)
                if hit:
                    item_type = (hit.get("asset_description") or {}).get("type")
                    listings = hit.get("sell_listings")
            except Exception:  # noqa: BLE001
                pass  # details are best-effort; price still returned

        cs = _parse_cs_attributes(params.market_hash_name) if params.appid == 730 else {}
        url = ("https://steamcommunity.com/market/listings/"
               f"{params.appid}/{quote(params.market_hash_name)}")

        if not priced:
            base = (f"No current Community Market listings for '{params.market_hash_name}' "
                    f"(app {params.appid}). Check the exact Market Hash Name and appid"
                    + (f"; it's a {item_type}." if item_type else "."))
            if params.response_format == ResponseFormat.JSON:
                return render._dump({"appid": params.appid,
                                     "market_hash_name": params.market_hash_name,
                                     "available": False, "type": item_type,
                                     "attributes": cs, "market_url": url})
            return base

        summary = {
            "appid": params.appid,
            "market_hash_name": params.market_hash_name,
            "currency": params.currency,
            "available": True,
            "lowest_price": po.get("lowest_price"),
            "median_price": po.get("median_price"),
            "volume_24h": po.get("volume"),
            "listings": listings,
            "type": item_type,
            "attributes": cs,
            "market_url": url,
        }
        if params.response_format == ResponseFormat.JSON:
            return render._dump(summary)

        lines = [f"# Market: {params.market_hash_name} (app {params.appid})"]
        price_bits = [f"**Lowest** {po.get('lowest_price')}"]
        if po.get("median_price"):
            price_bits.append(f"**Median** {po.get('median_price')}")
        if po.get("volume"):
            price_bits.append(f"**Sold (24h)** {po.get('volume')}")
        lines.append("- " + "  |  ".join(price_bits))
        if item_type:
            lines.append(f"- **Type / rarity**: {item_type}")
        if cs.get("exterior") or cs.get("stattrak") or cs.get("souvenir") or cs.get("star"):
            flags = []
            if cs.get("star"):
                flags.append("★")
            if cs.get("stattrak"):
                flags.append("StatTrak™")
            if cs.get("souvenir"):
                flags.append("Souvenir")
            if cs.get("exterior"):
                flags.append(cs["exterior"])
            lines.append(f"- **Condition**: {', '.join(flags)}")
        if listings is not None:
            lines.append(f"- **Listings**: {listings:,}")
        lines.append(f"- {url}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)
