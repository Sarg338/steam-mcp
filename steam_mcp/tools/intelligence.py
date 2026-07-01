"""Intelligence tools: composite decision + recommendation helpers."""

from __future__ import annotations

import asyncio
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from steam_mcp import errors, identity, render, transport
from steam_mcp.app import mcp
from steam_mcp.cache import CACHE_TTL_APPDETAILS, CACHE_TTL_REVIEWS
from steam_mcp.data import catalog, players, pricing, tags
from steam_mcp.render import ResponseFormat
from steam_mcp.tools import reviews


class ShouldIBuyInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    appid: int = Field(..., description="Steam application (game) ID to evaluate.", ge=1)
    steamid: Optional[str] = Field(
        default=None, max_length=200,
        description="Optional: personalize — whether you already own it and how its "
        "tags match your most-played games. SteamID64, vanity, or profile URL.",
    )
    country_code: str = Field(default="us", min_length=2, max_length=2)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


@mcp.tool(
    name="steam_should_i_buy",
    annotations={
        "title": "Steam Buying Brief (Should I Buy?)",
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def steam_should_i_buy(params: ShouldIBuyInput) -> str:
    """Decide whether to buy ONE specific game — price, recent + lifetime reviews, tags, Metacritic, and taste match in one call; for evaluating a single known game, not finding new ones.

    Fuses the decision-relevant signals: current price/discount, lifetime AND
    last-30-days review scores (the divergence shows whether a game is improving or
    declining), top community tags, Metacritic, and release status. Pass a steamid
    to personalize — whether you already own it and which of its tags match your
    most-played games. Returns the facts for a reasoned call (it does not hard-code
    a yes/no). The store data needs no API key; personalization does.

    Args:
        params (ShouldIBuyInput): appid, steamid, country_code.

    Returns:
        str: Markdown brief or JSON — price, reviews (lifetime + recent + trend),
        tags, metacritic, and (if steamid) ownership + taste match.
    """
    try:
        cc = params.country_code
        details, rev, tags_map = await asyncio.gather(
            transport._store_get("appdetails", {"appids": params.appid, "cc": cc, "l": "english"},
                       cache_ttl=CACHE_TTL_APPDETAILS),
            transport._raw_get(f"https://store.steampowered.com/appreviews/{params.appid}",
                     {"json": 1, "filter": "all", "language": "english",
                      "review_type": "all", "purchase_type": "all",
                      "num_per_page": 0, "cc": cc},
                     cache_ttl=CACHE_TTL_REVIEWS),
            tags._items_tags([params.appid]),
        )
        entry = details.get(str(params.appid), {}) if isinstance(details, dict) else {}
        if not entry.get("success"):
            return f"No store details found for app {params.appid}."
        d = entry.get("data", {})
        name = d.get("name") or str(params.appid)
        price = d.get("price_overview") or {}
        is_free = d.get("is_free", False)
        rel = d.get("release_date") or {}

        summ = rev.get("query_summary", {}) if isinstance(rev, dict) else {}
        l_pos, l_neg = summ.get("total_positive", 0), summ.get("total_negative", 0)
        l_pct = round(100 * l_pos / (l_pos + l_neg), 1) if (l_pos + l_neg) else None
        window, capped = await reviews._collect_recent_reviews(params.appid, 30, cc)
        r_n = len(window)
        r_pct = round(100 * sum(1 for r in window if r.get("voted_up")) / r_n, 1) if r_n else None
        trend = round(r_pct - l_pct, 1) if (r_pct is not None and l_pct is not None) else None

        name_map = await tags._tag_name_map()
        top_tag_ids, top_tags = [], []
        for t in (tags_map.get(params.appid, []) or [])[:8]:
            try:
                tid = int(t.get("tagid"))
            except (TypeError, ValueError):
                continue
            top_tag_ids.append(tid)
            if name_map.get(tid):
                top_tags.append(name_map[tid])

        personal = None
        if params.steamid:
            sid = await identity._resolve_steamid(params.steamid)
            taste = await tags._taste_profile(sid)
            taste_set = set(taste["tag_ids"])
            personal = {
                "already_owns": params.appid in taste["owned_ids"],
                "taste_match_tags": [name_map[t] for t in top_tag_ids
                                     if t in taste_set and name_map.get(t)],
                "your_top_tags": taste["tag_names"],
            }

        summary = {
            "appid": params.appid, "name": name, "is_free": is_free,
            "price": price.get("final_formatted") or ("Free" if is_free else None),
            "initial_price": price.get("initial_formatted") or None,
            "discount_pct": price.get("discount_percent", 0),
            "released": rel.get("date"), "coming_soon": rel.get("coming_soon", False),
            "genres": [g.get("description") for g in d.get("genres", [])],
            "metacritic": (d.get("metacritic") or {}).get("score"),
            "review_lifetime": {"desc": summ.get("review_score_desc"),
                                "positive_pct": l_pct,
                                "total": summ.get("total_reviews", 0)},
            "review_recent_30d": {"positive_pct": r_pct, "reviews_counted": r_n,
                                  "sampled": capped},
            "review_trend_pts": trend,
            "top_tags": top_tags,
            "personal": personal,
        }
        if params.response_format == ResponseFormat.JSON:
            return render._dump(summary)

        price_str = summary["price"] or "Unknown"
        if summary["discount_pct"]:
            price_str = (f"{summary['price']} (was {summary['initial_price']}, "
                         f"-{summary['discount_pct']}%)")
        lines = [
            f"# Should I buy: {name} (appid {params.appid})",
            f"- **Price**: {price_str}"
            + (" — coming soon" if summary["coming_soon"] else ""),
            f"- **Released**: {summary['released'] or 'n/a'}  |  "
            f"**Genres**: {', '.join(g for g in summary['genres'] if g) or 'n/a'}",
        ]
        if summary["metacritic"]:
            lines.append(f"- **Metacritic**: {summary['metacritic']}")
        lt = summary["review_lifetime"]
        lines.append(
            f"- **Reviews (lifetime)**: {lt['desc'] or 'n/a'} — "
            f"{lt['positive_pct']}% of {lt['total']:,}"
        )
        rc = summary["review_recent_30d"]
        if rc["positive_pct"] is not None:
            tnote = f" ({'+' if (trend or 0) >= 0 else ''}{trend} pts vs lifetime)" \
                if trend is not None else ""
            samp = " [sampled]" if rc["sampled"] else ""
            lines.append(
                f"- **Reviews (last 30d)**: {rc['positive_pct']}% of "
                f"{rc['reviews_counted']}{samp}{tnote}"
            )
        if top_tags:
            lines.append(f"- **Tags**: {', '.join(top_tags)}")
        if personal:
            if personal["already_owns"]:
                lines.append("- ⚠️ **You already own this.**")
            if personal["taste_match_tags"]:
                lines.append(
                    f"- **Matches your taste**: shares "
                    f"{', '.join(personal['taste_match_tags'])} with your most-played"
                )
            elif personal["your_top_tags"]:
                lines.append(
                    f"- Your taste leans {', '.join(personal['your_top_tags'])} "
                    f"(little overlap here)"
                )
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)


class RecommendInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    seed_appid: Optional[int] = Field(
        default=None, ge=1,
        description="Recommend games similar to THIS game (by community tags).",
    )
    steamid: Optional[str] = Field(
        default=None, max_length=200,
        description="Excludes games this user already owns; seeds the tags from "
        "their taste (most-played + recent) only when no seed_appid/tags are "
        "given. SteamID64, vanity, or profile URL.",
    )
    tags: list[str] = Field(
        default_factory=list, max_length=10,
        description="Explicit tag names to base recommendations on. Takes precedence "
        "over seed_appid/steamid tags if given.",
    )
    max_price: Optional[int] = Field(
        default=None, ge=0, le=1000,
        description="Optional max price (country's currency units).",
    )
    limit: int = Field(default=10, ge=1, le=30, description="Max recommendations (1-30).")
    country_code: str = Field(default="us", min_length=2, max_length=2)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


@mcp.tool(
    name="steam_recommend",
    annotations={
        "title": "Recommend Steam Games (with reasons)",
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def steam_recommend(params: RecommendInput) -> str:
    """Recommend games similar to a seed game ("like Hades") or to a user's taste, explaining the shared tags; for "games like X" / "what should I play" (for filtered search use steam_discover).

    Pick a basis: a seed_appid ("games like Hades"), a steamid (your most-played +
    recent taste), or explicit tags — precedence tags > seed_appid > taste. Pass
    BOTH seed_appid and steamid for "games like X that I don't own": the seed
    drives the tags and the steamid supplies the ownership exclusion. Finds
    well-reviewed games that share those tags — excluding the seed game and (with
    steamid) games you already own — and explains WHY each matches (the shared
    tags). The store search needs no key; steamid personalization does.

    Args:
        params (RecommendInput): seed_appid, steamid, tags, max_price, limit, cc.

    Returns:
        str: Markdown or JSON — the basis plus ranked recommendations (appid, name,
        price, matching_tags), best tag-overlap first.
    """
    try:
        cc = params.country_code
        seed_ids: list[int] = []     # full tag set, for scoring overlap
        filter_ids: list[int] = []   # the AND filter for the store search
        basis = None
        exclude: set = set()
        owned_ids: set = set()

        # Basis precedence: explicit tags > seed game > taste. A steamid ALWAYS
        # contributes the ownership exclusion, but only seeds the tags when
        # neither tags nor seed_appid were given — "games like X that I don't
        # own" must anchor on X, not on the user's most-played genres.
        if params.tags:
            seed_ids, _ = await tags._resolve_tag_ids(params.tags)
            filter_ids = seed_ids[:]
            basis = "tags: " + ", ".join(params.tags)
        if not seed_ids and params.seed_appid:
            tmap = await tags._items_tags([params.seed_appid])
            for t in (tmap.get(params.seed_appid, []) or [])[:10]:
                try:
                    seed_ids.append(int(t.get("tagid")))
                except (TypeError, ValueError):
                    continue
            filter_ids = seed_ids[:3]
            info = await pricing._app_price(params.seed_appid, cc)
            basis = "like " + (info.get("name") or f"app {params.seed_appid}")
            exclude.add(params.seed_appid)
        if params.steamid:
            sid = await identity._resolve_steamid(params.steamid)
            taste = await tags._taste_profile(sid)
            owned_ids = {a for a in taste["owned_ids"] if a}
            if not seed_ids and taste["tag_ids"]:
                seed_ids = taste["tag_ids"]
                filter_ids = seed_ids[:3]
                basis = "your taste (" + ", ".join(taste["seed_games"][:3]) + ")"

        if not seed_ids:
            return ("Provide a basis: seed_appid (games like X), steamid (your "
                    "taste), or tags.")
        exclude |= owned_ids

        query = {
            "json": 1, "infinite": 1, "cc": cc, "l": "english", "category1": 998,
            "start": 0, "count": 100, "sort_by": "Reviews_DESC",
            "tags": ",".join(str(t) for t in (filter_ids or seed_ids)),
        }
        if params.max_price is not None:
            query["maxprice"] = str(params.max_price)
        cand, _ = await catalog._discover_appids(query)
        excluded_owned = sum(1 for a in cand if a in owned_ids)
        cand = [a for a in cand if a not in exclude][:40]
        if not cand:
            return "No recommendations found — try fewer/different tags or a higher price."

        cand_tags = await tags._items_tags(cand)
        name_map = await tags._tag_name_map()
        seed_set = set(seed_ids)
        scored = []
        for a in cand:
            shared = []
            for t in cand_tags.get(a, []) or []:
                try:
                    tid = int(t.get("tagid"))
                except (TypeError, ValueError):
                    continue
                if tid in seed_set and name_map.get(tid):
                    shared.append(name_map[tid])
            scored.append((a, shared))
        scored.sort(key=lambda x: len(x[1]), reverse=True)  # stable: review rank on ties
        page = scored[: params.limit]
        pm = await pricing._app_prices([a for a, _ in page], cc)
        infos = [pm.get(a, {}) for a, _ in page]
        rows = []
        for (a, shared), info in zip(page, infos, strict=True):
            rows.append({
                "appid": a, "name": info.get("name") or f"app {a}",
                "price": info.get("price"), "on_sale": info.get("on_sale", False),
                "discount_pct": info.get("discount_pct", 0),
                "matching_tags": shared,
            })

        if params.response_format == ResponseFormat.JSON:
            return render._dump({"basis": basis, "excluded_owned": excluded_owned,
                          "count": len(rows), "recommendations": rows})

        owned_note = (f", excluding {excluded_owned} you own"
                      if excluded_owned else "")
        lines = [f"# Recommendations — {basis}", f"{len(rows)} games{owned_note}:", ""]
        for r in rows:
            why = f" — matches: {', '.join(r['matching_tags'])}" if r["matching_tags"] else ""
            if r["on_sale"]:
                price = f" [{r['price']} -{r['discount_pct']}%]"
            elif r["price"]:
                price = f" [{r['price']}]"
            else:
                price = ""
            lines.append(f"- **{r['name']}** (appid {r['appid']}){price}{why}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)


class PlanCoopNightInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    steamid: Optional[str] = Field(
        default=None, max_length=200,
        description="The host whose library to match against friends. SteamID64, "
        "vanity, or profile URL. Omit to use the configured STEAM_USER, if set.",
    )
    friends: list[str] = Field(
        default_factory=list, max_length=50,
        description="Optional explicit group (SteamID64s / vanity names) — pass this "
        "to plan with specific people. If omitted, uses the host's friends (online "
        "ones by default).",
    )
    mode: str = Field(
        default="owned",
        description="'owned' (default): co-op games the host + friends already SHARE, "
        "ranked by how many own each. 'new': well-reviewed co-op games NONE of the "
        "group owns yet — fresh picks to buy and play together.",
    )
    online_only: bool = Field(
        default=True,
        description="When the group is derived from the friend list, include only "
        "friends online right now. Ignored when 'friends' is given.",
    )
    max_friends: int = Field(
        default=20, ge=1, le=100,
        description="Max friends to check when deriving the group (bounds lookups).",
    )
    min_friends_owning: int = Field(
        default=1, ge=1, le=50,
        description="A game must be owned by the host AND at least this many group "
        "members to be suggested.",
    )
    limit: int = Field(default=20, ge=1, le=50, description="Max co-op games to list.")
    country_code: str = Field(default="us", min_length=2, max_length=2)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in {"owned", "new"}:
            raise ValueError("mode must be 'owned' or 'new'")
        return v


@mcp.tool(
    name="steam_plan_coop_night",
    annotations={
        "title": "Plan a Steam Co-op Night",
        "readOnlyHint": True, "destructiveHint": False,
        "idempotentHint": True, "openWorldHint": True,
    },
)
async def steam_plan_coop_night(params: PlanCoopNightInput) -> str:
    """Find co-op games the host and their friends all own — for game night.

    Two modes. mode='owned' (default) cross-references the host's library with
    friends' libraries, keeps co-op games, and ranks by how many of the group own
    each. mode='new' instead recommends well-reviewed co-op games that NONE of the
    group owns yet — fresh picks to buy and play together (it excludes every readable
    library in the group). By default the group is the host's friends who are ONLINE
    right now (the "tonight" framing); pass an explicit `friends` list to plan with
    specific people, or online_only=false for everyone. 'owned' needs the host's
    Game Details Public; both need the friend list Public when the group is derived.
    Needs an API key.

    Args:
        params (PlanCoopNightInput): steamid (host), friends, mode, online_only,
            max_friends, min_friends_owning, limit, country_code.

    Returns:
        str: Markdown or JSON. the group (+ who's online), and either co-op games the
        group shares (ranked by owners) or — in mode='new' — fresh co-op games to buy.
    """
    try:
        host = await identity._resolve_steamid(params.steamid)

        if params.friends:
            group, seen = [], set()
            for f in params.friends:
                try:
                    g = await identity._resolve_steamid(f)
                except Exception:  # noqa: BLE001
                    continue
                if g != host and g not in seen:
                    seen.add(g)
                    group.append(g)
            if not group:
                return "Couldn't resolve any of the given friends."
            summaries = await players._summaries_for(group)
            derived = False
        else:
            fdata = await transport._steam_get(
                "ISteamUser/GetFriendList/v1/",
                {"steamid": host, "relationship": "friend"},
            )
            fids = [f["steamid"] for f in fdata.get("friendslist", {}).get("friends", [])]
            if not fids:
                return ("No friends returned — the host's friend list isn't public. "
                        + errors._privacy_hint("Friends List"))
            summaries = await players._summaries_for(fids)
            group = ([g for g in fids if players._is_online(summaries.get(g, {}))]
                     if params.online_only else fids)
            if params.online_only and not group:
                return ("None of the host's friends are online right now — try "
                        "online_only=false, or pass an explicit friends list.")
            group = group[: params.max_friends]
            derived = True

        host_owned = await players._owned_set(host)
        member_sets = await transport._gather_limited([players._owned_set(g) for g in group])
        private = sum(1 for s in member_sets if s is None)
        checked = [g for g, s in zip(group, member_sets, strict=True) if s is not None]
        online_names = [summaries.get(g, {}).get("personaname", "Unknown")
                        for g in checked if players._is_online(summaries.get(g, {}))]
        if derived:
            grp_desc = (f"your {len(group)} online friends" if params.online_only
                        else f"{len(group)} friends")
        else:
            grp_desc = ", ".join(summaries.get(g, {}).get("personaname", g)
                                 for g in checked) or "your group"
        header = [
            f"# Co-op night for {host}",
            f"Group: {grp_desc}."
            + (f" Online now: {', '.join(online_names)}." if online_names else ""),
        ]

        # --- "new" mode: well-reviewed co-op games NONE of the group owns yet ---
        if params.mode == "new":
            cc = params.country_code
            owned_union = set(host_owned or ())
            for s in member_sets:
                if s:
                    owned_union |= s
            coop_tag_ids, _ = await tags._resolve_tag_ids(["Co-op"])
            query = {"json": 1, "infinite": 1, "cc": cc, "l": "english",
                     "category1": 998, "start": 0, "count": 100,
                     "sort_by": "Reviews_DESC"}
            if coop_tag_ids:
                query["tags"] = ",".join(str(t) for t in coop_tag_ids)
            found, _ = await catalog._discover_appids(query)
            fresh = [a for a in found if a not in owned_union][:60]
            coop_info = await catalog._items_coop(fresh)
            picks = []
            for a in fresh:
                ci = coop_info.get(a)
                if (ci and ci.get("coop")
                        and not catalog._is_temp_client(ci.get("name") or "")):
                    picks.append(a)
                if len(picks) >= params.limit:
                    break
            pm = await pricing._app_prices(picks, cc) if picks else {}
            rows = [{
                "appid": a,
                "name": (pm.get(a, {}).get("name")
                         or (coop_info.get(a) or {}).get("name") or f"app {a}"),
                "price": pm.get(a, {}).get("price"),
                "on_sale": pm.get(a, {}).get("on_sale", False),
                "discount_pct": pm.get(a, {}).get("discount_pct", 0),
            } for a in picks]
            libs = len(checked) + (1 if host_owned is not None else 0)
            if params.response_format == ResponseFormat.JSON:
                return render._dump({
                    "host": host, "mode": "new", "group_size": len(group),
                    "checked": len(checked), "private_or_unknown": private,
                    "online_now": online_names, "excluded_owned": len(owned_union),
                    "count": len(rows), "games": rows,
                })
            lines = header + [
                f"Fresh co-op picks — none of the {libs} readable "
                f"{'library' if libs == 1 else 'libraries'} own these:",
                "",
            ]
            if rows:
                for r in rows:
                    sale = f" (-{r['discount_pct']}%)" if r["on_sale"] else ""
                    lines.append(f"- **{r['name']}** (appid {r['appid']}) — "
                                 f"{r['price'] or 'price n/a'}{sale}")
            else:
                lines.append("Couldn't find fresh co-op games right now — try again, "
                             "or widen the group.")
            return "\n".join(lines)

        # --- "owned" mode (default): co-op games the group already shares ---
        if host_owned is None:
            return ("Can't plan — the host's Game details aren't public. "
                    + errors._privacy_hint("Game details"))
        owners_by_app: dict = {}
        for g, s in zip(group, member_sets, strict=True):
            if s is None:
                continue
            for a in (s & host_owned):
                owners_by_app.setdefault(a, []).append(g)

        candidates = [(a, owners) for a, owners in owners_by_app.items()
                      if len(owners) >= params.min_friends_owning]
        if not candidates:
            return ("No shared games among the host and the selected friends "
                    "(with public libraries). Try more friends, online_only=false, "
                    "or mode='new' to find games none of you own yet.")
        candidates.sort(key=lambda x: len(x[1]), reverse=True)
        coop_info = await catalog._items_coop([a for a, _ in candidates[:150]])

        rows = []
        for a, owners in candidates[:150]:
            ci = coop_info.get(a)
            if not ci or not ci.get("coop"):
                continue
            if catalog._is_temp_client(ci.get("name") or ""):
                continue  # unlaunchable beta/playtest — a dead co-op-night pick
            rows.append({
                "appid": a, "name": ci.get("name") or f"app {a}",
                "owner_count": len(owners),
                "owners": [summaries.get(o, {}).get("personaname", "Unknown")
                           for o in owners],
            })
            if len(rows) >= params.limit:
                break

        if params.response_format == ResponseFormat.JSON:
            return render._dump({
                "host": host, "mode": "owned", "group_size": len(group),
                "checked": len(checked), "private_or_unknown": private,
                "online_now": online_names, "count": len(rows), "games": rows,
            })
        lines = header + [
            f"Checked {len(checked)} libraries ({private} private/unknown).",
            "",
        ]
        if rows:
            lines.append("Co-op games you can play together (most-owned first):")
            for r in rows:
                shown = r["owners"][:5]
                more = f" +{len(r['owners']) - 5} more" if len(r["owners"]) > 5 else ""
                lines.append(
                    f"- **{r['name']}** (appid {r['appid']}) — you + "
                    f"{r['owner_count']} ({', '.join(shown)}{more})"
                )
        else:
            lines.append("No co-op games shared across the group "
                         "(everyone owns different things, or libraries are private).")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)
