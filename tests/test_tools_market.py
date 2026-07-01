"""Tests for steam_mcp.tools.market — workshop, inventory, and market prices."""
import json

from conftest import run

from steam_mcp import transport
from steam_mcp.tools import market
from steam_mcp.tools.market import (
    InventoryInput,
    MarketPriceInput,
    WorkshopItemInput,
    steam_get_inventory,
    steam_get_market_price,
    steam_get_workshop_item,
)


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

    monkeypatch.setattr(transport, "_steam_post", fake_post)
    out = run(steam_get_workshop_item(
        WorkshopItemInput(published_file_id=1619685021, response_format="json")))
    d = json.loads(out)
    assert d["title"] == "Move It" and d["app_id"] == 255710
    assert d["subscriptions"] == 3432587 and d["favorited"] == 159078
    assert d["tags"] == ["Mod"]
    assert d["description"] == "A mod"          # HTML stripped
    assert d["created"].startswith("2019-01")   # ts_to_date(1547052076)


def test_workshop_item_not_found(monkeypatch):
    async def fake_post(path, data, **k):
        return {"response": {"publishedfiledetails": [{"result": 9}]}}

    monkeypatch.setattr(transport, "_steam_post", fake_post)
    out = run(steam_get_workshop_item(WorkshopItemInput(published_file_id=1)))
    assert "No Workshop item found" in out


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

    monkeypatch.setattr(transport, "_raw_get", fake_raw)
    out = run(steam_get_inventory(
        InventoryInput(steamid="76561197960287930", response_format="json")))
    d = json.loads(out)
    assert captured["url"].endswith("/753/6")        # default app -> context auto 6
    assert d["total_inventory_count"] == 503
    items = {i["name"]: i for i in d["items"]}
    assert items["Booster Pack"]["count"] == 3        # 2 + 1 aggregated
    assert items["Booster Pack"]["marketable"] is True
    assert items["Emoticon"]["marketable"] is False
    assert d["items"][0]["name"] == "Booster Pack"    # most-numerous first

    # a game appid auto-picks context 2
    run(steam_get_inventory(InventoryInput(steamid="76561197960287930", appid=730)))
    assert captured["url"].endswith("/730/2")


def test_inventory_private(monkeypatch):
    async def fake_raw(url, params, cache_ttl=0):
        return None                                   # community endpoint: private/empty

    monkeypatch.setattr(transport, "_raw_get", fake_raw)
    out = run(steam_get_inventory(InventoryInput(steamid="76561197960287930")))
    # Privacy-aware message: names the Inventory setting + points to the fix.
    assert "Inventory" in out and "public" in out.lower()
    assert "steamcommunity.com/my/edit/settings" in out


def test_parse_cs_attributes():
    a = market._parse_cs_attributes("StatTrak™ AK-47 | Redline (Field-Tested)")
    assert a["exterior"] == "Field-Tested" and a["stattrak"] is True
    assert a["souvenir"] is False and a["star"] is False
    b = market._parse_cs_attributes("Souvenir AWP | Dragon Lore (Factory New)")
    assert b["exterior"] == "Factory New" and b["souvenir"] is True
    c = market._parse_cs_attributes("★ Karambit | Doppler (Minimal Wear)")
    assert c["star"] is True and c["exterior"] == "Minimal Wear"
    d = market._parse_cs_attributes("Mann Co. Supply Crate Key")
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

    monkeypatch.setattr(transport, "_raw_get", fake_raw)
    out = run(steam_get_market_price(MarketPriceInput(
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

    monkeypatch.setattr(transport, "_raw_get", fake_raw)
    out = run(steam_get_market_price(MarketPriceInput(
        appid=730, market_hash_name="Nonexistent Item")))
    assert "no current" in out.lower()
