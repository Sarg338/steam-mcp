"""Tests for steam_mcp.tools.achievements — user stats and rarest unlocks."""
import json

from conftest import run

from steam_mcp import transport
from steam_mcp.models import PlayerGameInput
from steam_mcp.tools.achievements import (
    RarestUnlocksInput,
    steam_get_rarest_unlocks,
    steam_get_user_game_stats,
)


def test_user_game_stats(monkeypatch):
    async def fake_steam(path, params, **k):
        return {"playerstats": {"gameName": "TF2", "stats": [
            {"name": "kills", "value": 100}, {"name": "deaths", "value": 50}]}}

    monkeypatch.setattr(transport, "_steam_get", fake_steam)
    out = run(steam_get_user_game_stats(
        PlayerGameInput(steamid="76561197960287930", appid=440,
                        response_format="json")))
    d = json.loads(out)
    assert d["game"] == "TF2" and d["stat_count"] == 2
    assert d["stats"][0] == {"name": "kills", "value": 100}


def test_user_game_stats_empty(monkeypatch):
    async def fake_steam(path, params, **k):
        return {"playerstats": {"gameName": "X", "stats": []}}

    monkeypatch.setattr(transport, "_steam_get", fake_steam)
    out = run(steam_get_user_game_stats(
        PlayerGameInput(steamid="76561197960287930", appid=1)))
    assert "No stats available" in out


def test_rarest_unlocks(monkeypatch):
    async def fake_steam(path, params, **k):
        if "GetPlayerAchievements" in path:
            return {"playerstats": {"success": True, "gameName": "G", "achievements": [
                {"apiname": "A", "name": "Ach A", "achieved": 1, "unlocktime": 1700000000},
                {"apiname": "B", "name": "Ach B", "achieved": 1, "unlocktime": 1700000000},
                {"apiname": "C", "name": "Ach C", "achieved": 0}]}}
        return {"achievementpercentages": {"achievements": [
            {"name": "A", "percent": 5.0}, {"name": "B", "percent": 80.0}]}}

    monkeypatch.setattr(transport, "_steam_get", fake_steam)
    out = run(steam_get_rarest_unlocks(
        RarestUnlocksInput(steamid="76561197960287930", appid=1,
                           response_format="json")))
    d = json.loads(out)
    assert d["unlocked_count"] == 2
    assert d["rarest"][0]["name"] == "Ach A"        # 5% rarer than 80%
    assert d["rarest"][0]["global_pct"] == 5.0
