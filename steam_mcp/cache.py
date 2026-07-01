"""Per-process TTL cache for static Steam GET responses."""

from __future__ import annotations

import time
from typing import Any

# --- Static-response cache (per-process, opt-in) -----------------------------
CACHE_TTL_APPDETAILS = 600      # 10 min (price can change on sales)
CACHE_TTL_PACKAGE = 3600
CACHE_TTL_FEATURED = 300        # 5 min
CACHE_TTL_SCHEMA = 86400        # achievement/stat definitions are static
CACHE_TTL_GLOBAL_ACH = 3600
CACHE_TTL_TAGS = 3600           # community tag weights (slow-changing)
CACHE_TTL_TAGMAP = 86400        # tagid -> name dictionary is effectively static
CACHE_TTL_DISCOVER = 300        # storefront search results (5 min)
CACHE_TTL_NEWS = 900            # news / patch notes change slowly (15 min)
CACHE_TTL_REVIEWS = 300         # lifetime review summary (5 min)
CACHE_TTL_WORKSHOP = 3600       # workshop item metadata (slow-changing)
CACHE_TTL_GROUP = 3600          # group name / url / member count (slow-changing)
CACHE_TTL_MARKET = 600          # market price (10 min — also eases the tight rate limit)
CACHE_TTL_DECK = 86400          # Steam Deck compatibility rating (effectively static)


class _TTLCache:
    """Tiny in-memory TTL cache for static GET responses.

    Keeps the server gentle on Steam's rate limit and speeds up tools that fan
    out many lookups (wishlist enrichment, library/app detail comparisons). Only
    static endpoints opt in via a positive cache_ttl; live data (player status,
    current players, wishlists, friends) is never cached.
    """

    def __init__(self, maxsize: int = 256):
        self._d: dict[str, tuple[float, Any]] = {}
        self._max = maxsize

    def get(self, key: str):
        item = self._d.get(key)
        if not item:
            return None
        expiry, value = item
        if expiry < time.time():
            self._d.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        if len(self._d) >= self._max:
            now = time.time()
            for k in [k for k, (e, _) in self._d.items() if e < now]:
                self._d.pop(k, None)
            if len(self._d) >= self._max:
                self._d.clear()
        self._d[key] = (time.time() + ttl, value)

    def clear(self) -> None:
        self._d.clear()


_CACHE = _TTLCache()


def _cache_key(prefix: str, params: dict) -> str:
    """Stable cache key from a path/URL + params, excluding the secret API key."""
    items = sorted((k, v) for k, v in params.items() if k != "key")
    return prefix + "?" + "&".join(f"{k}={v}" for k, v in items)
