<!-- mcp-name: io.github.Sarg338/steam-mcp -->

# Steam MCP

[![PyPI](https://img.shields.io/pypi/v/steam-mcp?cacheSeconds=3600)](https://pypi.org/project/steam-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/steam-mcp)](https://pypi.org/project/steam-mcp/)
[![CI](https://github.com/Sarg338/steam-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Sarg338/steam-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![MCP Registry](https://img.shields.io/badge/MCP%20Registry-io.github.Sarg338%2Fsteam--mcp-blue)](https://registry.modelcontextprotocol.io)

A read-only [Model Context Protocol](https://modelcontextprotocol.io) server for the
public Steam Web API and storefront — **41 tools, 5 prompts, and 2 resources** that let
any MCP client (Claude Desktop, Claude Code, Cursor, …) answer questions about Steam:
your friends, games, playtime, and achievements, plus account-independent things like
sales, reviews, live player counts, Steam Deck compatibility, discovery,
recommendations, and co-op planning.

**Read-only · official Steam APIs only · open source.** Nobody logs in, and the server
never writes, trades, posts, launches games, or makes purchases.

## Quick start — no API key needed

Install [`uv`](https://docs.astral.sh/uv/), then:

**Claude Code**

```bash
claude mcp add steam -- uvx steam-mcp
```

That's the whole setup. **19 of the 41 tools work with no credential at all** — anything
about the store or a game itself:

> *"Is Baldur's Gate 3 worth buying, and how are its recent reviews trending?"*
> *"What co-op games are on sale under £20 right now?"*
> *"How many people are playing Helldivers 2 this minute?"*
> *"Will Hades II run properly on my Steam Deck?"*

The three game-finders (`steam_discover`, `steam_should_i_buy`, `steam_recommend`) work
without a key too, as long as you don't personalize them.

### Adding your own account

A [free Steam Web API key](https://steamcommunity.com/dev/apikey) (a minute to get)
unlocks the other 22 — the ones that read a *specific account*: library, playtime,
friends, achievements, wishlist, inventory. Add `STEAM_USER` too and "my"/"I" default to
you, so you never have to paste a SteamID:

```bash
claude mcp add steam --env STEAM_API_KEY=YOUR_KEY --env STEAM_USER=your_steam_name -- uvx steam-mcp
```

> Tip: this defaults to the current project. Add `--scope user` only if you want
> Steam in *every* project — that keeps its tools in context everywhere, so prefer
> per-project scope unless Steam is cross-cutting for you.

**Claude Desktop** — download `steam-mcp.mcpb` from the
[latest release](https://github.com/Sarg338/steam-mcp/releases/latest) and open it
(Settings → Extensions). Both fields are optional; leave them blank for the keyless
tools and fill them in later.

Cursor / Cline / Windsurf and the manual `pip` setup are under [Setup](#setup) below.

> Without a key, the account tools are still listed but marked
> `[unavailable: needs STEAM_API_KEY]`, so your assistant knows to reach for a keyless
> tool instead of failing at one it can't use.

---

## What it can answer

Account / profile (needs a public profile; set `STEAM_USER` and "my"/"I" default
to you — no SteamID needed):
- "Who's on my friends list, and who's online right now?"
- "Which of my friends own *Helldivers 2* — and who's playing it now?"
- "It's game night — what co-op games do my online friends and I all own?"
- "Analyze my library — my backlog, and what I loved but abandoned."
- "Which achievements am I missing in *Hollow Knight*, and which are my rarest?"
- "What's on my wishlist, and is any of it on sale?"
- "Based on what I play most, what should I check out next?"
- "What's in my CS2 inventory, and which items are marketable?"

Account-independent (works for any game, no SteamID needed):
- "Is *Baldur's Gate 3* worth buying — and how are its recent reviews trending?"
- "What's on sale right now, and what are the current top sellers?"
- "How many people are playing *Counter-Strike 2* this minute?"
- "Will *Hades II* run on my Steam Deck?"
- "What's the Community Market price of a Field-Tested AK-47 | Redline?"
- "Is *Elden Ring* a soulslike? What are its community tags?"
- "Find well-reviewed co-op roguelikes under $20."
- "Recommend games like *Hollow Knight* that I don't already own."

---

## Tools

| Tool | What it returns | Needs key? |
|------|-----------------|-----------|
| `steam_resolve_vanity_url` | Vanity name / profile URL → SteamID64 | yes |
| `steam_get_player_summary` | Status (Online/Away/In-Game…), current game, for 1–100 users | yes |
| `steam_get_friend_list` | Friends enriched with name + live status | yes |
| `steam_find_friends_who_own` | **Which friends own (or are playing) a game** — "who can I play X with" | yes |
| `steam_get_user_groups` | The Steam groups/clans a user is in (name, URL, member count) | yes |
| `steam_plan_coop_night` | **Co-op games the host + friends all own** (ranked by owners) — or `mode="new"` for **fresh co-op games none of them own yet**; with who's online now | yes |
| `steam_get_owned_games` | Owned games with total/recent hours (sortable) | yes |
| `steam_analyze_library` | **Backlog, playtime distribution, abandoned games** across a whole library | yes |
| `steam_get_recently_played_games` | Last-2-weeks playtime | yes |
| `steam_get_steam_level` | Steam community level | yes |
| `steam_get_player_bans` | VAC / game / community / economy bans | yes |
| `steam_get_player_achievements` | Per-game unlocked vs locked achievements | yes |
| `steam_get_game_schema` | A game's achievement definitions (names, descriptions, hidden flag) | yes |
| `steam_get_global_achievement_percentages` | Achievement rarity (global %) | no |
| `steam_get_user_game_stats` | **A user's in-game stats** (kills, wins, distance…) for a game | yes |
| `steam_get_rarest_unlocks` | **A player's rarest achievement unlocks** in a game (by global rarity) | yes |
| `steam_search_apps` | Game title → appid (+ price) | no |
| `steam_discover` | **Find/recommend games** by tag, price, sale, platform, **release window** ("last N days") — optionally **personalized** to a user's taste (excludes games they own) | no* |
| `steam_should_i_buy` | **Buying brief** — price, lifetime + recent reviews (trend), tags, Metacritic, and your taste match | no* |
| `steam_recommend` | **Recommend games** like a seed game or your taste, with the shared tags as the "why" | no* |
| `steam_get_app_details` | **Full store details** — play modes/co-op, controller, DLC, languages, requirements, Metacritic, Steam Deck | no |
| `steam_get_deck_compatibility` | **Steam Deck rating** (Verified/Playable/Unsupported) + the per-criterion test results | no |
| `steam_get_dlc` | **A game's DLC**, with live prices and what's on sale | no |
| `steam_get_app_regional_pricing` | A game's price **across regions** (each in local currency) | no |
| `steam_get_workshop_item` | **Workshop item** metadata (game, tags, subscribers, favorites, views) | no |
| `steam_get_app_tags` | **A game's top community tags** (Souls-like, Roguelike, Cozy…) | no |
| `steam_get_app_reviews` | Lifetime verdict, +/- counts, sample reviews; optional **recent (last-N-days) score** via `review_filter='recent'` | no |
| `steam_analyze_game` | **One-call brief on a game**: price, all-time and 30-day reviews, players now, Deck, tags, the latest update's effect on reviews, and news | no |
| `steam_compare_games` | **Compare 2-5 games side by side**: price, reviews and their trend, players now, Steam Deck, co-op, tags | no |
| `steam_get_update_impact` | **Did an update change the reviews?** Review score in the days before vs after each recent patch | no |
| `steam_analyze_app_reviews` | **Analyze thousands of reviews**: sentiment over time, by language and playtime, Steam Deck, key activations vs Steam purchases, refunds, developer replies | no |
| `steam_get_featured_specials` | Games currently on sale (regional) | no |
| `steam_get_store_highlights` | **Top sellers, new releases, or coming soon** | no |
| `steam_get_wishlist` | **A user's wishlist, with live prices + what's on sale** | yes |
| `steam_get_inventory` | **A user's inventory** — game items or Steam Community items (cards, emoticons…), with tradable/marketable flags | yes† |
| `steam_get_market_price` | **Community Market price** for an item (lowest/median/24h volume) + type/rarity + CS2 condition | no |
| `steam_get_player_badges` | Badges + the XP breakdown behind a Steam level | yes |
| `steam_get_package_details` | Package/bundle price + included games | no |
| `steam_compare_players` | Shared games between two users, with playtime | yes |
| `steam_get_current_players` | Live concurrent player count | no |
| `steam_get_app_news` | Recent news / patch notes | no |

Every tool supports `response_format: "markdown"` (default) or `"json"`, and all are
annotated `readOnlyHint: true`. Prefer the composite tools (`steam_should_i_buy`,
`steam_recommend`, `steam_discover`, `steam_plan_coop_night`) over chaining several
calls, and ask for `json` only when you need to parse fields. Tools that read
localized text accept a `language` parameter — a Steam language name like `french` or
`schinese` (default `english`).

> \* `steam_discover`, `steam_should_i_buy`, and `steam_recommend` need no key for
> the store data; their **personalization** (passing a `steamid` to use a user's
> library/taste) requires a key and a public profile.
>
> † `steam_get_inventory` reads a keyless endpoint, but it still has to know *whose*
> inventory — and turning a vanity name (or `STEAM_USER`) into a SteamID64 is itself a
> keyed call. Pass a raw 17-digit SteamID64 and it works with no key.

### Prompts & resources

Beyond tools, the server ships **prompts** (guided one-click flows that orchestrate
the tools) and **resources** (reference Steam entities by URI):

- Prompts: `what_should_i_play`, `is_it_worth_buying`, `plan_game_night`,
  `steam_deals`, `game_overview`.
- Resources: `steam://app/{appid}` (store details) and `steam://user/{steamid}`
  (profile + live status).

> **Recent reviews:** `steam_get_app_reviews` with `review_filter='recent'` asks
> Steam for the exact review count and score of the last `day_range` days (default
> 30), the same numbers as the store page's "Recent Reviews" row. If Steam refuses
> that request it falls back to counting the newest reviews, up to
> `recent_max_reviews`, and marks the result `sampled: true`.
>
> **Whose reviews count:** by default both scores count what the store page
> counts: Steam purchases only for a paid game (key activations are left out) and
> everyone for a free game. `purchase_type='all'` or `'steam'` forces either.

> **Market prices:** `steam_get_market_price` uses Steam's Community Market
> endpoints, which are undocumented and tightly rate-limited. Results are cached
> briefly; an item with no current listings reports its price as unavailable.

---

## Setup

### 1. Get a free Steam Web API key *(optional)*

Skip this if you only want the 19 keyless tools — the server runs fine without a
key and the account tools simply advertise themselves as unavailable.

To unlock the account tools, visit <https://steamcommunity.com/dev/apikey>, sign in,
register a domain (any domain you control works; `localhost` is commonly used for
personal keys), and copy the key. Usage is governed by the
[Steam Web API Terms of Use](https://steamcommunity.com/dev/apiterms).

### 2. Install

The published package needs no checkout (Python 3.10+):

```bash
uvx steam-mcp          # zero-install via uv (recommended)
# or
pip install steam-mcp  # run as: python -m steam_mcp.server
```

Both MCP Python SDK majors work (`mcp>=1.28`). On the v2 SDK the server speaks
spec revision 2026-07-28 — stateless, no `initialize` handshake — advertises
cache hints on its tool/prompt/resource listings, and can ask you which Steam
account is yours when `STEAM_USER` isn't set (once per session, and only if your
client supports elicitation). On the v1.x line it serves the `initialize`
handshake that modern clients fall back to anyway. Nothing to configure either
way.

> **TLS note:** the HTTP client is `httpx2`, which verifies certificates against
> your **operating system's** trust store rather than a bundled CA list. If you
> run this somewhere minimal (a slim container with no system CA store, or behind
> a private CA), point `SSL_CERT_FILE` or `SSL_CERT_DIR` at a CA bundle.

### 3. Add it to your MCP client

Both settings are optional. `STEAM_API_KEY` unlocks the account tools; `STEAM_USER`
(your Steam vanity name, SteamID64, or profile URL) makes those tools default to *you*
whenever you don't name a user, so you never paste a SteamID. It's a public profile
name, not a secret, and you can still pass a `steamid` to any call to override it.

Configure neither and you get the keyless server; configure both and you get everything.

**Smaller tool set (optional).** Every tool's definition goes into the model's context
on every request, about 11.5k tokens for all 41. Set `STEAM_MCP_TOOLS=essentials` to
load just 15: search, the one-call game brief, details, reviews, compare, should-I-buy,
discover, recommend, update impact, sales, and your profile, library, library analysis,
wishlist and co-op night. Add others by name (`essentials,get_inventory`), or list
exactly the ones you want. The default is `all`. The built-in prompts may mention a
tool your set leaves out.

**Claude Code**

```bash
claude mcp add steam --env STEAM_API_KEY=YOUR_KEY --env STEAM_USER=your_steam_name -- uvx steam-mcp
```

> `STEAM_USER` is optional — drop the second `--env` if you'd rather give a
> SteamID to each call.

**Claude Desktop** — install `steam-mcp.mcpb` from the
[latest release](https://github.com/Sarg338/steam-mcp/releases/latest) via
Settings → Extensions and paste your key (and, optionally, your Steam name).

**Everything else** (Claude Desktop config, Cursor, Cline, Windsurf, VS Code, …) —
drop this block into the client's MCP config file:

```json
{
  "mcpServers": {
    "steam": {
      "command": "uvx",
      "args": ["steam-mcp"],
      "env": {
        "STEAM_API_KEY": "YOUR_KEY_HERE",
        "STEAM_USER": "your_steam_name"
      }
    }
  }
}
```

Installing with an AI agent such as Cline? Point it at
[`llms-install.md`](llms-install.md), which walks it through the setup.

Config locations: Claude Desktop `claude_desktop_config.json` (`%APPDATA%\Claude\`
on Windows, `~/Library/Application Support/Claude/` on macOS); Cursor
`.cursor/mcp.json`; Cline `cline_mcp_settings.json`. Restart the client and the
Steam tools appear. Running from a source checkout instead? Use
`"command": "python", "args": ["-m", "steam_mcp.server"]`.

---

## Security

Read-only, official-Steam-only, and bring-your-own-key. In short:

- **Read-only** — never writes, trades, posts, launches games, or buys anything.
- **Your key stays yours** — read from `STEAM_API_KEY`; never written to disk,
  logged, cached, or put in output (and redacted from error messages).
- **Official hosts only** — the request layer refuses any host that isn't
  `api.steampowered.com` / `store.steampowered.com` / `steamcommunity.com` (SSRF
  guard), with per-host rate limiting and retry/backoff.
- **Typed, validated inputs** (`extra="forbid"`); no data kept between requests
  beyond a small TTL cache of non-user store data.

Full details and how to report issues are in [SECURITY.md](SECURITY.md).

---

## Versioning & stability

`steam-mcp` follows [Semantic Versioning](https://semver.org). As of **1.0**, the
following are the **stable public surface** — they won't change without a major
(2.0) release:

- **Tool names** and their **input parameters** (names, types, whether required,
  defaults)
- **JSON output fields** (`response_format: "json"`) — names, types, and structure
- **Prompt** names/arguments and **resource** URI templates
  (`steam://app/{appid}`, `steam://user/{steamid}`)
- Core semantics: read-only, bring-your-own-key, prices in cents / playtime in
  minutes, and errors returned as strings

Within a major version, **minor** releases may *add* tools, prompts, resources,
optional parameters, and JSON fields; **patch** releases are bug fixes only. The
**Markdown** output wording, internal implementation, caching behavior, and which
Steam endpoints back a given tool may change at any time and are **not** part of
the contract.

---

## License

MIT. Not affiliated with Valve. "Steam" is a trademark of Valve Corporation.
