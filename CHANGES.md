# Changelog

A concise, one-line-per-change history. Versions follow
[Semantic Versioning](https://semver.org/). Releases:
<https://github.com/Sarg338/steam-mcp/releases>

## [Unreleased]
- **Fixed: `steam_discover` threw away your `sort` whenever you used `released_within_days`.** Steam has no server-side date filter, so the window was enumerated newest-first and the sort was force-overridden to match — meaning "well-reviewed games from the last year" returned days-old unreviewed releases. The window is now enumerated newest-first across up to 3 search pages (stopping at the first pre-window release) and then **re-ranked client-side by the sort you asked for**, with a review-volume guard so a game with four glowing reviews doesn't outrank an established 90%-positive one and unreviewed games rank last. Review percentage and count are parsed out of the search HTML that was already being fetched, so this costs no extra requests.
- **Fixed: the release-window filter ran *after* the limit slice**, so `released_within_days` with `limit=20` returned only however many of the first 20 results happened to fall inside the window — frequently none, even with plenty of matches further down. The window is now applied across the whole candidate set before the slice.
- **Fixed: `steam_recommend` ignored your seed game if you also passed a `steamid`.** Basis precedence was tags > taste > seed, and the `steamid` is exactly what you must pass to get the ownership exclusion — so "games like Hades that I don't own" silently recommended from your most-played genres instead of from Hades. Precedence is now tags > seed > taste; a `steamid` always contributes the ownership exclusion and only seeds the tags when nothing else was given.
- `excluded_owned` (in both `steam_discover` and `steam_recommend`) now reports the candidates actually hidden rather than the size of your library — it read "excluding 900 you own" on a result set of 20.
- A null `total_count` from the store search no longer crashes the markdown summary.
- Added to the JSON output: `review_pct` and `review_count` per `steam_discover` result, and `window_coverage` (`full`/`partial`) when a release window is used — additive only, so the stability contract holds.

## [1.14.1]
- **Fixed: `steam_get_app_details` reported every feature as absent for non-English callers.** The `features` flags and the markdown play-mode line are derived by matching Steam's English category names ("Co-op", "Steam Cloud", …), but they were matched against the *localized* `categories` the request had asked for — so `language="french"` returned `is_coop: false`, `has_cloud_saves: false`, `is_singleplayer: false` and an empty play-mode list for every game, with no error to suggest anything was wrong. Detection now keys off the language-independent category ids, resolved through one extra cached lookup that is made only when the caller asked for something other than English and is skipped when the app has no categories. The categories and play modes *shown* stay in the caller's language, and a failed lookup falls back to the previous behavior rather than failing the call.
- Review and news excerpts were one character over their 280-character cap: the ellipsis was appended *past* the slice rather than replacing the last character it kept. Both now go through a shared `_excerpt()` helper that caps the total, matching what `_strip_html()` already did.
- **Fixed: achievement rarity failed outright when Steam returned a percentage as a string.** `GetGlobalAchievementPercentagesForApp` returns `percent` as a JSON number for most apps but as a string for some, and omits it for others; `round()` raised `TypeError` and took `steam_get_global_achievement_percentages` and `steam_get_rarest_unlocks` down with it. Percentages are now coerced, and a value that genuinely isn't a number is reported as unknown (`null`, sorted last) instead of failing the tool.

## [1.14.0]
- **The API key is now optional, and the server says so.** 15 of the 37 tools never needed a credential — store, games, reviews, prices, deals, tags, live player counts, Steam Deck, achievement rarity — and the three game-finders (`steam_discover`, `steam_should_i_buy`, `steam_recommend`) join them as long as you don't personalize with a `steamid`. `server.json` no longer marks `STEAM_API_KEY` as required (and now declares `STEAM_USER`, which it never did), so clients stop presenting a key as a precondition to installing. README leads with the keyless quickstart.
- When no key is configured, the tools that need one are marked `[unavailable: needs STEAM_API_KEY]` in their descriptions and the finders as `[works without a key unless you pass steamid]`. Without that the model picks an account tool, gets an error, and the server looks broken rather than unconfigured. The markers are omitted entirely when a key *is* present — every tool works then, so they would be pure tokens on every request.
- The "no key configured" error now names the keyless tools to use instead, rather than only saying what's missing.
- The `.mcpb` desktop bundle is registered with the MCP Registry as a second package, so the one-click Claude Desktop install is discoverable there and not only on the releases page. Its `fileSha256` is computed from the artifact attached to the release during publishing, since a hash kept in git goes stale the moment the bundle is rebuilt.

## [1.13.0]
- On the v2 SDK, the "about me" tools can now **ask who you are** instead of failing. Omit a `steamid` with no `STEAM_USER` configured and the server puts one question in front of you; the answer is reused for the rest of the session. It rides the negotiated protocol — a multi-round-trip `tools/call` on 2026-07-28, a push elicitation on 2025-11-25 and earlier — from one code path. The question is invisible to the model (it never enters a tool's input schema), is never asked when the call already names a user or `STEAM_USER` is set, and is never asked of a client that hasn't declared the elicitation capability — those clients keep today's "set STEAM_USER" error exactly as before, as does declining the question. The keyless game-finders (`steam_discover` / `steam_should_i_buy` / `steam_recommend`) still treat an omitted `steamid` as "don't personalize" and never ask.
- Switched our HTTP client from `httpx` to **`httpx2`**, which is what the v2 SDK uses — a fresh install now carries one HTTP stack instead of two. Note that httpx2 verifies TLS against the **operating system trust store** (via truststore) rather than certifi's bundled CA list: a minimal container with no system CA store, or a private CA that only certifi knew about, needs `SSL_CERT_FILE`/`SSL_CERT_DIR` set. Our API-key log scrubbing now covers the `httpx2`/`httpcore2` logger names as well as the old ones.

## [1.12.0]
- **Fixes a broken install.** The MCP Python SDK v2 removed `mcp.server.fastmcp` outright (FastMCP is now `MCPServer` under `mcp.server.mcpserver`), and our unbounded `mcp>=1.2.0` meant a fresh `uvx steam-mcp` picked up v2 and died with `ModuleNotFoundError`. The server now runs on **both** SDK majors — v2 (spec revision 2026-07-28) and the v1.x maintenance line — and the requirement is `mcp>=1.28`.
- On the v2 SDK the server speaks spec revision **2026-07-28** (stateless: no `initialize` handshake, no session id) and reports its own `version` in `serverInfo`. On v1.x it keeps using the `initialize` handshake, which modern clients still fall back to after probing `server/discover`.
- Cache freshness hints (SEP-2549, v2 SDK only): `tools/list`, `prompts/list`, `resources/list`, `resources/templates/list` advertise a 1-hour TTL and `resources/read` 10 minutes, all `public`. Our listings are static for the life of the process, so clients can stop re-fetching ~58 KB of `tools/list` on every reconnect.

## [1.11.0]
- New optional `STEAM_USER` config (set it next to your API key to your Steam vanity name / ID / profile URL). The "about me" tools — library, owned games, achievements, wishlist, friends, inventory, level, bans, badges, groups, co-op night, compare — now default to you when you omit the `steamid`, so you don't have to paste your ID every time. Passing a `steamid` still overrides. The keyless game-finders (discover / should_i_buy / recommend) keep personalization explicit.

## [1.10.0]
- `steam_plan_coop_night` gains `mode="new"` — recommend well-reviewed co-op games that NONE of the group owns yet (fresh picks to buy together), vs the default `mode="owned"` (games you already share). (Filtering which friends to include already works via the `friends` list.)

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
