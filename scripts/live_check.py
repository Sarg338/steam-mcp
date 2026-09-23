#!/usr/bin/env python
"""Live ground-truth check: do our tools say what the Steam store page says?

The unit tests mock HTTP, so they prove the code does what we *think* Steam
returns. This script checks what Steam actually shows people: for a spread of
games (paid, free-to-play, early access, on sale, review-bombed, unreleased) it
runs our tools and compares their key numbers with the store page itself.
That is how the 1.17 review-population bug was found — nothing in Steam's docs
said the store leaves key activations out of a paid game's score.

Needs network access to Steam; no API key. Dev-only (not in the bundle/sdist).

    py scripts/live_check.py              # every game, mismatches only
    py scripts/live_check.py -v           # also list the checks that passed
    py scripts/live_check.py 1091500 570  # just these appids

Exit status is 1 if anything disagrees. Steam serves slightly different numbers
to different requests seconds apart (reviews arrive constantly), so counts are
compared with a small tolerance.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import sys

import httpx2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import steam_mcp.server as S  # noqa: E402

# A deliberately mixed set: each earns its place with a different shape.
GAMES = {
    1091500: "paid, big, review-bombed at launch",
    2358720: "paid, mostly non-English reviews",
    1245620: "paid, huge",
    413150: "paid, cheap, Overwhelmingly Positive",
    1086940: "paid, many languages",
    570: "free-to-play, no Steam purchases",
    730: "free-to-play (formerly paid)",
    3241660: "early access",
    892970: "early access, developer replies",
    105600: "old, cheap, 1000s of DLC-less reviews",
    1145350: "sequel, recent full release",
    1172710: "often on sale",
}
# Titles people type, and the game they mean. Steam's storesearch ranks the
# popular sequel or a DLC first for most of these; the store's search page, and
# steam_search_apps since 1.17.1, put the exact title (then base games) first.
SEARCHES = {
    "Hades": 1145360, "Portal": 400, "Doom": 379720, "Half-Life": 70,
    "The Witcher 3": 292030, "Prey": 480490, "Elden Ring": 1245620,
    "Stardew": 413150, "cs2": 730,
}
COOKIES = {"birthtime": "0", "wants_mature_content": "1",
           "lastagecheckage": "1-0-1990", "Steam_Language": "english"}
COUNT_TOLERANCE = 0.005   # 0.5% drift between the page and API requests
PCT_TOLERANCE = 1.0       # the page rounds to whole percent


class Report:
    def __init__(self, verbose: bool):
        self.verbose, self.failures, self.passes = verbose, [], 0

    def check(self, app: str, what: str, ours, theirs, ok: bool | None = None):
        ok = (ours == theirs) if ok is None else ok
        if ok:
            self.passes += 1
            if self.verbose:
                print(f"  ok   {what}: {ours!r}")
        else:
            self.failures.append((app, what, ours, theirs))
            print(f"  DIFF {what}: ours={ours!r} store={theirs!r}")

    def skip(self, what: str, why: str):
        if self.verbose:
            print(f"  --   {what}: {why}")


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"\s+", " ", fragment))).strip()


def _first(rx: str, page: str) -> str | None:
    m = re.search(rx, page, re.S)
    return _text(m.group(1)) if m else None


def _int(s: str | None) -> int | None:
    return int(re.sub(r"[^\d]", "", s)) if s and re.search(r"\d", s) else None


def _near(a, b, tol=COUNT_TOLERANCE) -> bool:
    if a is None or b is None:
        return a == b
    return abs(a - b) <= max(2, tol * max(a, b))


def parse_page(page: str) -> dict:
    """Pull the fields we compare out of a store page's HTML."""
    out: dict = {}
    # The purchase box for the base game is the first on the page; DLC and
    # bundles follow it, so only the first price/discount counts.
    box = re.search(r'<div class="game_purchase_action_bg">(.*?)</div>\s*</div>',
                    page, re.S)
    box_html = box.group(1) if box else ""
    out["price"] = (_first(r'class="discount_final_price"[^>]*>(.*?)<', box_html)
                    or _first(r'class="game_purchase_price price"[^>]*>(.*?)<',
                              box_html))
    out["discount_pct"] = _int(_first(r'class="discount_pct"[^>]*>(.*?)<', box_html)) or 0
    out["release_date"] = _first(r'class="date">(.*?)<', page)
    out["developers"] = [_text(a) for a in re.findall(
        r"<a[^>]*>(.*?)</a>", (re.search(r'id="developers_list">(.*?)</div>',
                                         page, re.S) or [None, ""])[1])]
    out["metacritic"] = _int(_first(
        r'id="game_area_metascore".*?class="score[^"]*">\s*(\d+)', page))
    out["achievements"] = _int(_first(r"Includes ([\d,]+) Steam Achievements", page))
    plat_box = re.search(r'class="game_area_purchase_platform">(.*?)</div>', page, re.S)
    plats = set(re.findall(r'platform_img (win|mac|linux)',
                           plat_box.group(1) if plat_box else ""))
    out["platforms"] = {"win": "windows", "mac": "mac", "linux": "linux"}
    out["platforms"] = {out["platforms"][p] for p in plats}
    out["tags"] = [_text(t) for t in re.findall(r'class="app_tag"[^>]*>(.*?)<', page, re.S)]
    out["early_access"] = "early_access_header" in page
    for tip in re.findall(r'data-tooltip-html="([^"]+)"', page):
        tip = html.unescape(tip)
        m = re.search(r"(\d+)% of the ([\d,]+) user reviews (in your language|in the "
                      r"last 30 days)", tip)
        if m:
            key = "lifetime" if "language" in m.group(3) else "recent"
            out.setdefault(key, (int(m.group(1)), _int(m.group(2))))
    return out


