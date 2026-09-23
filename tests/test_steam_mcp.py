"""Tests for steam_mcp.server.

Covers the pure helpers, the TTL cache, and the tool logic with mocked HTTP —
no network and no API key required. Run with: pytest -q
"""
import asyncio
import inspect
import json
import logging
import re
import time

import pytest
from pydantic import ValidationError

import steam_mcp.server as S


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def test_strip_html():
    assert S._strip_html(None) is None
    assert S._strip_html("<b>Hi</b>&amp; <br>there") == "Hi & there"
    assert S._strip_html("<p>   </p>") is None
    out = S._strip_html("x" * 1000, limit=50)
    assert len(out) == 50 and out.endswith("…")


def test_excerpt_never_exceeds_its_limit():
    assert S._excerpt("x" * 279) == "x" * 279          # under the cap: untouched
    assert S._excerpt("x" * 280) == "x" * 280          # exactly at it: untouched
    out = S._excerpt("x" * 281)
    assert len(out) == 280 and out.endswith("…")       # over it: capped, not 281
    assert len(S._excerpt("x" * 5000)) == 280
    assert S._excerpt("abcdef", limit=3) == "ab…"
    # Multi-byte text: the cap counts characters, not bytes. Live Korean and
    # Japanese review excerpts come back at 280 chars / 660-818 bytes.
    ko = S._excerpt("가" * 400)
    assert len(ko) == 280 and ko.endswith("…")
    assert len(ko.encode("utf-8")) == 840   # 279 hangul x 3 bytes + the ellipsis


def test_review_excerpt_respects_the_cap():
    review = S._fmt_review({"review": "y" * 400, "votes_up": 1, "voted_up": True})
    assert len(review["excerpt"]) == 280 and review["excerpt"].endswith("…")


def test_parse_languages():
    a, au = S._parse_languages(
        "English<strong>*</strong>, French, German<br><strong>*</strong>full audio"
    )
    assert a == ["English", "French", "German"]
    assert au == ["English"]
    assert S._parse_languages("") == ([], [])


def test_ts_to_date():
    assert S._ts_to_date(0) is None
    assert S._ts_to_date(86400) is None  # pre-2001 sentinel
    assert S._ts_to_date(1700000000) == "2023-11-14"


def test_minutes_to_hours():
    assert S._minutes_to_hours(90) == 1.5
    assert S._minutes_to_hours(None) == 0.0
    assert S._minutes_to_hours(0) == 0.0


def test_persona_label():
    assert S._persona_label({"personastate": 3}) == "Away"
    assert S._persona_label({"personastate": 1, "gameextrainfo": "Dota 2"}) == "In-Game: Dota 2"
    assert S._persona_label({}) == "Offline"


def test_featured_rows():
    rows = S._featured_rows(
        [{"id": 1, "name": "G", "original_price": 1999, "final_price": 999,
          "discount_percent": 50}], 5)
    assert rows[0] == {"appid": 1, "name": "G", "original_price": 19.99,
                       "final_price": 9.99, "discount_pct": 50, "currency": None}


def test_cache_key_excludes_api_key():
    k = S._cache_key("u", {"key": "SECRET", "appid": 7, "cc": "us"})
    assert "SECRET" not in k
    assert k == S._cache_key("u", {"cc": "us", "appid": 7})  # order-independent


def test_steamid64_regex():
    assert S.STEAMID64_RE.match("76561197960287930")
    assert not S.STEAMID64_RE.match("123")
    assert not S.STEAMID64_RE.match("12345678901234567")


# --------------------------------------------------------------------------- #
# _resolve_steamid — the no-network short-circuits
# --------------------------------------------------------------------------- #

def test_resolve_steamid_passthrough():
    assert run(S._resolve_steamid("76561197960287930")) == "76561197960287930"
    assert run(S._resolve_steamid(
        "https://steamcommunity.com/profiles/76561197960287930")) == "76561197960287930"


# --------------------------------------------------------------------------- #
# STEAM_USER default-user config
# --------------------------------------------------------------------------- #

def test_get_default_user_env(monkeypatch):
    monkeypatch.setenv("STEAM_USER", "76561197960287930")
    assert S._get_default_user() == "76561197960287930"
    monkeypatch.delenv("STEAM_USER", raising=False)
    monkeypatch.setattr(S, "_dotenv_value", lambda name: "")
    assert S._get_default_user() == ""


def test_resolve_steamid_defaults_to_steam_user(monkeypatch):
    # When no identifier is passed, fall back to the configured default user.
    monkeypatch.setattr(S, "_get_default_user", lambda: "76561197960287930")
    assert run(S._resolve_steamid(None)) == "76561197960287930"
    assert run(S._resolve_steamid("")) == "76561197960287930"
    assert run(S._resolve_steamid("   ")) == "76561197960287930"


def test_resolve_steamid_no_default_raises(monkeypatch):
    monkeypatch.setattr(S, "_get_default_user", lambda: "")
    with pytest.raises(S.SteamApiError):
        run(S._resolve_steamid(None))


def test_subject_tool_uses_default_user(monkeypatch):
    # A subject tool with the steamid omitted resolves to STEAM_USER end-to-end.
    monkeypatch.setattr(S, "_get_default_user", lambda: "76561197960287930")

    async def fake_steam(path, params, **k):
        assert params.get("steamid") == "76561197960287930"
        return {"response": {"game_count": 1, "games": [
            {"appid": 440, "name": "Team Fortress 2",
             "playtime_forever": 120, "playtime_2weeks": 0}]}}

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    out = run(S.steam_get_owned_games(S.OwnedGamesInput()))
    assert "Team Fortress 2" in out


# --------------------------------------------------------------------------- #
# TTL cache
# --------------------------------------------------------------------------- #

def test_ttl_cache_basic():
    c = S._TTLCache(maxsize=2)
    c.set("a", 1, ttl=100)
    assert c.get("a") == 1
    c.set("b", 2, ttl=-1)          # already expired
    assert c.get("b") is None
    assert c.get("missing") is None


def test_ttl_cache_eviction():
    c = S._TTLCache(maxsize=2)
    c.set("x", 1, 100)
    c.set("y", 2, 100)
    c.set("z", 3, 100)             # exceeds maxsize -> eviction kicks in
    assert len(c._d) <= 2


def test_get_api_key_missing(monkeypatch):
    monkeypatch.delenv("STEAM_API_KEY", raising=False)
    monkeypatch.setattr(S, "_load_key_from_dotenv", lambda: "")
    with pytest.raises(S.SteamApiError):
        S._get_api_key()


def test_store_get_caches(monkeypatch):
    S._CACHE.clear()
    calls = {"n": 0}

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"ok": calls["n"]}

    class FakeClient:
        async def get(self, *a, **k):
            calls["n"] += 1
            return FakeResp()

    monkeypatch.setattr(S, "_http_client", lambda: FakeClient())
    run(S._store_get("appdetails", {"appids": 1}, cache_ttl=60))
    run(S._store_get("appdetails", {"appids": 1}, cache_ttl=60))
    assert calls["n"] == 1                      # second served from cache
    run(S._store_get("appdetails", {"appids": 1}))   # no ttl -> refetch
    assert calls["n"] == 2


def test_http_client_rebinds_across_event_loops():
    # Regression: the shared AsyncClient binds to the loop it first runs on. A new
    # asyncio.run() creates a new loop, so the client must be recreated — otherwise
    # reuse raises "RuntimeError: Event loop is closed". Pre-fix this returned the
    # same client across loops (c1 == c2).
    S._CLIENT = None
    S._CLIENT_LOOP = None

    async def grab():
        return id(S._http_client()), id(asyncio.get_running_loop())

    c1, l1 = run(grab())
    c2, l2 = run(grab())
    assert l1 != l2          # genuinely different event loops
    assert c1 != c2          # client was rebuilt for the new loop
    S._CLIENT = None         # reset shared state for any later test
    S._CLIENT_LOOP = None


# --------------------------------------------------------------------------- #
# 0.9.0: discovery / recommendation search
# --------------------------------------------------------------------------- #

def test_resolve_tag_ids(monkeypatch):
    async def fake_map():
        return {29482: "Souls-like", 1685: "Co-op"}

    monkeypatch.setattr(S, "_tag_name_map", fake_map)
    ids, missing = run(S._resolve_tag_ids(["souls-like", "Co-op", "Nonexistent"]))
    assert ids == [29482, 1685]          # case-insensitive
    assert missing == ["Nonexistent"]


def test_discover_basic(monkeypatch):
    async def fake_raw(url, params, cache_ttl=0):
        return {"success": 1, "total_count": 3,
                "results_html": '<a data-ds-appid="10"></a>'
                                '<a data-ds-appid="20"></a><a data-ds-appid="30"></a>'}

    prices = {10: {"name": "A", "price": "$5", "discount_pct": 0, "on_sale": False},
              20: {"name": "B", "price": "$10", "discount_pct": 50, "on_sale": True},
              30: {"name": "C", "price": "$1", "discount_pct": 0, "on_sale": False}}

    async def fake_app_prices(appids, cc):
        return {a: prices[a] for a in appids}

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    monkeypatch.setattr(S, "_app_prices", fake_app_prices)
    out = run(S.steam_discover(S.DiscoverInput(term="x", response_format="json")))
    d = json.loads(out)
    assert d["total_count"] == 3 and d["count"] == 3
    assert [r["appid"] for r in d["results"]] == [10, 20, 30]   # ranked order kept
    assert d["personalized"] is False


def _review_row(appid, pct=None, count=0):
    """One search-result row as Steam renders it, with the review tooltip."""
    tip = (f'<div class="search_review_summary" data-tooltip-html="Very Positive'
           f'<br>{pct}% of the {count:,} user reviews for this game are positive.">'
           f'</div>') if pct is not None else ""
    return f'<a data-ds-appid="{appid}"></a>{tip}'


def test_discover_search_parses_review_tooltips(monkeypatch):
    async def fake_raw(url, params, cache_ttl=0):
        return {"total_count": 2,
                "results_html": _review_row(10, 92, 15632) + _review_row(20)}

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    rows, total = run(S._discover_search({}))
    assert total == 2
    assert rows[0] == {"appid": 10, "review_pct": 92, "review_count": 15632}
    assert rows[1] == {"appid": 20, "review_pct": None, "review_count": 0}


def test_discover_null_total_count_does_not_crash(monkeypatch):
    # Steam sends an explicit null here; "Matched {total:,}" used to raise on it.
    async def fake_raw(url, params, cache_ttl=0):
        return {"total_count": None, "results_html": _review_row(10, 90, 100)}

    async def fake_app_prices(appids, cc):
        return {a: {"name": "A"} for a in appids}

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    monkeypatch.setattr(S, "_app_prices", fake_app_prices)
    text = run(S.steam_discover(S.DiscoverInput(term="x")))
    assert "Matched 1 games" in text          # falls back to the row count


def test_discover_window_honours_requested_sort(monkeypatch):
    # Newest-first from Steam, but the caller asked for best-reviewed first.
    now = time.time()
    html = (_review_row(1)                    # newest, unreviewed shovelware
            + _review_row(2, 75, 4)           # thinly reviewed
            + _review_row(3, 95, 5000))       # the one a human wants first

    async def fake_raw(url, params, cache_ttl=0):
        return {"total_count": 3,
                "results_html": html if int(params.get("start", 0)) == 0 else ""}

    async def fake_app_prices(appids, cc):
        return {a: {"name": f"G{a}", "release_ts": now - a * 86400,
                    "price_cents": 1000} for a in appids}

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    monkeypatch.setattr(S, "_app_prices", fake_app_prices)
    out = run(S.steam_discover(S.DiscoverInput(
        released_within_days=30, sort="reviews", response_format="json")))
    d = json.loads(out)
    # Well-reviewed first, thin second, unreviewed last — not Steam's date order.
    assert [r["appid"] for r in d["results"]] == [3, 2, 1]
    assert d["results"][0]["review_pct"] == 95
    assert d["window_coverage"] == "full"

    out = run(S.steam_discover(S.DiscoverInput(
        released_within_days=30, sort="release", response_format="json")))
    assert [r["appid"] for r in json.loads(out)["results"]] == [1, 2, 3]


def test_discover_window_filters_before_the_limit_slice(monkeypatch):
    # 5 results; only the last 2 are inside the window. Asking for 2 must return
    # both, not "whichever of the first 2 happened to qualify" (which is none).
    now = time.time()
    in_window = {40, 50}

    async def fake_raw(url, params, cache_ttl=0):
        if int(params.get("start", 0)):
            return {"total_count": 5, "results_html": ""}
        return {"total_count": 5,
                "results_html": "".join(_review_row(a, 80, 100)
                                        for a in (10, 20, 30, 40, 50))}

    async def fake_app_prices(appids, cc):
        return {a: {"name": f"G{a}", "price_cents": 100,
                    "release_ts": now - (1 if a in in_window else 400) * 86400}
                for a in appids}

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    monkeypatch.setattr(S, "_app_prices", fake_app_prices)
    out = run(S.steam_discover(S.DiscoverInput(
        released_within_days=30, limit=2, sort="release", response_format="json")))
    d = json.loads(out)
    assert d["count"] == 2
    assert sorted(r["appid"] for r in d["results"]) == [40, 50]


def test_discover_excluded_owned_counts_hidden_not_library(monkeypatch):
    async def fake_raw(url, params, cache_ttl=0):
        return {"total_count": 2,
                "results_html": _review_row(10, 90, 50) + _review_row(20, 90, 50)}

    async def fake_app_prices(appids, cc):
        return {a: {"name": f"G{a}"} for a in appids}

    async def fake_taste(sid, **k):
        # A 900-game library, but only appid 10 is among the results.
        return {"owned_ids": {10} | set(range(1000, 1899)),
                "tag_ids": [], "tag_names": [], "seed_games": ["X"]}

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    monkeypatch.setattr(S, "_app_prices", fake_app_prices)
    monkeypatch.setattr(S, "_taste_profile", fake_taste)
    monkeypatch.setattr(S, "_resolve_steamid", lambda x=None: _ident())
    out = run(S.steam_discover(S.DiscoverInput(
        steamid="76561197960287930", response_format="json")))
    d = json.loads(out)
    assert d["excluded_owned"] == 1          # not 900
    assert [r["appid"] for r in d["results"]] == [20]


async def _ident():
    return "76561197960287930"


def test_discover_explicit_tags(monkeypatch):
    captured = {}

    async def fake_map():
        return {29482: "Souls-like"}

    async def fake_raw(url, params, cache_ttl=0):
        captured.update(params)
        return {"success": 1, "total_count": 1,
                "results_html": '<a data-ds-appid="5"></a>'}

    async def fake_app_prices(appids, cc):
        return {a: {"name": "G"} for a in appids}

    monkeypatch.setattr(S, "_tag_name_map", fake_map)
    monkeypatch.setattr(S, "_raw_get", fake_raw)
    monkeypatch.setattr(S, "_app_prices", fake_app_prices)
    out = run(S.steam_discover(S.DiscoverInput(
        tags=["Souls-like"], max_price=20, on_sale=True, platform="win",
        response_format="json")))
    d = json.loads(out)
    assert d["filters"]["resolved_tag_ids"] == [29482]
    assert captured["tags"] == "29482"        # name -> id mapped into the query
    assert captured["maxprice"] == "20"
    assert captured["specials"] == 1
    assert captured["os"] == "win"


def test_discover_personalized(monkeypatch):
    async def fake_steam(path, params, **k):
        if "GetOwnedGames" in path:
            return {"response": {"games": [
                {"appid": 10, "name": "Hades", "playtime_forever": 6000},
                {"appid": 99, "name": "Owned Thing", "playtime_forever": 100}]}}
        if "GetRecentlyPlayedGames" in path:
            return {"response": {"games": [
                {"appid": 10, "name": "Hades", "playtime_2weeks": 300}]}}
        if "GetItems" in path:
            return {"response": {"store_items": [
                {"appid": 10, "tags": [{"tagid": 1716, "weight": 100},
                                       {"tagid": 4231, "weight": 50}]}]}}
        return {}

    async def fake_map():
        return {1716: "Roguelike", 4231: "Action RPG"}

    async def fake_raw(url, params, cache_ttl=0):
        # search returns three apps, one of which (99) the user owns
        return {"success": 1, "total_count": 50,
                "results_html": '<a data-ds-appid="20"></a>'
                                '<a data-ds-appid="99"></a><a data-ds-appid="30"></a>'}

    prices = {20: {"name": "New A"}, 30: {"name": "New B"}}

    async def fake_app_prices(appids, cc):
        return {a: prices.get(a, {}) for a in appids}

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    monkeypatch.setattr(S, "_tag_name_map", fake_map)
    monkeypatch.setattr(S, "_raw_get", fake_raw)
    monkeypatch.setattr(S, "_app_prices", fake_app_prices)
    out = run(S.steam_discover(S.DiscoverInput(
        steamid="76561197960287930", response_format="json")))
    d = json.loads(out)
    assert d["personalized"] is True
    assert d["taste_tags"] == ["Roguelike", "Action RPG"]   # derived from Hades
    assert "Hades" in d["seed_games"]
    assert d["filters"]["resolved_tag_ids"] == [1716, 4231]  # seeded from taste
    assert 99 not in [r["appid"] for r in d["results"]]     # owned game excluded
    assert [r["appid"] for r in d["results"]] == [20, 30]


# --------------------------------------------------------------------------- #
# 0.10.0: intelligence tools (should-i-buy, recommend)
# --------------------------------------------------------------------------- #

def test_should_i_buy(monkeypatch):
    import time as _t
    now = int(_t.time())

    async def fake_store(path, params, cache_ttl=0):
        return {"5": {"success": True, "data": {
            "name": "Game5", "is_free": False,
            "price_overview": {"final_formatted": "$20", "initial_formatted": "$40",
                               "discount_percent": 50},
            "genres": [{"description": "Action"}],
            "release_date": {"date": "2022", "coming_soon": False},
            "metacritic": {"score": 88}}}}

    async def fake_raw(url, params, cache_ttl=0):
        if "start_date" in params:  # refuse the windowed summary: use the scan
            return {"success": 2}
        if params.get("filter") == "recent":
            return {"success": 1, "cursor": "*", "reviews": [
                {"voted_up": True, "timestamp_created": now - 10},
                {"voted_up": True, "timestamp_created": now - 20},
                {"voted_up": False, "timestamp_created": now - 30},
                {"voted_up": True, "timestamp_created": now - 99 * 86400}]}  # old -> stop
        return {"success": 1, "query_summary": {
            "review_score_desc": "Very Positive", "total_positive": 900,
            "total_negative": 100, "total_reviews": 1000}}

    async def fake_items(appids):
        return {5: [{"tagid": 1, "weight": 10}, {"tagid": 2, "weight": 5}]}

    async def fake_map():
        return {1: "Action", 2: "Indie"}

    monkeypatch.setattr(S, "_store_get", fake_store)
    monkeypatch.setattr(S, "_raw_get", fake_raw)
    monkeypatch.setattr(S, "_items_tags", fake_items)
    monkeypatch.setattr(S, "_tag_name_map", fake_map)
    out = run(S.steam_should_i_buy(S.ShouldIBuyInput(appid=5, response_format="json")))
    d = json.loads(out)
    assert d["name"] == "Game5"
    assert d["discount_pct"] == 50
    assert d["review_lifetime"]["positive_pct"] == 90.0
    assert d["review_recent_30d"]["positive_pct"] == 66.7   # 2 of 3 in-window
    assert d["review_recent_30d"]["reviews_counted"] == 3
    assert d["review_trend_pts"] == round(66.7 - 90.0, 1)
    assert d["top_tags"] == ["Action", "Indie"]
    assert d["personal"] is None                            # no steamid


