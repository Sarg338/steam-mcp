# Changelog

All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org/).

## [1.8.0]

### Added
- **Steam Deck compatibility** — new tool **`steam_get_deck_compatibility`** returns
  Valve's official Deck rating (Verified / Playable / Unsupported / Unknown) plus the
  per-criterion test results (default controller config, interface text legibility,
  default performance, …), each marked pass (✓) or caveat (⚠). No API key required.
  Sourced from the storefront's `ajaxgetdeckappcompatibilityreport` (cached 24h).
- **`steam_get_app_details` now includes the Deck rating inline** (a `steam_deck`
  field in JSON, a "Steam Deck" line in Markdown), fetched concurrently with the
  store details and best-effort (a Deck-lookup failure never breaks app details).

That brings the surface to **37 tools**. Additive — backward-compatible.

## [1.7.7]

Hardening from an adversarial (red-team) pass. The audit confirmed no API-key
leak, SSRF/allowlist bypass, injection, or path-traversal was reachable; the items
below are the residual robustness findings.

### Security
- **ReDoS guard: cap input length before three quadratic regexes.** `_strip_html`
  (`<[^>]+>`), `_parse_cs_attributes` (`\(([^)]+)\)\s*$`), and the temp-client
  `_TEMP_SUFFIX_RE` are O(n²) on pathological input (e.g. a flood of unmatched `<`
  or `(`). These run on upstream Steam-controlled text (store descriptions, game
  names), so a malicious/oversized field could stall the event loop for seconds.
  Inputs are now length-capped (HTML 20k, CS hash 300, names 200) before the regex,
  bounding worst-case work to a constant. Normal output is unchanged.
- **`/profiles/<id>` URLs must carry a valid SteamID64.** `_resolve_steamid` now
  validates the captured segment against the SteamID64 pattern and rejects junk
  (e.g. `x@host`) early, instead of passing it downstream into a community URL path
  (where it was already neutralized by percent-encoding — this closes the door).

## [1.7.6]

Security hardening from an internal audit. No tool/API changes.

### Security
- **SSRF allowlist now enforced on every redirect hop, not just the first URL.**
  The HTTP client follows redirects, so a pre-flight `_check_host` on the initial
  URL alone could be bypassed by a 3xx that left the allowlist (e.g. to a
  cloud-metadata or internal host). A request `event_hook` (`_enforce_host`) now
  re-checks the host allowlist before every hop, including redirects. (Practical
  risk was low — it requires Steam itself to redirect off-domain — but the
  allowlist is a documented control and should hold per-hop.)
- **API key scrubbing now also covers `SteamApiError` messages.** `_handle_error`
  ran `_scrub` only on the generic-exception branch; it now scrubs the
  `SteamApiError` branch too, so any future error string that includes a request
  URL can't surface the `?key=` value. (No current message contained it.)

## [1.7.5]

### Fixed
- **No "truncated" nag when a list limit is explicitly 0.** Calling
  `steam_analyze_library` with `backlog_limit=0` (or `abandoned_limit=0`) used to
  still print "⚠️ Backlog truncated: showing 0 of 90 … call again" and set
  `backlog_truncated: true`. Asking for zero is intentional suppression, not
  truncation — so both the warning and the JSON flag are now `false` when the limit
  is 0.

## [1.7.4]

