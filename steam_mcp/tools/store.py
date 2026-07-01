"""Store tools: app search, details, DLC, tags, packages, pricing, Deck."""

from __future__ import annotations

import asyncio
import json
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from steam_mcp import errors, render, transport
from steam_mcp.app import mcp
from steam_mcp.cache import (
    CACHE_TTL_APPDETAILS,
    CACHE_TTL_PACKAGE,
    CACHE_TTL_TAGS,
)
from steam_mcp.data import catalog, pricing, tags
from steam_mcp.render import ResponseFormat


class DeckCompatInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    appid: int = Field(..., description="Steam application (game) ID.", ge=1)
    language: str = Field(
        default="english", max_length=32,
        description="Steam language name for the report (the category label is "
        "normalized to English regardless).",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


@mcp.tool(
    name="steam_get_deck_compatibility",
    annotations={
        "title": "Steam Deck Compatibility",
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def steam_get_deck_compatibility(params: DeckCompatInput) -> str:
    """Steam Deck rating for a game: Verified, Playable, Unsupported, or Unknown.

    Answers "can I play this on my Steam Deck" and "why is it only Playable" —
    returns Valve's official Deck compatibility category plus the per-criterion test
    results (default controller config, interface text legibility, default
    performance, etc.), each marked pass (✓) or caveat (⚠). No API key required.

    Args:
        params (DeckCompatInput): appid, language, response_format.

    Returns:
        str: Markdown or JSON — the category and the list of Deck test-result notes.
    """
    try:
        deck = await catalog._deck_compat(params.appid, params.language)
        if not deck:
            return (f"No Steam Deck compatibility rating published for appid "
                    f"{params.appid} (untested, or not a game).")
        if params.response_format == ResponseFormat.JSON:
            return render._dump({"appid": params.appid, **deck})
        lines = [f"# Steam Deck: {deck['label']} (appid {params.appid})"]
        for it in deck["items"]:
            lines.append(f"- {it['status']} {it['text']}")
        if deck["blog_url"]:
            lines.append(f"\nDeveloper notes: {deck['blog_url']}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)

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
    language: str = Field(
        default="english",
        description="Steam language name for localized text (name, description, "
        "requirements), e.g. 'english', 'french', 'schinese'. Not ISO codes.",
        min_length=2, max_length=32,
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
    language: str = Field(
        default="english",
        description="Steam language name for localized result names. Not ISO codes.",
        min_length=2, max_length=32,
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)

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
    """Look up a game's appid by its title — when you already know the name and need its ID (not for discovery, recommendations, or buy decisions).

    Use this to turn a game name into an appid for the achievement/details tools.
    Does not require an API key.

    Args:
        params (AppSearchInput): query, limit, country_code.

    Returns:
        str: Markdown or JSON list of matches: appid, name, price (if any).
    """
    try:
        data = await transport._store_get(
            "storesearch/",
            {"term": params.query, "l": params.language, "cc": params.country_code},
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
            return render._dump({"query": params.query, "count": len(rows), "results": rows})

        lines = [f"# Store search: '{params.query}'", ""]
        for r in rows:
            price = ""
            if r["price"]:
                price = f" — {render._fmt_amount(r['price'] / 100, r['currency'])}"
            lines.append(f"- **{r['name']}** (appid {r['appid']}){price}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)

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
        # Fetch the Deck rating concurrently (best-effort: failure must not break
        # app details). Both are cached, so repeat calls are free.
        data, deck = await asyncio.gather(
            transport._store_get(
                "appdetails",
                {"appids": params.appid, "cc": params.country_code,
                 "l": params.language},
                cache_ttl=CACHE_TTL_APPDETAILS,
            ),
            catalog._deck_compat(params.appid, params.language),
            return_exceptions=True,
        )
        if isinstance(data, BaseException):
            raise data
        if isinstance(deck, BaseException):
            deck = None
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
        langs, audio_langs = render._parse_languages(d.get("supported_languages", ""))
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
            "steam_deck": (deck or {}).get("label"),
            "platforms": platforms,
            "metacritic": (d.get("metacritic") or {}).get("score"),
            "metacritic_url": (d.get("metacritic") or {}).get("url"),
            "recommendations_total": (d.get("recommendations") or {}).get("total"),
            "achievements_total": (d.get("achievements") or {}).get("total"),
            "dlc": d.get("dlc", []),
            "dlc_count": len(d.get("dlc", [])),
            "required_age": req_age,
            "mature_content": render._strip_html(cd.get("notes")) if cd.get("notes") else None,
            "supported_languages": langs,
            "full_audio_languages": audio_langs,
            "website": d.get("website"),
            "short_description": render._strip_html(d.get("short_description"), 600),
        }
        if params.include_requirements and pcr:
            def _req(v):
                v = render._strip_html(v, 500)
                return re.sub(r"^(Minimum|Recommended)\s*:\s*", "", v, flags=re.I) if v else v
            summary["pc_requirements"] = {
                "minimum": _req(pcr.get("minimum")),
                "recommended": _req(pcr.get("recommended")),
            }
        if params.include_long_description:
            summary["about_the_game"] = render._strip_html(d.get("about_the_game"), 2000)

        if params.response_format == ResponseFormat.JSON:
            return render._dump(summary)

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
        if summary["steam_deck"]:
            lines.append(f"- **Steam Deck**: {summary['steam_deck']}")
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
        return errors._handle_error(e)

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
        data = await transport._store_get(
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
            pm = await pricing._app_prices(page_ids, params.country_code)
            infos = [pm.get(i) for i in page_ids]
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
            return render._dump(
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
        return errors._handle_error(e)

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
        data = await transport._steam_get(
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
        name_map = await tags._tag_name_map()
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
            return render._dump(
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
        return errors._handle_error(e)

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
        data = await transport._store_get(
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
            return render._dump(summary)

        lines = [f"# {summary['name']} (package {params.packageid})"]
        if price:
            if summary["discount_pct"]:
                lines.append(
                    f"- **Price**: {render._fmt_amount(summary['final_price'], currency)} "
                    f"(was {render._fmt_amount(summary['initial_price'], currency)}, "
                    f"-{summary['discount_pct']}%)"
                )
            else:
                lines.append(
                    f"- **Price**: {render._fmt_amount(summary['final_price'], currency)}"
                )
        if summary["release_date"]:
            lines.append(f"- **Released**: {summary['release_date']}")
        if apps:
            lines.append(f"- **Includes {len(apps)} app(s)**: " + ", ".join(apps[:20]))
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)

class RegionalPricingInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    appid: int = Field(..., description="Steam application (game) ID.", ge=1)
    countries: list[str] = Field(
        default_factory=lambda: ["us", "gb", "de", "br", "jp", "au", "ca", "in"],
        description="ISO country codes to price in (2 letters each, max 20). Prices "
        "are returned in each region's own currency.",
        max_length=20,
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)

    @field_validator("countries")
    @classmethod
    def _check_countries(cls, v):
        out = []
        for c in v:
            c = c.strip().lower()
            if len(c) != 2:
                raise ValueError("each country code must be 2 letters")
            out.append(c)
        return out or ["us"]

@mcp.tool(
    name="steam_get_app_regional_pricing",
    annotations={
        "title": "Get Steam Regional Pricing",
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def steam_get_app_regional_pricing(params: RegionalPricingInput) -> str:
    """Compare a game's price across regions (each in its own local currency).

    Fetches the store price for the same app in several countries at once. Note the
    amounts are in different currencies (USD, EUR, BRL, JPY, …), so they are NOT
    directly comparable without an exchange rate — this shows each region's local
    price and discount, not a converted ranking. No API key required.

    Args:
        params (RegionalPricingInput): appid, countries.

    Returns:
        str: Markdown or JSON. game name plus, per country, the localized price,
        discount, and on-sale flag.
    """
    try:
        infos = await transport._gather_limited(
            [pricing._app_price(params.appid, cc) for cc in params.countries]
        )
        name = next((i.get("name") for i in infos if i.get("name")),
                    f"app {params.appid}")
        rows = []
        for cc, info in zip(params.countries, infos, strict=True):
            rows.append({
                "country": cc,
                "is_free": info.get("is_free", False),
                "price": info.get("price"),
                "discount_pct": info.get("discount_pct", 0),
                "on_sale": info.get("on_sale", False),
            })
        if params.response_format == ResponseFormat.JSON:
            return render._dump({"appid": params.appid, "name": name, "prices": rows})

        lines = [f"# Regional pricing: {name} (appid {params.appid})",
                 "_Each price is in that region's own currency._", ""]
        for r in rows:
            if r["price"]:
                tail = f" (-{r['discount_pct']}%)" if r["on_sale"] else ""
                lines.append(f"- **{r['country'].upper()}**: {r['price']}{tail}")
            else:
                lines.append(f"- **{r['country'].upper()}**: n/a (not sold / no price)")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)