def test_recommend_anchors_on_seed_when_steamid_also_given(monkeypatch):
    """seed_appid + steamid is "games like X that I don't own".

    The steamid is required for the ownership exclusion, so it must not also
    hijack the basis — the old precedence (tags > taste > seed) silently
    recommended from the user's most-played genres instead of from X.
    """
    async def fake_map():
        return {1: "Roguelike", 2: "Action", 9: "Farming"}

    async def fake_items(appids):
        m = {100: [{"tagid": 1, "weight": 99}],      # the seed game: Roguelike
             20: [{"tagid": 1, "weight": 10}],
             30: [{"tagid": 1, "weight": 8}]}
        return {a: m.get(a, []) for a in appids}

    async def fake_taste(sid, **k):
        # Taste says Farming — nothing like the seed game.
        return {"owned_ids": {30}, "tag_ids": [9], "tag_names": ["Farming"],
                "seed_games": ["Stardew"]}

    async def fake_discover(query):
        return [20, 30], 2

    async def fake_app_price(a, cc):
        return {"name": f"G{a}"}

    async def fake_app_prices(appids, cc):
        return {a: {"name": f"G{a}"} for a in appids}

    monkeypatch.setattr(S, "_tag_name_map", fake_map)
    monkeypatch.setattr(S, "_items_tags", fake_items)
    monkeypatch.setattr(S, "_taste_profile", fake_taste)
    monkeypatch.setattr(S, "_resolve_steamid", lambda x=None: _ident())
    monkeypatch.setattr(S, "_discover_appids", fake_discover)
    monkeypatch.setattr(S, "_app_price", fake_app_price)
    monkeypatch.setattr(S, "_app_prices", fake_app_prices)
    out = run(S.steam_recommend(S.RecommendInput(
        seed_appid=100, steamid="76561197960287930", response_format="json")))
    d = json.loads(out)
    assert d["basis"] == "like G100"          # the seed, NOT "your taste (Stardew)"
    ids = [r["appid"] for r in d["recommendations"]]
    assert 30 not in ids                      # the steamid still excludes owned
    assert ids == [20]
    assert d["excluded_owned"] == 1           # candidates hidden, not library size


def test_recommend_taste_still_used_when_no_seed_or_tags(monkeypatch):
    async def fake_map():
        return {9: "Farming"}

    async def fake_items(appids):
        return {a: [{"tagid": 9, "weight": 5}] for a in appids}

    async def fake_taste(sid, **k):
        return {"owned_ids": set(), "tag_ids": [9], "tag_names": ["Farming"],
                "seed_games": ["Stardew"]}

    async def fake_discover(query):
        return [20], 1

    async def fake_app_prices(appids, cc):
        return {a: {"name": f"G{a}"} for a in appids}

    monkeypatch.setattr(S, "_tag_name_map", fake_map)
    monkeypatch.setattr(S, "_items_tags", fake_items)
    monkeypatch.setattr(S, "_taste_profile", fake_taste)
    monkeypatch.setattr(S, "_resolve_steamid", lambda x=None: _ident())
    monkeypatch.setattr(S, "_discover_appids", fake_discover)
    monkeypatch.setattr(S, "_app_prices", fake_app_prices)
    out = run(S.steam_recommend(S.RecommendInput(
        steamid="76561197960287930", response_format="json")))
    assert json.loads(out)["basis"] == "your taste (Stardew)"


def test_recommend_seed(monkeypatch):
    async def fake_map():
        return {1: "Roguelike", 2: "Action", 3: "Co-op"}

    async def fake_items(appids):
        m = {100: [{"tagid": 1, "weight": 99}, {"tagid": 2, "weight": 50},
                   {"tagid": 3, "weight": 20}],
             20: [{"tagid": 1, "weight": 10}, {"tagid": 2, "weight": 5}],   # shares 1,2
             30: [{"tagid": 1, "weight": 8}],                               # shares 1
             40: [{"tagid": 1, "weight": 7}, {"tagid": 2, "weight": 3},
                  {"tagid": 3, "weight": 2}]}                               # shares 1,2,3
        return {a: m.get(a, []) for a in appids}

    async def fake_discover(query):
        return [20, 30, 40], 3

    async def fake_app_price(a, cc):
        return {"name": f"G{a}"}

    async def fake_app_prices(appids, cc):
        return {a: {"name": f"G{a}"} for a in appids}

    monkeypatch.setattr(S, "_tag_name_map", fake_map)
    monkeypatch.setattr(S, "_items_tags", fake_items)
    monkeypatch.setattr(S, "_discover_appids", fake_discover)
    monkeypatch.setattr(S, "_app_price", fake_app_price)
    monkeypatch.setattr(S, "_app_prices", fake_app_prices)
    out = run(S.steam_recommend(S.RecommendInput(seed_appid=100, response_format="json")))
    d = json.loads(out)
    assert d["basis"] == "like G100"
    ids = [r["appid"] for r in d["recommendations"]]
    assert 100 not in ids                       # seed excluded
    assert ids[0] == 40                          # most shared tags (3) ranked first
    assert d["recommendations"][0]["matching_tags"] == ["Roguelike", "Action", "Co-op"]


def test_recommend_requires_basis():
    out = run(S.steam_recommend(S.RecommendInput()))
    assert "Provide a basis" in out


# --------------------------------------------------------------------------- #
# 0.11.0: co-op night planner
# --------------------------------------------------------------------------- #

def test_plan_coop_night(monkeypatch):
    host = "76561197960000001"
    alice, bob, carol, dave = "2", "3", "4", "5"

    async def fake_steam(path, params, **k):
        if "GetFriendList" in path:
            return {"friendslist": {"friends": [
                {"steamid": alice}, {"steamid": bob},
                {"steamid": carol}, {"steamid": dave}]}}
        if "GetPlayerSummaries" in path:
            names = {alice: "Alice", bob: "Bob", carol: "Carol", dave: "Dave"}
            states = {alice: 1, bob: 1, carol: 0, dave: 1}   # carol offline
            return {"response": {"players": [
                {"steamid": i, "personaname": names.get(i, "?"),
                 "personastate": states.get(i, 0)}
                for i in params["steamids"].split(",")]}}
        if "GetOwnedGames" in path:
            libs = {host: [10, 20, 30], alice: [10, 20], bob: [10], dave: None}
            ap = libs.get(params["steamid"])
            if ap is None:
                return {"response": {}}                       # dave: private
            return {"response": {"game_count": len(ap),
                                 "games": [{"appid": a} for a in ap]}}
        if "GetItems" in path:
            ij = json.loads(params["input_json"])
            meta = {10: ("Co-op A", [9, 38]), 20: ("Co-op B", [24]),
                    30: ("Solo", [2])}
            return {"response": {"store_items": [
                {"appid": x["appid"], "name": meta[x["appid"]][0],
                 "categories": {"supported_player_categoryids": meta[x["appid"]][1]}}
                for x in ij["ids"] if x["appid"] in meta]}}
        return {}

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    out = run(S.steam_plan_coop_night(
        S.PlanCoopNightInput(steamid=host, response_format="json")))
    d = json.loads(out)
    # online group = Alice, Bob, Dave (Carol offline). Dave's library is private.
    assert d["private_or_unknown"] == 1                       # Dave skipped
    assert d["checked"] == 2                                  # Alice + Bob
    # host owns {10,20,30}; Alice {10,20}, Bob {10} -> 10 owned by 2, 20 by 1
    # 30 is solo (filtered out); 10 & 20 are co-op
    names = {g["name"]: g["owner_count"] for g in d["games"]}
    assert names == {"Co-op A": 2, "Co-op B": 1}
    assert d["games"][0]["name"] == "Co-op A"                 # most-owned first
    assert d["games"][0]["owners"] == ["Alice", "Bob"]
    assert set(d["online_now"]) == {"Alice", "Bob"}


# --------------------------------------------------------------------------- #
# 0.12.0: prompts, resources, localization
# --------------------------------------------------------------------------- #

def test_prompts_registered():
    names = {p.name for p in run(S.mcp.list_prompts())}
    assert {"what_should_i_play", "is_it_worth_buying", "plan_game_night",
            "steam_deals", "game_overview"} <= names


def test_prompt_renders():
    res = run(S.mcp.get_prompt("plan_game_night", {"steamid": "123"}))
    text = " ".join(getattr(m.content, "text", str(m.content)) for m in res.messages)
    assert "steam_plan_coop_night" in text and "123" in text


def _wire(model):
    """Dump an SDK model to its on-the-wire (camelCase) shape.

    SDK v2 renamed every model attribute to snake_case (`input_schema`,
    `uri_template`) while keeping the JSON camelCase; v1 named the attributes
    camelCase directly. Dumping by alias is the one spelling both agree on.
    """
    return model.model_dump(by_alias=True)


def test_resources_registered():
    uris = {_wire(t)["uriTemplate"] for t in run(S.mcp.list_resource_templates())}
    assert "steam://app/{appid}" in uris
    assert "steam://user/{steamid}" in uris


def test_resource_app_reads(monkeypatch):
    async def fake_store(path, params, cache_ttl=0):
        return {"570": {"success": True,
                        "data": {"name": "Dota 2", "type": "game", "is_free": True}}}

    monkeypatch.setattr(S, "_store_get", fake_store)
    parts = list(run(S.mcp.read_resource("steam://app/570")))
    text = " ".join(str(getattr(p, "content", p)) for p in parts)
    assert "Dota 2" in text


def test_app_details_language(monkeypatch):
    seen = []

    async def fake_store(path, params, cache_ttl=0):
        seen.append(params.get("l"))
        return {"5": {"success": True, "data": {"name": "G", "type": "game"}}}

    monkeypatch.setattr(S, "_store_get", fake_store)
    run(S.steam_get_app_details(S.AppDetailsInput(appid=5, language="french")))
    # The caller's own request is in their language; the follow-up that reads
    # the fields Steam localizes away is deliberately English.
    assert seen == ["french", "english"]


# Feature flags match English category names. A localized response must not turn
# them all off, and the categories the caller sees must stay in their language.
LOCALIZED_CATS = [
    {"id": 2, "description": "Un joueur"},
    {"id": 9, "description": "Coop"},
    {"id": 38, "description": "Coop en ligne"},
    {"id": 23, "description": "Steam Cloud (nuage)"},
]
ENGLISH_CATS = [
    {"id": 2, "description": "Single-player"},
    {"id": 9, "description": "Co-op"},
    {"id": 38, "description": "Online Co-op"},
    {"id": 23, "description": "Steam Cloud"},
]
# A non-Latin script exercises the same path with nothing an English substring
# match could latch onto by accident, and round-trips multi-byte text through
# the response. Steam's language name for Korean is "koreana", not "korean".
KOREAN_CATS = [
    {"id": 2, "description": "싱글 플레이어"},
    {"id": 9, "description": "협동"},
    {"id": 38, "description": "온라인 협동"},
    {"id": 23, "description": "Steam 클라우드"},
]


async def _none():
    return None


def _app_details_store(calls, english_cats=ENGLISH_CATS,
                       localized_cats=LOCALIZED_CATS):
    async def fake_store(path, params, cache_ttl=0):
        calls.append(params.get("l"))
        cats = english_cats if params.get("l") == "english" else localized_cats
        return {"5": {"success": True,
                      "data": {"name": "G", "type": "game", "categories": cats}}}
    return fake_store


def test_app_details_features_survive_a_localized_response(monkeypatch):
    calls = []
    monkeypatch.setattr(S, "_store_get", _app_details_store(calls))
    monkeypatch.setattr(S, "_deck_compat", lambda *a, **k: _none())
    out = run(S.steam_get_app_details(
        S.AppDetailsInput(appid=5, language="french", response_format="json")))
    d = json.loads(out)

    assert d["features"]["is_singleplayer"] is True
    assert d["features"]["is_coop"] is True
    assert d["features"]["is_online_coop"] is True
    assert d["features"]["has_cloud_saves"] is True
    # Display stays in the caller's language.
    assert d["categories"] == [c["description"] for c in LOCALIZED_CATS]
    assert calls == ["french", "english"]

    # Same for the markdown play-mode line: detected in English, shown localized.
    calls.clear()
    text = run(S.steam_get_app_details(
        S.AppDetailsInput(appid=5, language="french")))
    assert "Un joueur, Coop, Coop en ligne" in text
    assert "Steam Cloud (nuage)" not in text     # a feature, not a play mode


def test_app_details_features_survive_a_non_latin_response(monkeypatch):
    """Same defect in a non-Latin script, where no English substring could match
    by accident. Verified live against CS2 / Stardew Valley / Elden Ring in
    koreana, japanese, russian, thai and tchinese."""
    calls = []
    monkeypatch.setattr(S, "_store_get",
                        _app_details_store(calls, localized_cats=KOREAN_CATS))
    monkeypatch.setattr(S, "_deck_compat", lambda *a, **k: _none())
    out = run(S.steam_get_app_details(
        S.AppDetailsInput(appid=5, language="koreana", response_format="json")))
    d = json.loads(out)

    assert d["features"]["is_singleplayer"] is True
    assert d["features"]["is_coop"] is True
    assert d["features"]["is_online_coop"] is True
    assert d["features"]["has_cloud_saves"] is True
    # Hangul comes back intact, not escaped or transliterated.
    assert d["categories"] == [c["description"] for c in KOREAN_CATS]
    assert calls == ["koreana", "english"]

    calls.clear()
    text = run(S.steam_get_app_details(
        S.AppDetailsInput(appid=5, language="koreana")))
    assert "싱글 플레이어, 협동, 온라인 협동" in text
    assert "Steam 클라우드" not in text   # a feature, not a play mode


def test_app_details_features_skip_the_english_lookup_for_english(monkeypatch):
    calls = []
    monkeypatch.setattr(S, "_store_get", _app_details_store(calls))
    monkeypatch.setattr(S, "_deck_compat", lambda *a, **k: _none())
    out = run(S.steam_get_app_details(
        S.AppDetailsInput(appid=5, response_format="json")))
    assert json.loads(out)["features"]["is_coop"] is True
    assert calls == ["english"]        # no second round trip


def test_app_details_survives_a_failed_english_lookup(monkeypatch):
    async def fake_store(path, params, cache_ttl=0):
        if params.get("l") == "english":
            raise RuntimeError("upstream down")
        return {"5": {"success": True, "data": {
            "name": "G", "type": "game", "categories": LOCALIZED_CATS}}}

    monkeypatch.setattr(S, "_store_get", fake_store)
    monkeypatch.setattr(S, "_deck_compat", lambda *a, **k: _none())
    out = run(S.steam_get_app_details(
        S.AppDetailsInput(appid=5, language="french", response_format="json")))
    d = json.loads(out)
    # Best-effort: the tool still answers, with the pre-fix detection quality.
    assert d["name"] == "G"
    assert d["categories"] == [c["description"] for c in LOCALIZED_CATS]


def _achievement_store(calls, localized_cats, english_cats, total=42):
    """appdetails where only the English payload carries the achievement count.

    Verified live 2026-09: CS2, TF2 and Elden Ring all report
    `achievements.total` as 0 in German while English reports 1 / 520 / 42.
    """
    async def fake_store(path, params, cache_ttl=0):
        english = params.get("l") == "english"
        calls.append(params.get("l"))
        return {"5": {"success": True, "data": {
            "name": "G", "type": "game",
            "categories": english_cats if english else localized_cats,
            "achievements": {"total": total if english else 0},
        }}}
    return fake_store


def test_app_details_reads_the_achievement_count_in_english(monkeypatch):
    """An app with achievements but no "Steam Achievements" category (CS2's
    shape) has only the count to go on — and the count is 0 in any language."""
    calls = []
    monkeypatch.setattr(S, "_store_get", _achievement_store(
        calls,
        [{"id": 1, "description": "Mehrspieler"}],
        [{"id": 1, "description": "Multi-player"}],
    ))
    monkeypatch.setattr(S, "_deck_compat", lambda *a, **k: _none())
    out = run(S.steam_get_app_details(
        S.AppDetailsInput(appid=5, language="german", response_format="json")))
    d = json.loads(out)
    assert d["achievements_total"] == 42
    assert d["features"]["has_achievements"] is True
    assert calls == ["german", "english"]


def test_app_details_keeps_a_genuine_zero_for_english(monkeypatch):
    """The English payload is authoritative for an English caller: no second
    lookup, and an app that really has no achievements still says so."""
    calls = []
    monkeypatch.setattr(S, "_store_get", _achievement_store(
        calls, LOCALIZED_CATS, ENGLISH_CATS, total=0))
    monkeypatch.setattr(S, "_deck_compat", lambda *a, **k: _none())
    out = run(S.steam_get_app_details(
        S.AppDetailsInput(appid=5, response_format="json")))
    d = json.loads(out)
    assert d["achievements_total"] == 0
    assert d["features"]["has_achievements"] is False
    assert calls == ["english"]


def test_app_details_looks_up_english_without_categories(monkeypatch):
    """No categories is not a reason to skip the lookup: the achievement count
    still needs it."""
    calls = []
    monkeypatch.setattr(S, "_store_get", _achievement_store(calls, [], []))
    monkeypatch.setattr(S, "_deck_compat", lambda *a, **k: _none())
    out = run(S.steam_get_app_details(
        S.AppDetailsInput(appid=5, language="german", response_format="json")))
    assert json.loads(out)["achievements_total"] == 42
    assert calls == ["german", "english"]


def test_app_reviews_language(monkeypatch):
    captured = {}

    async def fake_raw(url, params, cache_ttl=0):
        captured.update(params)
        return {"success": 1, "reviews": [], "query_summary": {
            "review_score_desc": "x", "total_positive": 1,
            "total_negative": 0, "total_reviews": 1}}

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    run(S.steam_get_app_reviews(S.AppReviewsInput(appid=1, language="german")))
    assert captured.get("language") == "german"


# --------------------------------------------------------------------------- #
# 1.0.0: retry / backoff on transient failures
# --------------------------------------------------------------------------- #

class _RetryResp:
    def __init__(self, code):
        self.status_code = code
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return {"ok": True}


def test_get_with_retry_succeeds_after_429(monkeypatch):
    S._CACHE.clear()
    calls = {"n": 0}

    class FakeClient:
        async def get(self, *a, **k):
            calls["n"] += 1
            return _RetryResp(429 if calls["n"] == 1 else 200)

    async def no_sleep(*a, **k):
        return None

    monkeypatch.setattr(S, "_http_client", lambda: FakeClient())
    monkeypatch.setattr(S.asyncio, "sleep", no_sleep)
    data = run(S._raw_get("https://api.steampowered.com/x", {}))
    assert data == {"ok": True}
    assert calls["n"] == 2          # one 429 -> retried once, then 200


def test_get_with_retry_gives_up(monkeypatch):
    calls = {"n": 0}

    class FakeClient:
        async def get(self, *a, **k):
            calls["n"] += 1
            return _RetryResp(503)   # always failing

    async def no_sleep(*a, **k):
        return None

    monkeypatch.setattr(S, "_http_client", lambda: FakeClient())
    monkeypatch.setattr(S.asyncio, "sleep", no_sleep)
    with pytest.raises(RuntimeError):
        run(S._raw_get("https://api.steampowered.com/x", {}))
    assert calls["n"] == S.MAX_RETRIES + 1   # initial try + MAX_RETRIES


# --------------------------------------------------------------------------- #
# 1.1.0: regional pricing, workshop items, user groups
# --------------------------------------------------------------------------- #

def test_regional_pricing(monkeypatch):
    prices = {
        "us": {"name": "G", "price": "$10", "discount_pct": 0, "on_sale": False},
        "de": {"name": "G", "price": "9,99€", "discount_pct": 0, "on_sale": False},
        "br": {"name": "G", "price": "R$ 50", "discount_pct": 50, "on_sale": True},
    }

    async def fake_app_price(appid, cc):
        return prices[cc]

    monkeypatch.setattr(S, "_app_price", fake_app_price)
    out = run(S.steam_get_app_regional_pricing(S.RegionalPricingInput(
        appid=5, countries=["us", "de", "br"], response_format="json")))
    d = json.loads(out)
    assert d["name"] == "G"
    assert {p["country"]: p["price"] for p in d["prices"]} == {
        "us": "$10", "de": "9,99€", "br": "R$ 50"}
    assert [p for p in d["prices"] if p["country"] == "br"][0]["on_sale"] is True


