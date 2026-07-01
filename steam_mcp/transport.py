"""HTTP layer for the Steam MCP server: allowlisted, rate-limited, retrying GET/POST."""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from steam_mcp import config
from steam_mcp.cache import _CACHE, _cache_key
from steam_mcp.errors import SteamApiError

API_BASE = "https://api.steampowered.com"
STORE_BASE = "https://store.steampowered.com/api"
HTTP_TIMEOUT = 30.0

# Bounded retry for transient failures. 429 (rate limit) and 502/503/504 are
# retried with exponential backoff + jitter, honoring a Retry-After header; other
# statuses (401/403/404/500) fail fast since retrying won't help.
MAX_RETRIES = 3
RETRY_BASE_DELAY = 0.5
RETRY_MAX_DELAY = 10.0
RETRYABLE_STATUS = {429, 502, 503, 504}

# Security: only these Steam hosts may be contacted (SSRF defense-in-depth — we
# never take a URL from the user, but the request layer enforces it anyway).
ALLOWED_HOSTS = frozenset({
    "api.steampowered.com",
    "store.steampowered.com",
    "steamcommunity.com",
})

# Proactive per-host rate limiting (token bucket: sustained `rate`/sec, burst up to
# `burst`). Bursts ≥ the fan-out cap so concurrent enrichment isn't serialized;
# steamcommunity (market/inventory) is the strict one. Complements the 429 retry.
RATE_LIMITS = {
    "api.steampowered.com": (20.0, 20),
    "store.steampowered.com": (8.0, 12),
    "steamcommunity.com": (2.0, 5),
}


_CLIENT: Optional[httpx.AsyncClient] = None
_CLIENT_LOOP: Optional[asyncio.AbstractEventLoop] = None


def _http_client() -> httpx.AsyncClient:
    """Return a shared AsyncClient bound to the *current* event loop.

    Reusing one client avoids a fresh TCP/TLS handshake per request and lets the
    fan-out tools (wishlist, DLC, comparisons) run many concurrent lookups over
    pooled connections; an AsyncClient is safe for concurrent use. An AsyncClient
    binds to the loop it first runs on, so if the running loop has changed (e.g. a
    fresh asyncio.run() in a script or test) we recreate it — otherwise reuse would
    raise "RuntimeError: Event loop is closed". The long-lived MCP server uses a
    single loop, so in normal operation the client is created exactly once.
    """
    global _CLIENT, _CLIENT_LOOP
    loop = asyncio.get_running_loop()
    if _CLIENT is None or _CLIENT.is_closed or _CLIENT_LOOP is not loop:
        _CLIENT = httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
            headers={"Accept": "application/json"},
            event_hooks={"request": [_enforce_host]},
        )
        _CLIENT_LOOP = loop
    return _CLIENT


def _check_host(url: str) -> None:
    """Reject any request whose host isn't a known Steam host (SSRF guard)."""
    host = (urlsplit(url).hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise SteamApiError(f"Refusing request to non-Steam host: {host or url!r}")


async def _enforce_host(request: httpx.Request) -> None:
    """httpx request hook: enforce the allowlist on EVERY hop, including redirects.

    The client follows redirects, so a pre-flight `_check_host` on the initial URL
    alone would miss a 3xx that leaves the allowlist (e.g. to an internal/metadata
    host). This fires before each hop is sent. `_check_host` keys only on the host,
    so the key in `request.url`'s query string is never surfaced in the error.
    """
    _check_host(str(request.url))


class _Bucket:
    """Lock-free async token bucket: sustained `rate`/sec with bursts up to `burst`.

    Lock-free on purpose — benign races only over/under-count by a token, which is
    fine for rate-limiting, and it avoids binding an asyncio primitive to a loop
    (so it's safe across multiple asyncio.run() calls).
    """

    def __init__(self, rate: float, burst: int):
        self.rate = rate
        self.cap = float(burst)
        self.tokens = float(burst)
        self.ts = time.monotonic()

    async def take(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.cap, self.tokens + (now - self.ts) * self.rate)
        self.ts = now
        if self.tokens < 1.0:
            await asyncio.sleep((1.0 - self.tokens) / self.rate)
            self.tokens = 0.0
            self.ts = time.monotonic()
        else:
            self.tokens -= 1.0


_BUCKETS = {host: _Bucket(rate, burst) for host, (rate, burst) in RATE_LIMITS.items()}


async def _rate_limit(url: str) -> None:
    """Wait for the per-host rate budget before a request (no-op for unlisted hosts)."""
    bucket = _BUCKETS.get((urlsplit(url).hostname or "").lower())
    if bucket is not None:
        await bucket.take()


def _retry_delay(resp, attempt: int) -> float:
    """Seconds to wait before a retry: honor Retry-After (seconds), else backoff."""
    ra = resp.headers.get("Retry-After") if resp is not None else None
    if ra:
        try:
            return min(float(ra), RETRY_MAX_DELAY)
        except (TypeError, ValueError):
            pass
    return min(RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.3),
               RETRY_MAX_DELAY)


