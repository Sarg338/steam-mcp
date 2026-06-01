"""Tests for steam_mcp.server.

Covers the pure helpers, the TTL cache, and the tool logic with mocked HTTP —
no network and no API key required. Run with: pytest -q
"""
import asyncio
import json

import pytest

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

    monkeypatch.setattr(S, "_store_get", fake_store)
    out = run(S.steam_get_app_details(
        S.AppDetailsInput(appid=123, response_format="json")))
    d = json.loads(out)
    f = d["features"]
    assert f["is_singleplayer"] and f["is_coop"] and f["is_online_coop"]
    assert f["has_controller_support"] and f["has_cloud_saves"]
    assert d["dlc_count"] == 2
    assert d["full_audio_languages"] == ["English"]
    assert d["pc_requirements"]["minimum"] == "8GB RAM"   # label stripped


def test_wishlist_on_sale_filter(monkeypatch):
    async def fake_steam(path, params, **k):
        return {"response": {"items": [{"appid": 10, "priority": 0},
                                       {"appid": 11, "priority": 1}]}}

    prices = {
        10: {"appid": 10, "name": "OnSale", "price": "$5", "discount_pct": 50, "on_sale": True},
        11: {"appid": 11, "name": "Full", "price": "$20", "discount_pct": 0, "on_sale": False},
    }

    async def fake_app_price(appid, cc):
        return prices[appid]

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    monkeypatch.setattr(S, "_app_price", fake_app_price)
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

    monkeypatch.setattr(S, "_store_get", fake_store)
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

    async def fake_app_price(appid, cc):
        return prices[appid]

    monkeypatch.setattr(S, "_store_get", fake_store)
    monkeypatch.setattr(S, "_app_price", fake_app_price)

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
        S.PlayerGameInput(steamid="76561197960287930", appid=440,
                          response_format="json")))
    d = json.loads(out)
    assert d["game"] == "TF2" and d["stat_count"] == 2
    assert d["stats"][0] == {"name": "kills", "value": 100}


def test_user_game_stats_empty(monkeypatch):
    async def fake_steam(path, params, **k):
        return {"playerstats": {"gameName": "X", "stats": []}}

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    out = run(S.steam_get_user_game_stats(
        S.PlayerGameInput(steamid="76561197960287930", appid=1)))
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
    # the reviews tool takes the reviews input (has appid + review_filter),
    # not _fmt_review's raw-dict signature
    schema = json.dumps(by_name["steam_get_app_reviews"].inputSchema)
    assert "appid" in schema and "review_filter" in schema
