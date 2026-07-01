"""Tests for steam_mcp.tools.player — summaries and user groups."""
import json

from conftest import run

from steam_mcp import transport
from steam_mcp.models import PlayersInput
from steam_mcp.tools.player import (
    UserGroupsInput,
    steam_get_player_summary,
    steam_get_user_groups,
)


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

    monkeypatch.setattr(transport, "_steam_get", fake_steam)
    monkeypatch.setattr(transport, "_raw_get_text", fake_text)
    out = run(steam_get_user_groups(
        UserGroupsInput(steamid="76561197960287930", response_format="json")))
    d = json.loads(out)
    assert d["total"] == 2 and d["count"] == 2
    # sorted by member_count desc -> Steam Universe first
    assert d["groups"][0]["name"] == "Steam Universe"
    assert d["groups"][0]["member_count"] == 5000000
    assert d["groups"][0]["url"] == "https://steamcommunity.com/groups/SteamUniverse"
    assert d["groups"][1]["name"] == "Valve"


def test_player_summary(monkeypatch):
    async def fake_steam(path, params, **k):
        return {"response": {"players": [
            {"steamid": "76561197960287930", "personaname": "Gabe",
             "personastate": 1, "communityvisibilitystate": 3}]}}

    monkeypatch.setattr(transport, "_steam_get", fake_steam)
    out = run(steam_get_player_summary(
        PlayersInput(steamids=["76561197960287930"])))
    assert "Gabe" in out and "Online" in out
