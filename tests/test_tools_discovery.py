"""Tests for steam_mcp.tools.discovery — recommendation search."""
import json

from conftest import run

from steam_mcp import transport
from steam_mcp.data import pricing, tags
from steam_mcp.tools.discovery import DiscoverInput, steam_discover


def test_discover_basic(monkeypatch):
    async def fake_raw(url, params, cache_ttl=0):
        return {"success": 1, "total_count": 3,
                "results_html": '<a data-ds-appid="10"></a>'
                                '<a data-ds-appid="20"></a><a data-ds-appid="30"></a>'}

    prices = {10: {"name": "A", "price": "$5", "discount_pct": 0, "on_sale": False},
              20: {"name": "B", "price": "$10", "discount_pct": 50, "on_sale": True},
              30: {"name": "C", "price": "$1", "discount_pct": 0, "on_sale": False}}

    async def fake_app_prices(appids, cc):
        return {a: prices[a] for a in appids}

    monkeypatch.setattr(transport, "_raw_get", fake_raw)
    monkeypatch.setattr(pricing, "_app_prices", fake_app_prices)
    out = run(steam_discover(DiscoverInput(term="x", response_format="json")))
    d = json.loads(out)
    assert d["total_count"] == 3 and d["count"] == 3
    assert [r["appid"] for r in d["results"]] == [10, 20, 30]   # ranked order kept
    assert d["personalized"] is False


def test_discover_explicit_tags(monkeypatch):
    captured = {}

    async def fake_map():
        return {29482: "Souls-like"}

    async def fake_raw(url, params, cache_ttl=0):
        captured.update(params)
        return {"success": 1, "total_count": 1,
                "results_html": '<a data-ds-appid="5"></a>'}

    async def fake_app_prices(appids, cc):
        return {a: {"name": "G"} for a in appids}

    monkeypatch.setattr(tags, "_tag_name_map", fake_map)
    monkeypatch.setattr(transport, "_raw_get", fake_raw)
    monkeypatch.setattr(pricing, "_app_prices", fake_app_prices)
    out = run(steam_discover(DiscoverInput(
        tags=["Souls-like"], max_price=20, on_sale=True, platform="win",
        response_format="json")))
    d = json.loads(out)
    assert d["filters"]["resolved_tag_ids"] == [29482]
    assert captured["tags"] == "29482"        # name -> id mapped into the query
    assert captured["maxprice"] == "20"
    assert captured["specials"] == 1
    assert captured["os"] == "win"


def test_discover_personalized(monkeypatch):
    async def fake_steam(path, params, **k):
        if "GetOwnedGames" in path:
            return {"response": {"games": [
                {"appid": 10, "name": "Hades", "playtime_forever": 6000},
                {"appid": 99, "name": "Owned Thing", "playtime_forever": 100}]}}
        if "GetRecentlyPlayedGames" in path:
            return {"response": {"games": [
                {"appid": 10, "name": "Hades", "playtime_2weeks": 300}]}}
        if "GetItems" in path:
            return {"response": {"store_items": [
                {"appid": 10, "tags": [{"tagid": 1716, "weight": 100},
                                       {"tagid": 4231, "weight": 50}]}]}}
        return {}

    async def fake_map():
        return {1716: "Roguelike", 4231: "Action RPG"}

    async def fake_raw(url, params, cache_ttl=0):
        # search returns three apps, one of which (99) the user owns
        return {"success": 1, "total_count": 50,
                "results_html": '<a data-ds-appid="20"></a>'
                                '<a data-ds-appid="99"></a><a data-ds-appid="30"></a>'}

    prices = {20: {"name": "New A"}, 30: {"name": "New B"}}

    async def fake_app_prices(appids, cc):
        return {a: prices.get(a, {}) for a in appids}

    monkeypatch.setattr(transport, "_steam_get", fake_steam)
    monkeypatch.setattr(tags, "_tag_name_map", fake_map)
    monkeypatch.setattr(transport, "_raw_get", fake_raw)
    monkeypatch.setattr(pricing, "_app_prices", fake_app_prices)
    out = run(steam_discover(DiscoverInput(
        steamid="76561197960287930", response_format="json")))
    d = json.loads(out)
    assert d["personalized"] is True
    assert d["taste_tags"] == ["Roguelike", "Action RPG"]   # derived from Hades
    assert "Hades" in d["seed_games"]
    assert d["filters"]["resolved_tag_ids"] == [1716, 4231]  # seeded from taste
    assert 99 not in [r["appid"] for r in d["results"]]     # owned game excluded
    assert [r["appid"] for r in d["results"]] == [20, 30]


