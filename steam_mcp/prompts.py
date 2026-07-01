"""Prompts — guided, one-shot flows that orchestrate the tools."""

from __future__ import annotations

from steam_mcp.app import mcp


@mcp.prompt(
    name="what_should_i_play",
    description="Recommend what to play next from a user's library and taste.",
)
def prompt_what_should_i_play(steamid: str) -> str:
    return (
        f"Recommend what the Steam user '{steamid}' should play next. "
        f"(1) Call steam_analyze_library(steamid='{steamid}') to surface their "
        f"backlog and abandoned games they already own. (2) Call "
        f"steam_recommend(steamid='{steamid}') for NEW games matching their taste. "
        f"Then give a short, friendly shortlist: a couple of owned-but-unplayed "
        f"games worth finishing AND a couple of new games to consider — one line on "
        f"why each fits their taste."
    )


@mcp.prompt(
    name="is_it_worth_buying",
    description="Decide whether a game is worth buying right now.",
)
def prompt_is_it_worth_buying(game: str, steamid: str = "") -> str:
    base = (
        f"Help decide whether to buy '{game}' on Steam right now. If '{game}' is a "
        f"title rather than an appid, resolve it with steam_search_apps first, then "
        f"call steam_should_i_buy with that appid. Weigh the price/discount, the "
        f"LIFETIME vs RECENT review trend, the tags, and Metacritic, then give a "
        f"clear recommendation with the reasoning."
    )
    if steamid:
        base += (
            f" Personalize it: pass steamid='{steamid}' to steam_should_i_buy to "
            f"check whether they already own it and how its tags match their "
            f"most-played games."
        )
    return base


@mcp.prompt(
    name="plan_game_night",
    description="Plan a co-op game night with a user's online friends.",
)
def prompt_plan_game_night(steamid: str) -> str:
    return (
        f"Plan a co-op game night for Steam user '{steamid}'. Call "
        f"steam_plan_coop_night(steamid='{steamid}') to find co-op games the user "
        f"and their online friends all own. Present the top options — noting who's "
        f"online now and how many of the group own each — and suggest one to start."
    )


@mcp.prompt(
    name="steam_deals",
    description="Find Steam deals worth buying right now.",
)
def prompt_steam_deals(max_price: str = "") -> str:
    extra = f" Focus on games at or under {max_price} (pass max_price)." if max_price else ""
    return (
        "Find good Steam deals right now. Use steam_get_featured_specials and/or "
        "steam_discover(on_sale=true, sort='reviews') to get discounted games, prefer "
        "well-reviewed ones (check steam_get_app_reviews for anything promising), and "
        f"summarize the best 5-10 with price, discount, and review score.{extra}"
    )


@mcp.prompt(
    name="game_overview",
    description="Give a comprehensive overview of a game.",
)
def prompt_game_overview(game: str) -> str:
    return (
        f"Give a comprehensive overview of '{game}' on Steam. Resolve the appid with "
        f"steam_search_apps if needed, then combine steam_get_app_details, "
        f"steam_get_app_tags, steam_get_app_reviews (lifetime + recent), and "
        f"steam_get_current_players into a tight summary: what it is, price, how it "
        f"reviews, its vibe (tags), and how alive it is right now."
    )
