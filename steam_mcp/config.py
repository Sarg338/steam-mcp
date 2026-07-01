"""Environment / .env configuration (API key, default user) for the Steam MCP server."""

from __future__ import annotations

import os

from steam_mcp.errors import SteamApiError

ENV_KEY = "STEAM_API_KEY"
ENV_USER = "STEAM_USER"  # optional: the user's own SteamID64 / vanity / profile URL


def _dotenv_value(name: str) -> str:
    """Read a single NAME=value from a .env file in the project root (gitignored).

    Lets secrets/config live only in .env instead of the MCP client config. The
    root is the parent directory of this package, resolved from __file__ so it
    works regardless of cwd.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, ".env"), "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip().strip('"').strip()
    except OSError:
        pass
    return ""


def _load_key_from_dotenv() -> str:
    """Fallback: read STEAM_API_KEY from a .env file in the project root."""
    return _dotenv_value(ENV_KEY)


def _get_api_key() -> str:
    """Read the Steam Web API key from the environment or .env, or raise."""
    key = os.environ.get(ENV_KEY, "").strip() or _load_key_from_dotenv()
    if not key:
        raise SteamApiError(
            f"No Steam Web API key configured. Set the {ENV_KEY} environment "
            f"variable in your MCP client config, or put it in a .env file next to "
            f"the project. Get a free key at https://steamcommunity.com/dev/apikey"
        )
    return key


def _get_default_user() -> str:
    """Optional default user (STEAM_USER): a SteamID64, vanity name, or profile URL.

    Lets a user set their own identity once (env or .env) so the "about me" tools
    (library, achievements, wishlist, friends, ...) work without passing a steamid.
    Returns "" when unset. Not a secret — it's a public profile name.
    """
    return os.environ.get(ENV_USER, "").strip() or _dotenv_value(ENV_USER)
