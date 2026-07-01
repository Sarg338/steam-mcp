"""Tests for steam_mcp.errors — scrubbing, privacy hints, error handling."""
from steam_mcp import errors
from steam_mcp.errors import PRIVACY_SETTINGS_URL, SteamApiError


def test_privacy_hint():
    h = errors._privacy_hint("Game details")
    assert "Game details" in h
    assert PRIVACY_SETTINGS_URL in h
    assert "public" in h.lower()


def test_scrub_api_key():
    key = "0123456789abcdef0123456789ABCDEF"
    out = errors._scrub(f"GET https://api.steampowered.com/x?key={key}&appid=1 failed")
    assert key not in out and "key=***" in out


def test_handle_error_scrubs_key_from_steamapi_error():
    key = "0123456789abcdef0123456789abcdef"
    msg = errors._handle_error(SteamApiError(f"boom key={key} happened"))
    assert key not in msg and "key=***" in msg