### Fixed
- **Temp-client matcher now catches names like "REMATCH BETA TEST".** The matcher
  was already case-insensitive (so all-caps wasn't the issue) — the gap was token
  coverage: "beta" was only matched as a *trailing* qualifier, and "beta test"
  wasn't a recognized phrase. Standalone **`beta`** is now matched anywhere in the
  name, and **`alpha test` / `alpha build` / `closed`+`open alpha` / `play test` /
  `test server`+`client`+`build` / `dev`+`developer`+`press`+`preview build` /
  `ptr`** were added. Bare `test` and `alpha` are deliberately still excluded (they
  collide with *The Turing Test*, *Test Drive*, *Alpha Protocol*, *Alpha Centauri*),
  so those remain unflagged. Improves `steam_analyze_library`, `steam_recommend`,
  `steam_discover`, `steam_should_i_buy`, and `steam_plan_coop_night`.

## [1.7.3]

### Fixed
- **Non-retail clients no longer distort recommendations (cross-tool sweep of the
  1.7.0 filter).** `_taste_profile` — which seeds taste from a user's most-played
  and recently-played games for **`steam_recommend`**, **`steam_discover`**, and
  **`steam_should_i_buy`** — now drops betas/playtests/demos/test clients before
  seeding, so a 165h playtest can't dominate the tag profile. **`steam_plan_coop_night`**
  likewise skips temp clients when listing co-op suggestions (an unlaunchable beta
  is a dead game-night pick). Behavior-only; no parameter or field changes.

### Audit notes
- Swept the sibling tools for the 1.5.0 limit-coupling bug: none recur.
  `steam_get_owned_games`, `steam_compare_players`, `steam_recommend`, and
  `steam_discover` each return a single bounded list, and `steam_plan_coop_night`
  already uses independent controls (`max_friends`, `min_friends_owning`, `limit`).
- `steam_get_owned_games` and `steam_compare_players` are intentionally left
  unfiltered: they report a factual inventory / shared-overlap, where hiding owned
  games would misrepresent the counts. Filtering is applied only on "what to play"
  surfaces.

## [1.7.2]

### Changed
- **`steam_analyze_library` header now shows the persona name**, e.g. `Library
  analysis for Sarg338 (7656…)` instead of a bare SteamID64. The display name comes
  from a small best-effort `GetPlayerSummaries` lookup (the identifier resolver only
  yields a SteamID64); if it's unavailable the header falls back to the SteamID, and
  a lookup failure never breaks the analysis. JSON gains a `persona_name` field.
- **Clearer average wording**: `Avg hours/game: 27.9 across all owned, 35.7 across
  played games` (was the awkward "27.9 owned, 35.7 of played"). Same math.

## [1.7.1]

### Fixed
- **No more "0.0h" on a game classified as played/abandoned.** A game launched only
  briefly (1–2 minutes) has a positive `playtime_forever` that *rounds* to 0.0h, so
  it correctly counted as played/abandoned but rendered as a contradictory "0.0h".
  The never-vs-played predicate is unchanged and was already consistent everywhere
  (`playtime_forever` minutes > 0; the `0h` bucket is exactly 0 minutes) — now
  documented in code — and launched-but-tiny playtimes render as **`<0.1h`** instead
  of `0.0h`. JSON rows gain a `hours_str` display field alongside the numeric
  `hours` (which still rounds to 0.0, so the data stays exact). Additive field only.

## [1.7.0]

### Added
- **`steam_analyze_library` now excludes non-retail clients by default** — betas,
  playtests, demos, trials, test servers, staging branches, and prototypes. These
  show up in Steam's owned-games list as ordinary games (often with real playtime),
  but they're frequently unlaunchable, so recommending one as "play next" is dead on
  arrival. They're detected by name (Steam's owned-games payload carries no type, so
  this is a best-effort heuristic tuned for precision — retail titles like
  *Prototype*, *Prototype 2*, *Trials Rising*, and *Alpha Protocol* are **not**
  flagged). The filtering is never silent: the excluded count appears in every
  response (`summary.temp_clients_excluded` + a Markdown note), the excluded names
  are listed in JSON (`temp_clients_excluded_names`), and a new
  **`exclude_temp_clients`** parameter (default `true`) turns it off to include
  everything in all stats and lists.

### Changed
- With filtering on (the default), the summary stats — games owned, total played,
  both averages, and the playtime distribution buckets — now reflect retail games
  only. The excluded count is always reported so the delta is explicit.

## [1.6.1]

### Changed
- **The Abandoned list header now always shows `(N total, showing M)`, matching the
  Backlog header.** Since 1.5.0 the abandoned count only appeared when the list was
  truncated, and in a slightly different format; now both lists use the identical
  `{title} — {description} (N total, showing M)` shape unconditionally, so a caller
  can always see how many abandoned games exist and whether the list is truncated.
  Markdown display only — no schema, field, or parameter changes.

## [1.6.0]

### Added
- **`steam_analyze_library` gains an `abandoned_sort` parameter** —
  `recent` (default), `oldest`, or `playtime`.

