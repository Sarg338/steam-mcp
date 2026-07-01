"""Pure data tables (labels, symbols, IDs, regexes) for the Steam MCP server."""

from __future__ import annotations

import re

# Recent-reviews computation: Steam's query_summary is always lifetime, so the
# recent (last-N-days) score is computed by paginating the newest reviews. These
# bound that work so a hugely-reviewed game can't trigger unbounded requests.
RECENT_PAGE_SIZE = 100
MAX_RECENT_PAGES = 6  # up to 600 most-recent reviews considered

# Steam persona (online) states -> human-readable label.
PERSONA_STATES = {
    0: "Offline",
    1: "Online",
    2: "Busy",
    3: "Away",
    4: "Snooze",
    5: "Looking to trade",
    6: "Looking to play",
}

# Community visibility states from GetPlayerSummaries.
VISIBILITY_STATES = {
    1: "Private",
    2: "Friends only",
    3: "Public",
}

# Currency code -> display symbol. Steam's storefront list endpoints (storesearch,
# featuredcategories, packagedetails) return prices in the requested country's
# currency as integer minor units plus a currency code, but no preformatted
# string -- so we format them ourselves. Unknown codes fall back to
# "<amount> <CODE>", and a missing code falls back to "$".
CURRENCY_SYMBOLS = {
    "USD": "$", "GBP": "£", "EUR": "€", "JPY": "¥", "CNY": "¥",
    "KRW": "₩", "INR": "₹", "RUB": "₽", "BRL": "R$", "CAD": "CA$",
    "AUD": "A$", "NZD": "NZ$", "MXN": "MX$", "ARS": "ARS$", "CLP": "CLP$",
    "COP": "COL$", "PEN": "S/.", "ZAR": "R", "TRY": "₺", "UAH": "₴",
    "PLN": "zł", "CHF": "CHF", "SEK": "kr", "NOK": "kr", "DKK": "kr",
    "HKD": "HK$", "TWD": "NT$", "SGD": "S$", "THB": "฿", "VND": "₫",
    "IDR": "Rp", "MYR": "RM", "PHP": "₱", "AED": "AED", "SAR": "SAR",
    "ILS": "₪", "KZT": "₸", "CRC": "₡",
}

# Steam store "supported player" category IDs that indicate co-op play, used to
# detect co-op games from IStoreBrowseService/GetItems. 9=Co-op, 24=Shared/Split
# Screen, 38=Online Co-op, 39=LAN Co-op.
COOP_CATEGORY_IDS = {9, 24, 38, 39}

PROFILE_URL_RE = re.compile(r"steamcommunity\.com/(profiles|id)/([^/?#]+)", re.IGNORECASE)
STEAMID64_RE = re.compile(r"^7656\d{13}$")  # 17-digit SteamID64 starting 7656

# Steam Deck compatibility (storefront `ajaxgetdeckappcompatibilityreport`):
# resolved_category -> label; resolved_items[].display_type -> a glyph.
DECK_COMPAT_URL = "https://store.steampowered.com/saleaction/ajaxgetdeckappcompatibilityreport"
DECK_CATEGORIES = {0: "Unknown", 1: "Unsupported", 2: "Playable", 3: "Verified"}
DECK_ITEM_STATUS = {2: "✗", 3: "⚠", 4: "✓"}

# CS2/CSGO item wear tiers, as they appear in a market_hash_name's trailing (…).
CS_EXTERIORS = (
    "Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred",
)
