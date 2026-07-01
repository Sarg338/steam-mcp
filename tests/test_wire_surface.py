"""Golden-snapshot test of the MCP wire surface exposed by steam_mcp.server.

Importing ``steam_mcp.server`` registers every tool, prompt, and resource
template on the FastMCP instance and runs ``_compact_descriptions()``, so the
tool descriptions captured here are the compacted one-line summaries — exactly
what a client sees on the wire. This test freezes that surface (names,
descriptions, complete input schemas, annotations, prompt arguments, and
resource templates) against ``tests/golden/wire_surface.json`` so a mechanical
package split can prove it changed nothing observable.

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
    return model.model_dump(mode="json", exclude_none=True)


async def _build_surface() -> dict:
    """Canonical, deterministic dict of the full MCP wire surface.

    Each Tool/Prompt/ResourceTemplate is dumped in full with
    ``model_dump(mode="json", exclude_none=True)`` — matching the SDK's own
    exclude_none wire serialization — so *every* wire-visible field
    (outputSchema, mimeType, titles, icons, argument descriptions, meta,
    annotations, ...) is frozen by the snapshot.
    """
    tools = [_dump(t) for t in await S.mcp.list_tools()]
    prompts = [_dump(p) for p in await S.mcp.list_prompts()]
    templates = [_dump(rt) for rt in await S.mcp.list_resource_templates()]
    # Static (non-template) resources: the server registers none today, but a
    # refactor that accidentally added one would be wire-visible — freeze them too.
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
