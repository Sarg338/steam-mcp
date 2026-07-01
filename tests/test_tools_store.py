"""Tests for steam_mcp.tools.store — search, details, DLC, tags, packages,
regional pricing, and Steam Deck compatibility."""
import json

from conftest import run

from steam_mcp import transport
from steam_mcp.data import catalog, pricing
from steam_mcp.tools.store import (
    AppDetailsInput,
    AppSearchInput,
    AppTagsInput,
    DeckCompatInput,
    DlcInput,
    PackageDetailsInput,
    RegionalPricingInput,
    steam_get_app_details,
    steam_get_app_regional_pricing,
    steam_get_app_tags,
    steam_get_deck_compatibility,
    steam_get_dlc,
    steam_get_package_details,
    steam_search_apps,
)


def test_app_details_language(monkeypatch):
    captured = {}

    async def fake_store(path, params, cache_ttl=0):
        captured.update(params)
        return {"5": {"success": True, "data": {"name": "G", "type": "game"}}}

    monkeypatch.setattr(transport, "_store_get", fake_store)
    run(steam_get_app_details(AppDetailsInput(appid=5, language="french")))
    assert captured.get("l") == "french"


def test_regional_pricing(monkeypatch):
    prices = {
        "us": {"name": "G", "price": "$10", "discount_pct": 0, "on_sale": False},
        "de": {"name": "G", "price": "9,99€", "discount_pct": 0, "on_sale": False},
        "br": {"name": "G", "price": "R$ 50", "discount_pct": 50, "on_sale": True},
    }

    async def fake_app_price(appid, cc):
        return prices[cc]

    monkeypatch.setattr(pricing, "_app_price", fake_app_price)
    out = run(steam_get_app_regional_pricing(RegionalPricingInput(
        appid=5, countries=["us", "de", "br"], response_format="json")))
    d = json.loads(out)
    assert d["name"] == "G"
    assert {p["country"]: p["price"] for p in d["prices"]} == {
        "us": "$10", "de": "9,99€", "br": "R$ 50"}
    assert [p for p in d["prices"] if p["country"] == "br"][0]["on_sale"] is True


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

    monkeypatch.setattr(transport, "_store_get", fake_store)
    monkeypatch.setattr(catalog, "_deck_compat", fake_deck)  # hermetic + assert below
    out = run(steam_get_app_details(
        AppDetailsInput(appid=123, response_format="json")))
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

    monkeypatch.setattr(transport, "_raw_get", fake_raw)

    # Helper: category mapping + loc_token humanization + glyphs.
    d = run(catalog._deck_compat(730))
    assert d["category"] == 2 and d["label"] == "Playable"
    assert d["items"][0]["status"] == "⚠"  # caveat
    assert d["items"][0]["text"] == "Controller Glyphs Do Not Match Deck Device"
    assert d["items"][1]["status"] == "✓"  # pass

    # Tool: markdown + json.
    md = run(steam_get_deck_compatibility(DeckCompatInput(appid=730)))
    assert "# Steam Deck: Playable" in md
    assert "Controller Glyphs Do Not Match Deck Device" in md
    j = json.loads(run(steam_get_deck_compatibility(
        DeckCompatInput(appid=730, response_format="json"))))
    assert j["appid"] == 730 and j["label"] == "Playable"

    # No published rating -> friendly message, not an error.
    async def fake_none(url, params, cache_ttl=0):
        return {"success": 0}
    monkeypatch.setattr(transport, "_raw_get", fake_none)
    msg = run(steam_get_deck_compatibility(DeckCompatInput(appid=1)))
    assert "No Steam Deck compatibility rating" in msg


def test_search_apps_currency(monkeypatch):
    async def fake_store(path, params, cache_ttl=0):
        return {"items": [
            {"id": 7, "name": "Game7", "price": {"currency": "EUR", "final": 1999}}]}

    monkeypatch.setattr(transport, "_store_get", fake_store)
    out = run(steam_search_apps(AppSearchInput(query="g", country_code="de")))
    assert "€19.99" in out and "$" not in out


def test_package_details_currency(monkeypatch):
    async def fake_store(path, params, cache_ttl=0):
        return {"55": {"success": True, "data": {
            "name": "Bundle",
            "price": {"currency": "GBP", "initial": 3000, "final": 1500,
                      "discount_percent": 50},
            "apps": [{"name": "A"}, {"name": "B"}]}}}

    monkeypatch.setattr(transport, "_store_get", fake_store)
    out = run(steam_get_package_details(
        PackageDetailsInput(packageid=55, country_code="gb")))
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

    monkeypatch.setattr(transport, "_store_get", fake_store)
    monkeypatch.setattr(pricing, "_app_prices", fake_app_prices)

    out = run(steam_get_dlc(DlcInput(appid=100, response_format="json")))
    d = json.loads(out)
    assert d["base_game"] == "Base"
    assert d["dlc_total"] == 2 and d["count"] == 2
    assert d["dlc"][0]["name"] == "DLC One"        # order preserved by gather

    out2 = run(steam_get_dlc(
        DlcInput(appid=100, on_sale_only=True, response_format="json")))
    d2 = json.loads(out2)
    assert d2["count"] == 1 and d2["dlc"][0]["name"] == "DLC Two"


def test_get_dlc_none(monkeypatch):
    async def fake_store(path, params, cache_ttl=0):
        return {"100": {"success": True, "data": {"name": "Base", "dlc": []}}}

    monkeypatch.setattr(transport, "_store_get", fake_store)
    out = run(steam_get_dlc(DlcInput(appid=100)))
    assert "no listed DLC" in out


def test_get_app_tags(monkeypatch):
    async def fake_steam(path, params, **k):
        return {"response": {"store_items": [{
            "appid": 1, "success": 1, "name": "Game",
            "tags": [{"tagid": 10, "weight": 100}, {"tagid": 20, "weight": 50},
                     {"tagid": 99, "weight": 10}]}]}}

    async def fake_raw(url, params, cache_ttl=0):
        return [{"tagid": 10, "name": "Roguelike"}, {"tagid": 20, "name": "Co-op"}]

    monkeypatch.setattr(transport, "_steam_get", fake_steam)
    monkeypatch.setattr(transport, "_raw_get", fake_raw)
    out = run(steam_get_app_tags(AppTagsInput(appid=1, response_format="json")))
    d = json.loads(out)
    assert d["name"] == "Game"
    assert d["count"] == 2                          # tagid 99 has no name -> skipped
    assert d["tags"][0] == {"tag": "Roguelike", "tagid": 10, "weight": 100}
    md = run(steam_get_app_tags(AppTagsInput(appid=1)))
    assert "Roguelike" in md and "Co-op" in md
