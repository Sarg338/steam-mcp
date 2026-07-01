"""Tests for steam_mcp.tools.friends — player comparison and friends-who-own."""
import json

from conftest import run

from steam_mcp import transport
from steam_mcp.data import pricing
from steam_mcp.tools.friends import (
    ComparePlayersInput,
    FriendsWhoOwnInput,
    steam_compare_players,
    steam_find_friends_who_own,
)


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

    monkeypatch.setattr(transport, "_steam_get", fake_steam)
    out = run(steam_compare_players(
        ComparePlayersInput(steamid_a=a, steamid_b=b, response_format="json")))
    d = json.loads(out)
    assert d["shared_count"] == 1
    assert d["shared"][0]["name"] == "Shared"
    assert d["shared"][0]["hours_a"] == 10.0 and d["shared"][0]["hours_b"] == 2.0


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

    monkeypatch.setattr(transport, "_steam_get", fake_steam)
    monkeypatch.setattr(pricing, "_app_price", fake_app_price)
    out = run(steam_find_friends_who_own(
        FriendsWhoOwnInput(steamid="76561197960287930", appid=730,
                           response_format="json")))
    d = json.loads(out)
    assert d["game"] == "Counter-Strike 2"
    assert d["total_friends"] == 3 and d["checked"] == 3
    assert d["owners"] == 1 and d["private_or_unknown"] == 1
    assert d["friends"][0]["name"] == "Alice"
    assert d["friends"][0]["playing_now"] is True
    assert d["friends"][0]["playtime_hours"] == 10.0
