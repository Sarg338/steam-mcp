# Privacy Policy — Steam MCP

_Last updated: 2026-06-01_

Steam MCP is a read-only, locally-run Model Context Protocol server. It is not a
hosted service. This document explains what it does and does not do with data.

## What it accesses

When you (or your AI client) invoke a tool, the server makes read-only HTTPS
requests to Valve's official endpoints:

- `https://api.steampowered.com` (Steam Web API)
- `https://store.steampowered.com` (Steam storefront/reviews)

Requests may include:

- **Your Steam Web API key**, read from the key you supply at install time (or a
  local `.env` / environment variable). The key is sent only to Valve, only as
  required to authenticate API calls. It is never transmitted anywhere else.
- **SteamIDs / vanity names / app IDs** you ask about, passed as request
  parameters to Valve.

## What it does NOT do

- It does **not** collect, store, log, or transmit your data to the author or any
  third party. There is no analytics, no telemetry, and no server operated by the
  project — everything runs on your machine.
- It is **read-only**: it cannot send messages, change your status, modify your
  account, launch games, or make purchases.
- It does **not** write your API key to any file it ships. The key stays in your
  local configuration.

## Storage, sharing, and retention

- **Storage:** none. The server persists nothing to disk. A small in-memory cache
  holds only *non-user*, static responses (store/app/tag/news data) and lives only
  for the running process.
- **Third-party sharing:** none. Data is exchanged only between your machine and
  Valve's official endpoints; it is never sent to the author or any analytics or
  third-party service.
- **Retention:** none. Nothing is retained between requests beyond the short-lived
  in-memory cache above, which is discarded when the process exits.

## Data visibility

The server can only read data that Valve exposes. Friends lists, owned games, and
achievements are returned only when the target Steam profile's privacy settings
make them **Public**. The server cannot access private profile data.

## Your responsibilities

- Keep your Steam Web API key secret. Treat it like a password.
- Your use of the Steam Web API is governed by Valve's
  [Steam Web API Terms of Use](https://steamcommunity.com/dev/apiterms).

## Contact

Issues and questions: https://github.com/Sarg338/steam-mcp/issues

_This project is not affiliated with or endorsed by Valve Corporation._