async def _get_with_retry(client, url: str, params: dict, timeout: float):
    """GET with bounded retry on 429/502/503/504 and timeouts (honors Retry-After).

    Returns a status-checked response. On the final attempt a retryable status is
    raised like any other HTTP error, so _handle_error can format it.
    """
    _check_host(url)
    await _rate_limit(url)
    for attempt in range(MAX_RETRIES + 1):
        final = attempt == MAX_RETRIES
        try:
            resp = await client.get(url, params=params, timeout=timeout)
        except httpx.TimeoutException:
            if final:
                raise
            await asyncio.sleep(_retry_delay(None, attempt))
            continue
        if resp.status_code in RETRYABLE_STATUS and not final:
            await asyncio.sleep(_retry_delay(resp, attempt))
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError("unreachable")  # pragma: no cover


async def _steam_get(path: str, params: dict[str, Any], *, with_key: bool = True,
                     cache_ttl: float = 0) -> dict:
    """GET a Steam Web API endpoint and return parsed JSON.

    Args:
        path: Path after the host, e.g. "ISteamUser/GetFriendList/v1/".
        params: Query parameters (the API key is injected automatically).
        with_key: Whether to attach the configured API key.
        cache_ttl: If > 0, cache the response for this many seconds. Use only for
            static endpoints (e.g. game schemas); never for live/user data.
    """
    ck = _cache_key(API_BASE + "/" + path, params) if cache_ttl else None
    if ck is not None:
        hit = _CACHE.get(ck)
        if hit is not None:
            return hit
    query = dict(params)
    if with_key:
        query["key"] = config._get_api_key()
    client = _http_client()
    resp = await _get_with_retry(client, f"{API_BASE}/{path}", query, HTTP_TIMEOUT)
    data = resp.json()
    if ck is not None:
        _CACHE.set(ck, data, cache_ttl)
    return data


async def _store_get(path: str, params: dict[str, Any], cache_ttl: float = 0) -> Any:
    """GET a public storefront API endpoint (no key required)."""
    return await _raw_get(f"{STORE_BASE}/{path}", params, cache_ttl=cache_ttl)


async def _raw_get(url: str, params: dict[str, Any], cache_ttl: float = 0) -> Any:
    """GET an arbitrary public Steam JSON endpoint (no key required)."""
    ck = _cache_key(url, params) if cache_ttl else None
    if ck is not None:
        hit = _CACHE.get(ck)
        if hit is not None:
            return hit
    client = _http_client()
    resp = await _get_with_retry(client, url, params, HTTP_TIMEOUT)
    data = resp.json()
    if ck is not None:
        _CACHE.set(ck, data, cache_ttl)
    return data


async def _raw_get_text(url: str, params: dict[str, Any] | None = None,
                        cache_ttl: float = 0) -> str:
    """GET a public endpoint and return the raw text body (e.g. community XML)."""
    params = params or {}
    ck = _cache_key("text:" + url, params) if cache_ttl else None
    if ck is not None:
        hit = _CACHE.get(ck)
        if hit is not None:
            return hit
    client = _http_client()
    resp = await _get_with_retry(client, url, params, HTTP_TIMEOUT)
    text = resp.text
    if ck is not None:
        _CACHE.set(ck, text, cache_ttl)
    return text


async def _steam_post(path: str, data: dict[str, Any], *, with_key: bool = False,
                      cache_ttl: float = 0) -> dict:
    """POST to a Steam Web API endpoint (some, e.g. GetPublishedFileDetails, are
    POST-only) and return parsed JSON. Caches static responses like _steam_get."""
    body = dict(data)
    if with_key:
        body["key"] = config._get_api_key()
    ck = _cache_key("post:" + API_BASE + "/" + path, body) if cache_ttl else None
    if ck is not None:
        hit = _CACHE.get(ck)
        if hit is not None:
            return hit
    url = f"{API_BASE}/{path}"
    _check_host(url)
    await _rate_limit(url)
    client = _http_client()
    resp = await client.post(url, data=body, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    out = resp.json()
    if ck is not None:
        _CACHE.set(ck, out, cache_ttl)
    return out


FANOUT_LIMIT = 8  # max concurrent storefront lookups for fan-out tools


async def _gather_limited(coros, limit: int = FANOUT_LIMIT):
    """Await many coroutines with bounded concurrency, preserving input order.

    Keeps fan-out tools (wishlist / DLC enrichment) fast without hammering the
    storefront: at most `limit` requests are in flight at once.
    """
    sem = asyncio.Semaphore(limit)

    async def _run(coro):
        async with sem:
            return await coro

    return await asyncio.gather(*(_run(c) for c in coros))
