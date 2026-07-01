"""Storefront pricing data helpers (featured specials, per-app prices) for the Steam MCP server."""

from __future__ import annotations

import json

from steam_mcp import transport
from steam_mcp.cache import CACHE_TTL_APPDETAILS, CACHE_TTL_FEATURED


async def _fetch_featured(cc: str) -> dict:
    """Fetch the storefront featuredcategories payload (no key required)."""
    return await transport._store_get("featuredcategories", {"cc": cc, "l": "english"},
                            cache_ttl=CACHE_TTL_FEATURED)


async def _app_price(appid: int, cc: str) -> dict:
    """Fetch a single app's name + current price/discount via the store API."""
    try:
        data = await transport._store_get(
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
            return {"appid": appid, "name": None, "price": None, "is_free": False,
                "on_sale": False, "discount_pct": 0}
        d = entry.get("data", {})
        price = d.get("price_overview") or {}
        is_free = d.get("is_free", False)
        disc = price.get("discount_percent", 0) or 0
        try:
            cents = 0 if is_free else int(price.get("final"))
        except (TypeError, ValueError):
            cents = None
        return {
            "appid": appid,
            "name": d.get("name"),
            "is_free": is_free,
            "price": price.get("final_formatted") or ("Free" if is_free else None),
            "price_cents": cents,
            "discount_pct": disc,
            "on_sale": disc > 0,
        }
    except Exception:  # noqa: BLE001
        return {"appid": appid, "name": None, "price": None, "is_free": False,
                "on_sale": False, "discount_pct": 0}


async def _app_prices(appids: list[int], cc: str = "us") -> dict[int, dict]:
    """Batched name + price/discount for many appids — ONE GetItems call per ~50,
    vs N appdetails calls. Same per-appid shape as `_app_price`, returned as a
    {appid: info} map. GetItems runs on the roomier Web API host (no key) and
    returns a preformatted price; any appid it can't price (bundle/region-locked/
    delisted) falls back to a single `_app_price` so callers still get a result.
    """
    ids = [a for a in dict.fromkeys(appids) if a]  # dedupe, drop falsy, keep order
    if not ids:
        return {}
    out: dict[int, dict] = {}

    async def _chunk(chunk: list[int]) -> dict:
        body = {
            "ids": [{"appid": a} for a in chunk],
            "context": {"language": "english", "country_code": cc.upper(),
                        "steam_realm": 1},
            "data_request": {"include_basic_info": True,
                             "include_all_purchase_options": True,
                             "include_release": True},
        }
        data = await transport._steam_get(
            "IStoreBrowseService/GetItems/v1/",
            {"input_json": json.dumps(body, separators=(",", ":"))},
            with_key=False, cache_ttl=CACHE_TTL_APPDETAILS,
        )
        res: dict[int, dict] = {}
        for it in (data.get("response") or {}).get("store_items", []) or []:
            aid = it.get("appid")
            if not aid:
                continue
            is_free = bool(it.get("is_free"))
            bpo = it.get("best_purchase_option") or {}
            disc = bpo.get("discount_pct") or 0
            price = bpo.get("formatted_final_price") or ("Free" if is_free else None)
            try:
                rts = int((it.get("release") or {}).get("steam_release_date"))
            except (TypeError, ValueError):
                rts = None
            try:
                cents = 0 if is_free else int(bpo.get("final_price_in_cents"))
            except (TypeError, ValueError):
                cents = None
            res[aid] = {
                "appid": aid, "name": it.get("name"), "is_free": is_free,
                "price": price, "price_cents": cents,
                "discount_pct": disc, "on_sale": disc > 0,
                "release_ts": rts,
            }
        return res

    chunks = [ids[i:i + 50] for i in range(0, len(ids), 50)]
    for part in await transport._gather_limited([_chunk(c) for c in chunks]):
        if part:
            out.update(part)

    # Fallback for appids GetItems didn't price (absent, or paid with no price).
    missing = [a for a in ids
               if a not in out or (not out[a]["price"] and not out[a]["is_free"])]
    if missing:
        fills = await transport._gather_limited([_app_price(a, cc) for a in missing])
        out.update({p["appid"]: p for p in fills})
    return out
