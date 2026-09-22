"""Golden-snapshot test of the MCP wire surface exposed by steam_mcp.server.

Importing ``steam_mcp.server`` registers every tool, prompt, and resource
template on the server instance and runs ``_compact_descriptions()``, so the
tool descriptions captured here are the compacted one-line summaries — exactly
what a client sees on the wire. This test freezes that surface (names,
descriptions, complete input schemas, annotations, prompt arguments, and
resource templates) against ``tests/golden/wire_surface.json``.

The point is the stability contract in the README: tool names, input parameters
and JSON output fields are stable public surface. Nothing enforced that
mechanically, so a reworded parameter description or a quietly added field
reached clients unannounced. Now it fails CI instead.

No network and no STEAM_API_KEY are needed: listing the surface never performs
HTTP calls.

Regenerating the golden file
----------------------------
If the surface changes *intentionally*, rewrite the snapshot with::

    UPDATE_GOLDEN=1 pytest tests/test_wire_surface.py

and review the resulting diff of tests/golden/wire_surface.json.
"""
import asyncio
import difflib
import json
import os
from pathlib import Path

import steam_mcp.server as S

GOLDEN_PATH = Path(__file__).parent / "golden" / "wire_surface.json"

EXPECTED_TOOLS = 37
EXPECTED_PROMPTS = 5
EXPECTED_RESOURCE_TEMPLATES = 2


def run(coro):
    return asyncio.run(coro)


def _dump(model) -> dict:
    """Serialize one Tool/Prompt/ResourceTemplate the way the wire sees it.

    `by_alias=True` is load-bearing, not cosmetic: the v1 SDK dumps the camelCase
    aliases (`inputSchema`, `uriTemplate`, `mimeType`) while v2 dumps the
    snake_case field names. CI runs both majors against this one golden file, so
    without the alias the snapshot matches on whichever SDK generated it and
    fails on the other. The aliases are also what actually travels on the wire,
    which is what this test claims to freeze.
    """
    return model.model_dump(mode="json", exclude_none=True, by_alias=True)


async def _build_surface() -> dict:
    """Canonical, deterministic dict of the full MCP wire surface.

    Each Tool/Prompt/ResourceTemplate is dumped in full, matching the SDK's own
    exclude_none wire serialization — so *every* wire-visible field
    (outputSchema, mimeType, titles, icons, argument descriptions, meta,
    annotations, ...) is frozen by the snapshot.
    """
    tools = [_dump(t) for t in await S.mcp.list_tools()]
    prompts = [_dump(p) for p in await S.mcp.list_prompts()]
    templates = [_dump(rt) for rt in await S.mcp.list_resource_templates()]
    # Static (non-template) resources: the server registers none today, but a
    # change that accidentally added one would be wire-visible — freeze them too.
    resources = [_dump(r) for r in await S.mcp.list_resources()]
    return {
        "tools": sorted(tools, key=lambda t: t["name"]),
        "prompts": sorted(prompts, key=lambda p: p["name"]),
        "resource_templates": sorted(templates, key=lambda r: r["uriTemplate"]),
        "resources": sorted(resources, key=lambda r: str(r["uri"])),
    }


def _canonical_json(surface: dict) -> str:
    return json.dumps(surface, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def test_wire_surface_matches_golden_snapshot():
    surface = run(_build_surface())

    assert len(surface["tools"]) == EXPECTED_TOOLS
    assert len(surface["prompts"]) == EXPECTED_PROMPTS
    assert len(surface["resource_templates"]) == EXPECTED_RESOURCE_TEMPLATES
    assert surface["resources"] == []  # no static resources registered today

    actual_json = _canonical_json(surface)

    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(actual_json, encoding="utf-8")
        return

    assert GOLDEN_PATH.exists(), (
        f"Golden snapshot {GOLDEN_PATH} is missing; regenerate it with "
        "UPDATE_GOLDEN=1 pytest tests/test_wire_surface.py"
    )
    golden_json = GOLDEN_PATH.read_text(encoding="utf-8")
    golden = json.loads(golden_json)

    if surface != golden:
        diff = "\n".join(
            difflib.unified_diff(
                golden_json.splitlines(),
                actual_json.splitlines(),
                fromfile="tests/golden/wire_surface.json",
                tofile="actual wire surface",
                lineterm="",
            )
        )
        raise AssertionError(
            "MCP wire surface differs from the golden snapshot. If the change "
            "is intentional, regenerate with UPDATE_GOLDEN=1.\n" + diff
        )
