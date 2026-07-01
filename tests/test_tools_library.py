"""Tests for steam_mcp.tools.library — whole-library analysis."""
import json

import pytest
from pydantic import ValidationError

from conftest import run

from steam_mcp import transport
from steam_mcp.data import players
from steam_mcp.tools.library import LibraryAnalysisInput, steam_analyze_library


def test_analyze_library(monkeypatch):
    payload = {"response": {"game_count": 3, "games": [
        {"appid": 1, "name": "A", "playtime_forever": 6000, "rtime_last_played": 1700000000},
        {"appid": 2, "name": "B", "playtime_forever": 0, "rtime_last_played": 0},
        {"appid": 3, "name": "C", "playtime_forever": 30, "playtime_2weeks": 30,
         "rtime_last_played": 1780000000},
    ]}}

    async def fake_steam(path, params, **k):
        return payload

    monkeypatch.setattr(transport, "_steam_get", fake_steam)
    out = run(steam_analyze_library(
        LibraryAnalysisInput(steamid="76561197960287930", response_format="json")))
    d = json.loads(out)
    assert d["summary"]["game_count"] == 3
    assert d["summary"]["never_played_count"] == 1
    assert d["top_played"][0]["name"] == "A"             # 100h, most played
    assert d["recently_played"][0]["name"] == "C"
    assert d["playtime_buckets"]["0h"] == 1


def test_analyze_library_backlog_truncation(monkeypatch):
    # 5 never-played games, alphabetical A..E; a small backlog_limit must flag
    # truncation and only show the early letters (the bug the chat surfaced).
    games = [
        {"appid": i, "name": ch, "playtime_forever": 0, "rtime_last_played": 0}
        for i, ch in enumerate("ABCDE", start=1)
    ]
    payload = {"response": {"game_count": 5, "games": games}}

    async def fake_steam(path, params, **k):
        return payload

    monkeypatch.setattr(transport, "_steam_get", fake_steam)

    # Truncated: ask for 3 of 5 -> alphabetical slice A,B,C + truncation flag.
    out = run(steam_analyze_library(LibraryAnalysisInput(
        steamid="76561197960287930", backlog_limit=3, response_format="json")))
    d = json.loads(out)
    assert d["backlog_truncated"] is True
    assert [g["name"] for g in d["backlog_never_played"]] == ["A", "B", "C"]

    md = run(steam_analyze_library(LibraryAnalysisInput(
        steamid="76561197960287930", backlog_limit=3)))
    assert "Backlog truncated" in md and "showing 3 of 5" in md

    # Default backlog_limit is the 100 max, so a small backlog is NOT truncated.
    full = run(steam_analyze_library(LibraryAnalysisInput(
        steamid="76561197960287930", response_format="json")))
    assert json.loads(full)["backlog_truncated"] is False
    assert LibraryAnalysisInput(steamid="x").backlog_limit == 100

    # backlog_limit=0 is intentional suppression, not truncation — no nag.
    z = run(steam_analyze_library(LibraryAnalysisInput(
        steamid="76561197960287930", backlog_limit=0)))
    assert "Backlog truncated" not in z
    assert json.loads(run(steam_analyze_library(LibraryAnalysisInput(
        steamid="76561197960287930", backlog_limit=0,
        response_format="json"))))["backlog_truncated"] is False


