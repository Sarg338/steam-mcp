# Changelog

A concise, one-line-per-change history. Versions follow
[Semantic Versioning](https://semver.org/). Releases:
<https://github.com/Sarg338/steam-mcp/releases>

## [1.9.0]
- `steam_discover` gains a `released_within_days` filter — "what came out in the last N days" matching your tags/price/taste (newest-first). Release dates ride in the existing batched GetItems call, so no extra requests and negligible token cost.

## [1.8.3]
- Sharper descriptions on the overlapping game-finding tools (search / discover / recommend / should_i_buy / find-friends) with explicit "use this / not that" boundaries, to reduce wrong-tool selection. README notes Claude Code `--scope`.

## [1.8.2]
- Privacy-aware errors: when a profile or sub-setting is private, each tool names the exact Steam setting to make Public (Game details / Friends List / Inventory / My profile) and links the settings page.

## [1.8.1]
- Batched price lookups — one `GetItems` call per ~50 appids for wishlist / DLC / discover / recommend (fewer requests, less rate-limiting). Internal only.

## [1.8.0]
- Steam Deck compatibility: new `steam_get_deck_compatibility` tool + Deck rating inline in `steam_get_app_details` (37 tools).

## [1.7.7]
- ReDoS guard: cap input length before the HTML / CS-name / temp-client regexes.
- `/profiles/<id>` URLs must carry a valid SteamID64.

## [1.7.6]
- SSRF allowlist now enforced on every redirect hop, not just the first URL.
- API-key scrubbing extended to `SteamApiError` messages.

## [1.7.5]
- No "truncated" nag when a list limit is explicitly 0.

## [1.7.4]
- Temp-client matcher catches all-caps / mid-string "beta" (e.g. "REMATCH BETA TEST") and more build/test markers.

## [1.7.3]
- Cross-tool sweep: taste-seeding (recommend / discover / should_i_buy) and co-op night now drop non-retail clients.

## [1.7.2]
- `analyze_library` header shows the persona name (+ `persona_name` in JSON); clearer average wording.

## [1.7.1]
- Launched-but-tiny playtime renders `<0.1h` instead of a contradictory `0.0h` (+ `hours_str`).

## [1.7.0]
- `steam_analyze_library` excludes non-retail clients (betas/playtests/demos) by default — new `exclude_temp_clients`.

## [1.6.1]
- Abandoned-list header always shows `(N total, showing M)`, matching the Backlog header.

## [1.6.0]
- Abandoned list surfaces recently-dropped games first — new `abandoned_sort` (recent / oldest / playtime).

## [1.5.0]
- `backlog_limit` no longer truncates the Abandoned list — new independent `abandoned_limit`.

## [1.4.3]
- `analyze_library` backlog no longer an alphabetical slice: `backlog_limit` defaults to 100, with a `backlog_truncated` flag.

## [1.4.2]
- Keep the API key out of logs (quiet the httpx/httpcore loggers).

## [1.4.1]
- Bundle icon + expanded PRIVACY.md (Connectors Directory prep).

## [1.4.0]
- ~88% smaller tool descriptions on the wire; host allowlist, per-host rate limiting, API-key scrubbing; SECURITY.md + SKILL.md.

## [1.3.0]
- `steam_get_market_price` — Community Market price for an item (type/rarity + CS2 condition).

## [1.2.0]
- `steam_get_inventory` — a user's game or Steam Community inventory.

## [1.1.0]
- `steam_get_app_regional_pricing`, `steam_get_workshop_item`, `steam_get_user_groups`.

## [1.0.0]
- First stable release — public surface under a SemVer stability contract; retry with backoff; broader caching.

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
- `steam_get_dlc`, `steam_get_user_game_stats`; pooled httpx client + bounded concurrent fan-out; international price formatting.

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
