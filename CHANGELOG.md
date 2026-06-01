# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org/).

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

[0.8.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.8.0
[0.7.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.7.0
[0.6.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.6.0
[0.5.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.5.0
[0.4.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.4.0
[0.3.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.3.0
[0.2.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.2.0
