---
name: steam
description: >-
  Query Steam for a user's library, playtime, achievements, friends, groups, and
  inventory, plus any game's store details, reviews, community tags, prices, sales,
  and live player counts — and higher-level help like game recommendations, "is
  this worth buying", and planning a co-op night. Read-only, bring-your-own-key.
  Use when the user asks about Steam games, their Steam account, what to play next,
  what a game/skin is worth, or whether to buy something.
---

# Steam

Drives the `steam-mcp` server (read-only Steam Web API + storefront): 36 tools,
plus 5 prompts and 2 resources.

## Token-efficient usage

- **Prefer the composite tools** — one call beats chaining five:
  - "what should I play" → `steam_recommend` (+ `steam_analyze_library` for the
    backlog they already own), not owned-games + tags + reviews assembled by hand.
  - "is X worth buying" → `steam_should_i_buy` (price + lifetime-vs-recent review
    trend + tags + taste match) in one call.
  - "find games like Y / matching filters" → `steam_discover`.
  - "co-op night" → `steam_plan_coop_night`.
- **Leave `response_format` on `markdown`** (the default, compact). Only ask for
  `json` when you actually need to parse fields.
- **Cap list sizes**: pass a small `limit` and page with `offset`; don't pull a
  2,000-game library or a whole inventory when you need the top few.
- Resolve a game name to an appid once with `steam_search_apps`, then reuse it.

## Common workflows

- **Game research** — `steam_get_app_details`, then `steam_get_app_reviews`
  (`review_filter='recent'` for the trend), `steam_get_app_tags`,
  `steam_get_current_players`.
- **Should I buy it** — `steam_should_i_buy` (pass `steamid` to personalize). Prices
  by region: `steam_get_app_regional_pricing`. An item/skin's value:
  `steam_get_market_price`.
- **What to play** — `steam_recommend(steamid=…)` for new games to get;
  `steam_analyze_library(steamid=…)` for the backlog you already own.
- **Friends & co-op** — `steam_find_friends_who_own(appid=…)`, or
  `steam_plan_coop_night` for what the user and their online friends can all play.
- **My stuff** — library, recently played, wishlist (with on-sale filter),
  achievements, rarest unlocks, badges, groups, inventory.

## Identifiers & privacy

- A user can be given as a SteamID64, a vanity name, or a profile URL — all work.
- Friends, owned games, achievements, wishlist, inventory, and groups require the
  target profile's relevant privacy to be **Public**; otherwise the tool reports no
  data. That's a Steam limitation, not an error to retry.

## Safety

Read-only and bring-your-own-key: it only reads public Steam data, talks only to
official Steam hosts, and never writes, trades, posts, launches games, or buys
anything.
