"""Tests for steam_mcp.tools.reviews — app reviews (localization, recent window)."""
import json

from conftest import run

from steam_mcp import transport
from steam_mcp.tools.reviews import AppReviewsInput, steam_get_app_reviews


def test_app_reviews_language(monkeypatch):
    captured = {}

    async def fake_raw(url, params, cache_ttl=0):
        captured.update(params)
        return {"success": 1, "reviews": [], "query_summary": {
            "review_score_desc": "x", "total_positive": 1,
            "total_negative": 0, "total_reviews": 1}}

    monkeypatch.setattr(transport, "_raw_get", fake_raw)
    run(steam_get_app_reviews(AppReviewsInput(appid=1, language="german")))
    assert captured.get("language") == "german"


def test_app_reviews_recent_window(monkeypatch):
    # filter='recent' should compute a positive % from the windowed reviews
    import time
    now = int(time.time())
    base = {"success": 1, "query_summary": {"review_score_desc": "Very Positive",
            "total_reviews": 100, "total_positive": 95, "total_negative": 5},
            "reviews": []}

    async def fake_raw(url, params, cache_ttl=0):
        if params.get("filter") == "recent":
            return {"success": 1, "reviews": [
                {"voted_up": True, "timestamp_created": now - 10},
                {"voted_up": False, "timestamp_created": now - 20},
                {"voted_up": True, "timestamp_created": now - 30},
                {"voted_up": True, "timestamp_created": now - 99 * 86400},  # too old -> stop
            ], "cursor": "*"}
        return base

    monkeypatch.setattr(transport, "_raw_get", fake_raw)
    out = run(steam_get_app_reviews(
        AppReviewsInput(appid=1, review_filter="recent", limit=0,
                        response_format="json")))
    d = json.loads(out)
    assert d["summary"]["total_reviews"] == 100         # lifetime preserved
    assert d["recent"]["reviews_counted"] == 3          # 4th is outside window
    assert d["recent"]["positive"] == 2
