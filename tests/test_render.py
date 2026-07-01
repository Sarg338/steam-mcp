"""Tests for steam_mcp.render — pure formatting helpers."""
from steam_mcp import render
from steam_mcp.data import catalog
from steam_mcp.tools import market


def test_strip_html():
    assert render._strip_html(None) is None
    assert render._strip_html("<b>Hi</b>&amp; <br>there") == "Hi & there"
    assert render._strip_html("<p>   </p>") is None
    out = render._strip_html("x" * 1000, limit=50)
    assert len(out) == 50 and out.endswith("…")


def test_parse_languages():
    a, au = render._parse_languages(
        "English<strong>*</strong>, French, German<br><strong>*</strong>full audio"
    )
    assert a == ["English", "French", "German"]
    assert au == ["English"]
    assert render._parse_languages("") == ([], [])


def test_ts_to_date():
    assert render._ts_to_date(0) is None
    assert render._ts_to_date(86400) is None  # pre-2001 sentinel
    assert render._ts_to_date(1700000000) == "2023-11-14"


def test_minutes_to_hours():
    assert render._minutes_to_hours(90) == 1.5
    assert render._minutes_to_hours(None) == 0.0
    assert render._minutes_to_hours(0) == 0.0


def test_persona_label():
    assert render._persona_label({"personastate": 3}) == "Away"
    assert render._persona_label({"personastate": 1, "gameextrainfo": "Dota 2"}) == "In-Game: Dota 2"
    assert render._persona_label({}) == "Offline"


def test_fmt_amount():
    assert render._fmt_amount(9.99, "USD") == "$9.99"
    assert render._fmt_amount(9.99, "GBP") == "£9.99"
    assert render._fmt_amount(1234.5, "EUR") == "€1,234.50"
    assert render._fmt_amount(9.99, "ZZZ") == "9.99 ZZZ"   # unknown -> code suffix
    assert render._fmt_amount(9.99, None) == "$9.99"        # no code -> $ fallback
    assert render._fmt_amount(None, "USD") is None


def test_hours_str_floor():
    assert render._hours_str(0) == "0.0"      # truly never launched
    assert render._hours_str(None) == "0.0"
    assert render._hours_str(1) == "<0.1"     # launched 1-2 min -> rounds to 0.0h
    assert render._hours_str(2) == "<0.1"
    assert render._hours_str(6) == "0.1"      # 6 min = 0.1h, shown normally
    assert render._hours_str(90) == "1.5"


def test_redos_inputs_are_bounded():
    # Inputs are length-capped before the O(n^2) regexes, so pathological strings
    # finish near-instantly instead of stalling the event loop for seconds.
    import time as _t
    cases = [
        lambda: render._strip_html("<" * 200_000, limit=2000),
        lambda: market._parse_cs_attributes("(" * 200_000),
        lambda: catalog._is_temp_client(("a." * 5000) + " demoX"),
    ]
    for fn in cases:
        t0 = _t.perf_counter()
        fn()
        assert _t.perf_counter() - t0 < 2.0
    # Capping doesn't change normal results.
    assert render._strip_html("<b>Hi</b> there") == "Hi there"
    assert market._parse_cs_attributes(
        "StatTrak™ AK-47 | Redline (Field-Tested)")["exterior"] == "Field-Tested"
