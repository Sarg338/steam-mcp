# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org/).

## [0.11.0]

### Added
- `steam_plan_coop_night` — find **co-op games the host and their friends all
  own**, for game night. Intersects the host's library with friends' libraries,
  keeps games that support co-op (detected from store category data in one batched
  call), and ranks them by how many of the group own each. The group defaults to
  the host's friends who are **online right now** (pass an explicit `friends` list
  or `online_only=false` for everyone). Friends with private libraries are skipped
  and counted. Concurrent, bounded by `max_friends`.

## [0.10.0]

### Added
- `steam_should_i_buy` — a one-call **buying brief**: current price/discount,
  **lifetime AND last-30-days** review scores (with the trend between them), top
  community tags, Metacritic, and release status. Pass a `steamid` to add whether
  you already own it and which tags match your most-played games. Surfaces the
  facts for a reasoned call rather than hard-coding a verdict.
- `steam_recommend` — recommends games similar to a **seed game** ("like Hades"),
  to your **taste** (`steamid` → most-played + recent), or to explicit **tags** —
  excluding the seed and games you own, and explaining WHY each matches (the
  shared community tags). Ranked by tag overlap.

## [0.9.0]

### Added
- `steam_discover` — find games by **community tags (by name), max price, on-sale,
  platform, and free text**, sorted by review score / recency / price. Pass a
  `steamid` to **personalize**: it seeds the tag filter from that user's
  most-played + recently-played games and (by default) excludes games they already
  own — turning discovery into a recommendation engine ("what should I play next").
  Steam does the filtering server-side; results are enriched with live names/prices
  concurrently. The search needs no API key (personalization does).

## [0.8.1]

### Fixed
- The shared `httpx` client (added in 0.7.0) bound to the event loop it was first
  used on, so invoking the tools across multiple `asyncio.run()` calls in one
  process raised "RuntimeError: Event loop is closed". `_http_client()` is now
  loop-aware and rebuilds the client when the running loop changes. The long-lived
  MCP server (single event loop) was never affected; this fixes script/test usage.

### Added
- Ruff lint configuration (`[tool.ruff]`) and a GitHub Actions **CI** workflow
  (`.github/workflows/ci.yml`) running ruff + pytest on pushes and pull requests
  across Python 3.10–3.13.

## [0.8.0]

### Added
- `steam_find_friends_who_own` — which of a user's friends own (or are right now
  playing) a given game, with each owner's playtime and live status. Answers
  "who can I play X with". Checks friends concurrently; friends with private
  libraries are reported separately rather than guessed.
- `steam_get_rarest_unlocks` — a player's rarest unlocked achievements in a game,
  joining their unlocks with global unlock rarity to surface their best "flexes".
- `steam_get_app_tags` — a game's top community tags (Souls-like, Roguelike,
  Cozy, …) by player weight — the sub-genre/vibe signal Steam's official genres
  miss. Built from the storefront item API plus its public tag dictionary; no key.

## [0.7.0]

### Added
- `steam_get_dlc` — list a game's DLC (add-ons) with live prices and an
  on-sale filter, resolving the bare DLC appids that `steam_get_app_details`
  exposes into names + prices. Enrichment runs concurrently.
- `steam_get_user_game_stats` — a user's in-game stats (kills, wins, distance,
  etc.) for a specific game, via `ISteamUserStats/GetUserStatsForGame`. The
  numeric counterpart to per-game achievements.

### Changed
- **Performance:** all HTTP now goes through one shared, pooled `httpx`
  AsyncClient (keep-alive instead of a new connection per request), and the
  fan-out tools (wishlist enrichment, DLC, player comparison) issue their
  lookups concurrently with bounded parallelism instead of serially.

### Fixed
- **International pricing:** `steam_search_apps`, `steam_get_featured_specials`,
  `steam_get_store_highlights`, and `steam_get_package_details` now format
  prices in the requested country's currency (e.g. `£`, `€`) instead of always
  prefixing `$`.
- **Reviews tool registration:** the `@mcp.tool` decorator for
  `steam_get_app_reviews` was attached to an internal helper, so the exposed
  tool was mis-wired. It is now bound to the correct function.

## [0.6.0]

### Added
- **In-memory TTL cache** for static storefront/API responses (app details,
  package details, store highlights, game schemas, global achievement
  percentages). Speeds up tools that fan out many lookups (wishlist enrichment,
  comparisons) and eases the Steam rate limit. Live/user data is never cached.
- **Test suite** (`tests/`, `pytest`): pure-helper unit tests, TTL-cache tests,
  and tool-logic tests with mocked HTTP (no network or API key needed).

### Changed
- `.mcpbignore` and the sdist file list tidied so bundles/sdists stay lean.

## [0.5.0]

### Added
- `steam_analyze_library` — whole-library analysis: backlog (never-played),
  playtime histogram, most-played with last-played dates, recently active, and
  "abandoned" games (played but untouched for a configurable window).

### Changed
- `steam_get_app_details` is now comprehensive: play modes (single-player /
  co-op / online & local co-op), controller support, platforms, developers &
  publishers, DLC, supported languages (with full-audio flags), Metacritic,
  review and achievement counts, mature-content flags, optional PC requirements,
  and a `features` boolean object for easy filtering.

## [0.4.0]

### Added
- `steam_get_player_badges` — badges and the XP breakdown behind a Steam level.
- `steam_get_package_details` — price and included games for a package/bundle.
- `steam_compare_players` — shared games between two users, with playtime.

## [0.3.0]

### Added
- `steam_get_store_highlights` — top sellers, new releases, and coming soon.
- `steam_get_wishlist` — a user's wishlist, optionally enriched with live prices
  and an on-sale filter.

### Changed
- `steam_get_app_reviews` gained `review_filter='recent'`, computing the
  last-N-days score by paginating the newest reviews (Steam exposes no native
  recent-summary field).

## [0.2.0]

- Initial public release: 16 read-only tools across profiles, friends, games,
  playtime, achievements, store details, reviews, sales, live player counts, and
  news. Bring-your-own-key; packaged as a `.mcpb` desktop extension and for PyPI.

[0.11.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.11.0
[0.10.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.10.0
[0.9.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.9.0
[0.8.1]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.8.1
[0.8.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.8.0
[0.7.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.7.0
[0.6.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.6.0
[0.5.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.5.0
[0.4.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.4.0
[0.3.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.3.0
[0.2.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.2.0
