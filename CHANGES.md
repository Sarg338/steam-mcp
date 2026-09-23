# Changelog

A concise, one-line-per-change history. Versions follow
[Semantic Versioning](https://semver.org/). Releases:
<https://github.com/Sarg338/steam-mcp/releases>

## [Unreleased]
- **Fixed: `steam_mcp.__version__` still reported 1.11.2.**

## [1.17.0]
- **Fixed: review scores counted key activations the store page leaves out.**
- **New: `recent_max_reviews` on `steam_get_app_reviews`.**
- **New tool: `steam_analyze_app_reviews`.**
- **Smaller tool definitions: no more `"default": null` in schemas.**
- **Fixed: a game's achievement count read as 0 for non-English callers.**

## [1.16.1]
- **Fixed: a positive or negative `review_type` could misreport the overall score.**
- **Fixed: one failed page threw away the whole review answer.**
- **Fixed: the recent-review tally could count a review twice.**
- **Fixed: unknown achievement rarity ranked as the rarest.**
- **Cache evicts least-recently-used entries instead of clearing everything.**
- **The server tells clients that Steam user text is data, not instructions.**

## [1.16.0]
- **Smaller tool definitions: about 18% fewer tokens on every request.**
- **Tool results are sent once, not repeated in `structuredContent`.**
- **Compact JSON output, about 25% smaller.**
- **Every JSON list now has a `limit` and a truncation flag.**
- **Fixed: the inventory header claimed every item was shown.**
- **Leaner tool annotations.**
- **Fixed: `steam_discover` with `released_within_days` could drop unpriced games.**

## [1.15.0]
- **Fixed: `steam_discover` ignored `sort` when `released_within_days` was set.**
- **Fixed: the release-window filter ran after the limit.**
- **Fixed: `steam_recommend` ignored the seed game when a `steamid` was also given.**
- **Fixed: `excluded_owned` reported the library size, not the hidden candidates.**
- **Fixed: a null `total_count` crashed the discover summary.**
- **New JSON fields: `review_pct`, `review_count` and `window_coverage` in discover.**

## [1.14.1]
- **Fixed: `steam_get_app_details` reported every feature as absent for non-English callers.**
- **Fixed: excerpts ran one character over their 280-character cap.**
- **Fixed: achievement rarity failed when Steam returned a percentage as a string.**

## [1.14.0]
- **The API key is now optional, and the server says so.**
- **Tools that need a key are marked unavailable when none is configured.**
- **The no-key error names the keyless tools to use instead.**
- **The `.mcpb` bundle is registered with the MCP Registry.**

## [1.13.0]
- **The "about me" tools can ask who you are instead of failing (v2 SDK).**
- **Switched the HTTP client to `httpx2`.**

## [1.12.0]
- **Fixed: fresh installs broke on the MCP Python SDK v2.**
- **Supports spec revision 2026-07-28 on the v2 SDK.**
- **Cache freshness hints on list and read responses (v2 SDK).**

## [1.11.0]
- **New optional `STEAM_USER`: the "about me" tools default to you.**

## [1.10.0]
- **`steam_plan_coop_night` gains `mode="new"` for co-op games nobody owns yet.**

## [1.9.0]
- **`steam_discover` gains a `released_within_days` filter.**

## [1.8.3]
- **Sharper descriptions on the overlapping game-finding tools.**

## [1.8.2]
- **Privacy errors name the exact Steam setting to make Public.**

## [1.8.1]
- **Batched price lookups: one request per ~50 games.**

## [1.8.0]
- **New tool: `steam_get_deck_compatibility`, plus the Deck rating in app details.**

## [1.7.7]
- ReDoS guard: cap input length before the HTML / CS-name / temp-client regexes.
- `/profiles/<id>` URLs must carry a valid SteamID64.

## [1.7.6]
- SSRF allowlist now enforced on every redirect hop, not just the first URL.
- API-key scrubbing extended to `SteamApiError` messages.

## [1.7.5]
- No "truncated" nag when a list limit is explicitly 0.

## [1.7.4]
- Temp-client matcher catches more beta and test builds.

## [1.7.3]
- Recommendations and co-op night skip non-retail clients too.

## [1.7.2]
- `analyze_library` shows the persona name.

## [1.7.1]
- Launched-but-tiny playtime renders `<0.1h` instead of a contradictory `0.0h` (+ `hours_str`).

## [1.7.0]
- `steam_analyze_library` excludes betas, playtests and demos by default.

## [1.6.1]
- Abandoned-list header always shows `(N total, showing M)`, matching the Backlog header.

## [1.6.0]
- New `abandoned_sort`: recently dropped games first.

## [1.5.0]
- `backlog_limit` no longer truncates the Abandoned list — new independent `abandoned_limit`.

## [1.4.3]
- `analyze_library` backlog is no longer an alphabetical slice.

## [1.4.2]
- Keep the API key out of logs (quiet the httpx/httpcore loggers).

## [1.4.1]
- Bundle icon + expanded PRIVACY.md (Connectors Directory prep).

## [1.4.0]
- ~88% smaller tool descriptions; security hardening.

## [1.3.0]
- `steam_get_market_price` — Community Market price for an item (type/rarity + CS2 condition).

## [1.2.0]
- `steam_get_inventory` — a user's game or Steam Community inventory.

## [1.1.0]
- `steam_get_app_regional_pricing`, `steam_get_workshop_item`, `steam_get_user_groups`.

## [1.0.0]
- First stable release, under a SemVer stability contract.

## [0.12.0]
- MCP prompts + resources + localization (`language` parameter).

## [0.11.0]
- `steam_plan_coop_night`.

## [0.10.0]
- `steam_should_i_buy`, `steam_recommend`.

## [0.9.0]
- `steam_discover`.

## [0.8.1]
- Loop-aware shared httpx client fix; CI (ruff + pytest across 3.10–3.13).

## [0.8.0]
- `steam_find_friends_who_own`, `steam_get_rarest_unlocks`, `steam_get_app_tags`.

## [0.7.0]
- `steam_get_dlc`, `steam_get_user_game_stats`; international prices.

## [0.6.0]
- In-memory TTL cache for static responses; test suite.

## [0.5.0]
- `steam_analyze_library`; comprehensive `steam_get_app_details`.

## [0.4.0]
- `steam_get_player_badges`, `steam_get_package_details`, `steam_compare_players`.

## [0.3.0]
- `steam_get_store_highlights`, `steam_get_wishlist`; recent-reviews filter.

## [0.2.0]
- Initial public release — 16 read-only tools; BYOK; `.mcpb` + PyPI.
