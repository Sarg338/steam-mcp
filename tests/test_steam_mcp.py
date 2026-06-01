"""Tests for steam_mcp.server.

Covers the pure helpers, the TTL cache, and the tool logic with mocked HTTP —
no network and no API key required. Run with: pytest -q
"""
import asyncio
import json
import logging

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

    async def fake_app_price(appid, cc):
        return prices[appid]

    monkeypatch.setattr(S, "_raw_get", fake_raw)
    monkeypatch.setattr(S, "_app_price", fake_app_price)
    out = run(S.steam_discover(S.DiscoverInput(term="x", response_format="json")))
    d = json.loads(out)
    assert d["total_count"] == 3 and d["count"] == 3
    assert [r["appid"] for r in d["results"]] == [10, 20, 30]   # ranked order kept
    assert d["personalized"] is False


def test_discover_explicit_tags(monkeypatch):
    captured = {}

    async def fake_map():
        return {29482: "Souls-like"}

    async def fake_raw(url, params, cache_ttl=0):
        captured.update(params)
        return {"success": 1, "total_count": 1,
                "results_html": '<a data-ds-appid="5"></a>'}

    async def fake_app_price(appid, cc):
        return {"name": "G"}

    monkeypatch.setattr(S, "_tag_name_map", fake_map)
    monkeypatch.setattr(S, "_raw_get", fake_raw)
    monkeypatch.setattr(S, "_app_price", fake_app_price)
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

    async def fake_app_price(appid, cc):
        return prices.get(appid, {})

    monkeypatch.setattr(S, "_steam_get", fake_steam)
    monkeypatch.setattr(S, "_tag_name_map", fake_map)
    monkeypatch.setattr(S, "_raw_get", fake_raw)
    monkeypatch.setattr(S, "_app_price", fake_app_price)
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

    monkeypatch.setattr(S, "_tag_name_map", fake_map)
    monkeypatch.setattr(S, "_items_tags", fake_items)
    monkeypatch.setattr(S, "_discover_appids", fake_discover)
    monkeypatch.setattr(S, "_app_price", fake_app_price)
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


def test_resources_registered():
    uris = {t.uriTemplate for t in run(S.mcp.list_resource_templates())}
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
    captured = {}

    async def fake_store(path, params, cache_ttl=0):
        captured.update(params)
        return {"5": {"success": True, "data": {"name": "G", "type": "game"}}}

    monkeypatch.setattr(S, "_store_get", fake_store)
    run(S.steam_get_app_details(S.AppDetailsInput(appid=5, language="french")))
    assert captured.get("l") == "french"


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
    assert "private" in out.lower()


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
    # The API key rides in request URLs (?key=); httpx/httpcore log those at INFO,
    # so importing the server must have quieted them to keep the key out of logs.
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


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
    ]
    for n in temp:
        assert S._is_temp_client(n), f"should flag: {n}"
    # Retail titles that share tokens must NOT be flagged (precision over recall).
    retail = [
        "Prototype",            # the 2009 retail game
        "Prototype 2",
        "Trials Rising",
        "Alpha Protocol",
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
    schema = json.dumps(by_name["steam_get_app_reviews"].inputSchema)
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
