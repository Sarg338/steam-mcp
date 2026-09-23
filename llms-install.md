# Installing steam-mcp (instructions for AI agents)

steam-mcp is a read-only Steam MCP server published on PyPI. **Do not clone the
repository**: it runs straight from PyPI with `uvx`.

## 1. Make sure `uv` is installed

Check with `uvx --version`. If it's missing, install uv:

- macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`

Python 3.10+ is required; uv fetches a suitable Python automatically if needed.

## 2. Ask the user for the optional settings

All three are optional. The server works without any of them: 19 of its 41
tools (store details, reviews, prices, sales, tags, player counts, Steam Deck,
comparisons) need no key at all.

| Variable | What to ask | If the user has none |
|---|---|---|
| `STEAM_API_KEY` | "Do you have a Steam Web API key? It's free at https://steamcommunity.com/dev/apikey and unlocks your library, playtime, friends, achievements, wishlist and inventory." | **Leave the variable out entirely.** Never put a placeholder like `YOUR_KEY_HERE`: the server would treat it as a real key and the account tools would fail. |
| `STEAM_USER` | "What's your Steam name (vanity URL name), SteamID64, or profile URL? Then 'my library' works without giving an ID." | Leave it out. |
| `STEAM_MCP_TOOLS` | Only mention it if the user wants a smaller context footprint: `essentials` loads 15 tools instead of 41. | Leave it out (all tools load). |

## 3. Add the server to the MCP settings

For Cline, edit `cline_mcp_settings.json`. For other clients, use the same
`mcpServers` block in their config file. Include only the `env` entries the user
actually gave you:

```json
{
  "mcpServers": {
    "steam": {
      "command": "uvx",
      "args": ["steam-mcp"],
      "env": {
        "STEAM_API_KEY": "<the user's key, only if provided>",
        "STEAM_USER": "<the user's Steam name, only if provided>"
      }
    }
  }
}
```

With no settings at all, `"env"` can be omitted:

```json
{
  "mcpServers": {
    "steam": {
      "command": "uvx",
      "args": ["steam-mcp"]
    }
  }
}
```

The API key is a secret: keep it in this config (or a `.env` file), never in
chat logs or committed files.

## 4. Verify

Call `steam_search_apps` with `{"params": {"query": "Hades"}}`. The first result
should be **Hades (appid 1145360)**. Then try `steam_analyze_game` with
`{"params": {"appid": 1145360}}` for a one-call brief.

If a key was configured, `steam_get_player_summary` with no `steamid` (when
`STEAM_USER` is set) should return the user's profile.

## Troubleshooting

- **`uvx` not found after installing uv:** restart the terminal or the client so
  the new PATH is picked up.
- **Account tools say they need a key:** `STEAM_API_KEY` is missing or wrong;
  the store and review tools still work.
- **"profile is private" errors:** the user's Steam privacy settings must set
  Game details (and Friends List / Inventory, for those tools) to Public.
- **Certificate errors behind a corporate proxy:** set `SSL_CERT_FILE` to the
  proxy's CA bundle.