def test_workshop_item(monkeypatch):
    async def fake_post(path, data, **k):
        assert "GetPublishedFileDetails" in path
        return {"response": {"publishedfiledetails": [{
            "result": 1, "title": "Move It", "consumer_app_id": 255710,
            "creator": "123", "description": "<b>A mod</b>",
            "tags": [{"tag": "Mod"}], "subscriptions": 3432587,
            "lifetime_subscriptions": 9000000, "favorited": 159078,
            "views": 2772323, "file_size": 1215506,
            "time_created": 1547052076, "time_updated": 0, "banned": 0}]}}

    monkeypatch.setattr(S, "_steam_post", fake_post)
    out = run(S.steam_get_workshop_item(
        S.WorkshopItemInput(published_file_id=1619685021, response_format="json")))
    d = json.loads(out)
    assert d["title"] == "Move It" and d["app_id"] == 255710
    assert d["subscriptions"] == 3432587 and d["favorited"] == 159078
    assert d["tags"] == ["Mod"]
    assert d["description"] == "A mod"          # HTML stripped
    assert d["created"].startswith("2019-01")   # ts_to_date(1547052076)


def test_workshop_item_not_found(monkeypatch):
    async def fake_post(path, data, **k):
        return {"response": {"publishedfiledetails": [{"result": 9}]}}

    monkeypatch.setattr(S, "_steam_post", fake_post)
    out = run(S.steam_get_workshop_item(S.WorkshopItemInput(published_file_id=1)))
    assert "No Workshop item found" in out


def test_user_groups(monkeypatch):
    async def fake_steam(path, params, **k):
        return {"response": {"success": True,
                             "groups": [{"gid": "4"}, {"gid": "5"}]}}

    xmls = {
        "4": "<memberList><groupDetails><groupName><![CDATA[Valve]]></groupName>"
             "<groupURL><![CDATA[Valve]]></groupURL>"
             "<memberCount>152</memberCount></groupDetails></memberList>",
        "5": "<memberList><groupDetails><groupName><![CDATA[Steam Universe]]>"
             "</groupName><groupURL><![CDATA[SteamUniverse]]></groupURL>"
             "<memberCount>5000000</memberCount></groupDetails></memberList>",
    }

    async def fake_text(url, params=None, cache_ttl=0):
        gid = url.split("/gid/")[1].split("/")[0]
        return xmls[gid]

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    monkeypatch.setattr(S, "_raw_get_text", fake_text)
    out = run(S.steam_get_user_groups(
        S.UserGroupsInput(steamid="76561197960287930", response_format="json")))
    d = json.loads(out)
    assert d["total"] == 2 and d["count"] == 2
    # sorted by member_count desc -> Steam Universe first
    assert d["groups"][0]["name"] == "Steam Universe"
    assert d["groups"][0]["member_count"] == 5000000
    assert d["groups"][0]["url"] == "https://steamcommunity.com/groups/SteamUniverse"
    assert d["groups"][1]["name"] == "Valve"


def test_inventory(monkeypatch):
    captured = {}

    async def fake_raw(url, params, cache_ttl=0):
        captured["url"] = url
        return {"success": 1, "total_inventory_count": 503,
                "assets": [
                    {"classid": "a", "instanceid": "0", "amount": "2"},
                    {"classid": "a", "instanceid": "0", "amount": "1"},
                    {"classid": "b", "instanceid": "0", "amount": "1"}],
                "descriptions": [
                    {"classid": "a", "instanceid": "0", "market_name": "Booster Pack",
                     "type": "Booster Pack", "tradable": 1, "marketable": 1},
                    {"classid": "b", "instanceid": "0", "market_name": "Emoticon",
                     "type": "Emoticon", "tradable": 1, "marketable": 0}]}

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    out = run(S.steam_get_inventory(
        S.InventoryInput(steamid="76561197960287930", response_format="json")))
    d = json.loads(out)
    assert captured["url"].endswith("/753/6")        # default app -> context auto 6
    assert d["total_inventory_count"] == 503
    items = {i["name"]: i for i in d["items"]}
    assert items["Booster Pack"]["count"] == 3        # 2 + 1 aggregated
    assert items["Booster Pack"]["marketable"] is True
    assert items["Emoticon"]["marketable"] is False
    assert d["items"][0]["name"] == "Booster Pack"    # most-numerous first

    # a game appid auto-picks context 2
    run(S.steam_get_inventory(S.InventoryInput(steamid="76561197960287930", appid=730)))
    assert captured["url"].endswith("/730/2")


def test_inventory_private(monkeypatch):
    async def fake_raw(url, params, cache_ttl=0):
        return None                                   # community endpoint: private/empty

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    out = run(S.steam_get_inventory(S.InventoryInput(steamid="76561197960287930")))
    # Privacy-aware message: names the Inventory setting + points to the fix.
    assert "Inventory" in out and "public" in out.lower()
    assert "steamcommunity.com/my/edit/settings" in out


def test_privacy_hint():
    h = S._privacy_hint("Game details")
    assert "Game details" in h
    assert S.PRIVACY_SETTINGS_URL in h
    assert "public" in h.lower()


def test_parse_cs_attributes():
    a = S._parse_cs_attributes("StatTrak™ AK-47 | Redline (Field-Tested)")
    assert a["exterior"] == "Field-Tested" and a["stattrak"] is True
    assert a["souvenir"] is False and a["star"] is False
    b = S._parse_cs_attributes("Souvenir AWP | Dragon Lore (Factory New)")
    assert b["exterior"] == "Factory New" and b["souvenir"] is True
    c = S._parse_cs_attributes("★ Karambit | Doppler (Minimal Wear)")
    assert c["star"] is True and c["exterior"] == "Minimal Wear"
    d = S._parse_cs_attributes("Mann Co. Supply Crate Key")
    assert d["exterior"] is None and d["stattrak"] is False


def test_market_price(monkeypatch):
    async def fake_raw(url, params, cache_ttl=0):
        if "priceoverview" in url:
            return {"success": True, "lowest_price": "$40.98",
                    "median_price": "$42.74", "volume": "97"}
        if "search/render" in url:
            return {"success": 1, "results": [
                {"name": "AK-47 | Redline (Field-Tested)",
                 "hash_name": "AK-47 | Redline (Field-Tested)",
                 "sell_listings": 1124, "sell_price_text": "$40.98",
                 "asset_description": {"type": "Classified Rifle"}}]}
        return {}

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    out = run(S.steam_get_market_price(S.MarketPriceInput(
        appid=730, market_hash_name="AK-47 | Redline (Field-Tested)",
        response_format="json")))
    d = json.loads(out)
    assert d["available"] is True
    assert d["lowest_price"] == "$40.98" and d["median_price"] == "$42.74"
    assert d["volume_24h"] == "97" and d["listings"] == 1124
    assert d["type"] == "Classified Rifle"
    assert d["attributes"]["exterior"] == "Field-Tested"


def test_market_price_unavailable(monkeypatch):
    async def fake_raw(url, params, cache_ttl=0):
        return {"success": True}      # priceoverview with no listings; search empty

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    out = run(S.steam_get_market_price(S.MarketPriceInput(
        appid=730, market_hash_name="Nonexistent Item")))
    assert "no current" in out.lower()


# --------------------------------------------------------------------------- #
# 1.4.0: token efficiency + security hardening
# --------------------------------------------------------------------------- #

def test_descriptions_compact():
    # The model pays for tool descriptions every request; they must be one-line
    # summaries, not the full multi-paragraph docstrings.
    tools = run(S.mcp.list_tools())
    for t in tools:
        assert "\n\n" not in (t.description or ""), t.name
    total = sum(len(t.description or "") for t in tools)
    assert total < 6000        # full docstrings were ~20k chars


def test_check_host_allowlist():
    for ok in ("https://api.steampowered.com/x",
               "https://store.steampowered.com/api/y",
               "https://steamcommunity.com/inventory/1/753/6"):
        S._check_host(ok)      # no raise
    for bad in ("https://evil.example.com/x",
                "https://api.steampowered.com.evil.com/x",
                "http://169.254.169.254/latest/meta-data"):
        with pytest.raises(S.SteamApiError):
            S._check_host(bad)


def test_scrub_api_key():
    key = "0123456789abcdef0123456789ABCDEF"
    out = S._scrub(f"GET https://api.steampowered.com/x?key={key}&appid=1 failed")
    assert key not in out and "key=***" in out


def test_enforce_host_hook_blocks_redirect_target():
    # The client follows redirects, so the allowlist must be enforced per-hop.
    run(S._enforce_host(S.httpx2.Request(
        "GET", "https://api.steampowered.com/x?key=secret")))  # allowed: no raise
    for bad in ("http://169.254.169.254/latest/meta-data",  # cloud metadata
                "https://evil.example.com/"):
        with pytest.raises(S.SteamApiError):
            run(S._enforce_host(S.httpx2.Request("GET", bad)))


def test_handle_error_scrubs_key_from_steamapi_error():
    key = "0123456789abcdef0123456789abcdef"
    msg = S._handle_error(S.SteamApiError(f"boom key={key} happened"))
    assert key not in msg and "key=***" in msg


def test_redos_inputs_are_bounded():
    # Inputs are length-capped before the O(n^2) regexes, so pathological strings
    # finish near-instantly instead of stalling the event loop for seconds.
    import time as _t
    cases = [
        lambda: S._strip_html("<" * 200_000, limit=2000),
        lambda: S._parse_cs_attributes("(" * 200_000),
        lambda: S._is_temp_client(("a." * 5000) + " demoX"),
    ]
    for fn in cases:
        t0 = _t.perf_counter()
        fn()
        assert _t.perf_counter() - t0 < 2.0
    # Capping doesn't change normal results.
    assert S._strip_html("<b>Hi</b> there") == "Hi there"
    assert S._parse_cs_attributes(
        "StatTrak™ AK-47 | Redline (Field-Tested)")["exterior"] == "Field-Tested"


def test_resolve_profiles_url_requires_steamid64():
    sid = "76561197960287930"
    assert run(S._resolve_steamid(
        f"https://steamcommunity.com/profiles/{sid}")) == sid
    # A malformed /profiles/ segment is rejected before any network call.
    with pytest.raises(S.SteamApiError):
        run(S._resolve_steamid(
            "https://steamcommunity.com/profiles/x@169.254.169.254"))


