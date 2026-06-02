#!/usr/bin/env python
"""Token-footprint audit for steam-mcp — catch context-bloat regressions early.

Measures the two sources of MCP token bloat (per Anthropic's tool best-practices
and the wider MCP-optimization discourse):

  1. Tool-definition footprint ("schema bloat") — the on-the-wire tool list
     (names + descriptions + input schemas) a client loads into context. This is
     what `_compact_descriptions()` keeps small; this audit guards that it stays
     small. Fully deterministic, no network.

  2. Worst-case tool-response sizes ("response bloat") — the heaviest tools run
     against synthetic MAXIMUM payloads (HTTP mocked, no network), checked against
     Anthropic's ~25,000-token-per-response guidance.

Usage:
    py scripts/audit_tokens.py           # human report; exit 1 if over budget
    py scripts/audit_tokens.py --json     # machine-readable (for CI)

Token counts are ESTIMATES (~chars/4): deterministic and dependency-free, so they
are stable for regression gating. Absolute Claude tokens differ — what matters here
is catching *growth*. Tune the budgets below to sit just above the current
baseline; CI then fails on meaningful increases.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import inspect
import json
import os
import sys

# Make the package importable whether or not it's pip-installed.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import steam_mcp.server as S  # noqa: E402
from steam_mcp.server import mcp  # noqa: E402

# --- Budgets (fail the audit if exceeded) -------------------------------------
# Baselined to the current footprint plus modest headroom. The defs total is
# dominated by the input SCHEMAS (Pydantic Field descriptions), not the top-level
# tool descriptions (_compact_descriptions already trims those). If you trim the
# schemas later, re-run and lower DEFS_TOKEN_BUDGET to the new baseline so the gate
# stays meaningful.
DEFS_TOKEN_BUDGET = 15_000    # all tool defs on the wire (baseline ~13.9k)
PER_TOOL_TOKEN_WARN = 700     # flag a single tool def that's an outlier
RESPONSE_HARD_CAP = 25_000    # Anthropic's per-response guidance (hard fail)
RESPONSE_WARN = 20_000        # warn band approaching the cap


def est_tokens(text: str) -> int:
    """Deterministic, dependency-free token estimate (~chars / 4)."""
    return (len(text) + 3) // 4


# --- Part 1: tool-definition footprint ----------------------------------------

def audit_definitions() -> dict:
    tools = asyncio.run(mcp.list_tools())
    reg = getattr(mcp._tool_manager, "_tools", {})  # internal; best-effort
    per_tool, total, raw_total = [], 0, 0
    for t in tools:
        obj = t.model_dump(mode="json", exclude_none=True)
        wire = json.dumps(obj, separators=(",", ":"))
        tok = est_tokens(wire)
        total += tok
        per_tool.append({"name": t.name, "tokens": tok, "chars": len(wire)})
        # What this def WOULD cost without _compact_descriptions (full docstring).
        fn = getattr(reg.get(t.name), "fn", None)
        doc = inspect.getdoc(fn) if fn else None
        raw_obj = dict(obj, description=doc) if doc else obj
        raw_total += est_tokens(json.dumps(raw_obj, separators=(",", ":")))
    per_tool.sort(key=lambda d: d["tokens"], reverse=True)
    return {
        "tool_count": len(tools),
        "total_tokens": total,
        "uncompacted_tokens": raw_total,
        "budget": DEFS_TOKEN_BUDGET,
        "per_tool": per_tool,
    }


# --- Part 2: worst-case response sizes ----------------------------------------

@contextlib.contextmanager
def _patch(**attrs):
    old = {k: getattr(S, k) for k in attrs}
    for k, v in attrs.items():
        setattr(S, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(S, k, v)


def _games(n: int, never_frac: float = 0.5) -> list:
    """Synthetic owned-games list with realistic-worst-case (~55-char) names."""
    never = int(n * never_frac)
    out = []
    for i in range(n):
        is_never = i < never
        out.append({
            "appid": 1_000_000 + i,
            "name": f"Example Game Title Number {i} - Definitive Deluxe Edition",
            "playtime_forever": 0 if is_never else ((i * 37) % 5999) + 1,
            "playtime_2weeks": (i % 60) if (i % 7 == 0 and not is_never) else 0,
            "rtime_last_played": 0 if is_never else 1_500_000_000 + i * 1000,
        })
    return out


def _run_tool(coro_fn, inp):
    return asyncio.run(coro_fn(inp))


def _scenarios() -> list:
    """(label, {markdown, json}) for the tools whose output scales with library
    size — the genuine response-bloat risk. Other tools are bounded by small
    per-item limits or per-field truncation."""
    owned200 = {"response": {"game_count": 200, "games": _games(200)}}
    owned200_played = {"response": {"game_count": 200,
                                    "games": _games(200, never_frac=0.0)}}

    async def fake_owned(path, params, **k):
        return owned200

    async def fake_owned_played(path, params, **k):
        return owned200_played

    async def fake_sum(ids):
        return {i: {"personaname": "Player One"} for i in ids}

    out = []

    # 1. analyze_library at max backlog + abandoned (the heaviest tool).
    with _patch(_steam_get=fake_owned, _summaries_for=fake_sum):
        res = {}
        for fmt in ("markdown", "json"):
            res[fmt] = _run_tool(S.steam_analyze_library, S.LibraryAnalysisInput(
                steamid="76561197960287930", backlog_limit=100,
                abandoned_limit=100, response_format=fmt))
        out.append(("steam_analyze_library (200 games, limits maxed)", res))

    # 2. get_owned_games at limit=200.
    with _patch(_steam_get=fake_owned_played):
        res = {}
        for fmt in ("markdown", "json"):
            res[fmt] = _run_tool(S.steam_get_owned_games, S.OwnedGamesInput(
                steamid="76561197960287930", limit=200, response_format=fmt))
        out.append(("steam_get_owned_games (limit=200)", res))

    # 3. compare_players at limit=100 (both libraries fully shared).
    with _patch(_steam_get=fake_owned_played):
        res = {}
        for fmt in ("markdown", "json"):
            res[fmt] = _run_tool(S.steam_compare_players, S.ComparePlayersInput(
                steamid_a="76561197960287930", steamid_b="76561197960287931",
                limit=100, response_format=fmt))
        out.append(("steam_compare_players (limit=100)", res))

    return out


def audit_responses() -> dict:
    rows = []
    worst = 0
    for label, res in _scenarios():
        md, js = est_tokens(res["markdown"]), est_tokens(res["json"])
        worst = max(worst, md, js)
        rows.append({"tool": label, "markdown_tokens": md, "json_tokens": js})
    return {"hard_cap": RESPONSE_HARD_CAP, "warn": RESPONSE_WARN,
            "worst": worst, "rows": rows}


# --- Report -------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="steam-mcp token-footprint audit")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    defs = audit_definitions()
    resp = audit_responses()

    defs_over = defs["total_tokens"] > defs["budget"]
    resp_over = resp["worst"] > resp["hard_cap"]
    ok = not (defs_over or resp_over)

    if args.json:
        print(json.dumps({"ok": ok, "definitions": defs, "responses": resp},
                         indent=2))
        return 0 if ok else 1

    saved = (1 - defs["total_tokens"] / defs["uncompacted_tokens"]) * 100 \
        if defs["uncompacted_tokens"] else 0
    print("steam-mcp token audit")
    print("=" * 60)
    print(f"\nTool definitions (schema bloat) - {defs['tool_count']} tools")
    flag = "OVER BUDGET" if defs_over else "ok"
    print(f"  total: ~{defs['total_tokens']:,} est tokens   "
          f"[budget {defs['budget']:,}]  {flag}")
    print(f"  uncompacted would be ~{defs['uncompacted_tokens']:,} tokens "
          f"(_compact_descriptions saves ~{saved:.0f}%)")
    print("  largest tool defs:")
    for d in defs["per_tool"][:8]:
        mark = "  <-- large" if d["tokens"] > PER_TOOL_TOKEN_WARN else ""
        print(f"    {d['name']:<34} ~{d['tokens']:>4} tok{mark}")

    print(f"\nResponse sizes (response bloat) - worst-case synthetic, "
          f"cap {resp['hard_cap']:,}")
    for r in resp["rows"]:
        hi = max(r["markdown_tokens"], r["json_tokens"])
        flag = ("  OVER CAP" if hi > resp["hard_cap"]
                else "  warn" if hi > resp["warn"] else "")
        print(f"    {r['tool']:<46} md ~{r['markdown_tokens']:>5}  "
              f"json ~{r['json_tokens']:>5}{flag}")

    print("\n" + "=" * 60)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