### Fixed
- **The Abandoned list now surfaces recently-dropped games first.** It was sorted
  oldest-last-played first, so on a library with a big pile of long-ago abandons
  the truncated list was entirely ancient games and a title dropped a few months
  ago never appeared — the opposite of what "what should I play next" wants. The
  default order is now most-recently-dropped first (`abandoned_sort="recent"`), with
  `oldest` (the previous behavior) and `playtime` (most hours sunk first — the
  strongest re-engagement signal) available as alternatives. Output-ordering +
  additive-parameter change; no schema/field removals, backward-compatible.

## [1.5.0]

### Added
- **`steam_analyze_library` gains an `abandoned_limit` parameter** (default 25,
  range 0–100). The "Abandoned" list now has its own control.

### Fixed
- **`backlog_limit` no longer truncates the Abandoned list.** Both the never-played
  backlog and the abandoned-games list were sliced by the single `backlog_limit`,
  so setting `backlog_limit=3` for a tight backlog silently shrank the unrelated
  Abandoned list to 3 as well. The two are now independent (`abandoned_limit`
  governs Abandoned). Since the abandoned list is sorted most-stale-first, its
  default cap of 25 keeps the longest-dropped games. The Markdown header and a new
  `abandoned_truncated` JSON field now report when it's truncated, mirroring
  `backlog_truncated`. Additive (new optional param, new JSON field) — no removals,
  backward-compatible.

## [1.4.3]

### Fixed
- **`steam_analyze_library` no longer recommends from an alphabetical slice of the
  backlog.** The never-played list is sorted A–Z, and `backlog_limit` defaulted to
  25 — so on a 100+ game backlog the model only ever saw the early letters (e.g. it
  could recommend a game starting with "B" while never seeing Skyrim or Titan
  Quest). `backlog_limit` now defaults to the **100-game maximum**, and when the
  backlog still overflows the output is explicitly flagged truncated (a new
  `backlog_truncated` field in JSON and a ⚠️ line in Markdown) telling the caller to
  see the full set before recommending. Additive/behavior-only — no schema or field
  removals, so it stays within the 1.x stability contract.

## [1.4.2]

### Fixed
- **Keep the API key out of logs.** `httpx`/`httpcore` log full request URLs at
  INFO, and Steam requires the key as a `?key=` query parameter — so those loggers
  are now quieted to `WARNING`, preventing the key from ever landing in logs the
  host might capture. (Complements the existing error-message scrubbing.)

## [1.4.1]

### Added
- A bundle **icon** (`icon.png`, referenced from `manifest.json`) and an expanded
  `PRIVACY.md` (explicit storage / third-party-sharing / retention sections) — to
  meet the Anthropic Connectors Directory submission requirements. No code changes.

## [1.4.0]

Token-efficiency + security hardening. No tool changes (still 36 tools, 5 prompts,
2 resources) — fully backward-compatible.

### Changed
- **~88% smaller tool descriptions on the wire.** FastMCP was sending each tool's
  full docstring as its MCP description (~5,200 tokens across the toolset, every
  request); they're now trimmed to their one-line summary (~630 tokens). The full
  docstrings stay in the source for humans/IDEs.

### Added
- **Security hardening:**
  - **Host allowlist** — the request layer refuses any host other than
    `api.steampowered.com` / `store.steampowered.com` / `steamcommunity.com`
    (SSRF defense-in-depth).
  - **Per-host rate limiting** — token-bucket limiters (burst-friendly, so fan-out
    isn't serialized) on top of the existing 429 retry/backoff.
  - **API-key scrubbing** — keys are redacted from any error output.
- `SECURITY.md` documenting the full posture, and `SKILL.md` (an agent skill that
  teaches token-efficient, correct use of the toolset).

## [1.3.0]

### Added
- `steam_get_market_price` — Community Market price for a single item: current
  lowest + median price and 24-hour volume, plus the item's type/rarity (e.g.
  "Classified Rifle", "Mythical Bow") and listing count, and — for CS2 — the
  wear/exterior, StatTrak™, Souvenir, and ★ flags parsed from the name. No API
  key. Uses Steam's Community Market endpoints (undocumented + tightly
  rate-limited), so prices are cached briefly and best-effort.