def test_analyze_library_abandoned_decoupled(monkeypatch):
    # 5 abandoned games (played, last launched long ago). abandoned_limit must
    # govern the Abandoned list on its own — backlog_limit must NOT shrink it.
    old = 1_500_000_000  # well before a 365-day cutoff from 'now'
    games = [
        {"appid": i, "name": ch, "playtime_forever": 120, "rtime_last_played": old + i}
        for i, ch in enumerate("ABCDE", start=1)
    ]
    payload = {"response": {"game_count": 5, "games": games}}

    async def fake_steam(path, params, **k):
        return payload

    monkeypatch.setattr(transport, "_steam_get", fake_steam)

    def abandoned_count(**kw):
        out = run(steam_analyze_library(LibraryAnalysisInput(
            steamid="76561197960287930", response_format="json", **kw)))
        return len(json.loads(out)["abandoned"])

    # backlog_limit must not touch the abandoned list (the decoupling bug fix).
    assert abandoned_count(backlog_limit=1) == abandoned_count(backlog_limit=100) == 5
    # abandoned_limit is what actually bounds it.
    assert abandoned_count(abandoned_limit=2) == 2

    md = run(steam_analyze_library(LibraryAnalysisInput(
        steamid="76561197960287930", abandoned_limit=2)))
    assert "5 total, showing 2" in md

    # Count appears even when NOT truncated — parity with the Backlog header.
    md_full = run(steam_analyze_library(LibraryAnalysisInput(
        steamid="76561197960287930")))
    assert "5 total, showing 5" in md_full

    j = json.loads(run(steam_analyze_library(LibraryAnalysisInput(
        steamid="76561197960287930", abandoned_limit=2, response_format="json"))))
    assert j["abandoned_truncated"] is True
    # abandoned_limit=0 is intentional suppression, not truncation.
    z = json.loads(run(steam_analyze_library(LibraryAnalysisInput(
        steamid="76561197960287930", abandoned_limit=0, response_format="json"))))
    assert z["abandoned"] == [] and z["abandoned_truncated"] is False
    # Defaults: dedicated 25-game abandoned cap, independent of backlog.
    assert LibraryAnalysisInput(steamid="x").abandoned_limit == 25


def test_analyze_library_abandoned_sort(monkeypatch):
    # A..E with increasing last_played (A oldest, E newest) and varied playtime,
    # so the three sort orders are all distinguishable.
    old = 1_500_000_000
    pt = {"A": 600, "B": 100, "C": 500, "D": 200, "E": 50}
    games = [
        {"appid": i, "name": ch, "playtime_forever": pt[ch],
         "rtime_last_played": old + i}
        for i, ch in enumerate("ABCDE", start=1)
    ]
    payload = {"response": {"game_count": 5, "games": games}}

    async def fake_steam(path, params, **k):
        return payload

    monkeypatch.setattr(transport, "_steam_get", fake_steam)

    def order(**kw):
        out = run(steam_analyze_library(LibraryAnalysisInput(
            steamid="76561197960287930", response_format="json", **kw)))
        return [g["name"] for g in json.loads(out)["abandoned"]]

    # Default is 'recent' => most recently dropped first (the ISSUE-2 fix).
    assert order() == ["E", "D", "C", "B", "A"]
    assert LibraryAnalysisInput(steamid="x").abandoned_sort == "recent"
    assert order(abandoned_sort="oldest") == ["A", "B", "C", "D", "E"]
    assert order(abandoned_sort="playtime") == ["A", "C", "D", "B", "E"]

    # Truncation now keeps the most recent, not the most ancient.
    assert order(abandoned_limit=2) == ["E", "D"]

    with pytest.raises(ValidationError):
        LibraryAnalysisInput(steamid="x", abandoned_sort="bogus")


def test_analyze_library_excludes_temp_clients(monkeypatch):
    games = [
        {"appid": 1, "name": "Real Game A", "playtime_forever": 600,
         "rtime_last_played": 1700000000},
        {"appid": 2, "name": "Real Game B", "playtime_forever": 0,
         "rtime_last_played": 0},
        {"appid": 3, "name": "Cool Shooter Playtest", "playtime_forever": 9000,
         "rtime_last_played": 1700000000},
        {"appid": 4, "name": "Big RPG - Beta", "playtime_forever": 0,
         "rtime_last_played": 0},
    ]
    payload = {"response": {"game_count": 4, "games": games}}

    async def fake_steam(path, params, **k):
        return payload

    monkeypatch.setattr(transport, "_steam_get", fake_steam)

    # Default: temp clients dropped from counts and every list.
    d = json.loads(run(steam_analyze_library(LibraryAnalysisInput(
        steamid="76561197960287930", response_format="json"))))
    assert d["summary"]["game_count"] == 2
    assert d["summary"]["temp_clients_excluded"] == 2
    backlog_names = {g["name"] for g in d["backlog_never_played"]}
    assert "Big RPG - Beta" not in backlog_names and "Real Game B" in backlog_names
    assert "Cool Shooter Playtest" not in {g["name"] for g in d["top_played"]}
    assert set(d["temp_clients_excluded_names"]) == {
        "Cool Shooter Playtest", "Big RPG - Beta"}

    md = run(steam_analyze_library(LibraryAnalysisInput(
        steamid="76561197960287930")))
    assert "Excluded **2** non-retail" in md

    # Opt out: everything counted again, including the 150h playtest.
    d2 = json.loads(run(steam_analyze_library(LibraryAnalysisInput(
        steamid="76561197960287930", exclude_temp_clients=False,
        response_format="json"))))
    assert d2["summary"]["game_count"] == 4
    assert d2["summary"]["temp_clients_excluded"] == 0
    assert "Cool Shooter Playtest" in {g["name"] for g in d2["top_played"]}
    assert LibraryAnalysisInput(steamid="x").exclude_temp_clients is True


