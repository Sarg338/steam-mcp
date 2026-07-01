"""Tests for steam_mcp.cache — the TTL cache and cache-key hygiene."""
from conftest import run

from steam_mcp import cache, transport
from steam_mcp.cache import _TTLCache


def test_cache_key_excludes_api_key():
    k = cache._cache_key("u", {"key": "SECRET", "appid": 7, "cc": "us"})
    assert "SECRET" not in k
    assert k == cache._cache_key("u", {"cc": "us", "appid": 7})  # order-independent


def test_ttl_cache_basic():
    c = _TTLCache(maxsize=2)
    c.set("a", 1, ttl=100)
    assert c.get("a") == 1
    c.set("b", 2, ttl=-1)          # already expired
    assert c.get("b") is None
    assert c.get("missing") is None


def test_ttl_cache_eviction():
    c = _TTLCache(maxsize=2)
    c.set("x", 1, 100)
    c.set("y", 2, 100)
    c.set("z", 3, 100)             # exceeds maxsize -> eviction kicks in
    assert len(c._d) <= 2


def test_store_get_caches(monkeypatch):
    cache._CACHE.clear()
    calls = {"n": 0}

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"ok": calls["n"]}

    class FakeClient:
        async def get(self, *a, **k):
            calls["n"] += 1
            return FakeResp()

    monkeypatch.setattr(transport, "_http_client", lambda: FakeClient())
    run(transport._store_get("appdetails", {"appids": 1}, cache_ttl=60))
    run(transport._store_get("appdetails", {"appids": 1}, cache_ttl=60))
    assert calls["n"] == 1                      # second served from cache
    run(transport._store_get("appdetails", {"appids": 1}))   # no ttl -> refetch
    assert calls["n"] == 2