def _search_html(entries):
    """Render fake search rows: entries = [(appid, review_pct, review_count)|appid]."""
    rows = []
    for e in entries:
        if isinstance(e, int):
            rows.append(f'<a href="x" data-ds-appid="{e}"><span class='
                        '"search_review_summary no_reviews"></span></a>')
        else:
            a, pct, n = e
            rows.append(
                f'<a href="x" data-ds-appid="{a}"><span class="search_review_summary'
                f' positive" data-tooltip-html="Positive&lt;br&gt;{pct}% of the'
                f' {n:,} user reviews for this game are positive."></span></a>')
    return "".join(rows)


def test_discover_released_within_days(monkeypatch):
    # "well-reviewed AND recent": the window is enumerated newest-first but the
    # RESULTS are ranked by the requested sort (reviews by default) — a days-old
    # unreviewed release must not outrank an 8-month-old 95%-positive game.
    import time as _t
    now = _t.time()
    rel = {1: now - 2 * 86400,     # newest, unreviewed shovelware
           2: now - 200 * 86400,   # 95%, in window
           3: now - 100 * 86400,   # 80%, in window
           4: now - 50 * 86400,    # 98% but only 5 reviews (thin)
           5: now - 900 * 86400}   # out of window

    async def fake_raw(url, params, cache_ttl=0):
        assert params.get("sort_by") == "Released_DESC"  # window enumeration
        return {"success": 1, "total_count": 456,
                "results_html": _search_html(
                    [1, (2, 95, 500), (3, 80, 2000), (4, 98, 5), (5, 99, 9000)])}

    async def fake_app_prices(appids, cc):
        return {a: {"name": f"G{a}", "release_ts": int(rel[a])} for a in appids}

    monkeypatch.setattr(transport, "_raw_get", fake_raw)
    monkeypatch.setattr(pricing, "_app_prices", fake_app_prices)
    d = json.loads(run(steam_discover(DiscoverInput(
        released_within_days=365, limit=3, response_format="json"))))
    # ranked by reviews (volume-guarded), not newest-first; #5 outside the window
    # is dropped and backfilled (slice happens AFTER the window filter)
    assert [r["appid"] for r in d["results"]] == [2, 3, 4]
    assert d["results"][0]["review_pct"] == 95
    assert d["results"][0]["review_count"] == 500
    assert d["window_coverage"] == "full"
    assert d["filters"]["released_within_days"] == 365


def test_discover_window_respects_release_sort(monkeypatch):
    import time as _t
    now = _t.time()
    rel = {1: now - 30 * 86400, 2: now - 5 * 86400, 3: now - 10 * 86400}

    async def fake_raw(url, params, cache_ttl=0):
        return {"success": 1, "total_count": 3,
                "results_html": _search_html([(2, 70, 50), (3, 90, 50), (1, 99, 50)])}

    async def fake_app_prices(appids, cc):
        return {a: {"name": f"G{a}", "release_ts": int(rel[a])} for a in appids}

    monkeypatch.setattr(transport, "_raw_get", fake_raw)
    monkeypatch.setattr(pricing, "_app_prices", fake_app_prices)
    d = json.loads(run(steam_discover(DiscoverInput(
        released_within_days=60, sort="release", response_format="json"))))
    assert [r["appid"] for r in d["results"]] == [2, 3, 1]  # newest first


def test_discover_window_price_sort(monkeypatch):
    import time as _t
    now = _t.time()

    async def fake_raw(url, params, cache_ttl=0):
        return {"success": 1, "total_count": 3,
                "results_html": _search_html([(1, 90, 10), (2, 90, 10), (3, 90, 10)])}

    prices = {1: 1500, 2: 500, 3: None}

    async def fake_app_prices(appids, cc):
        return {a: {"name": f"G{a}", "release_ts": int(now - 86400),
                    "price_cents": prices[a]} for a in appids}

    monkeypatch.setattr(transport, "_raw_get", fake_raw)
    monkeypatch.setattr(pricing, "_app_prices", fake_app_prices)
    d = json.loads(run(steam_discover(DiscoverInput(
        released_within_days=30, sort="price_asc", response_format="json"))))
    # cheapest first; unknown price ranks last
    assert [r["appid"] for r in d["results"]] == [2, 1, 3]
    assert "price_cents" not in d["results"][0]  # internal key not exposed


