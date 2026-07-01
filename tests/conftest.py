"""Shared test helpers for the steam_mcp test suite."""
import asyncio


def run(coro):
    return asyncio.run(coro)
