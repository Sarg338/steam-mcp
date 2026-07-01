"""Tests for the steam_mcp.data services (tags, catalog, pricing)."""
from conftest import run

from steam_mcp import transport
from steam_mcp.data import catalog, pricing, tags


def test_resolve_tag_ids(monkeypatch):
    async def fake_map():
        return {29482: "Souls-like", 1685: "Co-op"}

    monkeypatch.setattr(tags, "_tag_name_map", fake_map)
    ids, missing = run(tags._resolve_tag_ids(["souls-like", "Co-op", "Nonexistent"]))
    assert ids == [29482, 1685]          # case-insensitive
    assert missing == ["Nonexistent"]


def test_is_temp_client():
    temp = [
        "Super Buckyball Tournament Playtest",
        "Knockout City Trial",
        "PAYDAY 3 - Beta",
        "SMITE 2 - Public Test",
        "Quake Champions PTS",
        "Rust - Staging Branch",
        "Tom Clancy's Rainbow Six Siege - Test Server",
        "Battlerite Public Test",
        "DEFCON Beta Demo",
        "Spacebase DF-9 Prototype",
        "Halo Infinite - Open Beta",
        "Some Game - Closed Beta",
        "Cool Game Demo",
        "REMATCH BETA TEST",    # all-caps, mid-string BETA (case-insensitive)
        "Brawlhalla BETA",
        "Project X - Alpha Test",
        "Some Game Dev Build",
        "World of Foo PTR",
    ]
    for n in temp:
        assert catalog._is_temp_client(n), f"should flag: {n}"
    # Retail titles that share tokens must NOT be flagged (precision over recall).
    retail = [
        "Prototype",            # the 2009 retail game
        "Prototype 2",
        "Trials Rising",
        "Alpha Protocol",       # bare 'alpha' is intentionally NOT a token
        "Sid Meier's Alpha Centauri",
        "The Turing Test",      # bare 'test' is intentionally NOT a token
        "Test Drive Unlimited",
        "Counter-Strike 2",
        "Dota 2",
        "Half-Life 2: Episode One",
        "The Elder Scrolls V: Skyrim",
        "Batman: Arkham Asylum",
        "Borderlands",
    ]
    for n in retail:
        assert not catalog._is_temp_client(n), f"should NOT flag: {n}"


def test_taste_profile_excludes_temp_clients(monkeypatch):
    sid = "76561197960287930"
    owned = {"response": {"games": [
        {"appid": 100, "name": "Real RPG", "playtime_forever": 5000},
        {"appid": 200, "name": "Cool Shooter Playtest", "playtime_forever": 99999},
        {"appid": 300, "name": "PAYDAY 3 - Beta", "playtime_forever": 8000},
    ]}}
    recent = {"response": {"games": [
        {"appid": 200, "name": "Cool Shooter Playtest", "playtime_2weeks": 600},
    ]}}

    async def fake_steam(path, params, **k):
        return recent if "GetRecentlyPlayed" in path else owned

    seeded = {}

    async def fake_items_tags(ids):
        seeded["ids"] = list(ids)
        return {a: [{"tagid": 1, "weight": 1}] for a in ids}

    async def fake_tag_names():
        return {1: "RPG"}

    monkeypatch.setattr(transport, "_steam_get", fake_steam)
    monkeypatch.setattr(tags, "_items_tags", fake_items_tags)
    monkeypatch.setattr(tags, "_tag_name_map", fake_tag_names)

    prof = run(tags._taste_profile(sid))
    # The 99,999-minute playtest and the beta must NOT seed taste.
    assert seeded["ids"] == [100]
    assert "Cool Shooter Playtest" not in prof["seed_games"]
    assert "Real RPG" in prof["seed_games"]
    # owned_ids stays full (used to exclude already-owned games from recs).
    assert prof["owned_ids"] == {100, 200, 300}


def test_app_prices_batch_and_fallback(monkeypatch):
    # GetItems returns 10 (on sale), 11 (free); 12 is absent -> per-item fallback.
    def _item(aid, name, free=False, final=None, disc=None, released=None):
        bpo = {} if free else {"formatted_final_price": final, "discount_pct": disc}
        d = {"appid": aid, "name": name, "is_free": free,
             "best_purchase_option": bpo}
        if released is not None:
            d["release"] = {"steam_release_date": released}
        return d

    async def fake_steam(path, params, with_key=True, cache_ttl=0):
        return {"response": {"store_items": [
            _item(10, "OnSale", final="$5.00", disc=50, released=1700000000),
            _item(11, "FreeGame", free=True),
        ]}}

    async def fake_app_price(appid, cc):  # fallback path for the missing appid
        return {"appid": appid, "name": "Fallback", "is_free": False,
                "price": "$9.99", "discount_pct": 0, "on_sale": False}

    monkeypatch.setattr(transport, "_steam_get", fake_steam)
    monkeypatch.setattr(pricing, "_app_price", fake_app_price)

    pm = run(pricing._app_prices([10, 11, 12], "us"))
    assert set(pm) == {10, 11, 12}
    assert pm[10]["price"] == "$5.00" and pm[10]["on_sale"] and pm[10]["discount_pct"] == 50
    assert pm[10]["release_ts"] == 1700000000   # parsed from include_release
    assert pm[11]["is_free"] and pm[11]["price"] == "Free"
    assert pm[11]["release_ts"] is None         # no release block -> None
    assert pm[12]["name"] == "Fallback"   # came from the single-item fallback


def test_discover_search_parses_review_summaries(monkeypatch):
    # Rows as the store search renders them: a tooltip with "N% of the M user
    # reviews" when reviewed, a bare/no_reviews span otherwise. Duplicate appids
    # are collapsed, and a null total_count falls back to the row count instead
    # of crashing downstream formatting.
    html = (
        '<a href="x" data-ds-appid="10"><span class="search_review_summary positive"'
        ' data-tooltip-html="Very Positive&lt;br&gt;92% of the 15,632 user reviews'
        ' for this game are positive."></span></a>'
        '<a href="x" data-ds-appid="20"><span class="search_review_summary'
        ' no_reviews"></span></a>'
        '<a href="x" data-ds-appid="10"></a>'
    )

    async def fake_raw(url, params, cache_ttl=0):
        return {"success": 1, "total_count": None, "results_html": html}

    monkeypatch.setattr(transport, "_raw_get", fake_raw)
    rows, total = run(catalog._discover_search({}))
    assert rows == [
        {"appid": 10, "review_pct": 92, "review_count": 15632},
        {"appid": 20, "review_pct": None, "review_count": 0},
    ]
    assert total == 2


def test_discover_appids_wrapper(monkeypatch):
    async def fake_raw(url, params, cache_ttl=0):
        return {"success": 1, "total_count": 7,
                "results_html": '<a data-ds-appid="5"></a><a data-ds-appid="6"></a>'}

    monkeypatch.setattr(transport, "_raw_get", fake_raw)
    ids, total = run(catalog._discover_appids({}))
    assert ids == [5, 6] and total == 7
