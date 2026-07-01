"""Tests for steam_mcp.tools.intelligence — should-i-buy, recommend, co-op night."""
import json

from conftest import run

from steam_mcp import identity, transport
from steam_mcp.data import catalog, players, pricing, tags
from steam_mcp.tools.intelligence import (
    PlanCoopNightInput,
    RecommendInput,
    ShouldIBuyInput,
    steam_plan_coop_night,
    steam_recommend,
    steam_should_i_buy,
)


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

    monkeypatch.setattr(transport, "_store_get", fake_store)
    monkeypatch.setattr(transport, "_raw_get", fake_raw)
    monkeypatch.setattr(tags, "_items_tags", fake_items)
    monkeypatch.setattr(tags, "_tag_name_map", fake_map)
    out = run(steam_should_i_buy(ShouldIBuyInput(appid=5, response_format="json")))
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

    async def fake_app_prices(appids, cc):
        return {a: {"name": f"G{a}"} for a in appids}

    monkeypatch.setattr(tags, "_tag_name_map", fake_map)
    monkeypatch.setattr(tags, "_items_tags", fake_items)
    monkeypatch.setattr(catalog, "_discover_appids", fake_discover)
    monkeypatch.setattr(pricing, "_app_price", fake_app_price)
    monkeypatch.setattr(pricing, "_app_prices", fake_app_prices)
    out = run(steam_recommend(RecommendInput(seed_appid=100, response_format="json")))
    d = json.loads(out)
    assert d["basis"] == "like G100"
    ids = [r["appid"] for r in d["recommendations"]]
    assert 100 not in ids                       # seed excluded
    assert ids[0] == 40                          # most shared tags (3) ranked first
    assert d["recommendations"][0]["matching_tags"] == ["Roguelike", "Action", "Co-op"]


def test_recommend_requires_basis():
    out = run(steam_recommend(RecommendInput()))
    assert "Provide a basis" in out


def test_recommend_seed_beats_taste(monkeypatch):
    # "Games like X that I don't own": seed_appid + steamid together must anchor
    # the tags on the SEED game (not the user's most-played taste) while the
    # steamid still supplies the ownership exclusion. Regression for the basis
    # precedence running taste before seed.
    async def fake_resolve(identifier=None):
        return "76561197960287930"

    async def fake_taste(sid, **kw):
        # A taste wildly unlike the seed: competitive-multiplayer tags, owns 40.
        return {"owned_ids": {40, 999}, "seed_games": ["CS2", "Dota 2"],
                "tag_ids": [7, 8], "tag_names": ["FPS", "MOBA"]}

    async def fake_map():
        return {1: "Metroidvania", 2: "Souls-like", 7: "FPS", 8: "MOBA"}

    async def fake_items(appids):
        m = {100: [{"tagid": 1, "weight": 99}, {"tagid": 2, "weight": 50}],  # seed
             20: [{"tagid": 1, "weight": 10}, {"tagid": 2, "weight": 5}],
             30: [{"tagid": 1, "weight": 8}],
             40: [{"tagid": 1, "weight": 7}, {"tagid": 2, "weight": 6}]}     # owned
        return {a: m.get(a, []) for a in appids}

    captured = {}

    async def fake_discover(query):
        captured.update(query)
        return [20, 30, 40], 3

    async def fake_app_price(a, cc):
        return {"name": f"G{a}"}

    async def fake_app_prices(appids, cc):
        return {a: {"name": f"G{a}"} for a in appids}

    monkeypatch.setattr(identity, "_resolve_steamid", fake_resolve)
    monkeypatch.setattr(tags, "_taste_profile", fake_taste)
    monkeypatch.setattr(tags, "_tag_name_map", fake_map)
    monkeypatch.setattr(tags, "_items_tags", fake_items)
    monkeypatch.setattr(catalog, "_discover_appids", fake_discover)
    monkeypatch.setattr(pricing, "_app_price", fake_app_price)
    monkeypatch.setattr(pricing, "_app_prices", fake_app_prices)
    d = json.loads(run(steam_recommend(RecommendInput(
        seed_appid=100, steamid="sarg", response_format="json"))))
    assert d["basis"] == "like G100"                      # seed wins, not taste
    assert captured["tags"] == "1,2"                      # search filtered by SEED tags
    ids = [r["appid"] for r in d["recommendations"]]
    assert ids == [20, 30]                                # owned 40 excluded, seed absent
    assert d["excluded_owned"] == 1                       # 40 dropped; library size is 2


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

    monkeypatch.setattr(transport, "_steam_get", fake_steam)
    out = run(steam_plan_coop_night(
        PlanCoopNightInput(steamid=host, response_format="json")))
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

    monkeypatch.setattr(players, "_summaries_for", fake_summaries)
    monkeypatch.setattr(players, "_owned_set", fake_owned_set)
    monkeypatch.setattr(catalog, "_items_coop", fake_items_coop)

    d = json.loads(run(steam_plan_coop_night(PlanCoopNightInput(
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

    monkeypatch.setattr(players, "_summaries_for", fake_summaries)
    monkeypatch.setattr(players, "_owned_set", fake_owned_set)
    monkeypatch.setattr(tags, "_resolve_tag_ids", fake_resolve_tags)
    monkeypatch.setattr(catalog, "_discover_appids", fake_discover)
    monkeypatch.setattr(catalog, "_items_coop", fake_items_coop)
    monkeypatch.setattr(pricing, "_app_prices", fake_app_prices)

    d = json.loads(run(steam_plan_coop_night(PlanCoopNightInput(
        steamid=host, friends=[friend], mode="new", response_format="json"))))
    assert d["mode"] == "new"
    assert [g["appid"] for g in d["games"]] == [300, 400]  # owned excluded, non-coop dropped
    assert d["excluded_owned"] == 2                        # union of {100} and {200}
    assert {g["name"] for g in d["games"]} == {"Fresh Co-op A", "Fresh Co-op B"}
