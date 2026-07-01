"""Tests for steam_mcp.tools.discovery — recommendation search."""
import json

from conftest import run

from steam_mcp import transport
from steam_mcp.data import pricing, tags
from steam_mcp.tools.discovery import DiscoverInput, steam_discover


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

    monkeypatch.setattr(transport, "_raw_get", fake_raw)
    monkeypatch.setattr(pricing, "_app_prices", fake_app_prices)
    out = run(steam_discover(DiscoverInput(term="x", response_format="json")))
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

    async def fake_app_prices(appids, cc):
        return {a: {"name": "G"} for a in appids}

    monkeypatch.setattr(tags, "_tag_name_map", fake_map)
    monkeypatch.setattr(transport, "_raw_get", fake_raw)
    monkeypatch.setattr(pricing, "_app_prices", fake_app_prices)
    out = run(steam_discover(DiscoverInput(
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

    monkeypatch.setattr(transport, "_steam_get", fake_steam)
    monkeypatch.setattr(tags, "_tag_name_map", fake_map)
    monkeypatch.setattr(transport, "_raw_get", fake_raw)
    monkeypatch.setattr(pricing, "_app_prices", fake_app_prices)
    out = run(steam_discover(DiscoverInput(
        steamid="76561197960287930", response_format="json")))
    d = json.loads(out)
    assert d["personalized"] is True
    assert d["taste_tags"] == ["Roguelike", "Action RPG"]   # derived from Hades
    assert "Hades" in d["seed_games"]
    assert d["filters"]["resolved_tag_ids"] == [1716, 4231]  # seeded from taste
    assert 99 not in [r["appid"] for r in d["results"]]     # owned game excluded
    assert [r["appid"] for r in d["results"]] == [20, 30]


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

    monkeypatch.setattr(transport, "_raw_get", fake_raw)
    monkeypatch.setattr(pricing, "_app_prices", fake_app_prices)
    d = json.loads(run(steam_discover(DiscoverInput(
        released_within_days=30, response_format="json"))))
    assert [r["appid"] for r in d["results"]] == [1, 3]   # 200-day-old #2 dropped
    assert d["filters"]["released_within_days"] == 30