def test_rate_limiter_bucket(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(S.asyncio, "sleep", fake_sleep)

    async def go():
        b = S._Bucket(rate=10.0, burst=2)
        await b.take()
        await b.take()              # within burst -> no wait
        burst_sleeps = len(slept)
        await b.take()              # over budget -> must wait
        return burst_sleeps, len(slept)

    burst_sleeps, after = run(go())
    assert burst_sleeps == 0
    assert after == 1


def test_http_logging_silenced():
    # The API key rides in request URLs (?key=); the HTTP stack logs those at
    # INFO, so importing the server must have quieted them to keep the key out of
    # logs. httpx2/httpcore2 are the ones our own client writes to — a migration
    # that renamed the library without renaming these would silently leak the key.
    for name in ("httpx2", "httpcore2", "httpx", "httpcore"):
        assert logging.getLogger(name).level == logging.WARNING, name


# --------------------------------------------------------------------------- #
# Tool logic with mocked HTTP
# --------------------------------------------------------------------------- #

def test_analyze_library(monkeypatch):
    payload = {"response": {"game_count": 3, "games": [
        {"appid": 1, "name": "A", "playtime_forever": 6000, "rtime_last_played": 1700000000},
        {"appid": 2, "name": "B", "playtime_forever": 0, "rtime_last_played": 0},
        {"appid": 3, "name": "C", "playtime_forever": 30, "playtime_2weeks": 30,
         "rtime_last_played": 1780000000},
    ]}}

    async def fake_steam(path, params, **k):
        return payload

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    out = run(S.steam_analyze_library(
        S.LibraryAnalysisInput(steamid="76561197960287930", response_format="json")))
    d = json.loads(out)
    assert d["summary"]["game_count"] == 3
    assert d["summary"]["never_played_count"] == 1
    assert d["top_played"][0]["name"] == "A"             # 100h, most played
    assert d["recently_played"][0]["name"] == "C"
    assert d["playtime_buckets"]["0h"] == 1


def test_analyze_library_backlog_truncation(monkeypatch):
    # 5 never-played games, alphabetical A..E; a small backlog_limit must flag
    # truncation and only show the early letters (the bug the chat surfaced).
    games = [
        {"appid": i, "name": ch, "playtime_forever": 0, "rtime_last_played": 0}
        for i, ch in enumerate("ABCDE", start=1)
    ]
    payload = {"response": {"game_count": 5, "games": games}}

    async def fake_steam(path, params, **k):
        return payload

    monkeypatch.setattr(S, "_steam_get", fake_steam)

    # Truncated: ask for 3 of 5 -> alphabetical slice A,B,C + truncation flag.
    out = run(S.steam_analyze_library(S.LibraryAnalysisInput(
        steamid="76561197960287930", backlog_limit=3, response_format="json")))
    d = json.loads(out)
    assert d["backlog_truncated"] is True
    assert [g["name"] for g in d["backlog_never_played"]] == ["A", "B", "C"]

    md = run(S.steam_analyze_library(S.LibraryAnalysisInput(
        steamid="76561197960287930", backlog_limit=3)))
    assert "Backlog truncated" in md and "showing 3 of 5" in md

    # Default backlog_limit is the 100 max, so a small backlog is NOT truncated.
    full = run(S.steam_analyze_library(S.LibraryAnalysisInput(
        steamid="76561197960287930", response_format="json")))
    assert json.loads(full)["backlog_truncated"] is False
    assert S.LibraryAnalysisInput(steamid="x").backlog_limit == 100

    # backlog_limit=0 is intentional suppression, not truncation — no nag.
    z = run(S.steam_analyze_library(S.LibraryAnalysisInput(
        steamid="76561197960287930", backlog_limit=0)))
    assert "Backlog truncated" not in z
    assert json.loads(run(S.steam_analyze_library(S.LibraryAnalysisInput(
        steamid="76561197960287930", backlog_limit=0,
        response_format="json"))))["backlog_truncated"] is False


def test_analyze_library_abandoned_decoupled(monkeypatch):
    # 5 abandoned games (played, last launched long ago). abandoned_limit must
    # govern the Abandoned list on its own — backlog_limit must NOT shrink it.
    old = 1_500_000_000  # well before a 365-day cutoff from 'now'
    games = [
        {"appid": i, "name": ch, "playtime_forever": 120, "rtime_last_played": old + i}
        for i, ch in enumerate("ABCDE", start=1)
    ]
    payload = {"response": {"game_count": 5, "games": games}}

    async def fake_steam(path, params, **k):
        return payload

    monkeypatch.setattr(S, "_steam_get", fake_steam)

    def abandoned_count(**kw):
        out = run(S.steam_analyze_library(S.LibraryAnalysisInput(
            steamid="76561197960287930", response_format="json", **kw)))
        return len(json.loads(out)["abandoned"])

    # backlog_limit must not touch the abandoned list (the decoupling bug fix).
    assert abandoned_count(backlog_limit=1) == abandoned_count(backlog_limit=100) == 5
    # abandoned_limit is what actually bounds it.
    assert abandoned_count(abandoned_limit=2) == 2

    md = run(S.steam_analyze_library(S.LibraryAnalysisInput(
        steamid="76561197960287930", abandoned_limit=2)))
    assert "5 total, showing 2" in md

    # Count appears even when NOT truncated — parity with the Backlog header.
    md_full = run(S.steam_analyze_library(S.LibraryAnalysisInput(
        steamid="76561197960287930")))
    assert "5 total, showing 5" in md_full

    j = json.loads(run(S.steam_analyze_library(S.LibraryAnalysisInput(
        steamid="76561197960287930", abandoned_limit=2, response_format="json"))))
    assert j["abandoned_truncated"] is True
    # abandoned_limit=0 is intentional suppression, not truncation.
    z = json.loads(run(S.steam_analyze_library(S.LibraryAnalysisInput(
        steamid="76561197960287930", abandoned_limit=0, response_format="json"))))
    assert z["abandoned"] == [] and z["abandoned_truncated"] is False
    # Defaults: dedicated 25-game abandoned cap, independent of backlog.
    assert S.LibraryAnalysisInput(steamid="x").abandoned_limit == 25


def test_analyze_library_abandoned_sort(monkeypatch):
    # A..E with increasing last_played (A oldest, E newest) and varied playtime,
    # so the three sort orders are all distinguishable.
    old = 1_500_000_000
    pt = {"A": 600, "B": 100, "C": 500, "D": 200, "E": 50}
    games = [
        {"appid": i, "name": ch, "playtime_forever": pt[ch],
         "rtime_last_played": old + i}
        for i, ch in enumerate("ABCDE", start=1)
    ]
    payload = {"response": {"game_count": 5, "games": games}}

    async def fake_steam(path, params, **k):
        return payload

    monkeypatch.setattr(S, "_steam_get", fake_steam)

    def order(**kw):
        out = run(S.steam_analyze_library(S.LibraryAnalysisInput(
            steamid="76561197960287930", response_format="json", **kw)))
        return [g["name"] for g in json.loads(out)["abandoned"]]

    # Default is 'recent' => most recently dropped first (the ISSUE-2 fix).
    assert order() == ["E", "D", "C", "B", "A"]
    assert S.LibraryAnalysisInput(steamid="x").abandoned_sort == "recent"
    assert order(abandoned_sort="oldest") == ["A", "B", "C", "D", "E"]
    assert order(abandoned_sort="playtime") == ["A", "C", "D", "B", "E"]

    # Truncation now keeps the most recent, not the most ancient.
    assert order(abandoned_limit=2) == ["E", "D"]

    with pytest.raises(ValidationError):
        S.LibraryAnalysisInput(steamid="x", abandoned_sort="bogus")


def test_is_temp_client():
    temp = [
        "Super Buckyball Tournament Playtest",
        "Knockout City Trial",
        "PAYDAY 3 - Beta",
        "SMITE 2 - Public Test",
        "Quake Champions PTS",
        "Rust - Staging Branch",
        "Tom Clancy's Rainbow Six Siege - Test Server",
        "Battlerite Public Test",
        "DEFCON Beta Demo",
        "Spacebase DF-9 Prototype",
        "Halo Infinite - Open Beta",
        "Some Game - Closed Beta",
        "Cool Game Demo",
        "REMATCH BETA TEST",    # all-caps, mid-string BETA (case-insensitive)
        "Brawlhalla BETA",
        "Project X - Alpha Test",
        "Some Game Dev Build",
        "World of Foo PTR",
    ]
    for n in temp:
        assert S._is_temp_client(n), f"should flag: {n}"
    # Retail titles that share tokens must NOT be flagged (precision over recall).
    retail = [
        "Prototype",            # the 2009 retail game
        "Prototype 2",
        "Trials Rising",
        "Alpha Protocol",       # bare 'alpha' is intentionally NOT a token
        "Sid Meier's Alpha Centauri",
        "The Turing Test",      # bare 'test' is intentionally NOT a token
        "Test Drive Unlimited",
        "Counter-Strike 2",
        "Dota 2",
        "Half-Life 2: Episode One",
        "The Elder Scrolls V: Skyrim",
        "Batman: Arkham Asylum",
        "Borderlands",
    ]
    for n in retail:
        assert not S._is_temp_client(n), f"should NOT flag: {n}"


def test_analyze_library_excludes_temp_clients(monkeypatch):
    games = [
        {"appid": 1, "name": "Real Game A", "playtime_forever": 600,
         "rtime_last_played": 1700000000},
        {"appid": 2, "name": "Real Game B", "playtime_forever": 0,
         "rtime_last_played": 0},
        {"appid": 3, "name": "Cool Shooter Playtest", "playtime_forever": 9000,
         "rtime_last_played": 1700000000},
        {"appid": 4, "name": "Big RPG - Beta", "playtime_forever": 0,
         "rtime_last_played": 0},
    ]
    payload = {"response": {"game_count": 4, "games": games}}

    async def fake_steam(path, params, **k):
        return payload

    monkeypatch.setattr(S, "_steam_get", fake_steam)

    # Default: temp clients dropped from counts and every list.
    d = json.loads(run(S.steam_analyze_library(S.LibraryAnalysisInput(
        steamid="76561197960287930", response_format="json"))))
    assert d["summary"]["game_count"] == 2
    assert d["summary"]["temp_clients_excluded"] == 2
    backlog_names = {g["name"] for g in d["backlog_never_played"]}
    assert "Big RPG - Beta" not in backlog_names and "Real Game B" in backlog_names
    assert "Cool Shooter Playtest" not in {g["name"] for g in d["top_played"]}
    assert set(d["temp_clients_excluded_names"]) == {
        "Cool Shooter Playtest", "Big RPG - Beta"}

    md = run(S.steam_analyze_library(S.LibraryAnalysisInput(
        steamid="76561197960287930")))
    assert "Excluded **2** non-retail" in md

    # Opt out: everything counted again, including the 150h playtest.
    d2 = json.loads(run(S.steam_analyze_library(S.LibraryAnalysisInput(
        steamid="76561197960287930", exclude_temp_clients=False,
        response_format="json"))))
    assert d2["summary"]["game_count"] == 4
    assert d2["summary"]["temp_clients_excluded"] == 0
    assert "Cool Shooter Playtest" in {g["name"] for g in d2["top_played"]}
    assert S.LibraryAnalysisInput(steamid="x").exclude_temp_clients is True


def test_hours_str_floor():
    assert S._hours_str(0) == "0.0"      # truly never launched
    assert S._hours_str(None) == "0.0"
    assert S._hours_str(1) == "<0.1"     # launched 1-2 min -> rounds to 0.0h
    assert S._hours_str(2) == "<0.1"
    assert S._hours_str(6) == "0.1"      # 6 min = 0.1h, shown normally
    assert S._hours_str(90) == "1.5"


def test_analyze_library_tiny_playtime_render(monkeypatch):
    # 2 minutes, launched in 2013: 'played'/abandoned but rounds to 0.0h.
    games = [
        {"appid": 1, "name": "A Virus Named TOM", "playtime_forever": 2,
         "rtime_last_played": 1379462400},  # 2013-09-18
        {"appid": 2, "name": "Untouched Game", "playtime_forever": 0,
         "rtime_last_played": 0},
    ]
    payload = {"response": {"game_count": 2, "games": games}}

    async def fake_steam(path, params, **k):
        return payload

    monkeypatch.setattr(S, "_steam_get", fake_steam)

    d = json.loads(run(S.steam_analyze_library(S.LibraryAnalysisInput(
        steamid="76561197960287930", stale_days=365, response_format="json"))))
    # Consistent predicate: launched (>0 min) => played/abandoned, NOT backlog.
    assert "A Virus Named TOM" in {g["name"] for g in d["abandoned"]}
    assert "A Virus Named TOM" not in {g["name"] for g in d["backlog_never_played"]}
    assert "Untouched Game" in {g["name"] for g in d["backlog_never_played"]}
    assert d["summary"]["played_count"] == 1
    assert d["playtime_buckets"]["0h"] == 1  # only the truly-untouched game
    # Numeric hours still rounds to 0.0, but the display string is non-contradictory.
    tom = next(g for g in d["abandoned"] if g["name"] == "A Virus Named TOM")
    assert tom["hours"] == 0.0 and tom["hours_str"] == "<0.1"

    md = run(S.steam_analyze_library(S.LibraryAnalysisInput(
        steamid="76561197960287930", stale_days=365)))
    assert "<0.1h, last played 2013-09-18" in md
    assert "0.0h, last played 2013-09-18" not in md


def test_analyze_library_persona_header(monkeypatch):
    sid = "76561197960287930"
    games = [{"appid": 1, "name": "Game A", "playtime_forever": 600,
              "rtime_last_played": 1700000000}]
    payload = {"response": {"game_count": 1, "games": games}}

    async def fake_steam(path, params, **k):
        return payload

    async def persona_ok(ids):
        return {sid: {"personaname": "Sarg338"}}

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    monkeypatch.setattr(S, "_summaries_for", persona_ok)

    # ISSUE-8: header shows persona (sid), not a bare SteamID64.
    md = run(S.steam_analyze_library(S.LibraryAnalysisInput(steamid=sid)))
    assert md.split("\n")[0] == f"# Library analysis for Sarg338 ({sid})"
    # ISSUE-7: clearer average wording.
    assert "across all owned" in md and "across played games" in md
    assert "of played" not in md

    d = json.loads(run(S.steam_analyze_library(S.LibraryAnalysisInput(
        steamid=sid, response_format="json"))))
    assert d["persona_name"] == "Sarg338" and d["steamid"] == sid

    # Persona unavailable -> fall back to the bare SteamID (no parens).
    async def persona_empty(ids):
        return {}
    monkeypatch.setattr(S, "_summaries_for", persona_empty)
    md2 = run(S.steam_analyze_library(S.LibraryAnalysisInput(steamid=sid)))
    assert md2.split("\n")[0] == f"# Library analysis for {sid}"

    # Persona lookup failure must not break the analysis (best-effort).
    async def persona_boom(ids):
        raise RuntimeError("network")
    monkeypatch.setattr(S, "_summaries_for", persona_boom)
    md3 = run(S.steam_analyze_library(S.LibraryAnalysisInput(steamid=sid)))
    assert md3.split("\n")[0] == f"# Library analysis for {sid}"


def test_taste_profile_excludes_temp_clients(monkeypatch):
    sid = "76561197960287930"
    owned = {"response": {"games": [
        {"appid": 100, "name": "Real RPG", "playtime_forever": 5000},
        {"appid": 200, "name": "Cool Shooter Playtest", "playtime_forever": 99999},
        {"appid": 300, "name": "PAYDAY 3 - Beta", "playtime_forever": 8000},
    ]}}
    recent = {"response": {"games": [
        {"appid": 200, "name": "Cool Shooter Playtest", "playtime_2weeks": 600},
    ]}}

    async def fake_steam(path, params, **k):
        return recent if "GetRecentlyPlayed" in path else owned

    seeded = {}

    async def fake_items_tags(ids):
        seeded["ids"] = list(ids)
        return {a: [{"tagid": 1, "weight": 1}] for a in ids}

    async def fake_tag_names():
        return {1: "RPG"}

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    monkeypatch.setattr(S, "_items_tags", fake_items_tags)
    monkeypatch.setattr(S, "_tag_name_map", fake_tag_names)

    prof = run(S._taste_profile(sid))
    # The 99,999-minute playtest and the beta must NOT seed taste.
    assert seeded["ids"] == [100]
    assert "Cool Shooter Playtest" not in prof["seed_games"]
    assert "Real RPG" in prof["seed_games"]
    # owned_ids stays full (used to exclude already-owned games from recs).
    assert prof["owned_ids"] == {100, 200, 300}


def test_coop_night_excludes_temp_clients(monkeypatch):
    host = "76561197960287930"
    friend = "76561197960287931"

    async def fake_summaries(ids):
        return {i: {"personaname": "Pal", "personastate": 1} for i in ids}

    async def fake_owned_set(sid):
        return {1, 2}

    async def fake_items_coop(appids):
        return {
            1: {"name": "Real Coop Game", "coop": True},
            2: {"name": "Some Game Playtest", "coop": True},
        }

    monkeypatch.setattr(S, "_summaries_for", fake_summaries)
    monkeypatch.setattr(S, "_owned_set", fake_owned_set)
    monkeypatch.setattr(S, "_items_coop", fake_items_coop)

    d = json.loads(run(S.steam_plan_coop_night(S.PlanCoopNightInput(
        steamid=host, friends=[friend], response_format="json"))))
    names = {g["name"] for g in d["games"]}
    assert "Real Coop Game" in names
    assert "Some Game Playtest" not in names  # unlaunchable playtest skipped
    assert d["mode"] == "owned"


def test_coop_night_new_mode(monkeypatch):
    host = "76561197960287930"
    friend = "76561197960287931"
    owned = {host: {100}, friend: {200}}     # union {100, 200} must be excluded

    async def fake_summaries(ids):
        return {i: {"personaname": "Pal", "personastate": 1} for i in ids}

    async def fake_owned_set(sid):
        return owned.get(sid, set())

    async def fake_resolve_tags(names):
        return ([1685], [])                  # "Co-op" -> tag id

    async def fake_discover(query):
        assert query.get("sort_by") == "Reviews_DESC"   # 'new' ranks by reviews
        return [100, 300, 200, 400, 500], 5  # 100/200 owned; 500 isn't co-op

    async def fake_items_coop(appids):
        meta = {300: ("Fresh Co-op A", True), 400: ("Fresh Co-op B", True),
                500: ("Solo Only", False)}
        return {a: {"name": meta.get(a, ("?", False))[0],
                    "coop": meta.get(a, ("?", False))[1]} for a in appids}

    async def fake_app_prices(appids, cc):
        nm = {300: "Fresh Co-op A", 400: "Fresh Co-op B"}
        return {a: {"name": nm.get(a), "price": "$19.99",
                    "on_sale": False, "discount_pct": 0} for a in appids}

    monkeypatch.setattr(S, "_summaries_for", fake_summaries)
    monkeypatch.setattr(S, "_owned_set", fake_owned_set)
    monkeypatch.setattr(S, "_resolve_tag_ids", fake_resolve_tags)
    monkeypatch.setattr(S, "_discover_appids", fake_discover)
    monkeypatch.setattr(S, "_items_coop", fake_items_coop)
    monkeypatch.setattr(S, "_app_prices", fake_app_prices)

    d = json.loads(run(S.steam_plan_coop_night(S.PlanCoopNightInput(
        steamid=host, friends=[friend], mode="new", response_format="json"))))
    assert d["mode"] == "new"
    assert [g["appid"] for g in d["games"]] == [300, 400]  # owned excluded, non-coop dropped
    assert d["excluded_owned"] == 2                        # union of {100} and {200}
    assert {g["name"] for g in d["games"]} == {"Fresh Co-op A", "Fresh Co-op B"}


def test_app_details_features(monkeypatch):
    data = {"123": {"success": True, "data": {
        "name": "Game", "type": "game", "is_free": False,
        "price_overview": {"final_formatted": "$10", "discount_percent": 0},
        "categories": [{"description": "Single-player"}, {"description": "Online Co-op"},
                       {"description": "Full controller support"},
                       {"description": "Steam Cloud"}],
        "genres": [{"description": "Action"}],
        "platforms": {"windows": True, "mac": False, "linux": False},
        "controller_support": "full",
        "release_date": {"date": "2020", "coming_soon": False},
        "supported_languages": "English<strong>*</strong>, French",
        "achievements": {"total": 10}, "recommendations": {"total": 500},
        "dlc": [1, 2], "required_age": 0,
        "content_descriptors": {"notes": "Violence"},
        "pc_requirements": {"minimum": "<strong>Minimum:</strong> 8GB RAM"},
        "short_description": "A game.",
    }}}

    async def fake_store(path, params, cache_ttl=0):
        return data

    async def fake_deck(appid, language="english"):
        return {"category": 3, "label": "Verified", "items": [], "blog_url": None}

    monkeypatch.setattr(S, "_store_get", fake_store)
    monkeypatch.setattr(S, "_deck_compat", fake_deck)  # hermetic + assert below
    out = run(S.steam_get_app_details(
        S.AppDetailsInput(appid=123, response_format="json")))
    d = json.loads(out)
    f = d["features"]
    assert f["is_singleplayer"] and f["is_coop"] and f["is_online_coop"]
    assert f["has_controller_support"] and f["has_cloud_saves"]
    assert d["dlc_count"] == 2
    assert d["full_audio_languages"] == ["English"]
    assert d["pc_requirements"]["minimum"] == "8GB RAM"   # label stripped
    assert d["steam_deck"] == "Verified"                  # inline Deck enrichment


def test_deck_compatibility(monkeypatch):
    report = {"success": 1, "results": {
        "appid": 730, "resolved_category": 2,  # 2 = Playable
        "resolved_items": [
            {"display_type": 3,  # caveat
             "loc_token": "#SteamDeckVerified_TestResult_ControllerGlyphsDoNotMatchDeckDevice"},
            {"display_type": 4,  # pass
             "loc_token": "#SteamDeckVerified_TestResult_DefaultConfigurationIsPerformant"},
        ],
        "steam_deck_blog_url": "",
    }}

    async def fake_raw(url, params, cache_ttl=0):
        return report

    monkeypatch.setattr(S, "_raw_get", fake_raw)

    # Helper: category mapping + loc_token humanization + glyphs.
    d = run(S._deck_compat(730))
    assert d["category"] == 2 and d["label"] == "Playable"
    assert d["items"][0]["status"] == "⚠"  # caveat
    assert d["items"][0]["text"] == "Controller Glyphs Do Not Match Deck Device"
    assert d["items"][1]["status"] == "✓"  # pass

    # Tool: markdown + json.
    md = run(S.steam_get_deck_compatibility(S.DeckCompatInput(appid=730)))
    assert "# Steam Deck: Playable" in md
    assert "Controller Glyphs Do Not Match Deck Device" in md
    j = json.loads(run(S.steam_get_deck_compatibility(
        S.DeckCompatInput(appid=730, response_format="json"))))
    assert j["appid"] == 730 and j["label"] == "Playable"

    # No published rating -> friendly message, not an error.
    async def fake_none(url, params, cache_ttl=0):
        return {"success": 0}
    monkeypatch.setattr(S, "_raw_get", fake_none)
    msg = run(S.steam_get_deck_compatibility(S.DeckCompatInput(appid=1)))
    assert "No Steam Deck compatibility rating" in msg


def test_app_prices_batch_and_fallback(monkeypatch):
    # GetItems returns 10 (on sale), 11 (free); 12 is absent -> per-item fallback.
    def _item(aid, name, free=False, final=None, disc=None, released=None):
        bpo = {} if free else {"formatted_final_price": final, "discount_pct": disc}
        d = {"appid": aid, "name": name, "is_free": free,
             "best_purchase_option": bpo}
        if released is not None:
            d["release"] = {"steam_release_date": released}
        return d

    async def fake_steam(path, params, with_key=True, cache_ttl=0):
        return {"response": {"store_items": [
            _item(10, "OnSale", final="$5.00", disc=50, released=1700000000),
            _item(11, "FreeGame", free=True),
        ]}}

    async def fake_app_price(appid, cc):  # fallback path for the missing appid
        return {"appid": appid, "name": "Fallback", "is_free": False,
                "price": "$9.99", "discount_pct": 0, "on_sale": False}

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    monkeypatch.setattr(S, "_app_price", fake_app_price)

    pm = run(S._app_prices([10, 11, 12], "us"))
    assert set(pm) == {10, 11, 12}
    assert pm[10]["price"] == "$5.00" and pm[10]["on_sale"] and pm[10]["discount_pct"] == 50
    assert pm[10]["release_ts"] == 1700000000   # parsed from include_release
    assert pm[11]["is_free"] and pm[11]["price"] == "Free"
    assert pm[11]["release_ts"] is None         # no release block -> None
    assert pm[12]["name"] == "Fallback"   # came from the single-item fallback


def test_discover_released_within_days(monkeypatch):
    import time as _t
    now = _t.time()
    rel = {1: now - 5 * 86400, 2: now - 200 * 86400, 3: now - 10 * 86400}  # 2 is old

    async def fake_raw(url, params, cache_ttl=0):
        assert params.get("sort_by") == "Released_DESC"   # window forces newest-first
        return {"success": 1, "total_count": 100,
                "results_html": '<a data-ds-appid="1"></a><a data-ds-appid="2"></a>'
                                '<a data-ds-appid="3"></a>'}

    async def fake_app_prices(appids, cc):
        return {a: {"name": f"G{a}", "release_ts": int(rel[a])} for a in appids}

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    monkeypatch.setattr(S, "_app_prices", fake_app_prices)
    d = json.loads(run(S.steam_discover(S.DiscoverInput(
        released_within_days=30, response_format="json"))))
    assert [r["appid"] for r in d["results"]] == [1, 3]   # 200-day-old #2 dropped
    assert d["filters"]["released_within_days"] == 30


def test_wishlist_on_sale_filter(monkeypatch):
    async def fake_steam(path, params, **k):
        return {"response": {"items": [{"appid": 10, "priority": 0},
                                       {"appid": 11, "priority": 1}]}}

    prices = {
        10: {"appid": 10, "name": "OnSale", "price": "$5", "discount_pct": 50, "on_sale": True},
        11: {"appid": 11, "name": "Full", "price": "$20", "discount_pct": 0, "on_sale": False},
    }

    async def fake_app_prices(appids, cc):
        return {a: prices[a] for a in appids}

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    monkeypatch.setattr(S, "_app_prices", fake_app_prices)
    out = run(S.steam_get_wishlist(
        S.WishlistInput(steamid="76561197960287930", on_sale_only=True,
                        response_format="json")))
    d = json.loads(out)
    assert d["count"] == 1 and d["items"][0]["name"] == "OnSale"


def test_player_summary(monkeypatch):
    async def fake_steam(path, params, **k):
        return {"response": {"players": [
            {"steamid": "76561197960287930", "personaname": "Gabe",
             "personastate": 1, "communityvisibilitystate": 3}]}}

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    out = run(S.steam_get_player_summary(
        S.PlayersInput(steamids=["76561197960287930"])))
    assert "Gabe" in out and "Online" in out


def test_compare_players(monkeypatch):
    a, b = "76561197960000001", "76561197960000002"

    async def fake_steam(path, params, **k):
        if params["steamid"] == a:
            games = [{"appid": 1, "name": "Shared", "playtime_forever": 600},
                     {"appid": 2, "name": "OnlyA", "playtime_forever": 60}]
        else:
            games = [{"appid": 1, "name": "Shared", "playtime_forever": 120},
                     {"appid": 3, "name": "OnlyB", "playtime_forever": 60}]
        return {"response": {"games": games}}

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    out = run(S.steam_compare_players(
        S.ComparePlayersInput(steamid_a=a, steamid_b=b, response_format="json")))
    d = json.loads(out)
    assert d["shared_count"] == 1
    assert d["shared"][0]["name"] == "Shared"
    assert d["shared"][0]["hours_a"] == 10.0 and d["shared"][0]["hours_b"] == 2.0


def test_app_reviews_recent_window(monkeypatch):
    # filter='recent' should compute a positive % from the windowed reviews
    import time
    now = int(time.time())
    base = {"success": 1, "query_summary": {"review_score_desc": "Very Positive",
            "total_reviews": 100, "total_positive": 95, "total_negative": 5},
            "reviews": []}

    async def fake_raw(url, params, cache_ttl=0):
        if "start_date" in params:  # refuse the windowed summary: use the scan
            return {"success": 2}
        if params.get("filter") == "recent":
            return {"success": 1, "reviews": [
                {"voted_up": True, "timestamp_created": now - 10},
                {"voted_up": False, "timestamp_created": now - 20},
                {"voted_up": True, "timestamp_created": now - 30},
                {"voted_up": True, "timestamp_created": now - 99 * 86400},  # too old -> stop
            ], "cursor": "*"}
        return base

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    out = run(S.steam_get_app_reviews(
        S.AppReviewsInput(appid=1, review_filter="recent", limit=0,
                          response_format="json")))
    d = json.loads(out)
    assert d["summary"]["total_reviews"] == 100         # lifetime preserved
    assert d["recent"]["reviews_counted"] == 3          # 4th is outside window
    assert d["recent"]["positive"] == 2


# --------------------------------------------------------------------------- #
# 0.7.0: currency formatting, bounded fan-out, DLC, user stats, registration
# --------------------------------------------------------------------------- #

def test_fmt_amount():
    assert S._fmt_amount(9.99, "USD") == "$9.99"
    assert S._fmt_amount(9.99, "GBP") == "£9.99"
    assert S._fmt_amount(1234.5, "EUR") == "€1,234.50"
    assert S._fmt_amount(9.99, "ZZZ") == "9.99 ZZZ"   # unknown -> code suffix
    assert S._fmt_amount(9.99, None) == "$9.99"        # no code -> $ fallback
    assert S._fmt_amount(None, "USD") is None


def test_gather_limited_preserves_order():
    async def make(n):
        return n * 2

    out = run(S._gather_limited([make(1), make(2), make(3)], limit=2))
    assert out == [2, 4, 6]


def test_search_apps_currency(monkeypatch):
    async def fake_store(path, params, cache_ttl=0):
        return {"items": [
            {"id": 7, "name": "Game7", "price": {"currency": "EUR", "final": 1999}}]}

    async def no_types(appids):
        return {}

    monkeypatch.setattr(S, "_store_get", fake_store)
    monkeypatch.setattr(S, "_items_types", no_types)
    out = run(S.steam_search_apps(S.AppSearchInput(query="g", country_code="de")))
    assert "€19.99" in out and "$" not in out


def test_featured_specials_currency(monkeypatch):
    async def fake_fetch(cc):
        return {"specials": {"items": [
            {"id": 5, "name": "Deal", "original_price": 1999, "final_price": 999,
             "discount_percent": 50, "currency": "GBP"}]}}

    monkeypatch.setattr(S, "_fetch_featured", fake_fetch)
    out = run(S.steam_get_featured_specials(S.FeaturedInput(country_code="gb")))
    assert "£9.99" in out and "£19.99" in out and "$" not in out


def test_package_details_currency(monkeypatch):
    async def fake_store(path, params, cache_ttl=0):
        return {"55": {"success": True, "data": {
            "name": "Bundle",
            "price": {"currency": "GBP", "initial": 3000, "final": 1500,
                      "discount_percent": 50},
            "apps": [{"name": "A"}, {"name": "B"}]}}}

    monkeypatch.setattr(S, "_store_get", fake_store)
    out = run(S.steam_get_package_details(
        S.PackageDetailsInput(packageid=55, country_code="gb")))
    assert "£15.00" in out and "£30.00" in out and "$" not in out


def test_get_dlc(monkeypatch):
    async def fake_store(path, params, cache_ttl=0):
        return {"100": {"success": True,
                        "data": {"name": "Base", "dlc": [201, 202]}}}

    prices = {
        201: {"appid": 201, "name": "DLC One", "price": "$5",
              "discount_pct": 0, "on_sale": False},
        202: {"appid": 202, "name": "DLC Two", "price": "$2.50",
              "discount_pct": 50, "on_sale": True},
    }

    async def fake_app_prices(appids, cc):
        return {a: prices[a] for a in appids}

    monkeypatch.setattr(S, "_store_get", fake_store)
    monkeypatch.setattr(S, "_app_prices", fake_app_prices)

    out = run(S.steam_get_dlc(S.DlcInput(appid=100, response_format="json")))
    d = json.loads(out)
    assert d["base_game"] == "Base"
    assert d["dlc_total"] == 2 and d["count"] == 2
    assert d["dlc"][0]["name"] == "DLC One"        # order preserved by gather

    out2 = run(S.steam_get_dlc(
        S.DlcInput(appid=100, on_sale_only=True, response_format="json")))
    d2 = json.loads(out2)
    assert d2["count"] == 1 and d2["dlc"][0]["name"] == "DLC Two"


def test_get_dlc_none(monkeypatch):
    async def fake_store(path, params, cache_ttl=0):
        return {"100": {"success": True, "data": {"name": "Base", "dlc": []}}}

    monkeypatch.setattr(S, "_store_get", fake_store)
    out = run(S.steam_get_dlc(S.DlcInput(appid=100)))
    assert "no listed DLC" in out


def test_user_game_stats(monkeypatch):
    async def fake_steam(path, params, **k):
        return {"playerstats": {"gameName": "TF2", "stats": [
            {"name": "kills", "value": 100}, {"name": "deaths", "value": 50}]}}

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    out = run(S.steam_get_user_game_stats(
        S.UserGameStatsInput(steamid="76561197960287930", appid=440,
                             response_format="json")))
    d = json.loads(out)
    assert d["game"] == "TF2" and d["stat_count"] == 2
    assert d["stats"][0] == {"name": "kills", "value": 100}


def test_user_game_stats_empty(monkeypatch):
    async def fake_steam(path, params, **k):
        return {"playerstats": {"gameName": "X", "stats": []}}

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    out = run(S.steam_get_user_game_stats(
        S.UserGameStatsInput(steamid="76561197960287930", appid=1)))
    assert "No stats available" in out


def test_tools_registered():
    """Reviews tool must be wired to the real function (regression: the
    @mcp.tool decorator used to sit on the _fmt_review helper), and the new
    0.7.0 tools must be registered."""
    tools = run(S.mcp.list_tools())
    by_name = {t.name: t for t in tools}
    assert "steam_get_app_reviews" in by_name
    assert "steam_get_dlc" in by_name
    assert "steam_get_user_game_stats" in by_name
    assert "steam_get_app_tags" in by_name
    assert "steam_get_rarest_unlocks" in by_name
    assert "steam_find_friends_who_own" in by_name
    assert "steam_discover" in by_name
    assert "steam_should_i_buy" in by_name
    assert "steam_recommend" in by_name
    assert "steam_plan_coop_night" in by_name
    assert "steam_get_app_regional_pricing" in by_name
    assert "steam_get_workshop_item" in by_name
    assert "steam_get_user_groups" in by_name
    assert "steam_get_inventory" in by_name
    assert "steam_get_market_price" in by_name
    # the reviews tool takes the reviews input (has appid + review_filter),
    # not _fmt_review's raw-dict signature
    schema = json.dumps(_wire(by_name["steam_get_app_reviews"])["inputSchema"])
    assert "appid" in schema and "review_filter" in schema


# --------------------------------------------------------------------------- #
# 0.8.0: community tags, rarest unlocks, friends-who-own
# --------------------------------------------------------------------------- #

def test_get_app_tags(monkeypatch):
    async def fake_steam(path, params, **k):
        return {"response": {"store_items": [{
            "appid": 1, "success": 1, "name": "Game",
            "tags": [{"tagid": 10, "weight": 100}, {"tagid": 20, "weight": 50},
                     {"tagid": 99, "weight": 10}]}]}}

    async def fake_raw(url, params, cache_ttl=0):
        return [{"tagid": 10, "name": "Roguelike"}, {"tagid": 20, "name": "Co-op"}]

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    monkeypatch.setattr(S, "_raw_get", fake_raw)
    out = run(S.steam_get_app_tags(S.AppTagsInput(appid=1, response_format="json")))
    d = json.loads(out)
    assert d["name"] == "Game"
    assert d["count"] == 2                          # tagid 99 has no name -> skipped
    assert d["tags"][0] == {"tag": "Roguelike", "tagid": 10, "weight": 100}
    md = run(S.steam_get_app_tags(S.AppTagsInput(appid=1)))
    assert "Roguelike" in md and "Co-op" in md


def test_rarest_unlocks(monkeypatch):
    async def fake_steam(path, params, **k):
        if "GetPlayerAchievements" in path:
            return {"playerstats": {"success": True, "gameName": "G", "achievements": [
                {"apiname": "A", "name": "Ach A", "achieved": 1, "unlocktime": 1700000000},
                {"apiname": "B", "name": "Ach B", "achieved": 1, "unlocktime": 1700000000},
                {"apiname": "C", "name": "Ach C", "achieved": 0}]}}
        return {"achievementpercentages": {"achievements": [
            {"name": "A", "percent": 5.0}, {"name": "B", "percent": 80.0}]}}

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    out = run(S.steam_get_rarest_unlocks(
        S.RarestUnlocksInput(steamid="76561197960287930", appid=1,
                             response_format="json")))
    d = json.loads(out)
    assert d["unlocked_count"] == 2
    assert d["rarest"][0]["name"] == "Ach A"        # 5% rarer than 80%
    assert d["rarest"][0]["global_pct"] == 5.0


def test_global_achievement_percentages_tolerate_string_percents(monkeypatch):
    async def fake_steam(path, params, **k):
        return {"achievementpercentages": {"achievements": [
            {"name": "A", "percent": "80.126"},     # string, not a number
            {"name": "B", "percent": 5.0},
            {"name": "C"},                          # absent entirely
            {"name": "D", "percent": None},
            {"name": "E", "percent": "NaN"}]}}      # float() accepts this

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    out = run(S.steam_get_global_achievement_percentages(
        S.GlobalAchievementsInput(appid=1, response_format="json")))
    rows = json.loads(out)["achievements"]
    # Unknown rarity is null and sorts LAST (as the 1.14.1 changelog promised),
    # never 0.0 — that would rank it as the game's rarest achievement.
    assert [r["global_pct"] for r in rows] == [5.0, 80.13, None, None, None]
    assert [r["api_name"] for r in rows[:2]] == ["B", "A"]
    assert {r["api_name"] for r in rows} == {"A", "B", "C", "D", "E"}
    md = run(S.steam_get_global_achievement_percentages(
        S.GlobalAchievementsInput(appid=1)))
    assert "- B: 5.0% of players" in md and "- C: rarity n/a" in md


def test_rarest_unlocks_tolerates_string_percents(monkeypatch):
    async def fake_steam(path, params, **k):
        if "GetPlayerAchievements" in path:
            return {"playerstats": {"success": True, "gameName": "G", "achievements": [
                {"apiname": "A", "name": "Ach A", "achieved": 1, "unlocktime": 1700000000},
                {"apiname": "B", "name": "Ach B", "achieved": 1, "unlocktime": 1700000000},
                {"apiname": "C", "name": "Ach C", "achieved": 1, "unlocktime": 1700000000}]}}
        return {"achievementpercentages": {"achievements": [
            {"name": "A", "percent": "5.0"},        # string
            {"name": "B", "percent": 80.0},
            {"name": "C", "percent": "n/a"}]}}      # unparseable -> unknown

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    out = run(S.steam_get_rarest_unlocks(
        S.RarestUnlocksInput(steamid="76561197960287930", appid=1,
                             response_format="json")))
    d = json.loads(out)
    assert [r["global_pct"] for r in d["rarest"]] == [5.0, 80.0, None]


def test_friends_who_own(monkeypatch):
    async def fake_steam(path, params, **k):
        if "GetFriendList" in path:
            return {"friendslist": {"friends": [
                {"steamid": "1"}, {"steamid": "2"}, {"steamid": "3"}]}}
        if "GetOwnedGames" in path:
            fid = params["steamid"]
            if fid == "1":
                return {"response": {"game_count": 5,
                                     "games": [{"appid": 730, "playtime_forever": 600}]}}
            if fid == "2":
                return {"response": {"game_count": 3, "games": [{"appid": 10}]}}  # not 730
            return {"response": {}}                  # fid 3: private library
        if "GetPlayerSummaries" in path:
            return {"response": {"players": [
                {"steamid": "1", "personaname": "Alice", "personastate": 1,
                 "gameid": "730", "gameextrainfo": "CS2"}]}}
        return {}

    async def fake_app_price(appid, cc):
        return {"name": "Counter-Strike 2"}

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    monkeypatch.setattr(S, "_app_price", fake_app_price)
    out = run(S.steam_find_friends_who_own(
        S.FriendsWhoOwnInput(steamid="76561197960287930", appid=730,
                             response_format="json")))
    d = json.loads(out)
    assert d["game"] == "Counter-Strike 2"
    assert d["total_friends"] == 3 and d["checked"] == 3
    assert d["owners"] == 1 and d["private_or_unknown"] == 1
    assert d["friends"][0]["name"] == "Alice"
    assert d["friends"][0]["playing_now"] is True
    assert d["friends"][0]["playtime_hours"] == 10.0


# --------------------------------------------------------------------------- #
# MCP SDK compatibility (v1.x and v2) + cache hints
# --------------------------------------------------------------------------- #

def test_server_registers_its_surface():
    """The server object builds on whichever SDK major is installed."""
    assert len(S.mcp._tool_manager.list_tools()) == 41
    assert len(S.mcp._prompt_manager.list_prompts()) == 5


def test_tool_descriptions_are_trimmed_to_one_line():
    """_compact_descriptions() reaches into SDK internals; catch it silently
    no-opping if the tool-manager shape changes under either major."""
    for tool in S.mcp._tool_manager.list_tools():
        assert "\n" not in (tool.description or ""), tool.name


def test_version_is_in_sync_across_metadata():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{S.__version__}"' in pyproject
    for name in ("server.json", "manifest.json"):
        text = (root / name).read_text(encoding="utf-8")
        assert f'"version": "{S.__version__}"' in text, name
    import steam_mcp
    assert steam_mcp.__version__ == S.__version__  # stuck at 1.11.2 until 1.17.1


def test_cache_hint_methods_are_all_cacheable():
    """Every key we pass must be a method the spec allows hints on — an unknown
    key is a ValueError at construction, i.e. a server that won't start."""
    if not S.MCP_SDK_V2:
        pytest.skip("cache hints require MCP SDK v2")
    from mcp_types.methods import CACHEABLE_METHODS

    assert set(S._CACHE_HINT_TTL_MS) <= set(CACHEABLE_METHODS)


def test_cache_hints_reach_the_wire():
    if not S.MCP_SDK_V2:
        pytest.skip("cache hints require MCP SDK v2")
    from mcp import Client

    async def go():
        async with Client(S.mcp) as client:
            assert client.protocol_version == "2026-07-28"
            assert client.server_info.version == S.__version__
            listing = await client.list_tools()
            assert listing.ttl_ms == S._CACHE_HINT_TTL_MS["tools/list"]
            assert listing.cache_scope == "public"
            prompts = await client.list_prompts()
            assert prompts.ttl_ms == S._CACHE_HINT_TTL_MS["prompts/list"]

    run(go())


# --------------------------------------------------------------------------- #
# Asking the user who they are (v2 SDK: Resolve + Elicit / MRTR)
# --------------------------------------------------------------------------- #

v2_only = pytest.mark.skipif(not S.MCP_SDK_V2,
                             reason="resolvers/elicitation require MCP SDK v2")


@pytest.fixture
def no_default_user(monkeypatch):
    """No STEAM_USER anywhere, and no answer remembered from an earlier call."""
    monkeypatch.delenv("STEAM_USER", raising=False)
    monkeypatch.setattr(S, "_dotenv_value", lambda name: "")
    monkeypatch.setattr(S, "_REMEMBERED_USER", "")
    yield
    S._REMEMBERED_USER = ""


def _level_backend(monkeypatch):
    """Stub GetSteamLevel + vanity resolution; record which id was used."""
    seen = {}

    async def fake_steam(path, params, **k):
        if "ResolveVanityURL" in path:
            seen["vanity"] = params.get("vanityurl")
            return {"response": {"success": 1, "steamid": "76561197960287930"}}
        if "GetSteamLevel" in path:
            seen["steamid"] = params.get("steamid")
            return {"response": {"player_level": 42}}
        return {}

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    return seen


def _elicit_client(answer=None, action="accept", **kwargs):
    """An in-memory client whose elicitation callback records how often it ran."""
    from mcp import Client
    from mcp.types import ElicitResult

    asked = {"n": 0}

    async def callback(ctx, params):
        asked["n"] += 1
        if action != "accept":
            return ElicitResult(action=action)
        return ElicitResult(action="accept", content={"steam_account": answer})

    return Client(S.mcp, elicitation_callback=callback, **kwargs), asked


def _call_level(client, args):
    async def go():
        async with client as c:
            return await c.call_tool("steam_get_steam_level", {"params": args})

    return run(go())


@v2_only
def test_resolver_parameter_is_invisible_to_the_model():
    """The 'who are you' parameter must never reach a tool's input schema."""
    for tool in run(S.mcp.list_tools()):
        assert "default_user" not in json.dumps(_wire(tool)["inputSchema"]), tool.name


@v2_only
def test_keyless_finders_never_ask():
    """discover / should_i_buy / recommend treat an omitted id as 'don't
    personalize', not 'me' — they must not have the resolver attached."""
    for name in ("steam_discover", "steam_should_i_buy", "steam_recommend"):
        fn = S.mcp._tool_manager._tools[name].fn
        assert "default_user" not in inspect.signature(fn).parameters, name


@v2_only
def test_asks_when_nothing_else_answers_it(monkeypatch, no_default_user):
    seen = _level_backend(monkeypatch)
    client, asked = _elicit_client(answer="gabelogannewell")
    result = _call_level(client, {})
    assert asked["n"] == 1
    assert result.is_error is False
    assert seen["vanity"] == "gabelogannewell"
    assert seen["steamid"] == "76561197960287930"


@v2_only
def test_does_not_ask_when_the_call_carries_an_identity(monkeypatch, no_default_user):
    seen = _level_backend(monkeypatch)
    client, asked = _elicit_client(answer="gabelogannewell")
    result = _call_level(client, {"steamid": "76561197960287930"})
    assert asked["n"] == 0
    assert result.is_error is False
    assert seen["steamid"] == "76561197960287930"


@v2_only
def test_does_not_ask_when_steam_user_is_configured(monkeypatch, no_default_user):
    _level_backend(monkeypatch)
    monkeypatch.setattr(S, "_get_default_user", lambda: "76561197960287930")
    client, asked = _elicit_client(answer="someone-else")
    result = _call_level(client, {})
    assert asked["n"] == 0
    assert result.is_error is False


@v2_only
def test_client_without_elicitation_capability_gets_the_old_error(monkeypatch, no_default_user):
    """The regression that matters: today's clients can't elicit, and asking one
    anyway is a protocol error the model can't act on. Stay quiet instead."""
    from mcp import Client

    _level_backend(monkeypatch)

    async def go():
        async with Client(S.mcp) as c:
            return await c.call_tool("steam_get_steam_level", {"params": {}})

    result = run(go())
    assert "no default user configured" in result.content[0].text.lower()
    assert "STEAM_USER" in result.content[0].text


@v2_only
def test_declining_falls_back_to_the_error_not_a_protocol_failure(monkeypatch, no_default_user):
    _level_backend(monkeypatch)
    client, asked = _elicit_client(action="decline")
    result = _call_level(client, {})
    assert asked["n"] == 1
    assert "no default user configured" in result.content[0].text.lower()


@v2_only
def test_an_answer_is_remembered_for_the_rest_of_the_session(monkeypatch, no_default_user):
    seen = _level_backend(monkeypatch)
    client, asked = _elicit_client(answer="gabelogannewell")

    async def go():
        async with client as c:
            await c.call_tool("steam_get_steam_level", {"params": {}})
            await c.call_tool("steam_get_steam_level", {"params": {}})
            return await c.call_tool("steam_get_steam_level", {"params": {}})

    result = run(go())
    assert asked["n"] == 1, "the user must be asked at most once per session"
    assert result.is_error is False
    assert seen["steamid"] == "76561197960287930"


@v2_only
def test_legacy_era_client_is_asked_the_same_question(monkeypatch, no_default_user):
    """Same tool body, 2025-era push elicitation instead of a multi-round-trip."""
    seen = _level_backend(monkeypatch)
    client, asked = _elicit_client(answer="gabelogannewell", mode="legacy")
    result = _call_level(client, {})
    assert asked["n"] == 1
    assert result.is_error is False
    assert seen["steamid"] == "76561197960287930"


@v2_only
def test_resolver_annotation_is_not_wrapped_in_a_union():
    """Pin the annotation shape the SDK actually reads.

    `get_type_hints` is what classifies the parameter, and through Python 3.10 it
    still applies implicit-Optional to a parameter defaulting to None — which
    would turn `Annotated[..., Resolve(...)]` into `Optional[Annotated[...]]` and
    fail registration with InvalidSignature. 3.11 dropped that behavior, so this
    only ever broke on our lowest supported Python.
    """
    import typing

    fn = S.mcp._tool_manager._tools["steam_get_wishlist"].fn
    hint = typing.get_type_hints(fn, include_extras=True)["default_user"]
    assert typing.get_origin(hint) is not typing.Union
    assert any(type(m).__name__ == "Resolve" for m in typing.get_args(hint)[1:])


# --------------------------------------------------------------------------- #
# Keyless surface: which tools need STEAM_API_KEY, and how that is advertised
# --------------------------------------------------------------------------- #

_MINIMAL_ARGS = {
    "appid": 570,
    "query": "dota",
    "packageid": 1,
    "published_file_id": "123",
    "market_hash_name": "AK-47 | Redline (Field-Tested)",
    "steamid_b": "76561197960287930",
    "appids": [570, 730],
}


def _call_with_no_key(monkeypatch, name):
    """Run a tool with no key configured and every HTTP call stubbed.

    Returns True if the tool reached for the API key. `_get_with_retry` is the
    one choke point every request funnels through, and `_steam_get` asks for the
    key *before* calling it — so a tool that never wants a key never trips this.
    """
    import inspect as _inspect

    asked = {"key": False}

    def fake_key():
        asked["key"] = True
        raise S.SteamApiError("no key")

    class _Resp:
        status_code = 200
        headers: dict = {}
        text = ""

        def json(self):
            return {}

    async def fake_http(client, url, params, timeout):
        return _Resp()

    # A default user, so the account tools get past SteamID resolution and
    # actually reach the key check instead of failing earlier for an unrelated
    # reason. A vanity name rather than a raw ID, because that is what people
    # actually put in STEAM_USER and resolving one is itself a keyed call — a
    # 17-digit ID would short-circuit resolution and make tools that genuinely
    # need a key look keyless.
    monkeypatch.setenv("STEAM_USER", "gabelogannewell")
    monkeypatch.setattr(S, "_get_api_key", fake_key)
    monkeypatch.setattr(S, "_get_with_retry", fake_http)
    monkeypatch.setattr(S, "_http_client", lambda: None)
    S._CACHE._d.clear()

    fn = S.mcp._tool_manager._tools[name].fn
    model = list(_inspect.signature(fn, eval_str=True).parameters.values())[0].annotation
    kwargs = {f: _MINIMAL_ARGS[f] for f, i in model.model_fields.items()
              if i.is_required()}
    run(fn(model(**kwargs)))
    return asked["key"]


@pytest.mark.parametrize("name", sorted(S.KEYLESS_TOOLS))
def test_keyless_tools_never_ask_for_a_key(monkeypatch, name):
    """The property the marker advertises, asserted against real behavior rather
    than a guess about which endpoints need credentials."""
    assert _call_with_no_key(monkeypatch, name) is False


@pytest.mark.parametrize("name", sorted(S.PARTLY_KEYLESS_TOOLS))
def test_partly_keyless_tools_need_no_key_until_personalized(monkeypatch, name):
    """Called without a steamid they must behave exactly like a keyless tool."""
    assert _call_with_no_key(monkeypatch, name) is False


def test_key_requirement_buckets_are_disjoint():
    assert not (set(S.KEYLESS_TOOLS) & set(S.PARTLY_KEYLESS_TOOLS))


def test_no_tool_is_wrongly_marked_as_needing_a_key(monkeypatch):
    """The other half of the drift guard.

    A tool that never asks for a key but sits outside both sets gets stamped
    "unavailable" on a keyless install and the model stops offering it — the
    exact bug that once hid steam_discover. Sweep every tool and require the two
    sets to be precisely the ones that run without a key.
    """
    free = set()
    for name in S.mcp._tool_manager._tools:
        if _call_with_no_key(monkeypatch, name) is False:
            free.add(name)
        monkeypatch.undo()
    declared = set(S.KEYLESS_TOOLS) | set(S.PARTLY_KEYLESS_TOOLS)
    assert free == declared, (
        f"run without a key but not declared: {free - declared}; "
        f"declared but do ask for a key: {declared - free}"
    )


def test_every_keyless_tool_is_registered():
    names = {t.name for t in S.mcp._tool_manager.list_tools()}
    assert set(S.KEYLESS_TOOLS) <= names
    assert set(S.PARTLY_KEYLESS_TOOLS) <= names


def _remark(monkeypatch, have_key):
    monkeypatch.setattr(S, "_have_api_key", lambda: have_key)
    S._compact_descriptions()
    return {t.name: (t.description or "") for t in S.mcp._tool_manager.list_tools()}


def test_keyed_tools_are_marked_unavailable_without_a_key(monkeypatch):
    try:
        descs = _remark(monkeypatch, have_key=False)
        marked = {n for n, d in descs.items() if S.NEEDS_KEY_MARKER in d}
        partly = {n for n, d in descs.items() if S.PARTLY_KEYLESS_MARKER in d}
        clean = set(descs) - marked - partly
        assert clean == set(S.KEYLESS_TOOLS)
        assert partly == set(S.PARTLY_KEYLESS_TOOLS)
        assert len(marked) == 19
    finally:
        monkeypatch.undo()
        S._compact_descriptions()


def test_no_marker_costs_nothing_when_a_key_is_configured(monkeypatch):
    try:
        descs = _remark(monkeypatch, have_key=True)
        assert not [d for d in descs.values() if S.NEEDS_KEY_MARKER in d]
        assert not [d for d in descs.values() if S.PARTLY_KEYLESS_MARKER in d]
    finally:
        monkeypatch.undo()
        S._compact_descriptions()


def test_marking_is_idempotent(monkeypatch):
    """Re-running the compactor must not stack markers or strand a stale one."""
    try:
        _remark(monkeypatch, have_key=False)
        descs = _remark(monkeypatch, have_key=False)
        owned = descs["steam_get_owned_games"]
        assert owned.count(S.NEEDS_KEY_MARKER) == 1
        descs = _remark(monkeypatch, have_key=True)
        assert S.NEEDS_KEY_MARKER not in descs["steam_get_owned_games"]
    finally:
        monkeypatch.undo()
        S._compact_descriptions()


def test_missing_key_error_points_at_the_keyless_alternatives(monkeypatch):
    monkeypatch.delenv("STEAM_API_KEY", raising=False)
    monkeypatch.setattr(S, "_dotenv_value", lambda name: "")
    with pytest.raises(S.SteamApiError) as e:
        S._get_api_key()
    msg = str(e.value)
    assert "steamcommunity.com/dev/apikey" in msg
    assert str(len(S.KEYLESS_TOOLS)) in msg
    # names it suggests must actually be keyless, or the advice sends the model
    # straight into another error
    usable = set(S.KEYLESS_TOOLS) | set(S.PARTLY_KEYLESS_TOOLS)
    for name in re.findall(r"steam_[a-z_]+", msg):
        assert name in usable, name


def test_readme_key_column_matches_the_code():
    """The README's "Needs key?" column and the runtime markers are two renderings
    of the same fact. Drift means the docs promise something the server denies."""
    import pathlib

    # encoding pinned: the table's "yes†" marker is UTF-8, and read_text()
    # would otherwise decode it through the platform codepage (cp1252 on
    # Windows), dropping that row from the parse.
    readme = (pathlib.Path(__file__).resolve().parent.parent / "README.md").read_text(
        encoding="utf-8"
    )
    rows = re.findall(r"^\| `(steam_\w+)` \|.*\| (no\*|no|yes†|yes) \|$", readme, re.M)
    assert len(rows) == 41, f"parsed {len(rows)} tool rows, expected 41"
    for name, marker in rows:
        if marker == "no":
            assert name in S.KEYLESS_TOOLS, f"{name} documented keyless, is not"
        elif marker == "no*":
            assert name in S.PARTLY_KEYLESS_TOOLS, f"{name} documented no*, is not"
        else:
            assert name not in S.KEYLESS_TOOLS and name not in S.PARTLY_KEYLESS_TOOLS, (
                f"{name} documented as needing a key, but runs without one")


# --------------------------------------------------------------------------- #
# Token footprint: lean schemas, no duplicated structured output, compact JSON
# --------------------------------------------------------------------------- #

def _schema_keywords(node, found=None):
    """Collect every schema *keyword* used (property names excluded)."""
    found = set() if found is None else found
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            if key in ("properties", "$defs") and isinstance(value, dict):
                for sub in value.values():
                    _schema_keywords(sub, found)
            elif key not in ("default", "enum", "required"):
                _schema_keywords(value, found)
    elif isinstance(node, list):
        for item in node:
            _schema_keywords(item, found)
    return found


def test_tool_schemas_are_lean():
    for t in run(S.mcp.list_tools()):
        schema = _wire(t)["inputSchema"]
        assert "title" not in _schema_keywords(schema), t.name
        assert "ResponseFormat" not in schema.get("$defs", {}), t.name
        # the enum is inlined where it's used, still fully constraining the field
        (model,) = schema["$defs"].values()
        rf = model["properties"]["response_format"]
        assert rf["enum"] == ["markdown", "json"] and rf["default"] == "markdown"
        # nothing a caller relies on went missing
        assert schema["required"] == ["params"], t.name
        for prop in model["properties"].values():
            assert "type" in prop or "anyOf" in prop, t.name


def test_lean_schemas_is_idempotent_and_keeps_params():
    before = {t.name: _wire(t)["inputSchema"] for t in run(S.mcp.list_tools())}
    S._lean_schemas()
    after = {t.name: _wire(t)["inputSchema"] for t in run(S.mcp.list_tools())}
    assert before == after
    props = after["steam_discover"]["$defs"]["DiscoverInput"]["properties"]
    assert props["limit"] == {"default": 15, "description": "Max results to return (1-50).",
                              "maximum": 50, "minimum": 1, "type": "integer"}


def test_strip_titles_leaves_a_property_named_title():
    schema = {"title": "M", "type": "object",
              "properties": {"title": {"title": "Title", "type": "string"}}}
    S._strip_schema_titles(schema)
    assert schema == {"type": "object", "properties": {"title": {"type": "string"}}}


def test_tools_declare_no_output_schema_and_send_text_once(monkeypatch):
    for t in run(S.mcp.list_tools()):
        assert _wire(t).get("outputSchema") is None, t.name

    async def fake(path, params, **k):
        return {"response": {"player_count": 1234, "result": 1}}

    monkeypatch.setattr(S, "_steam_get", fake)
    res = run(S.mcp.call_tool("steam_get_current_players", {"params": {"appid": 730}}))
    # v1 returns (content, structured) as a tuple only when structured output is on
    assert not isinstance(res, tuple)
    assert getattr(res, "structured_content", None) is None


def test_json_responses_are_compact():
    out = S._dump({"a": [1, 2], "b": {"c": "é"}})
    assert out == '{"a":[1,2],"b":{"c":"é"}}'


def test_app_prices_fallback_keeps_the_release_date(monkeypatch):
    # GetItems knows 20's release date but has no price for it (paid, unpriced):
    # the appdetails fallback fills the price and must not erase release_ts, or
    # steam_discover's release window silently drops the game.
    async def fake_steam(path, params, with_key=True, cache_ttl=0):
        return {"response": {"store_items": [
            {"appid": 20, "name": "Unpriced", "is_free": False,
             "best_purchase_option": {}, "release": {"steam_release_date": 1700000000}},
            {"appid": 21, "name": "KeepMyName", "is_free": False,
             "best_purchase_option": {}, "release": {"steam_release_date": 1700000001}},
        ]}}

    async def fake_app_price(appid, cc):
        if appid == 20:
            return {"appid": 20, "name": "Unpriced", "is_free": False,
                    "price": "$19.99", "price_cents": 1999,
                    "discount_pct": 0, "on_sale": False}
        # a failed fallback: nothing known
        return {"appid": appid, "name": None, "price": None, "is_free": False,
                "on_sale": False, "discount_pct": 0}

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    monkeypatch.setattr(S, "_app_price", fake_app_price)

    pm = run(S._app_prices([20, 21], "us"))
    assert pm[20]["price"] == "$19.99" and pm[20]["price_cents"] == 1999
    assert pm[20]["release_ts"] == 1700000000
    assert pm[21]["name"] == "KeepMyName"          # not blanked by the failed fill
    assert pm[21]["release_ts"] == 1700000001


# Every list a JSON response carries is bounded by a `limit`, reports the full
# count, and says when it was cut — so no response can outgrow the per-result
# token budget, and the model knows to ask for more.

def test_player_achievements_locked_list_is_capped(monkeypatch):
    achs = [{"apiname": f"A{i}", "name": f"Ach {i}", "achieved": int(i < 10)}
            for i in range(400)]

    async def fake_steam(path, params, **k):
        return {"playerstats": {"success": True, "gameName": "Big",
                                "achievements": achs}}

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    d = json.loads(run(S.steam_get_player_achievements(S.PlayerAchievementsInput(
        steamid="76561197960287930", appid=1, response_format="json"))))
    assert d["total"] == 400 and d["unlocked"] == 10
    assert len(d["locked"]) == 50 and d["locked_truncated"] is True
    d = json.loads(run(S.steam_get_player_achievements(S.PlayerAchievementsInput(
        steamid="76561197960287930", appid=1, limit=300, response_format="json"))))
    assert len(d["locked"]) == 300 and d["locked_truncated"] is True
    md = run(S.steam_get_player_achievements(S.PlayerAchievementsInput(
        steamid="76561197960287930", appid=1, limit=20)))
    assert "…and 370 more" in md


def test_game_schema_list_is_capped(monkeypatch):
    async def fake_steam(path, params, **k):
        return {"game": {"gameName": "Big", "availableGameStats": {"achievements": [
            {"name": f"A{i}", "displayName": f"Ach {i}", "description": "d"}
            for i in range(300)]}}}

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    d = json.loads(run(S.steam_get_game_schema(
        S.GameSchemaInput(appid=1, response_format="json"))))
    assert d["achievement_count"] == 300
    assert len(d["achievements"]) == 100 and d["truncated"] is True
    d = json.loads(run(S.steam_get_game_schema(
        S.GameSchemaInput(appid=1, limit=250, response_format="json"))))
    assert len(d["achievements"]) == 250


def test_global_achievements_list_is_capped(monkeypatch):
    async def fake_steam(path, params, **k):
        return {"achievementpercentages": {"achievements": [
            {"name": f"A{i}", "percent": i / 10} for i in range(120)]}}

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    d = json.loads(run(S.steam_get_global_achievement_percentages(
        S.GlobalAchievementsInput(appid=1, response_format="json"))))
    assert d["achievement_count"] == 120 and d["truncated"] is True
    assert [r["api_name"] for r in d["achievements"]] == [f"A{i}" for i in range(50)]
    d = json.loads(run(S.steam_get_global_achievement_percentages(
        S.GlobalAchievementsInput(appid=1, limit=500, response_format="json"))))
    assert len(d["achievements"]) == 120 and d["truncated"] is False


def test_user_game_stats_list_is_capped(monkeypatch):
    async def fake_steam(path, params, **k):
        return {"playerstats": {"gameName": "G", "stats": [
            {"name": f"s{i}", "value": i} for i in range(150)]}}

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    d = json.loads(run(S.steam_get_user_game_stats(S.UserGameStatsInput(
        steamid="76561197960287930", appid=1, response_format="json"))))
    assert d["stat_count"] == 150
    assert len(d["stats"]) == 100 and d["truncated"] is True


def test_inventory_item_list_is_capped(monkeypatch):
    n = 120

    async def fake_raw(url, params, cache_ttl=0):
        return {"success": 1, "total_inventory_count": n,
                "assets": [{"classid": str(i), "instanceid": "0", "amount": "1"}
                           for i in range(n)],
                "descriptions": [{"classid": str(i), "instanceid": "0",
                                  "name": f"Item {i}"} for i in range(n)]}

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    d = json.loads(run(S.steam_get_inventory(S.InventoryInput(
        steamid="76561197960287930", response_format="json"))))
    assert d["distinct_items"] == n
    assert len(d["items"]) == 50 and d["truncated"] is True
    md = run(S.steam_get_inventory(S.InventoryInput(
        steamid="76561197960287930", limit=10)))
    assert f"{n} distinct, showing 10." in md and "…and 110 more distinct" in md


def test_annotations_are_minimal_and_read_only():
    for t in run(S.mcp.list_tools()):
        ann = _wire(t)["annotations"]
        assert ann["readOnlyHint"] is True, t.name
        assert ann.get("title"), t.name
        assert {k for k, v in ann.items() if v is not None} == {"title", "readOnlyHint"}



# --------------------------------------------------------------------------- #
# 1.16.1: review scan resilience, overall score, percent guards, cache
# --------------------------------------------------------------------------- #

def test_pct_value_rejects_non_finite_and_bool():
    assert S._pct_value("12.5") == 12.5
    for bad in ("NaN", "nan", "Infinity", "-inf", float("nan"), True, "x", None):
        assert S._pct_value(bad) is None, bad


def _review(rid, ts, up=True):
    return {"recommendationid": rid, "timestamp_created": ts, "voted_up": up,
            "votes_up": 0, "author": {"playtime_forever": 60}, "review": "ok"}


def test_recent_scan_skips_reviews_repeated_across_pages(monkeypatch):
    now = time.time()
    pages = {
        "*": {"success": 1, "cursor": "c1",
              "reviews": [_review("1", now - 10), _review("2", now - 20)]},
        # a new review landed between requests, shifting "2" onto page two
        "c1": {"success": 1, "cursor": "c2",
               "reviews": [_review("2", now - 20), _review("3", now - 30, False)]},
        "c2": {"success": 1, "cursor": "c3", "reviews": []},
    }

    async def fake_raw(url, params, cache_ttl=0):
        return pages[params["cursor"]]

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    window, capped = run(S._collect_recent_reviews(1, 30, "us"))
    assert [r["recommendationid"] for r in window] == ["1", "2", "3"]
    assert capped is False


def test_recent_scan_keeps_what_it_counted_when_a_page_fails(monkeypatch):
    now = time.time()

    async def fake_raw(url, params, cache_ttl=0):
        if params["cursor"] == "*":
            return {"success": 1, "cursor": "c1",
                    "reviews": [_review("1", now - 10), _review("2", now - 20)]}
        raise S.httpx2.TimeoutException("slow")

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    window, capped = run(S._collect_recent_reviews(1, 30, "us"))
    assert len(window) == 2 and capped is True        # partial, and flagged


def test_recent_scan_flags_a_refused_page_as_incomplete(monkeypatch):
    async def fake_raw(url, params, cache_ttl=0):
        return {"success": 2}

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    assert run(S._collect_recent_reviews(1, 30, "us")) == ([], True)


def test_app_reviews_survive_a_failed_recent_scan(monkeypatch):
    # The lifetime summary came back; a timeout in the recent tally must not
    # turn the whole answer into an error.
    async def fake_raw(url, params, cache_ttl=0):
        if "start_date" in params:  # refuse the windowed summary: use the scan
            return {"success": 2}
        if params["filter"] == "all":
            return {"success": 1, "reviews": [], "query_summary": {
                "review_score_desc": "Very Positive", "total_reviews": 10,
                "total_positive": 9, "total_negative": 1}}
        raise S.httpx2.TimeoutException("slow")

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    md = run(S.steam_get_app_reviews(S.AppReviewsInput(appid=1, review_filter="recent")))
    assert "Very Positive" in md and "90.0%" in md
    assert "unavailable" in md and "0.0% of 0" not in md
    d = json.loads(run(S.steam_get_app_reviews(S.AppReviewsInput(
        appid=1, review_filter="recent", response_format="json"))))
    assert d["summary"]["positive_pct"] == 90.0
    assert d["recent"]["reviews_counted"] == 0 and d["recent"]["sampled"] is True


def test_app_reviews_overall_score_ignores_the_excerpt_filter(monkeypatch):
    calls = []

    async def fake_raw(url, params, cache_ttl=0):
        calls.append((params["review_type"], params["num_per_page"]))
        if params["review_type"] == "all":
            summ = {"review_score_desc": "Very Positive", "total_reviews": 10,
                    "total_positive": 8, "total_negative": 2}
            return {"success": 1, "reviews": [], "query_summary": summ}
        # Steam computes query_summary over the filtered set
        return {"success": 1, "reviews": [_review("9", 1, up=False)],
                "query_summary": {"total_reviews": 2, "total_positive": 0,
                                  "total_negative": 2}}

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    d = json.loads(run(S.steam_get_app_reviews(S.AppReviewsInput(
        appid=1, review_type="negative", limit=3, response_format="json"))))
    assert d["summary"]["positive_pct"] == 80.0          # not 0.0
    assert d["summary"]["total_positive"] == 8
    assert [r["voted_up"] for r in d["reviews"]] == [False]
    assert calls == [("all", 0), ("negative", 3)]


def test_app_reviews_default_is_still_one_request(monkeypatch):
    calls = []

    async def fake_raw(url, params, cache_ttl=0):
        calls.append((params["review_type"], params["num_per_page"]))
        return {"success": 1, "reviews": [_review("1", 1)], "query_summary": {
            "total_reviews": 1, "total_positive": 1, "total_negative": 0}}

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    run(S.steam_get_app_reviews(S.AppReviewsInput(appid=1, limit=5)))
    assert calls == [("all", 5)]


def test_ttl_cache_evicts_least_recently_used_not_everything():
    c = S._TTLCache(maxsize=3)
    c.set("a", 1, 100)
    c.set("b", 2, 100)
    c.set("c", 3, 100)
    assert c.get("a") == 1              # touch "a": "b" is now the oldest
    c.set("d", 4, 100)
    assert c.get("b") is None
    assert (c.get("a"), c.get("c"), c.get("d")) == (1, 3, 4)
    c._d["c"] = (0.0, 3)                # "c" expires in place
    c.set("f", 6, 100)                  # expired entries go before live LRU ones
    assert "c" not in c._d
    assert (c.get("a"), c.get("d"), c.get("f")) == (1, 4, 6)


def test_concurrent_identical_requests_share_one_fetch(monkeypatch):
    S._CACHE.clear()
    calls = {"n": 0}

    class FakeResp:
        def json(self):
            return {"ok": True}

    async def fake_http(client, url, params, timeout):
        calls["n"] += 1
        await asyncio.sleep(0.01)
        return FakeResp()

    monkeypatch.setattr(S, "_get_with_retry", fake_http)
    monkeypatch.setattr(S, "_http_client", lambda: None)

    async def many():
        return await asyncio.gather(*(
            S._raw_get("https://store.steampowered.com/x", {"a": 1}, cache_ttl=60)
            for _ in range(5)))

    results = run(many())
    assert calls["n"] == 1 and all(r == {"ok": True} for r in results)
    assert not S._INFLIGHT


def test_a_failed_shared_fetch_reaches_every_waiter_and_is_not_cached(monkeypatch):
    S._CACHE.clear()
    calls = {"n": 0}

    async def boom(client, url, params, timeout):
        calls["n"] += 1
        await asyncio.sleep(0.01)
        raise S.httpx2.TimeoutException("slow")

    monkeypatch.setattr(S, "_get_with_retry", boom)
    monkeypatch.setattr(S, "_http_client", lambda: None)

    async def many():
        return await asyncio.gather(*(
            S._raw_get("https://store.steampowered.com/y", {}, cache_ttl=60)
            for _ in range(3)), return_exceptions=True)

    results = run(many())
    assert calls["n"] == 1
    assert all(isinstance(r, S.httpx2.TimeoutException) for r in results)
    assert not S._INFLIGHT and S._CACHE.get(
        S._cache_key("https://store.steampowered.com/y", {})) is None


def test_server_instructions_mark_community_text_untrusted():
    assert S.mcp.instructions == S.SERVER_INSTRUCTIONS
    text = S.SERVER_INSTRUCTIONS.lower()
    assert "untrusted" in text and "review" in text and "never follow" in text
    assert len(S.SERVER_INSTRUCTIONS) < 400        # paid once per session


def test_cancelling_the_shared_fetch_leader_does_not_cancel_its_waiters(monkeypatch):
    S._CACHE.clear()
    calls = {"n": 0}

    class FakeResp:
        def json(self):
            return {"ok": calls["n"]}

    async def slow(client, url, params, timeout):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return FakeResp()

    monkeypatch.setattr(S, "_get_with_retry", slow)
    monkeypatch.setattr(S, "_http_client", lambda: None)
    url = "https://store.steampowered.com/z"

    async def scenario():
        leader = asyncio.ensure_future(S._raw_get(url, {}, cache_ttl=60))
        await asyncio.sleep(0.01)                 # leader's request in flight
        waiter = asyncio.ensure_future(S._raw_get(url, {}, cache_ttl=60))
        await asyncio.sleep(0.01)
        leader.cancel()                           # its client gave up
        with pytest.raises(asyncio.CancelledError):
            await leader
        return await waiter                       # retried as the new leader

    assert run(scenario()) == {"ok": 2}
    assert calls["n"] == 2 and not S._INFLIGHT


def test_manifest_lists_exactly_the_registered_tools():
    """manifest.json's tool list is what desktop installs show before the server
    ever runs. A tool added, renamed or removed in code but not there (or vice
    versa) ships a bundle that describes a different server."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    listed = [t["name"] for t in manifest["tools"]]
    registered = {t.name for t in run(S.mcp.list_tools())}
    assert len(listed) == len(set(listed)), "duplicate tool in manifest.json"
    assert set(listed) == registered, (
        f"only in manifest: {sorted(set(listed) - registered)}; "
        f"only in server: {sorted(registered - set(listed))}")


def test_release_script_check_passes_on_the_repo():
    import pathlib
    import subprocess
    import sys

    root = pathlib.Path(__file__).resolve().parent.parent
    res = subprocess.run([sys.executable, str(root / "scripts" / "release.py"), "check"],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


# --------------------------------------------------------------------------- #
# Review population: the store page leaves key activations out of a paid
# game's score and counts everyone for a free one (verified live 2026-09).
# --------------------------------------------------------------------------- #

def _population_fakes(monkeypatch, is_free, price_ok=True):
    calls = []

    async def fake_price(appid, cc):
        if not price_ok:
            return {"appid": appid, "name": None, "is_free": False}
        return {"appid": appid, "name": "G", "is_free": is_free}

    async def fake_raw(url, params, cache_ttl=0):
        if "start_date" in params:  # refuse the windowed summary: use the scan
            return {"success": 2}
        calls.append((params["filter"], params["purchase_type"]))
        if params["filter"] == "recent":
            return {"success": 1, "cursor": "*",
                    "reviews": [_review("1", time.time() - 5),
                                _review("2", time.time() - 99 * 86400)]}
        return {"success": 1, "reviews": [], "query_summary": {
            "review_score_desc": "Very Positive", "total_reviews": 10,
            "total_positive": 9, "total_negative": 1}}

    monkeypatch.setattr(S, "_app_price", fake_price)
    monkeypatch.setattr(S, "_raw_get", fake_raw)
    return calls


def test_app_reviews_count_steam_purchases_for_a_paid_game(monkeypatch):
    calls = _population_fakes(monkeypatch, is_free=False)
    d = json.loads(run(S.steam_get_app_reviews(S.AppReviewsInput(
        appid=1, review_filter="recent", limit=0, response_format="json"))))
    assert d["summary"]["purchase_type"] == "steam"
    assert calls == [("all", "steam"), ("recent", "steam")]


def test_app_reviews_count_everyone_for_a_free_game(monkeypatch):
    calls = _population_fakes(monkeypatch, is_free=True)
    d = json.loads(run(S.steam_get_app_reviews(S.AppReviewsInput(
        appid=1, review_filter="recent", limit=0, response_format="json"))))
    assert d["summary"]["purchase_type"] == "all"
    assert calls == [("all", "all"), ("recent", "all")]


def test_app_reviews_population_falls_back_when_the_lookup_fails(monkeypatch):
    calls = _population_fakes(monkeypatch, is_free=False, price_ok=False)
    out = run(S.steam_get_app_reviews(S.AppReviewsInput(appid=1, limit=0)))
    assert calls == [("all", "all")]
    assert "key activations included" in out


def test_app_reviews_explicit_population_skips_the_lookup(monkeypatch):
    calls = _population_fakes(monkeypatch, is_free=True)

    async def boom(appid, cc):
        raise AssertionError("no lookup for an explicit purchase_type")

    monkeypatch.setattr(S, "_app_price", boom)
    run(S.steam_get_app_reviews(S.AppReviewsInput(
        appid=1, limit=0, purchase_type="STEAM")))
    assert calls == [("all", "steam")]
    with pytest.raises(ValueError):
        S.AppReviewsInput(appid=1, purchase_type="keys")


def test_should_i_buy_scores_the_store_population(monkeypatch):
    seen = []

    async def fake_store(path, params, cache_ttl=0):
        return {"7": {"success": True, "data": {"name": "Paid", "is_free": False}}}

    async def fake_raw(url, params, cache_ttl=0):
        if "start_date" in params:  # refuse the windowed summary: use the scan
            return {"success": 2}
        seen.append((params["filter"], params["purchase_type"]))
        if params["filter"] == "recent":
            return {"success": 1, "cursor": "*", "reviews": []}
        pos = 90 if params["purchase_type"] == "steam" else 50
        return {"success": 1, "query_summary": {
            "review_score_desc": "x", "total_positive": pos,
            "total_negative": 100 - pos, "total_reviews": 100}}

    async def fake_items(appids):
        return {}

    async def fake_map():
        return {}

    monkeypatch.setattr(S, "_store_get", fake_store)
    monkeypatch.setattr(S, "_raw_get", fake_raw)
    monkeypatch.setattr(S, "_items_tags", fake_items)
    monkeypatch.setattr(S, "_tag_name_map", fake_map)
    d = json.loads(run(S.steam_should_i_buy(
        S.ShouldIBuyInput(appid=7, response_format="json"))))
    assert d["review_lifetime"]["positive_pct"] == 90.0
    assert d["review_lifetime"]["purchase_type"] == "steam"
    assert ("recent", "steam") in seen


def test_store_purchase_type():
    assert S._store_purchase_type(False) == "steam"
    assert S._store_purchase_type(True) == "all"
    assert S._store_purchase_type(None) == "all"


def test_recent_max_reviews_sets_the_page_budget(monkeypatch):
    pages = []

    async def fake_raw(url, params, cache_ttl=0):
        pages.append(params["cursor"])
        n = len(pages)
        return {"success": 1, "cursor": f"c{n}",
                "reviews": [_review(f"{n}-{i}", time.time() - 5) for i in range(100)]}

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    got, capped = run(S._collect_recent_reviews(1, 30, "us", max_reviews=1500))
    assert len(pages) == 15 and len(got) == 1500 and capped
    pages.clear()
    got, capped = run(S._collect_recent_reviews(1, 30, "us"))
    assert len(pages) == 6 and capped                  # default stays ~600
    with pytest.raises(ValueError):
        S.AppReviewsInput(appid=1, recent_max_reviews=50_000)


def test_wire_schemas_drop_null_defaults():
    optional_seen = 0
    for t in run(S.mcp.list_tools()):
        (model,) = _wire(t)["inputSchema"]["$defs"].values()
        required = set(model.get("required", []))
        for name, prop in model["properties"].items():
            assert not ("default" in prop and prop["default"] is None), (t.name, name)
            if name == "steamid" and "anyOf" in prop:
                optional_seen += 1
                assert name not in required            # still optional
    assert optional_seen                                # the case was exercised
    # a property literally named "default" would survive; only null values go
    node = {"properties": {"a": {"default": None, "type": "string"},
                           "b": {"default": 0, "type": "integer"}}}
    S._strip_null_defaults(node)
    assert node == {"properties": {"a": {"type": "string"},
                                   "b": {"default": 0, "type": "integer"}}}


# --------------------------------------------------------------------------- #
# steam_analyze_app_reviews: streaming aggregates over the review corpus
# --------------------------------------------------------------------------- #

def _corpus_review(rid, ts, up=True, **extra):
    r = {"recommendationid": rid, "timestamp_created": ts, "voted_up": up,
         "votes_up": 0, "language": "english", "review": "fine game",
         "steam_purchase": True, "received_for_free": False,
         "author": {"playtime_forever": 600, "playtime_at_review": 120}}
    r.update(extra)
    return r


def _corpus_fakes(monkeypatch, pages, fail_at=None):
    """Serve `pages` (cursor -> page) and record each request's params."""
    calls = []

    async def fake_raw(url, params, cache_ttl=0):
        calls.append(dict(params))
        if fail_at is not None and len(calls) == fail_at:
            raise S.httpx.TimeoutException("slow")
        return pages[params["cursor"]]

    async def fake_price(appid, cc):
        return {"appid": appid, "name": "G", "is_free": False}

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    monkeypatch.setattr(S, "_app_price", fake_price)
    return calls


def _analyze(**kw):
    kw.setdefault("response_format", "json")
    return json.loads(run(S.steam_analyze_app_reviews(
        S.ReviewAnalysisInput(appid=1, **kw))))


def test_analyze_reviews_aggregates_segments_and_dedupes_across_pages(monkeypatch):
    now = int(time.time())
    summary = {"review_score_desc": "Very Positive", "total_reviews": 1000,
               "total_positive": 900, "total_negative": 100}
    pages = {
        "*": {"success": 1, "cursor": "c1", "query_summary": summary, "reviews": [
            _corpus_review("1", now - 10, votes_up=50),
            _corpus_review("2", now - 20, up=False, steam_purchase=False,
                           language="koreana", review="별로예요",
                           author={"playtime_at_review": 30}),
            _corpus_review("3", now - 30, primarily_steam_deck=True,
                           developer_response="Thanks!", refunded=True),
        ]},
        "c1": {"success": 1, "cursor": "c2", "reviews": [
            _corpus_review("3", now - 30),                # overlap: counted once
            _corpus_review("4", now - 40, up=False, received_for_free=True,
                           steam_purchase=False, written_during_early_access=True,
                           votes_up=7),
        ]},
        "c2": {"success": 1, "cursor": "c2", "reviews": []},
    }
    calls = _corpus_fakes(monkeypatch, pages)
    d = _analyze(samples=1)
    sc = d["scanned"]
    assert (sc["reviews"], sc["positive"], sc["negative"]) == (4, 2, 2)
    assert sc["complete"] and sc["stop_reason"] == "exhausted"
    assert sc["next_cursor"] is None
    assert d["lifetime"]["positive_pct"] == 90.0
    assert d["scope"]["purchase_type"] == "all"          # default: compare sources
    seg = d["segments"]
    assert seg["steam_purchase"]["reviews"] == 2
    assert seg["key_activation"] == {"reviews": 1, "share_pct": 25.0,
                                     "positive_pct": 0.0}
    assert seg["received_for_free"]["reviews"] == 1
    assert seg["early_access"]["reviews"] == 1
    assert seg["steam_deck"]["reviews"] == 1
    assert seg["developer_response"]["reviews"] == 1
    assert seg["refunded"]["reviews"] == 1
    langs = {g["language"]: g["reviews"] for g in d["languages"]}
    assert langs == {"english": 3, "koreana": 1}
    buckets = {b["bucket"]: b["reviews"] for b in d["playtime_at_review"]["buckets"]}
    assert buckets == {"<1h": 1, "1-5h": 3}
    s = d["samples"]
    assert [r["votes_up"] for r in s["most_helpful_positive"]] == [50]
    assert [r["votes_up"] for r in s["most_helpful_negative"]] == [7]
    assert s["newest_negative"][0]["excerpt"] == "별로예요"   # Hangul survives
    assert [c["filter"] for c in calls] == ["recent"] * 3


def test_analyze_reviews_stops_at_max_reviews_with_a_resume_cursor(monkeypatch):
    now = int(time.time())
    pages = {c: {"success": 1, "cursor": f"n{i}",
                 "reviews": [_corpus_review(f"{i}-{j}", now - i * 100 - j)
                             for j in range(100)]}
             for i, c in enumerate(["*", "n0", "n1", "n2"])}
    calls = _corpus_fakes(monkeypatch, pages)
    d = _analyze(max_reviews=200, samples=0)
    assert d["scanned"]["reviews"] == 200
    assert d["scanned"]["stop_reason"] == "max_reviews"
    assert not d["scanned"]["complete"]
    assert d["scanned"]["next_cursor"] == "n1"          # the next unread page
    assert [c["num_per_page"] for c in calls] == [100, 100]
    calls.clear()
    resumed = _analyze(max_reviews=100, samples=0, cursor="n1")
    assert resumed["lifetime"] is None                  # only the first page has it
    assert calls[0]["cursor"] == "n1"


def test_analyze_reviews_stops_at_the_window_edge(monkeypatch):
    now = int(time.time())
    pages = {"*": {"success": 1, "cursor": "c1", "reviews": [
        _corpus_review("1", now - 86400),
        _corpus_review("2", now - 2 * 86400),
        _corpus_review("3", now - 9 * 86400),              # outside 7 days
    ]}}
    calls = _corpus_fakes(monkeypatch, pages)
    d = _analyze(day_range=7)
    assert d["scanned"]["reviews"] == 2
    assert d["scanned"]["complete"] and d["scanned"]["stop_reason"] == "window_edge"
    assert len(calls) == 1


def test_analyze_reviews_keeps_the_tally_when_a_page_fails(monkeypatch):
    now = int(time.time())
    pages = {"*": {"success": 1, "cursor": "c1",
                   "reviews": [_corpus_review("1", now - 5)]}}
    _corpus_fakes(monkeypatch, pages, fail_at=2)
    d = _analyze()
    assert d["scanned"]["reviews"] == 1
    assert d["scanned"]["stop_reason"] == "request_error"
    assert d["scanned"]["next_cursor"] == "c1" and d["scanned"]["error"]


def test_analyze_reviews_markdown_flags_a_partial_scan(monkeypatch):
    now = int(time.time())
    pages = {"*": {"success": 1, "cursor": "c1",
                   "reviews": [_corpus_review("1", now - 5)]}}
    _corpus_fakes(monkeypatch, pages, fail_at=2)
    md = run(S.steam_analyze_app_reviews(S.ReviewAnalysisInput(appid=1)))
    assert "Partial" in md and "cursor=`c1`" in md
    assert "Bought on Steam: 1 (100.0%)" in md


def test_analyze_reviews_store_population_and_validation(monkeypatch):
    calls = _corpus_fakes(monkeypatch, {"*": {"success": 1, "reviews": []}})
    d = _analyze(purchase_type="store")
    assert d["scope"]["purchase_type"] == "steam"        # paid game in the fake
    assert calls[0]["purchase_type"] == "steam"
    assert d["scanned"]["reviews"] == 0 and d["scanned"]["positive_pct"] is None
    for bad in ({"max_reviews": 50}, {"max_reviews": 20_001}, {"samples": 6},
                {"purchase_type": "keys"}, {"day_range": 0}):
        with pytest.raises(ValueError):
            S.ReviewAnalysisInput(appid=1, **bad)


def test_review_timeline_rolls_up_by_span():
    from collections import Counter
    days = Counter({"2026-01-01": 2, "2026-01-02": 1})
    pos = Counter({"2026-01-01": 1})
    tl = S._review_timeline(days, pos)
    assert tl["granularity"] == "day"
    assert tl["periods"][0] == {"period": "2026-01-01", "reviews": 2,
                                "positive_pct": 50.0}
    tl = S._review_timeline(Counter({"2025-01-01": 1, "2025-06-01": 1}), Counter())
    assert tl["granularity"] == "week"
    tl = S._review_timeline(Counter({"2020-01-15": 1, "2026-01-15": 3}),
                            Counter({"2026-01-15": 3}))
    assert tl["granularity"] == "month"
    assert tl["periods"] == [
        {"period": "2020-01", "reviews": 1, "positive_pct": 0.0},
        {"period": "2026-01", "reviews": 3, "positive_pct": 100.0}]
    many = Counter({f"{y}-{m:02d}-01": 1 for y in range(2010, 2026)
                    for m in range(1, 13)})
    tl = S._review_timeline(many, Counter())
    assert len(tl["periods"]) == S.REVIEW_TIMELINE_CAP and tl["truncated"]
    assert S._review_timeline(Counter(), Counter())["periods"] == []


def test_release_bump_updates_every_version_field(tmp_path):
    """Run a real bump on a scratch copy: every field moves, both __version__s
    included (the package one was missed for six releases)."""
    import pathlib
    import shutil
    import subprocess
    import sys
    root = pathlib.Path(__file__).resolve().parent.parent
    for rel in ("scripts/release.py", "pyproject.toml", "manifest.json",
                "server.json", "steam_mcp/server.py", "steam_mcp/__init__.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(root / rel, tmp_path / rel)
    (tmp_path / "CHANGES.md").write_text(
        "# Changelog\n\n## [Unreleased]\n- **Fixed: a thing.**\n\n"
        f"## [{S.__version__}]\n- **Older.**\n", encoding="utf-8")
    script = str(tmp_path / "scripts" / "release.py")
    res = subprocess.run([sys.executable, script, "bump", "patch"],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    new = res.stdout.strip().splitlines()[-1]
    assert new != S.__version__
    for rel in ("steam_mcp/server.py", "steam_mcp/__init__.py"):
        assert f'__version__ = "{new}"' in (tmp_path / rel).read_text(encoding="utf-8")
    check = subprocess.run([sys.executable, script, "check"],
                           capture_output=True, text=True)
    assert check.returncode == 0, check.stdout + check.stderr


# --------------------------------------------------------------------------- #
# Search ranking: what someone typing the title means (found by live_check)
# --------------------------------------------------------------------------- #

def _search_fakes(monkeypatch, items, types):
    async def fake_store(path, params, cache_ttl=0):
        return {"items": [{"id": a, "name": n} for a, n in items]}

    async def fake_types(appids):
        if types is None:
            raise AssertionError("unreachable")
        return types

    monkeypatch.setattr(S, "_store_get", fake_store)
    monkeypatch.setattr(S, "_items_types", fake_types)


def _search(query, **kw):
    return json.loads(run(S.steam_search_apps(S.AppSearchInput(
        query=query, response_format="json", **kw))))["results"]


def test_search_puts_the_exact_title_first(monkeypatch):
    # Steam's storesearch order for "Hades" (live, 2026-09): the sequel first.
    _search_fakes(monkeypatch, [(1145350, "Hades II"), (1145360, "Hades"),
                                (1206340, "Hades Original Soundtrack")],
                  {1145350: ("game", None), 1145360: ("game", None),
                   1206340: ("soundtrack", 1145360)})
    got = _search("hades")
    assert [r["appid"] for r in got] == [1145360, 1145350, 1206340]
    assert got[2]["type"] == "soundtrack" and got[2]["parent_appid"] == 1145360


def test_search_puts_games_before_their_dlc(monkeypatch):
    # "The Witcher 3" (live): a DLC first, the game second, no exact title.
    _search_fakes(monkeypatch, [
        (5006530, "The Witcher 3: Wild Hunt — Songs of the Past"),
        (292030, "The Witcher 3: Wild Hunt - Complete Edition"),
        (2684660, "The Witcher 3 REDkit")],
        {5006530: ("dlc", 292030), 292030: ("game", None),
         2684660: ("software", None)})
    assert [r["appid"] for r in _search("The Witcher 3")] == [292030, 5006530, 2684660]
    md = run(S.steam_search_apps(S.AppSearchInput(query="The Witcher 3")))
    assert "[dlc for appid 292030]" in md and "[software]" in md


def test_search_exact_match_ignores_case_marks_and_punctuation(monkeypatch):
    _search_fakes(monkeypatch, [(3017860, "DOOM: The Dark Ages"), (379720, "DOOM®")],
                  {})
    assert _search("doom")[0]["appid"] == 379720          # types unknown: still works
    assert S._title_key("Half-Life™") == S._title_key("half life")


def test_search_limit_applies_after_ranking(monkeypatch):
    _search_fakes(monkeypatch, [(1, "Portal 2"), (2, "Portal Knights"), (3, "Portal")],
                  {})
    assert [r["appid"] for r in _search("Portal", limit=1)] == [3]


def test_items_types_failure_is_not_fatal(monkeypatch):
    async def boom(*a, **k):
        raise S.httpx.TimeoutException("slow")

    monkeypatch.setattr(S, "_steam_get", boom)
    assert run(S._items_types([1, 2])) == {}


def test_empty_results_are_valid_json(monkeypatch):
    async def empty_store(path, params, cache_ttl=0):
        return {"items": [], "specials": {"items": []}, "top_sellers": {"items": []}}

    async def no_news(path, params, **k):
        return {"appnews": {"newsitems": []}}

    async def no_types(appids):
        return {}

    monkeypatch.setattr(S, "_store_get", empty_store)
    monkeypatch.setattr(S, "_steam_get", no_news)
    monkeypatch.setattr(S, "_items_types", no_types)
    j = "json"
    assert json.loads(run(S.steam_search_apps(S.AppSearchInput(
        query="zzz", response_format=j))))["results"] == []
    assert json.loads(run(S.steam_get_featured_specials(S.FeaturedInput(
        response_format=j))))["specials"] == []
    assert json.loads(run(S.steam_get_store_highlights(S.StoreHighlightsInput(
        section="top_sellers", response_format=j))))["items"] == []
    assert json.loads(run(S.steam_get_app_news(S.AppNewsInput(
        appid=1, response_format=j))))["news"] == []
    # markdown keeps its plain sentence
    assert "No store results" in run(S.steam_search_apps(S.AppSearchInput(query="zzz")))


# --------------------------------------------------------------------------- #
# Windowed review summaries: Steam scores any date range in one request
# --------------------------------------------------------------------------- #

def _summary(pos, neg, desc="Very Positive"):
    return {"success": 1, "reviews": [], "query_summary": {
        "review_score_desc": desc, "total_positive": pos, "total_negative": neg,
        "total_reviews": pos + neg}}


def test_recent_score_is_one_exact_windowed_request(monkeypatch):
    import time as _t
    calls = []

    async def fake_raw(url, params, cache_ttl=0):
        calls.append(dict(params))
        if "start_date" in params:
            return _summary(7_000, 3_000, "Mostly Positive")
        return _summary(90_000, 10_000)

    async def paid(appid, cc):
        return {"appid": appid, "name": "G", "is_free": False}

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    monkeypatch.setattr(S, "_app_price", paid)
    d = json.loads(run(S.steam_get_app_reviews(S.AppReviewsInput(
        appid=1, review_filter="recent", day_range=30, limit=0,
        response_format="json"))))
    rc = d["recent"]
    assert (rc["reviews_counted"], rc["positive"], rc["positive_pct"]) == (10_000, 7_000, 70.0)
    assert rc["review_score_desc"] == "Mostly Positive" and rc["sampled"] is False
    win = [c for c in calls if "start_date" in c]
    assert len(win) == 1 and not any(c["filter"] == "recent" for c in calls)  # no scan
    w = win[0]
    assert w["date_range_type"] == "include" and w["purchase_type"] == "steam"
    # the store counts from UTC midnight 30 days ago, through now
    assert w["start_date"] % 86400 == 0
    assert w["start_date"] == (int(_t.time()) // 86400 - 30) * 86400
    assert w["end_date"] % 3600 == 0 and w["end_date"] >= _t.time()


def test_recent_score_falls_back_to_the_scan_when_refused(monkeypatch):
    import time as _t
    now = int(_t.time())

    async def fake_raw(url, params, cache_ttl=0):
        if "start_date" in params:
            return {"success": 2}
        if params["filter"] == "recent":
            return {"success": 1, "cursor": "*", "reviews": [
                _review("1", now - 5), _review("2", now - 99 * 86400)]}
        return _summary(9, 1)

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    rc, rows = run(S._recent_reviews(1, 30, "us", language="english",
                                     purchase_type="all"))
    assert rc["reviews_counted"] == 1 and rc["review_score_desc"] is None
    assert len(rows) == 1


def test_review_window_is_none_on_failure(monkeypatch):
    async def boom(url, params, cache_ttl=0):
        raise S.httpx.TimeoutException("slow")

    monkeypatch.setattr(S, "_raw_get", boom)
    assert run(S._review_window(1, 0, 1, language="all", purchase_type="all",
                                cc="us")) is None


# --------------------------------------------------------------------------- #
# steam_compare_games
# --------------------------------------------------------------------------- #

def _compare_fakes(monkeypatch, broken=()):
    games = {
        1: {"name": "Cheap Classic", "price": "$9.99", "life": 98.0, "recent": 97.0,
            "players": 500, "cents": 999, "tags": [11, 12]},
        2: {"name": "Busy Sequel", "price": "$29.99", "life": 90.0, "recent": 80.0,
            "players": 9000, "cents": 2999, "tags": [11, 13]},
    }

    async def details(p):
        if p.appid in broken:
            return f"Error: no store details for app {p.appid}."
        g = games[p.appid]
        return json.dumps({"name": g["name"], "price": g["price"], "is_free": False,
                           "discount_pct": 0, "steam_deck": "Verified",
                           "features": {"is_online_coop": p.appid == 2}})

    async def reviews(p):
        g = games[p.appid]
        return json.dumps({
            "summary": {"review_score_desc": "Very Positive",
                        "positive_pct": g["life"], "total_reviews": 1000},
            "recent": {"positive_pct": g["recent"], "reviews_counted": 100,
                       "sampled": False}})

    async def players(p):
        return json.dumps({"current_players": games[p.appid]["players"]})

    async def tags(appids):
        return {a: [{"tagid": t} for t in games[a]["tags"]] for a in appids}

    async def prices(appids, cc):
        return {a: {"price_cents": games[a]["cents"]} for a in appids}

    async def names():
        return {11: "Roguelike", 12: "Indie", 13: "Action"}

    for name, fn in (("steam_get_app_details", details), ("steam_get_app_reviews", reviews),
                     ("steam_get_current_players", players), ("_items_tags", tags),
                     ("_app_prices", prices), ("_tag_name_map", names)):
        monkeypatch.setattr(S, name, fn)


def test_compare_games_rows_and_highlights(monkeypatch):
    _compare_fakes(monkeypatch)
    d = json.loads(run(S.steam_compare_games(S.CompareGamesInput(
        appids=[1, 2], response_format="json"))))
    a, b = d["games"]
    assert a["name"] == "Cheap Classic" and a["trend_pts"] == -1.0
    assert b["trend_pts"] == -10.0 and b["online_coop"] is True
    assert a["top_tags"] == ["Roguelike", "Indie"]
    hl = d["highlights"]
    assert hl["best_reviewed"]["appid"] == 1 and hl["most_played_now"]["appid"] == 2
    assert hl["cheapest"]["appid"] == 1 and hl["shared_tags"] == ["Roguelike"]
    md = run(S.steam_compare_games(S.CompareGamesInput(appids=[1, 2])))
    assert "| **Busy Sequel** (2) | $29.99 |" in md and "(-10.0)" in md
    assert "**Shared tags**: Roguelike" in md


def test_compare_games_keeps_going_when_one_game_fails(monkeypatch):
    _compare_fakes(monkeypatch, broken=(2,))
    d = json.loads(run(S.steam_compare_games(S.CompareGamesInput(
        appids=[1, 2], response_format="json"))))
    assert d["games"][1]["error"].startswith("Error")
    assert d["highlights"]["best_reviewed"] is None      # nothing to compare against


def test_compare_games_input_rules():
    assert S.CompareGamesInput(appids=[5, 5, 6]).appids == [5, 6]
    for bad in ([1], [1, 1], [1, 2, 3, 4, 5, 6], [0, 1]):
        with pytest.raises(ValueError):
            S.CompareGamesInput(appids=bad)


# --------------------------------------------------------------------------- #
# steam_get_update_impact
# --------------------------------------------------------------------------- #

def test_update_posts_are_told_from_marketing():
    yes = ["Patch 2.31", "Update 2.3 Patch Notes", "R.E.P.O. v0.4.4",
           "Counter-Strike 2 Update", "Hotfix 1.2", "Update 2.2 is live!"]
    no = ["Take Your Shot in Photo Mode Challenge 3.0",
          "REDstreams — Update 2.2 is coming!",
          "Revealing UPDATE RELEASE DATE with MAGIC!", "Summer Sale: 50% off",
          "5th Anniversary Trailer"]
    assert all(S._is_update_post({"title": t}) for t in yes)
    assert not any(S._is_update_post({"title": t}) for t in no)
    assert S._is_update_post({"title": "Big news!", "tags": ["patchnotes"]})


def _impact_fakes(monkeypatch, news, windows):
    seen = []

    async def fake_news(path, params, **k):
        return {"appnews": {"newsitems": news}}

    async def fake_raw(url, params, cache_ttl=0):
        seen.append((params["start_date"], params["end_date"]))
        return windows(params["start_date"], params["end_date"])

    async def paid(appid, cc):
        return {"appid": appid, "name": "Game", "is_free": False}

    monkeypatch.setattr(S, "_steam_get", fake_news)
    monkeypatch.setattr(S, "_raw_get", fake_raw)
    monkeypatch.setattr(S, "_app_price", paid)
    return seen


def test_update_impact_compares_before_and_after(monkeypatch):
    import time as _t
    now = int(_t.time())
    patch_at = now - 20 * 86400
    news = [{"date": patch_at, "title": "Patch 1.1", "url": "u", "tags": []},
            {"date": now - 10 * 86400, "title": "Summer Sale", "tags": []},
            {"date": now - 400 * 86400, "title": "Patch 0.9", "tags": []}]

    def windows(start, end):
        return _summary(90, 10) if end <= patch_at else _summary(60, 40)

    seen = _impact_fakes(monkeypatch, news, windows)
    d = json.loads(run(S.steam_get_update_impact(S.UpdateImpactInput(
        appid=1, days=90, response_format="json"))))
    (e,) = d["updates"]                                    # sale and old patch left out
    assert e["title"] == "Patch 1.1" and e["change_pts"] == -30.0
    assert e["before"]["reviews"] == 100 and e["after"]["positive_pct"] == 60.0
    assert e["after_window_complete"] and not e["next_update_within_window"]
    assert sorted(seen) == [(patch_at - 7 * 86400, patch_at),
                            (patch_at, patch_at + 7 * 86400)]
    assert d["scope"]["purchase_type"] == "steam"
    md = run(S.steam_get_update_impact(S.UpdateImpactInput(appid=1)))
    assert "90.0% → 60.0% (**-30.0 pts**" in md


def test_update_impact_needs_enough_reviews_and_survives_refusals(monkeypatch):
    import time as _t
    now = int(_t.time())
    news = [{"date": now - 2 * 86400, "title": "Hotfix 2", "tags": []},
            {"date": now - 5 * 86400, "title": "Hotfix 1", "tags": []}]

    def windows(start, end):
        return {"success": 2} if start >= now - 2 * 86400 - 1 else _summary(5, 5)

    _impact_fakes(monkeypatch, news, windows)
    d = json.loads(run(S.steam_get_update_impact(S.UpdateImpactInput(
        appid=1, response_format="json"))))
    newest, older = d["updates"]
    assert newest["after"] is None and newest["change_pts"] is None
    assert not newest["after_window_complete"]
    assert older["change_pts"] is None                     # 10 reviews a side < 20
    assert older["next_update_within_window"]
    md = run(S.steam_get_update_impact(S.UpdateImpactInput(appid=1)))
    assert "didn't return the review counts" in md and "too few reviews" in md


def test_update_impact_with_no_updates(monkeypatch):
    _impact_fakes(monkeypatch, [], lambda s, e: _summary(1, 1))
    md = run(S.steam_get_update_impact(S.UpdateImpactInput(appid=1)))
    assert "No update or patch-note posts" in md


# --------------------------------------------------------------------------- #
# STEAM_MCP_TOOLS: an opt-in smaller tool set
# --------------------------------------------------------------------------- #

def test_essentials_are_all_registered_tools():
    registered = set(S.mcp._tool_manager._tools)
    assert S.ESSENTIAL_TOOLS <= registered
    assert len(S.ESSENTIAL_TOOLS) == 15


def test_tool_selection_rules():
    reg = {"steam_a", "steam_b", "steam_get_inventory"} | set(S.ESSENTIAL_TOOLS)
    keep, bad = S._tool_selection("essentials", reg)
    assert keep == set(S.ESSENTIAL_TOOLS) and bad == []
    keep, bad = S._tool_selection("Essentials, get_inventory, steam_a", reg)
    assert keep == set(S.ESSENTIAL_TOOLS) | {"steam_get_inventory", "steam_a"}
    keep, bad = S._tool_selection("steam_b,nope", reg)
    assert keep == {"steam_b"} and bad == ["nope"]
    assert S._tool_selection("nope", reg)[0] == reg          # nothing left: keep all
    assert S._tool_selection("all", reg)[0] == reg
    assert S._tool_selection(" , ", reg)[0] == reg


class _FakeManager:
    def __init__(self, names, with_remove):
        self._tools = {n: object() for n in names}
        if with_remove:
            self.remove_tool = lambda name: self._tools.pop(name)


@pytest.mark.parametrize("with_remove", [True, False])   # v2 SDK / v1 SDK
def test_apply_tool_profile_unregisters_the_rest(monkeypatch, with_remove):
    fake = _FakeManager(["steam_search_apps", "steam_get_inventory",
                         "steam_get_friend_list"], with_remove)
    monkeypatch.setattr(S.mcp, "_tool_manager", fake)
    monkeypatch.setenv(S.ENV_TOOLS, "essentials,get_friend_list")
    S._apply_tool_profile()
    assert set(fake._tools) == {"steam_search_apps", "steam_get_friend_list"}


def test_apply_tool_profile_leaves_everything_by_default(monkeypatch):
    fake = _FakeManager(["steam_search_apps", "steam_get_inventory"], True)
    monkeypatch.setattr(S.mcp, "_tool_manager", fake)
    monkeypatch.delenv(S.ENV_TOOLS, raising=False)
    monkeypatch.setattr(S, "_dotenv_value", lambda name: "")
    S._apply_tool_profile()
    assert len(fake._tools) == 2


# --------------------------------------------------------------------------- #
# steam_analyze_game: the one-call brief
# --------------------------------------------------------------------------- #

def _analyze_game_fakes(monkeypatch, row=None, updates=None, news=None):
    base = {"appid": 7, "name": "Seven", "price": "$19.99", "discount_pct": 25,
            "release_date": "Jan 1, 2024", "developers": ["Dev Co"],
            "genres": ["Action"], "short_description": "A game about seven.",
            "review_score_desc": "Very Positive", "positive_pct": 90.0,
            "total_reviews": 12_000, "recent_positive_pct": 80.0,
            "recent_reviews": 400, "recent_sampled": False, "trend_pts": -10.0,
            "players_now": 3210, "steam_deck": "Verified", "metacritic": 81,
            "achievements_total": 1, "dlc_count": 2, "platforms": ["windows"],
            "singleplayer": True, "online_coop": True, "local_coop": False,
            "multiplayer": False, "controller_support": "full"}

    async def fake_row(appid, cc):
        return row if row is not None else base

    async def fake_tags(appids):
        return {7: [{"tagid": 1}, {"tagid": 2}]}

    async def fake_names():
        return {1: "Roguelike", 2: "Co-op"}

    async def fake_impact(p):
        return json.dumps({"updates": updates or []})

    async def fake_news(appid, count=3):
        return news or []

    for name, fn in (("_compare_row", fake_row), ("_items_tags", fake_tags),
                     ("_tag_name_map", fake_names),
                     ("steam_get_update_impact", fake_impact),
                     ("_announcements", fake_news)):
        monkeypatch.setattr(S, name, fn)


def test_analyze_game_brief(monkeypatch):
    update = {"date": "2026-09-20", "title": "Patch 1.2", "url": "u",
              "before": {"reviews": 300, "positive_pct": 85.0},
              "after": {"reviews": 250, "positive_pct": 78.0},
              "change_pts": -7.0, "after_window_complete": False,
              "next_update_within_window": False}
    _analyze_game_fakes(monkeypatch, updates=[update],
                        news=[{"date": "2026-09-20", "title": "Patch 1.2", "url": "u"}])
    d = json.loads(run(S.steam_analyze_game(S.AnalyzeGameInput(
        appid=7, response_format="json"))))
    assert d["top_tags"] == ["Roguelike", "Co-op"]
    assert d["latest_update"]["change_pts"] == -7.0 and d["players_now"] == 3210
    md = run(S.steam_analyze_game(S.AnalyzeGameInput(appid=7)))
    assert "# Seven (appid 7)" in md and "$19.99 (-25% off)" in md
    assert "80.0% of 400, -10.0 pts vs all-time" in md
    assert "1 achievement ·" in md and "2 DLC" in md          # singular / plural
    assert "85.0% → 78.0% (-7.0 pts), still settling" in md
    assert "## Recent announcements" in md


def test_analyze_game_without_updates_or_news(monkeypatch):
    _analyze_game_fakes(monkeypatch)
    md = run(S.steam_analyze_game(S.AnalyzeGameInput(appid=7)))
    assert "Latest update" not in md and "Recent announcements" not in md


def test_analyze_game_passes_on_a_bad_appid(monkeypatch):
    _analyze_game_fakes(monkeypatch, row={"appid": 7, "error": "No store details."})
    assert run(S.steam_analyze_game(S.AnalyzeGameInput(appid=7))) == "No store details."


def test_a_new_clients_first_request_runs_alone(monkeypatch):
    """Cold-start TLS race guard: until one request has completed, nothing else
    goes out on a fresh shared client; afterwards requests overlap as usual."""
    active = peak = 0
    log = []

    class FakeClient:
        is_closed = False

        async def get(self, url, **kw):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            log.append(active)
            await asyncio.sleep(0.01)
            active -= 1
            return url

    async def go():
        fake = FakeClient()
        monkeypatch.setattr(S, "_CLIENT", fake)
        monkeypatch.setattr(S, "_CLIENT_WARM", False)
        monkeypatch.setattr(S, "_WARM_LOCK", asyncio.Lock())
        out = await asyncio.gather(*(S._send(fake, "get", f"u{i}") for i in range(5)))
        return out

    import asyncio
    out = run(go())
    assert out == [f"u{i}" for i in range(5)]
    assert log[:2] == [1, 1]           # the 2nd started only after the 1st ended
    assert peak > 1                    # the rest still ran concurrently
    assert S._CLIENT_WARM is True
