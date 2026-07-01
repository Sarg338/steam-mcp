"""Deals tools: featured specials, storefront highlights, and wishlists."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from steam_mcp import errors, identity, render, transport
from steam_mcp.app import mcp
from steam_mcp.data import pricing
from steam_mcp.render import ResponseFormat


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

    steamid: Optional[str] = Field(
        default=None,
        description="SteamID64, vanity name, or profile URL of the wishlist owner. "
        "Omit to use the configured STEAM_USER, if set.",
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
        data = await pricing._fetch_featured(params.country_code)
        rows = _featured_rows(data.get("specials", {}).get("items", []), params.limit)
        if not rows:
            return "No featured specials returned right now."
        if params.response_format == ResponseFormat.JSON:
            return render._dump(
                {"country": params.country_code, "count": len(rows), "specials": rows}
            )

        lines = [f"# Steam specials on sale ({params.country_code.upper()})", ""]
        for r in rows:
            final = render._fmt_amount(r["final_price"], r["currency"])
            orig = render._fmt_amount(r["original_price"], r["currency"])
            lines.append(
                f"- **{r['name']}** (appid {r['appid']}): "
                f"{final} (was {orig}, -{r['discount_pct']}%)"
            )
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)


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
        data = await pricing._fetch_featured(params.country_code)
        node = data.get(params.section, {})
        items = node.get("items", []) if isinstance(node, dict) else []
        rows = _featured_rows(items, params.limit)
        if not rows:
            return f"No items returned for section '{params.section}'."
        if params.response_format == ResponseFormat.JSON:
            return render._dump(
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
                    f"{render._fmt_amount(r['final_price'], r['currency'])} (was "
                    f"{render._fmt_amount(r['original_price'], r['currency'])}, "
                    f"-{r['discount_pct']}%)"
                )
            elif r["final_price"]:
                price = render._fmt_amount(r["final_price"], r["currency"])
            else:
                price = "Free / TBA"
            lines.append(f"- **{r['name']}** (appid {r['appid']}): {price}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)


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
        sid = await identity._resolve_steamid(params.steamid)
        data = await transport._steam_get(
            "IWishlistService/GetWishlist/v1/", {"steamid": sid}
        )
        items = data.get("response", {}).get("items", [])
        if not items:
            return (
                "No wishlist items returned — the wishlist is empty, or the profile "
                "isn't public. " + errors._privacy_hint("My profile")
            )
        items.sort(key=lambda x: x.get("priority", 0))
        total = len(items)
        page = items[: params.limit]

        if params.enrich:
            pm = await pricing._app_prices([it.get("appid") for it in page],
                                           params.country_code)
            infos = [pm.get(it.get("appid")) for it in page]
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
            return render._dump(
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
        return errors._handle_error(e)