async def check_app(client: httpx2.AsyncClient, appid: int, rep: Report):
    r = await client.get(f"https://store.steampowered.com/app/{appid}/",
                         params={"l": "english", "cc": "us"})
    page = parse_page(r.text)
    d = json.loads(await S.steam_get_app_details(
        S.AppDetailsInput(appid=appid, response_format="json")))
    name = d.get("name") or appid
    print(f"\n{name} ({appid}) — {GAMES.get(appid, '')}")
    tag = f"{name}"

    if d.get("is_free"):
        rep.check(tag, "price (free)", True, page["price"] in (None, "Free",
                  "Free to Play", "Free To Play"), ok=True)
    elif page["price"]:
        rep.check(tag, "price", d.get("price"), page["price"])
        rep.check(tag, "discount_pct", d.get("discount_pct") or 0, page["discount_pct"])
    else:
        rep.skip("price", "no price on the page (unreleased or unavailable)")
    rep.check(tag, "release_date", d.get("release_date"), page["release_date"])
    rep.check(tag, "developers", d.get("developers"), page["developers"])
    rep.check(tag, "metacritic", d.get("metacritic"), page["metacritic"])
    if page["achievements"] is not None or d.get("achievements_total"):
        rep.check(tag, "achievements_total", d.get("achievements_total"),
                  page["achievements"])
    rep.check(tag, "platforms", set(d.get("platforms") or []), page["platforms"])

    t = json.loads(await S.steam_get_app_tags(
        S.AppTagsInput(appid=appid, response_format="json")))
    ours = [x["tag"] for x in t.get("tags", [])][:5]
    rep.check(tag, "top 5 tags", ours, page["tags"][:5])

    rv = json.loads(await S.steam_get_app_reviews(S.AppReviewsInput(
        appid=appid, limit=0, response_format="json")))
    s = rv.get("summary", {})
    if "lifetime" in page:
        pct, n = page["lifetime"]
        rep.check(tag, "lifetime reviews (count)", s.get("total_reviews"), n,
                  ok=_near(s.get("total_reviews"), n))
        rep.check(tag, "lifetime reviews (%)", s.get("positive_pct"), pct,
                  ok=abs((s.get("positive_pct") or 0) - pct) <= PCT_TOLERANCE)
    if "recent" in page and page["recent"][1] <= 10_000:
        pct, n = page["recent"]
        rc = json.loads(await S.steam_get_app_reviews(S.AppReviewsInput(
            appid=appid, limit=0, review_filter="recent", language="all",
            recent_max_reviews=10_000, response_format="json")))["recent"]
        rep.check(tag, "30-day reviews (count)", rc["reviews_counted"], n,
                  ok=_near(rc["reviews_counted"], n, 0.02))
        rep.check(tag, "30-day reviews (%)", rc["positive_pct"], pct,
                  ok=abs(rc["positive_pct"] - pct) <= PCT_TOLERANCE)
    elif "recent" in page:
        rep.skip("30-day reviews", f"{page['recent'][1]:,} is over the 10k cap")

    gb = await client.get(f"https://store.steampowered.com/app/{appid}/",
                          params={"l": "english", "cc": "gb"})
    gb_page = parse_page(gb.text)
    if gb_page["price"] and not d.get("is_free"):
        rp = json.loads(await S.steam_get_app_regional_pricing(S.RegionalPricingInput(
            appid=appid, countries=["gb"], response_format="json")))
        rep.check(tag, "price in GB", rp["prices"][0].get("price"), gb_page["price"])

    sr = json.loads(await S.steam_search_apps(S.AppSearchInput(
        query=str(name), response_format="json")))
    top = (sr.get("results") or [{}])[0].get("appid")
    rep.check(tag, "search finds it first", top, appid)


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("appids", nargs="*", type=int)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    rep = Report(args.verbose)
    headers = {"User-Agent": "Mozilla/5.0 (steam-mcp live_check)",
               "Accept-Language": "en-US,en;q=0.9"}
    async with httpx2.AsyncClient(timeout=30, headers=headers, cookies=COOKIES,
                                  follow_redirects=True) as client:
        if not args.appids:
            print("\nSearch: does the title find the game people mean?")
            for query, want in SEARCHES.items():
                got = json.loads(await S.steam_search_apps(S.AppSearchInput(
                    query=query, response_format="json")))["results"]
                rep.check("search", f"{query!r}", (got or [{}])[0].get("appid"), want)
        for appid in args.appids or list(GAMES):
            try:
                await check_app(client, appid, rep)
            except Exception as e:  # noqa: BLE001 - report and keep going
                rep.failures.append((appid, "check crashed", repr(e), None))
                print(f"  CRASH {e!r}")
    print(f"\n{rep.passes} checks agreed, {len(rep.failures)} disagreed.")
    return 1 if rep.failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
