<!-- mcp-name: io.github.Sarg338/steam-mcp -->

# Steam MCP

A read-only [Model Context Protocol](https://modelcontextprotocol.io) server for
the public Steam Web API and storefront. It lets any MCP-compatible AI client
(Claude Desktop, Claude Code, etc.) answer questions about Steam — your friends,
games, playtime and achievements, plus account-independent things like sales,
reviews, ratings, live player counts, and patch notes.

**Bring your own key (BYOK):** each user supplies their own free Steam Web API
key via an environment variable. Nobody logs in, and no credentials pass through
this server beyond the key you set yourself.

---

## What it can answer

Account / profile (needs a public profile):
- "Who's on my Steam friends list, and who's online right now?"
- "What's my most-played game, and how many hours?"
- "Which achievements am I still missing in Hollow Knight?"
- "What's my Steam level?" / "Does this account have any VAC bans?"
- "What's on my wishlist, and is any of it on sale right now?"

Account-independent (works for any game, no SteamID needed):
- "Is *Baldur's Gate 3* any good? What's its review score?"
- "What's on sale on Steam right now?" / "What are the current top sellers and new releases?"
- "How many people are playing *Counter-Strike 2* this minute?"
- "What was in the latest *Dota 2* update?"
- "How much does *Hades II* cost and what genres is it?"

---

## Tools

| Tool | What it returns | Needs key? |
|------|-----------------|-----------|
| `steam_resolve_vanity_url` | Vanity name / profile URL → SteamID64 | yes |
| `steam_get_player_summary` | Status (Online/Away/In-Game…), current game, for 1–100 users | yes |
| `steam_get_friend_list` | Friends enriched with name + live status | yes |
| `steam_get_owned_games` | Owned games with total/recent hours (sortable) | yes |
| `steam_get_recently_played_games` | Last-2-weeks playtime | yes |
| `steam_get_steam_level` | Steam community level | yes |
| `steam_get_player_bans` | VAC / game / community / economy bans | yes |
| `steam_get_player_achievements` | Per-game unlocked vs locked achievements | yes |
| `steam_get_game_schema` | A game's full achievement/stat definitions | yes |
| `steam_get_global_achievement_percentages` | Achievement rarity (global %) | no |
| `steam_search_apps` | Game title → appid (+ price) | no |
| `steam_get_app_details` | Description, price, genres, release date, Metacritic | no |
| `steam_get_app_reviews` | Lifetime verdict, +/- counts, sample reviews; optional **recent (last-N-days) score** via `review_filter='recent'` | no |
| `steam_get_featured_specials` | Games currently on sale (regional) | no |
| `steam_get_store_highlights` | **Top sellers, new releases, or coming soon** | no |
| `steam_get_wishlist` | **A user's wishlist, with live prices + what's on sale** | yes |
| `steam_get_player_badges` | Badges + the XP breakdown behind a Steam level | yes |
| `steam_get_package_details` | Package/bundle price + included games | no |
| `steam_compare_players` | Shared games between two users, with playtime | yes |
| `steam_get_current_players` | Live concurrent player count | no |
| `steam_get_app_news` | Recent news / patch notes | no |

Every tool supports `response_format: "markdown"` (default, human-readable) or
`"json"` (structured), and all are annotated `readOnlyHint: true`.

> **Recent reviews:** Steam's API only exposes a *lifetime* review summary — there
> is no "last 30 days" field. So `steam_get_app_reviews` with
> `review_filter='recent'` computes that score itself by paginating the newest
> reviews within `day_range` days (default 30). For games with a very high volume
> of recent reviews it counts up to ~600 and marks the result `sampled: true`.

---

## Setup

### 1. Get a free Steam Web API key

Visit <https://steamcommunity.com/dev/apikey>, sign in, register a domain (any
domain you control works; `localhost` is commonly used for personal keys), and
copy the key. Usage is governed by the
[Steam Web API Terms of Use](https://steamcommunity.com/dev/apiterms).

### 2. Install

```bash
git clone https://github.com/Sarg338/steam-mcp.git
cd steam-mcp
pip install -e .          # or: pip install -r requirements.txt
```

Requires Python 3.10+.

### 3. Add it to your MCP client

The server reads the key from the `STEAM_API_KEY` environment variable.

**Claude Desktop** — edit `claude_desktop_config.json`
(`%APPDATA%\Claude\` on Windows, `~/Library/Application Support/Claude/` on macOS):

```json
{
  "mcpServers": {
    "steam": {
      "command": "python",
      "args": ["-m", "steam_mcp.server"],
      "env": { "STEAM_API_KEY": "YOUR_KEY_HERE" }
    }
  }
}
```

**Claude Code** (CLI):

```bash
claude mcp add steam --env STEAM_API_KEY=YOUR_KEY_HERE -- python -m steam_mcp.server
```

If you installed with `pip install -e .`, you can use the `steam-mcp` console
script as the command instead of `python -m steam_mcp.server`.

Restart your client and the Steam tools appear.

---

## Important limits (read before publishing)

- **Privacy gates everything.** Friends, owned games, and achievements are only
  visible if the target user's profile (and the relevant sub-setting, e.g. *Game
  Details* / *Friends List*) is set to **Public**. Private profiles return empty —
  this is a Steam setting, not a bug in this server.
- **No private-data access.** Steam has no OAuth flow that lets a third party read
  another user's *private* data. "Sign in through Steam" only proves identity; it
  unlocks nothing extra. This server is therefore public-data only, by design.
- **One key per user.** Because it's BYOK, each user's traffic counts against
  *their own* key's rate limit (~100,000 calls/day). Do not ship a shared key in
  a public deployment — it will get throttled or banned.
- **Read-only.** There are deliberately no tools that send messages, change
  status, launch games, or make purchases. The Steam Web API does not expose those
  anyway, and they're out of scope here.

---

## Development

```bash
pip install -e .
python -m py_compile steam_mcp/server.py     # syntax check
npx @modelcontextprotocol/inspector python -m steam_mcp.server   # interactive test
```

## License

MIT. Not affiliated with Valve. "Steam" is a trademark of Valve Corporation.