def test_discover_window_stops_when_page_passes_window(monkeypatch):
    # Newest-first enumeration means the first pre-window release proves the rest
    # of the catalogue is older: no further search pages should be fetched.
    import time as _t
    now = _t.time()
    calls = {"n": 0}

    async def fake_raw(url, params, cache_ttl=0):
        calls["n"] += 1
        entries = [(a, 90, 100) for a in range(1, 101)]  # a full 100-row page
        return {"success": 1, "total_count": 5000,
                "results_html": _search_html(entries)}

    async def fake_app_prices(appids, cc):
        # appid 50 released before the window -> everything after it is older too
        return {a: {"name": f"G{a}",
                    "release_ts": int(now - (900 if a == 50 else 1) * 86400)}
                for a in appids}

    monkeypatch.setattr(transport, "_raw_get", fake_raw)
    monkeypatch.setattr(pricing, "_app_prices", fake_app_prices)
    d = json.loads(run(steam_discover(DiscoverInput(
        released_within_days=30, limit=5, response_format="json"))))
    assert calls["n"] == 1                     # stopped after the first page
    assert d["window_coverage"] == "full"
    assert 50 not in [r["appid"] for r in d["results"]]


def test_discover_window_partial_coverage(monkeypatch):
    # Every page is full and entirely inside the window -> the page cap is hit
    # and the result is honestly marked as partial coverage.
    import time as _t
    now = _t.time()
    calls = {"n": 0}

    async def fake_raw(url, params, cache_ttl=0):
        calls["n"] += 1
        start = int(params.get("start", 0))
        entries = [(start + i, 90, 100) for i in range(1, 101)]
        return {"success": 1, "total_count": 5000,
                "results_html": _search_html(entries)}

    async def fake_app_prices(appids, cc):
        return {a: {"name": f"G{a}", "release_ts": int(now - 86400)}
                for a in appids}

    monkeypatch.setattr(transport, "_raw_get", fake_raw)
    monkeypatch.setattr(pricing, "_app_prices", fake_app_prices)
    d = json.loads(run(steam_discover(DiscoverInput(
        released_within_days=365, response_format="json"))))
    assert calls["n"] == 3                     # _WINDOW_MAX_PAGES
    assert d["window_coverage"] == "partial"
    assert d["count"] == 15                    # default limit still honored


def test_discover_window_excluded_owned_counts_actual_drops(monkeypatch):
    # excluded_owned must report how many in-window matches were hidden, not the
    # size of the user's whole library.
    import time as _t
    now = _t.time()

    async def fake_resolve(identifier=None):
        return "76561197960287930"

    async def fake_taste(sid, **kw):
        return {"owned_ids": {2, 999}, "seed_games": ["Hades"],
                "tag_ids": [], "tag_names": []}

    async def fake_raw(url, params, cache_ttl=0):
        return {"success": 1, "total_count": 3,
                "results_html": _search_html([(1, 90, 50), (2, 95, 50), (3, 85, 50)])}

    async def fake_app_prices(appids, cc):
        return {a: {"name": f"G{a}", "release_ts": int(now - 86400)}
                for a in appids}

    from steam_mcp import identity
    from steam_mcp.data import tags as _tags
    monkeypatch.setattr(identity, "_resolve_steamid", fake_resolve)
    monkeypatch.setattr(_tags, "_taste_profile", fake_taste)
    monkeypatch.setattr(transport, "_raw_get", fake_raw)
    monkeypatch.setattr(pricing, "_app_prices", fake_app_prices)
    d = json.loads(run(steam_discover(DiscoverInput(
        steamid="x", released_within_days=30, response_format="json"))))
    assert d["excluded_owned"] == 1            # only appid 2 was actually dropped
    assert [r["appid"] for r in d["results"]] == [1, 3]