def test_analyze_library_tiny_playtime_render(monkeypatch):
    # 2 minutes, launched in 2013: 'played'/abandoned but rounds to 0.0h.
    games = [
        {"appid": 1, "name": "A Virus Named TOM", "playtime_forever": 2,
         "rtime_last_played": 1379462400},  # 2013-09-18
        {"appid": 2, "name": "Untouched Game", "playtime_forever": 0,
         "rtime_last_played": 0},
    ]
    payload = {"response": {"game_count": 2, "games": games}}

    async def fake_steam(path, params, **k):
        return payload

    monkeypatch.setattr(transport, "_steam_get", fake_steam)

    d = json.loads(run(steam_analyze_library(LibraryAnalysisInput(
        steamid="76561197960287930", stale_days=365, response_format="json"))))
    # Consistent predicate: launched (>0 min) => played/abandoned, NOT backlog.
    assert "A Virus Named TOM" in {g["name"] for g in d["abandoned"]}
    assert "A Virus Named TOM" not in {g["name"] for g in d["backlog_never_played"]}
    assert "Untouched Game" in {g["name"] for g in d["backlog_never_played"]}
    assert d["summary"]["played_count"] == 1
    assert d["playtime_buckets"]["0h"] == 1  # only the truly-untouched game
    # Numeric hours still rounds to 0.0, but the display string is non-contradictory.
    tom = next(g for g in d["abandoned"] if g["name"] == "A Virus Named TOM")
    assert tom["hours"] == 0.0 and tom["hours_str"] == "<0.1"

    md = run(steam_analyze_library(LibraryAnalysisInput(
        steamid="76561197960287930", stale_days=365)))
    assert "<0.1h, last played 2013-09-18" in md
    assert "0.0h, last played 2013-09-18" not in md


def test_analyze_library_persona_header(monkeypatch):
    sid = "76561197960287930"
    games = [{"appid": 1, "name": "Game A", "playtime_forever": 600,
              "rtime_last_played": 1700000000}]
    payload = {"response": {"game_count": 1, "games": games}}

    async def fake_steam(path, params, **k):
        return payload

    async def persona_ok(ids):
        return {sid: {"personaname": "Sarg338"}}

    monkeypatch.setattr(transport, "_steam_get", fake_steam)
    monkeypatch.setattr(players, "_summaries_for", persona_ok)

    # ISSUE-8: header shows persona (sid), not a bare SteamID64.
    md = run(steam_analyze_library(LibraryAnalysisInput(steamid=sid)))
    assert md.split("\n")[0] == f"# Library analysis for Sarg338 ({sid})"
    # ISSUE-7: clearer average wording.
    assert "across all owned" in md and "across played games" in md
    assert "of played" not in md

    d = json.loads(run(steam_analyze_library(LibraryAnalysisInput(
        steamid=sid, response_format="json"))))
    assert d["persona_name"] == "Sarg338" and d["steamid"] == sid

    # Persona unavailable -> fall back to the bare SteamID (no parens).
    async def persona_empty(ids):
        return {}
    monkeypatch.setattr(players, "_summaries_for", persona_empty)
    md2 = run(steam_analyze_library(LibraryAnalysisInput(steamid=sid)))
    assert md2.split("\n")[0] == f"# Library analysis for {sid}"

    # Persona lookup failure must not break the analysis (best-effort).
    async def persona_boom(ids):
        raise RuntimeError("network")
    monkeypatch.setattr(players, "_summaries_for", persona_boom)
    md3 = run(steam_analyze_library(LibraryAnalysisInput(steamid=sid)))
    assert md3.split("\n")[0] == f"# Library analysis for {sid}"
