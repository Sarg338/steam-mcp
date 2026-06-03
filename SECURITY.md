# Security Policy

`steam-mcp` is a **read-only** Model Context Protocol server for the public Steam
Web API and storefront. This describes its security posture and how to report
issues.

## Reporting a vulnerability

Open a private report via
[GitHub Security Advisories](https://github.com/Sarg338/steam-mcp/security/advisories/new),
or a regular issue for non-sensitive reports. Please include steps to reproduce.

## Posture

- **Read-only.** No tool writes, trades, posts, changes status, launches games, or
  makes purchases. Every tool is annotated `readOnlyHint: true`.
- **Bring-your-own-key.** The only credential is your own free Steam Web API key,
  read from the `STEAM_API_KEY` environment variable. It is never written to disk,
  logged, cached, or placed in tool output; it's excluded from cache keys, and
  error messages redact anything resembling a key. The optional `STEAM_USER`
  (a default-to-me convenience) is **not** a credential — it's a public Steam
  profile name and is treated as non-sensitive.
- **Official hosts only.** Requests go only to `api.steampowered.com`,
  `store.steampowered.com`, and `steamcommunity.com`; the request layer refuses any
  other host (SSRF guard). Market price/inventory use Steam's own (undocumented,
  rate-limited) Community Market endpoints — still read-only and keyless.
- **Input validation.** All tool inputs are typed Pydantic models with
  `extra="forbid"`; identifiers are validated/resolved before use.
- **Rate limiting & resilience.** Per-host token-bucket rate limiting, plus bounded
  retry with exponential backoff (honoring `Retry-After`) on 429/5xx.
- **No data retention.** Nothing is kept between requests except a small in-memory
  TTL cache of *non-user*, static responses (store/app/tag/news data); live user
  data (status, friends, wishlist, inventory) is never cached.

## Out of scope

No write/trade/purchase actions, no account login/OAuth, no third-party data
sources, and no market *history* (which would require a logged-in session).
