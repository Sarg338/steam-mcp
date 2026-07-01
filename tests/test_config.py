"""Tests for steam_mcp.config — API key and STEAM_USER default-user config."""
import pytest

from conftest import run

from steam_mcp import config, identity, transport
from steam_mcp.errors import SteamApiError
from steam_mcp.tools.library import OwnedGamesInput, steam_get_owned_games


def test_get_default_user_env(monkeypatch):
    monkeypatch.setenv("STEAM_USER", "76561197960287930")
    assert config._get_default_user() == "76561197960287930"
    monkeypatch.delenv("STEAM_USER", raising=False)
    monkeypatch.setattr(config, "_dotenv_value", lambda name: "")
    assert config._get_default_user() == ""


def test_resolve_steamid_defaults_to_steam_user(monkeypatch):
    # When no identifier is passed, fall back to the configured default user.
    monkeypatch.setattr(config, "_get_default_user", lambda: "76561197960287930")
    assert run(identity._resolve_steamid(None)) == "76561197960287930"
    assert run(identity._resolve_steamid("")) == "76561197960287930"
    assert run(identity._resolve_steamid("   ")) == "76561197960287930"


def test_resolve_steamid_no_default_raises(monkeypatch):
    monkeypatch.setattr(config, "_get_default_user", lambda: "")
    with pytest.raises(SteamApiError):
        run(identity._resolve_steamid(None))


def test_subject_tool_uses_default_user(monkeypatch):
    # A subject tool with the steamid omitted resolves to STEAM_USER end-to-end.
    monkeypatch.setattr(config, "_get_default_user", lambda: "76561197960287930")

    async def fake_steam(path, params, **k):
        assert params.get("steamid") == "76561197960287930"
        return {"response": {"game_count": 1, "games": [
            {"appid": 440, "name": "Team Fortress 2",
             "playtime_forever": 120, "playtime_2weeks": 0}]}}

    monkeypatch.setattr(transport, "_steam_get", fake_steam)
    out = run(steam_get_owned_games(OwnedGamesInput()))
    assert "Team Fortress 2" in out


def test_get_api_key_missing(monkeypatch):
    monkeypatch.delenv("STEAM_API_KEY", raising=False)
    monkeypatch.setattr(config, "_load_key_from_dotenv", lambda: "")
    with pytest.raises(SteamApiError):
        config._get_api_key()
