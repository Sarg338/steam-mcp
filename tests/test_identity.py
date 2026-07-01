"""Tests for steam_mcp.identity — SteamID resolution short-circuits."""
import pytest

from conftest import run

from steam_mcp import identity
from steam_mcp.constants import STEAMID64_RE
from steam_mcp.errors import SteamApiError


def test_steamid64_regex():
    assert STEAMID64_RE.match("76561197960287930")
    assert not STEAMID64_RE.match("123")
    assert not STEAMID64_RE.match("12345678901234567")


def test_resolve_steamid_passthrough():
    assert run(identity._resolve_steamid("76561197960287930")) == "76561197960287930"
    assert run(identity._resolve_steamid(
        "https://steamcommunity.com/profiles/76561197960287930")) == "76561197960287930"


def test_resolve_profiles_url_requires_steamid64():
    sid = "76561197960287930"
    assert run(identity._resolve_steamid(
        f"https://steamcommunity.com/profiles/{sid}")) == sid
    # A malformed /profiles/ segment is rejected before any network call.
    with pytest.raises(SteamApiError):
        run(identity._resolve_steamid(
            "https://steamcommunity.com/profiles/x@169.254.169.254"))
