"""Reviews & community-pulse tools: review scores, news, live player counts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from steam_mcp import errors, render, transport
from steam_mcp.app import mcp
from steam_mcp.cache import CACHE_TTL_NEWS, CACHE_TTL_REVIEWS
from steam_mcp.constants import MAX_RECENT_PAGES, RECENT_PAGE_SIZE
from steam_mcp.models import AppOnlyInput
from steam_mcp.render import ResponseFormat


class AppReviewsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    appid: int = Field(..., description="Steam application (game) ID.", ge=1)
    review_filter: str = Field(
        default="all",
        description="Scoring window: 'all' returns Steam's lifetime summary; "
        "'recent' additionally computes the last-N-days score (the store page's "
        "'Recent Reviews' box) by tallying the newest reviews. Default 'all'.",
    )
    day_range: int = Field(
        default=30,
        description="Window in days for review_filter='recent' (1-365). Ignored "
        "when review_filter='all'. Default 30 to match Steam's store page.",
        ge=1,
        le=365,
    )
    review_type: str = Field(
        default="all",
        description="Which reviews to sample for excerpts: 'all', 'positive', "
        "or 'negative'.",
    )
    limit: int = Field(
        default=5,
        description="Number of individual review excerpts to include (0-20). The "
        "score summary is always returned.",
        ge=0,
        le=20,
    )
    country_code: str = Field(default="us", min_length=2, max_length=2)
    language: str = Field(
        default="english",
        description="Review language to include and score: a Steam language name "
        "(e.g. 'english', 'french') or 'all' for every language. Default 'english'.",
        min_length=2, max_length=32,
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)

    @field_validator("review_type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in {"all", "positive", "negative"}:
            raise ValueError("review_type must be 'all', 'positive', or 'negative'")
        return v

    @field_validator("review_filter")
    @classmethod
    def _check_filter(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in {"all", "recent"}:
            raise ValueError("review_filter must be 'all' or 'recent'")
        return v


def _fmt_review(r: dict) -> dict:
    """Normalize one raw Steam review object into a compact dict."""
    text = (r.get("review") or "").strip().replace("\n", " ")
    return {
        "voted_up": r.get("voted_up"),
        "votes_up": r.get("votes_up", 0),
        "playtime_hours": render._minutes_to_hours(
            (r.get("author") or {}).get("playtime_forever")
        ),
        "timestamp_created": r.get("timestamp_created"),
        "excerpt": (text[:280] + "…") if len(text) > 280 else text,
    }


async def _collect_recent_reviews(
    appid: int, day_range: int, cc: str, language: str = "english"
) -> tuple[list[dict], bool]:
    """Paginate the newest reviews (filter=recent) within the last `day_range` days.

    Steam's query_summary is always lifetime, so the recent score must be tallied
    from individual reviews. Returns (reviews_in_window, capped) where `capped` is
    True if the page budget was exhausted before reaching the window's edge (i.e.
    there may be more recent reviews than were counted).
    """
    import time

    cutoff = time.time() - day_range * 86400
    collected: list[dict] = []
    cursor = "*"
    seen: set[str] = set()
    for _ in range(MAX_RECENT_PAGES):
        data = await transport._raw_get(
            f"https://store.steampowered.com/appreviews/{appid}",
            {
                "json": 1,
                "filter": "recent",
                "language": language,
                "review_type": "all",
                "purchase_type": "all",
                "num_per_page": RECENT_PAGE_SIZE,
                "cc": cc,
                "cursor": cursor,
            },
        )
        if data.get("success") != 1:
            return collected, False
        revs = data.get("reviews", [])
        if not revs:
            return collected, False
        for r in revs:
            if (r.get("timestamp_created") or 0) >= cutoff:
                collected.append(r)
            else:
                return collected, False  # reached the window edge: fully covered
        nxt = data.get("cursor")
        if not nxt or nxt in seen:
            return collected, False
        seen.add(nxt)
        cursor = nxt
    return collected, True  # exhausted page budget without reaching the edge


@mcp.tool(
    name="steam_get_app_reviews",
    annotations={
        "title": "Get Steam App Reviews & Rating",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_app_reviews(params: AppReviewsInput) -> str:
    """Get the review score and sample reviews for a game (lifetime and/or recent).

    Answers "is X any good", "what's the rating of X", and "how are the RECENT
    reviews for X". Always returns Steam's lifetime verdict (e.g. 'Very Positive').
    With review_filter='recent', it ALSO computes the last-N-days positive % by
    tallying the newest reviews — Steam's API has no recent-summary field, so this
    is derived from individual reviews and is marked 'sampled' if the game has more
    recent reviews than the page budget (~600). No API key required.

    Args:
        params (AppReviewsInput): appid, review_filter ('all'|'recent'),
            day_range (window for 'recent'), review_type (excerpt sampling),
            limit (number of excerpts), country_code.

    Returns:
        str: Markdown or JSON. Always includes the lifetime summary
        (review_score_desc, total_positive/negative/reviews, positive_pct). When
        review_filter='recent', adds a 'recent' block (day_range, reviews_counted,
        positive, negative, positive_pct, sampled) and samples excerpts from the
        recent window; otherwise samples from the most-helpful lifetime reviews.
    """
    try:
        # Lifetime summary (always) + excerpt source for the 'all' path.
        base = await transport._raw_get(
            f"https://store.steampowered.com/appreviews/{params.appid}",
            {
                "json": 1,
                "filter": "all",
                "language": params.language,
                "review_type": params.review_type,
                "purchase_type": "all",
                "num_per_page": params.limit if params.review_filter == "all" else 0,
                "cc": params.country_code,
            },
            cache_ttl=CACHE_TTL_REVIEWS,
        )
        if base.get("success") != 1:
            return f"No review data available for app {params.appid}."
        summ = base.get("query_summary", {})
        total = summ.get("total_reviews", 0)
        pos = summ.get("total_positive", 0)
        neg = summ.get("total_negative", 0)
        pos_pct = round(100.0 * pos / (pos + neg), 1) if (pos + neg) else 0.0

        recent = None
        if params.review_filter == "recent":
            window, capped = await _collect_recent_reviews(
                params.appid, params.day_range, params.country_code, params.language
            )
            rpos = sum(1 for r in window if r.get("voted_up"))
            rneg = len(window) - rpos
            rpct = round(100.0 * rpos / len(window), 1) if window else 0.0
            recent = {
                "day_range": params.day_range,
                "reviews_counted": len(window),
                "positive": rpos,
                "negative": rneg,
                "positive_pct": rpct,
                "sampled": capped,
            }
            sample_src = window
        else:
            sample_src = base.get("reviews", [])

        if params.review_type == "positive":
            sample_src = [r for r in sample_src if r.get("voted_up")]
        elif params.review_type == "negative":
            sample_src = [r for r in sample_src if not r.get("voted_up")]
        reviews = [_fmt_review(r) for r in sample_src[: params.limit]]

        if params.response_format == ResponseFormat.JSON:
            out = {
                "appid": params.appid,
                "summary": {
                    "review_score_desc": summ.get("review_score_desc"),
                    "total_reviews": total,
                    "total_positive": pos,
                    "total_negative": neg,
                    "positive_pct": pos_pct,
                },
                "reviews": reviews,
            }
            if recent is not None:
                out["recent"] = recent
            return render._dump(out)

        lines = [
            f"# Reviews for app {params.appid}",
            f"- **Overall (all-time)**: {summ.get('review_score_desc', 'n/a')} — "
            f"{pos:,}/{pos + neg:,} ({pos_pct}%)",
        ]
        if recent is not None:
            note = " (sampled — capped)" if recent["sampled"] else ""
            lines.append(
                f"- **Recent (last {recent['day_range']}d)**: "
                f"{recent['positive_pct']}% of {recent['reviews_counted']} "
                f"reviews{note}"
            )
        lines.append("")
        if reviews:
            scope = "recent" if params.review_filter == "recent" else params.review_type
            lines.append(f"## Sample {scope} reviews")
            for r in reviews:
                thumb = "👍" if r["voted_up"] else "👎"
                lines.append(
                    f"- {thumb} ({r['playtime_hours']}h played, "
                    f"{r['votes_up']} found helpful): {r['excerpt']}"
                )
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)


class AppNewsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    appid: int = Field(..., description="Steam application (game) ID.", ge=1)
    count: int = Field(default=5, description="Number of news items (1-20).", ge=1, le=20)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


@mcp.tool(
    name="steam_get_current_players",
    annotations={
        "title": "Get Steam Live Player Count",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_current_players(params: AppOnlyInput) -> str:
    """Get the number of players currently in-game for a title (live concurrency).

    Answers "how many people are playing X right now" / "is X still popular". No API
    key required.

    Args:
        params (AppOnlyInput): appid.

    Returns:
        str: The current concurrent player count, or an Error string.
    """
    try:
        data = await transport._steam_get(
            "ISteamUserStats/GetNumberOfCurrentPlayers/v1/",
            {"appid": params.appid},
            with_key=False,
        )
        resp = data.get("response", {})
        if resp.get("result") != 1:
            return f"No live player count available for app {params.appid}."
        count = resp.get("player_count", 0)
        if params.response_format == ResponseFormat.JSON:
            return render._dump({"appid": params.appid, "current_players": count})
        return f"App {params.appid} currently has {count:,} players in-game."
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)

@mcp.tool(
    name="steam_get_app_news",
    annotations={
        "title": "Get Steam App News/Updates",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def steam_get_app_news(params: AppNewsInput) -> str:
    """Get recent news/update posts for a game (patch notes, announcements).

    Answers "what's new in X" / "latest update for X". No API key required.

    Args:
        params (AppNewsInput): appid, count.

    Returns:
        str: Markdown or JSON. Per item: title, date, source feed, url, and a short
        excerpt of the contents.
    """
    try:
        data = await transport._steam_get(
            "ISteamNews/GetNewsForApp/v2/",
            {"appid": params.appid, "count": params.count, "maxlength": 300},
            with_key=False,
            cache_ttl=CACHE_TTL_NEWS,
        )
        items = data.get("appnews", {}).get("newsitems", [])
        rows = []
        for it in items:
            body = (it.get("contents") or "").strip().replace("\n", " ")
            rows.append(
                {
                    "title": it.get("title"),
                    "date": it.get("date"),
                    "feed": it.get("feedlabel"),
                    "url": it.get("url"),
                    "excerpt": (body[:280] + "…") if len(body) > 280 else body,
                }
            )
        if not rows:
            return f"No news found for app {params.appid}."
        if params.response_format == ResponseFormat.JSON:
            return render._dump({"appid": params.appid, "count": len(rows), "news": rows})

        import datetime as _dt

        lines = [f"# News for app {params.appid}", ""]
        for r in rows:
            when = (
                _dt.datetime.fromtimestamp(r["date"], _dt.timezone.utc).strftime("%Y-%m-%d")
                if r["date"]
                else "?"
            )
            lines.append(f"## {r['title']} ({when}, {r['feed']})")
            lines.append(r["excerpt"])
            lines.append(f"[Read more]({r['url']})")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return errors._handle_error(e)

