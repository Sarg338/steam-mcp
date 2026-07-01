"""Tests for the registered MCP surface: prompts, resources, tool registration,
and compacted wire descriptions.

Importing ``steam_mcp.server`` guarantees full registration (tools, prompts,
resources) and runs ``_compact_descriptions()``.
"""
import json

from conftest import run

import steam_mcp.server  # noqa: F401  (registration + description compaction)
from steam_mcp import transport
from steam_mcp.app import mcp


def test_prompts_registered():
    names = {p.name for p in run(mcp.list_prompts())}
    assert {"what_should_i_play", "is_it_worth_buying", "plan_game_night",
            "steam_deals", "game_overview"} <= names


def test_prompt_renders():
    res = run(mcp.get_prompt("plan_game_night", {"steamid": "123"}))
    text = " ".join(getattr(m.content, "text", str(m.content)) for m in res.messages)
    assert "steam_plan_coop_night" in text and "123" in text


def test_resources_registered():
    uris = {t.uriTemplate for t in run(mcp.list_resource_templates())}
    assert "steam://app/{appid}" in uris
    assert "steam://user/{steamid}" in uris


def test_resource_app_reads(monkeypatch):
    async def fake_store(path, params, cache_ttl=0):
        return {"570": {"success": True,
                        "data": {"name": "Dota 2", "type": "game", "is_free": True}}}

    monkeypatch.setattr(transport, "_store_get", fake_store)
    parts = list(run(mcp.read_resource("steam://app/570")))
    text = " ".join(str(getattr(p, "content", p)) for p in parts)
    assert "Dota 2" in text


def test_descriptions_compact():
    # The model pays for tool descriptions every request; they must be one-line
    # summaries, not the full multi-paragraph docstrings.
    tools = run(mcp.list_tools())
    for t in tools:
        assert "\n\n" not in (t.description or ""), t.name
    total = sum(len(t.description or "") for t in tools)
    assert total < 6000        # full docstrings were ~20k chars


def test_tools_registered():
    """Reviews tool must be wired to the real function (regression: the
    @mcp.tool decorator used to sit on the _fmt_review helper), and the new
    0.7.0 tools must be registered."""
    tools = run(mcp.list_tools())
    by_name = {t.name: t for t in tools}
    assert "steam_get_app_reviews" in by_name
    assert "steam_get_dlc" in by_name
    assert "steam_get_user_game_stats" in by_name
    assert "steam_get_app_tags" in by_name
    assert "steam_get_rarest_unlocks" in by_name
    assert "steam_find_friends_who_own" in by_name
    assert "steam_discover" in by_name
    assert "steam_should_i_buy" in by_name
    assert "steam_recommend" in by_name
    assert "steam_plan_coop_night" in by_name
    assert "steam_get_app_regional_pricing" in by_name
    assert "steam_get_workshop_item" in by_name
    assert "steam_get_user_groups" in by_name
    assert "steam_get_inventory" in by_name
    assert "steam_get_market_price" in by_name
    # the reviews tool takes the reviews input (has appid + review_filter),
    # not _fmt_review's raw-dict signature
    schema = json.dumps(by_name["steam_get_app_reviews"].inputSchema)
    assert "appid" in schema and "review_filter" in schema
