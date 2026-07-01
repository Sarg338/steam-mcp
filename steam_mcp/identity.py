"""SteamID resolution (SteamID64 / vanity name / profile URL) for the Steam MCP server."""

from __future__ import annotations

from typing import Optional

from steam_mcp import config, transport
from steam_mcp.constants import PROFILE_URL_RE, STEAMID64_RE
from steam_mcp.errors import SteamApiError


async def _resolve_steamid(identifier: Optional[str] = None) -> str:
    """Resolve a flexible identifier to a 17-digit SteamID64.

    Accepts:
        - A raw SteamID64 (e.g. "76561197960287930")
        - A vanity / custom-URL name (e.g. "gabelogannewell")
        - A full profile URL (steamcommunity.com/id/<name> or /profiles/<id>)
        - None / empty -> falls back to the configured STEAM_USER (default user)

    Raises SteamApiError if a vanity name cannot be resolved, or if nothing was
    given and no STEAM_USER is configured.
    """
    raw = (identifier or "").strip()
    if not raw:
        raw = config._get_default_user()
        if not raw:
            raise SteamApiError(
                "No SteamID provided and no default user configured. Pass a "
                "steamid (SteamID64 / vanity name / profile URL), or set STEAM_USER "
                "in your MCP client config to your own Steam name."
            )

    # Full profile URL?
    m = PROFILE_URL_RE.search(raw)
    if m:
        kind, value = m.group(1).lower(), m.group(2)
        if kind == "profiles":
            # A /profiles/ URL must carry a 17-digit SteamID64. Validate before
            # returning it (it flows into a community URL path downstream), so junk
            # like "x@host" or path segments can't ride through as a "steamid".
            if STEAMID64_RE.match(value):
                return value
            raise SteamApiError(
                f"Malformed profile URL: /profiles/ must contain a 17-digit "
                f"SteamID64, got {value!r}."
            )
        raw = value  # /id/<vanity> -> resolve the vanity below

    # Already a SteamID64?
    if STEAMID64_RE.match(raw):
        return raw

    # Otherwise treat as a vanity name and resolve it.
    data = await transport._steam_get(
        "ISteamUser/ResolveVanityURL/v1/", {"vanityurl": raw}
    )
    resp = data.get("response", {})
    if resp.get("success") == 1 and resp.get("steamid"):
        return resp["steamid"]
    raise SteamApiError(
        f"Could not resolve '{identifier}' to a SteamID. Provide a 17-digit "
        f"SteamID64, an exact vanity name, or a full profile URL."
    )
