# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org/).

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

[0.6.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.6.0
[0.5.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.5.0
[0.4.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.4.0
[0.3.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.3.0
[0.2.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.2.0