### Changed
- Non-goals updated: market *current* prices are now in scope (read-only, via the
  official `priceoverview` endpoint). Market *history* (needs a logged-in session),
  player-count history, and write/trade actions remain out of scope.

## [1.2.0]

### Added
- `steam_get_inventory` — a user's Steam inventory for any app: a game's items
  (CS2, TF2, Dota 2, …) or the **Steam Community** inventory (app 753 — trading
  cards, emoticons, backgrounds, gems). Aggregates duplicates by quantity and flags
  tradable/marketable; the context is auto-picked from the app. Requires the
  target's inventory privacy to be Public; no API key required.

## [1.1.0]

### Added
- `steam_get_app_regional_pricing` — a game's price across multiple countries at
  once, each in its own local currency (not converted — regions use different
  currencies). No API key.
- `steam_get_workshop_item` — metadata for a Steam Workshop item (title, game,
  description, tags, subscribers / favorites / views, created/updated). No API key.
- `steam_get_user_groups` — the Steam groups/clans a user belongs to; group IDs are
  enriched with name, community URL, and member count (sorted by size).

## [1.0.0]

First stable release. The public surface — tool names + input parameters, JSON
output fields, prompt names/arguments, and resource URIs — is now covered by a
**stability contract** (see "Versioning & stability" in the README); breaking
changes will require a 2.0.

### Added
- **Stability contract** documented in the README (SemVer policy + what's covered).
- **Retry with backoff** for transient failures: 429 and 502/503/504 are retried
  with exponential backoff + jitter, honoring a `Retry-After` header; other
  statuses still fail fast.

### Changed
- **Broader caching**: news (`steam_get_app_news`, 15 min) and the lifetime review
  summary (5 min) now use the in-memory TTL cache. Live data (player status,
  current players, wishlists, friends, recent-review pagination) is still uncached.
- Package classifier moved to "Production/Stable".

## [0.12.0]

### Added
- **MCP prompts** — guided one-shot flows that orchestrate the tools:
  `what_should_i_play`, `is_it_worth_buying`, `plan_game_night`, `steam_deals`,
  and `game_overview`.
- **MCP resources** — reference Steam entities by URI: `steam://app/{appid}`
  (store details) and `steam://user/{steamid}` (profile + live status).
- **Localization** — `steam_get_app_details`, `steam_search_apps`,
  `steam_get_app_reviews`, `steam_get_player_achievements`,
  `steam_get_user_game_stats`, and `steam_get_rarest_unlocks` now accept a
  `language` parameter (a Steam language name, e.g. `french`, `schinese`; default
  `english`). For reviews it also selects which language's reviews to score (or
  `all`).

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

[1.8.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v1.8.0
[1.7.7]: https://github.com/Sarg338/steam-mcp/releases/tag/v1.7.7
[1.7.6]: https://github.com/Sarg338/steam-mcp/releases/tag/v1.7.6
[1.7.5]: https://github.com/Sarg338/steam-mcp/releases/tag/v1.7.5
[1.7.4]: https://github.com/Sarg338/steam-mcp/releases/tag/v1.7.4
[1.7.3]: https://github.com/Sarg338/steam-mcp/releases/tag/v1.7.3
[1.7.2]: https://github.com/Sarg338/steam-mcp/releases/tag/v1.7.2
[1.7.1]: https://github.com/Sarg338/steam-mcp/releases/tag/v1.7.1
[1.7.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v1.7.0
[1.6.1]: https://github.com/Sarg338/steam-mcp/releases/tag/v1.6.1
[1.6.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v1.6.0
[1.5.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v1.5.0
[1.4.3]: https://github.com/Sarg338/steam-mcp/releases/tag/v1.4.3
[1.4.2]: https://github.com/Sarg338/steam-mcp/releases/tag/v1.4.2
[1.4.1]: https://github.com/Sarg338/steam-mcp/releases/tag/v1.4.1
[1.4.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v1.4.0
[1.3.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v1.3.0
[1.2.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v1.2.0
[1.1.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v1.1.0
[1.0.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v1.0.0
[0.12.0]: https://github.com/Sarg338/steam-mcp/releases/tag/v0.12.0
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
