"""Readwise tool: fetches saved highlights via the Readwise REST API.

The canonical implementation is async (``httpx.AsyncClient``) per the Phase 1
concurrency requirements. The synchronous LangChain ``_run`` bridges to it via
:func:`utils.async_bridge.run_async` so it can be driven from the sync worker
thread.
"""

from __future__ import annotations

import logging
import os
from typing import Type

import httpx
from pydantic import BaseModel

from config import (
    EXTERNAL_API_TIMEOUT,
    READWISE_API_ENDPOINT,
    READWISE_HIGHLIGHTS_LIMIT,
    READWISE_TOKEN_OPTIMIZATION_MAX_CHARS,
)
from utils.async_bridge import run_async
from utils.text import optimize_token_payload

from .base import BaseIvyTool

logger = logging.getLogger("ivy.tools.readwise")


async def fetch_readwise_highlights_async() -> str:
    """Fetch saved articles/highlights from the Readwise API (async)."""
    active_token = os.environ.get("READWISE_API_KEY", "")
    if not active_token:
        return "❌ Readwise pipeline offline: READWISE_API_KEY missing from environment."

    headers = {"Authorization": f"Token {active_token}"}
    try:
        async with httpx.AsyncClient(timeout=EXTERNAL_API_TIMEOUT) as client:
            response = await client.get(READWISE_API_ENDPOINT, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("Readwise request failed: %s", exc)
        return "❌ Readwise is unreachable right now."

    if response.status_code != 200:
        logger.warning("Readwise API status %s", response.status_code)
        return "❌ Readwise API connection issue. Please try again later."

    data = response.json()
    results = data.get("results", [])
    if not results:
        return "Your Readwise repository is currently clear of saved elements."

    compiled_items = []
    for item in results[:READWISE_HIGHLIGHTS_LIMIT]:
        text = item.get("text", "")
        note = item.get("note", "")
        title = item.get("title", "Saved Article")
        block = f"- From '{title}': \"{text}\""
        if note:
            block += f" (Note: {note})"
        compiled_items.append(block)

    raw_output = "\n".join(compiled_items)
    return optimize_token_payload(
        raw_output, max_chars=READWISE_TOKEN_OPTIMIZATION_MAX_CHARS
    )


class ReadwiseArgs(BaseModel):
    """Readwise fetch takes no arguments."""


class FetchReadwiseTool(BaseIvyTool):
    name: str = "fetch_readwise_highlights"
    description: str = (
        "Retrieves saved articles and highlights from the Readwise API. Use when "
        "the user asks about their reading, saved articles, or highlights."
    )
    args_schema: Type[BaseModel] = ReadwiseArgs

    def _run(self, **_: object) -> str:
        return run_async(fetch_readwise_highlights_async())

    async def _arun(self, **_: object) -> str:
        return await fetch_readwise_highlights_async()
