"""Tests for steam_mcp.transport — allowlist/SSRF, redirects, rate limit, retry,
client rebinding, and bounded fan-out."""
import asyncio
import logging

import httpx
import pytest

from conftest import run

# Importing the app module quiets the httpx/httpcore loggers (the key rides in
# request URLs), which test_http_logging_silenced asserts below.
import steam_mcp.app  # noqa: F401
from steam_mcp import cache, transport
from steam_mcp.errors import SteamApiError
from steam_mcp.transport import MAX_RETRIES, _Bucket


def test_http_client_rebinds_across_event_loops():
    # Regression: the shared AsyncClient binds to the loop it first runs on. A new
    # asyncio.run() creates a new loop, so the client must be recreated — otherwise
    # reuse raises "RuntimeError: Event loop is closed". Pre-fix this returned the
    # same client across loops (c1 == c2).
    transport._CLIENT = None
    transport._CLIENT_LOOP = None

    async def grab():
        return id(transport._http_client()), id(asyncio.get_running_loop())

    c1, l1 = run(grab())
    c2, l2 = run(grab())
    assert l1 != l2          # genuinely different event loops
    assert c1 != c2          # client was rebuilt for the new loop
    transport._CLIENT = None         # reset shared state for any later test
    transport._CLIENT_LOOP = None


# --------------------------------------------------------------------------- #
# 1.0.0: retry / backoff on transient failures
# --------------------------------------------------------------------------- #

class _RetryResp:
    def __init__(self, code):
        self.status_code = code
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return {"ok": True}


def test_get_with_retry_succeeds_after_429(monkeypatch):
    cache._CACHE.clear()
    calls = {"n": 0}

    class FakeClient:
        async def get(self, *a, **k):
            calls["n"] += 1
            return _RetryResp(429 if calls["n"] == 1 else 200)

    async def no_sleep(*a, **k):
        return None

    monkeypatch.setattr(transport, "_http_client", lambda: FakeClient())
    monkeypatch.setattr(transport.asyncio, "sleep", no_sleep)
    data = run(transport._raw_get("https://api.steampowered.com/x", {}))
    assert data == {"ok": True}
    assert calls["n"] == 2          # one 429 -> retried once, then 200


def test_get_with_retry_gives_up(monkeypatch):
    calls = {"n": 0}

    class FakeClient:
        async def get(self, *a, **k):
            calls["n"] += 1
            return _RetryResp(503)   # always failing

    async def no_sleep(*a, **k):
        return None

    monkeypatch.setattr(transport, "_http_client", lambda: FakeClient())
    monkeypatch.setattr(transport.asyncio, "sleep", no_sleep)
    with pytest.raises(RuntimeError):
        run(transport._raw_get("https://api.steampowered.com/x", {}))
    assert calls["n"] == MAX_RETRIES + 1   # initial try + MAX_RETRIES


def test_check_host_allowlist():
    for ok in ("https://api.steampowered.com/x",
               "https://store.steampowered.com/api/y",
               "https://steamcommunity.com/inventory/1/753/6"):
        transport._check_host(ok)      # no raise
    for bad in ("https://evil.example.com/x",
                "https://api.steampowered.com.evil.com/x",
                "http://169.254.169.254/latest/meta-data"):
        with pytest.raises(SteamApiError):
            transport._check_host(bad)


def test_enforce_host_hook_blocks_redirect_target():
    # The client follows redirects, so the allowlist must be enforced per-hop.
    run(transport._enforce_host(httpx.Request(
        "GET", "https://api.steampowered.com/x?key=secret")))  # allowed: no raise
    for bad in ("http://169.254.169.254/latest/meta-data",  # cloud metadata
                "https://evil.example.com/"):
        with pytest.raises(SteamApiError):
            run(transport._enforce_host(httpx.Request("GET", bad)))


def test_rate_limiter_bucket(monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(transport.asyncio, "sleep", fake_sleep)

    async def go():
        b = _Bucket(rate=10.0, burst=2)
        await b.take()
        await b.take()              # within burst -> no wait
        burst_sleeps = len(slept)
        await b.take()              # over budget -> must wait
        return burst_sleeps, len(slept)

    burst_sleeps, after = run(go())
    assert burst_sleeps == 0
    assert after == 1


def test_http_logging_silenced():
    # The API key rides in request URLs (?key=); httpx/httpcore log those at INFO,
    # so importing the server must have quieted them to keep the key out of logs.
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_gather_limited_preserves_order():
    async def make(n):
        return n * 2

    out = run(transport._gather_limited([make(1), make(2), make(3)], limit=2))
    assert out == [2, 4, 6]
