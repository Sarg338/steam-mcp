"""Shared input-model bases used across steam_mcp tool modules."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from steam_mcp.render import ResponseFormat


class PlayerInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    steamid: Optional[str] = Field(
        default=None,
        description="SteamID64 (17 digits), vanity name, or full profile URL "
        "(e.g. '76561197960287930', 'gabelogannewell'). Omit to use the configured "
        "STEAM_USER (your own Steam name), if set.",
        max_length=200,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="'markdown' for human-readable, 'json' for machine-readable.",
    )


class PlayerGameInput(PlayerInput):
    appid: int = Field(
        ...,
        description="Steam application (game) ID, e.g. 730 for CS2, 570 for Dota 2.",
        ge=1,
    )
    language: str = Field(
        default="english",
        description="Steam language name for localized text (achievement names, "
        "etc.), e.g. 'english', 'french', 'german', 'schinese'. Not ISO codes.",
        min_length=2, max_length=32,
    )


class FriendListInput(PlayerInput):
    limit: int = Field(
        default=50,
        description="Maximum friends to return (1-200). Each is enriched with "
        "name and current status.",
        ge=1,
        le=200,
    )
    offset: int = Field(default=0, description="Friends to skip for pagination.", ge=0)
    online_only: bool = Field(
        default=False,
        description="If true, return only friends who are not Offline.",
    )


class PlayersInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    steamids: list[str] = Field(
        default_factory=list,
        description="List of SteamID64 / vanity names / profile URLs (max 100). "
        "Omit/empty to use the configured STEAM_USER, if set.",
        max_length=100,
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class AppOnlyInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    appid: int = Field(..., description="Steam application (game) ID.", ge=1)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)
