"""Error types, error formatting, and privacy hints for the Steam MCP server."""

from __future__ import annotations

import re

import httpx


class SteamApiError(Exception):
    """Raised for Steam-specific (non-HTTP) problems with an actionable message."""


def _scrub(text: str) -> str:
    """Redact a Steam Web API key (32 hex chars) from text — defense in depth so a
    key can never leak through an error message."""
    return re.sub(r"(?i)key=[0-9a-f]{32}", "key=***", text)


def _handle_error(e: Exception) -> str:
    """Consistent, actionable error formatting across all tools."""
    if isinstance(e, SteamApiError):
        return _scrub(f"Error: {e}")
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code == 401 or code == 403:
            return (
                "Error: Steam rejected the request (401/403). Your API key may be "
                "invalid, or the target profile is private. Verify STEAM_API_KEY."
            )
        if code == 404:
            return "Error: Not found (404). Check the SteamID / app ID is correct."
        if code == 429:
            return (
                "Error: Rate limited by Steam (429). The Web API allows ~100,000 "
                "calls/day per key. Wait and retry, or reduce request volume."
            )
        if code == 500:
            return (
                "Error: Steam returned 500. This often means the SteamID is invalid "
                "or the profile/app has no data for this endpoint."
            )
        return f"Error: Steam API request failed with HTTP {code}."
    if isinstance(e, httpx.TimeoutException):
        return "Error: Request to Steam timed out. Please try again."
    return _scrub(f"Error: Unexpected {type(e).__name__}: {e}")


PRIVACY_SETTINGS_URL = "https://steamcommunity.com/my/edit/settings"


def _privacy_hint(setting: str) -> str:
    """Actionable hint naming the exact Steam privacy sub-setting to make Public.

    Phrased to cover both 'this is my own profile' and someone else's — Steam
    privacy is granular, so the fix is usually flipping one specific sub-setting.
    """
    return (
        f"If it's your profile, set **{setting}** to Public in your Steam privacy "
        f"settings ({PRIVACY_SETTINGS_URL}); another user's data is only readable "
        f"if they've made it public."
    )
