"""Rendering / formatting helpers (text, prices, times) for the Steam MCP server."""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any, Optional

from steam_mcp.constants import CURRENCY_SYMBOLS, PERSONA_STATES


class ResponseFormat(str, Enum):
    """Output format for tool responses."""

    MARKDOWN = "markdown"
    JSON = "json"


def _persona_label(player: dict) -> str:
    """Human label for a player's current status, including current game."""
    game = player.get("gameextrainfo")
    if game:
        return f"In-Game: {game}"
    return PERSONA_STATES.get(player.get("personastate", 0), "Unknown")


def _minutes_to_hours(minutes: Optional[int]) -> float:
    return round((minutes or 0) / 60.0, 1)


def _hours_str(minutes: Optional[int]) -> str:
    """Display hours, but never render a *launched* game (>0 min) as a flat '0.0'.

    A game played 1-5 minutes rounds to 0.0h, which looks like a contradiction next
    to a 'played'/'abandoned' classification (those use playtime_forever > 0, not
    the rounded hours). Show '<0.1' for launched-but-tiny playtime; 0 minutes stays
    '0.0'.
    """
    m = minutes or 0
    h = _minutes_to_hours(m)
    return "<0.1" if m > 0 and h == 0 else f"{h}"


def _dump(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _fmt_amount(amount: Optional[float], currency: Optional[str] = None) -> Optional[str]:
    """Format a price with the right currency symbol.

    `amount` is in major units (e.g. dollars — already divided by 100). Falls back
    to "<amount> <CODE>" for currencies without a known symbol, and to "$" only
    when no currency code is available at all.
    """
    if amount is None:
        return None
    if currency:
        sym = CURRENCY_SYMBOLS.get(currency.upper())
        if sym:
            return f"{sym}{amount:,.2f}"
        return f"{amount:,.2f} {currency.upper()}"
    return f"${amount:,.2f}"


_STRIP_HTML_MAX = 20000  # cap raw input before the O(n^2) tag regexes (ReDoS guard)


def _strip_html(s, limit: int = 600):
    """Strip HTML tags/entities to readable plain text, truncated to `limit`."""
    if not s:
        return None
    # `<[^>]+>` is quadratic on pathological input (a flood of unmatched '<'), and
    # this runs on upstream Steam descriptions. The output is truncated to `limit`
    # anyway, so cap the raw input first — 20k chars yields far more than any
    # realistic `limit` of text, while bounding worst-case work to a constant.
    if len(s) > _STRIP_HTML_MAX:
        s = s[:_STRIP_HTML_MAX]
    import html as _html
    s = re.sub(r"<\s*br\s*/?>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return None
    return (s[: limit - 1] + "…") if len(s) > limit else s


def _parse_languages(html_str):
    """Parse Steam's supported_languages HTML into (all, full_audio) name lists.

    Steam marks full-audio languages with an asterisk, e.g.
    'English<strong>*</strong>, French, German<br><strong>*</strong>languages...'.
    """
    if not html_str:
        return [], []
    head = re.split(r"<\s*br\s*/?>", html_str)[0]
    out, audio = [], []
    for seg in head.split(","):
        full = "*" in seg
        name = re.sub(r"<[^>]+>", "", seg).replace("*", "").strip()
        if name:
            out.append(name)
            if full:
                audio.append(name)
    return out, audio


def _ts_to_date(ts):
    """Unix seconds -> 'YYYY-MM-DD'. None for missing/sentinel values (pre-2001).

    Steam only began recording last-played timestamps ~2019; older plays carry a
    tiny placeholder value, so anything before 2001 is treated as 'unknown'.
    """
    try:
        if not ts or ts < 1_000_000_000:
            return None
        import datetime as _dt
        return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return None
