"""Tests for steam_mcp.tools.deals — featured specials and wishlist."""
import json

from conftest import run

from steam_mcp import transport
from steam_mcp.data import pricing
from steam_mcp.tools import deals
from steam_mcp.tools.deals import (
    FeaturedInput,
    WishlistInput,
    steam_get_featured_specials,
    steam_get_wishlist,
)


def test_featured_rows():
    rows = deals._featured_rows(
        [{"id": 1, "name": "G", "original_price": 1999, "final_price": 999,
          "discount_percent": 50}], 5)
    assert rows[0] == {"appid": 1, "name": "G", "original_price": 19.99,
                       "final_price": 9.99, "discount_pct": 50, "currency": None}


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

    monkeypatch.setattr(transport, "_steam_get", fake_steam)
    monkeypatch.setattr(pricing, "_app_prices", fake_app_prices)
    out = run(steam_get_wishlist(
        WishlistInput(steamid="76561197960287930", on_sale_only=True,
                      response_format="json")))
    d = json.loads(out)
    assert d["count"] == 1 and d["items"][0]["name"] == "OnSale"


def test_featured_specials_currency(monkeypatch):
    async def fake_fetch(cc):
        return {"specials": {"items": [
            {"id": 5, "name": "Deal", "original_price": 1999, "final_price": 999,
             "discount_percent": 50, "currency": "GBP"}]}}

    monkeypatch.setattr(pricing, "_fetch_featured", fake_fetch)
    out = run(steam_get_featured_specials(FeaturedInput(country_code="gb")))
    assert "£9.99" in out and "£19.99" in out and "$" not in out
