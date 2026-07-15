"""Async external-service calls (DeepSeek failover).

Uses ``httpx.AsyncClient`` (no synchronous ``requests``). Tool calls returned by
DeepSeek are dispatched through the shared :data:`tools.tool_registry`, never via
``globals()``.
"""

from __future__ import annotations

import json
import logging
import os

import httpx

from config import DEEPSEEK_TOOL_SCHEMA, EXTERNAL_API_TIMEOUT
from tools import tool_registry

logger = logging.getLogger("ivy.services.external")

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"


async def execute_deepseek_call_async(text_content: str, system_instruction: str) -> str:
    """Call the DeepSeek chat API (async), dispatching any tool calls.

    Returns a user-safe string. Internal failures are logged and summarized;
    stack traces / raw upstream errors are never returned to the caller.
    """
    active_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not active_key:
        logger.warning("DeepSeek call attempted with no DEEPSEEK_API_KEY configured.")
        return "DeepSeek is not configured (missing DEEPSEEK_API_KEY)."

    headers = {
        "Authorization": f"Bearer {active_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": text_content},
        ],
        "tools": DEEPSEEK_TOOL_SCHEMA,
        "temperature": 0.1,
    }

    try:
        async with httpx.AsyncClient(timeout=EXTERNAL_API_TIMEOUT) as client:
            response = await client.post(DEEPSEEK_URL, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("DeepSeek request failed: %s", exc)
        return "❌ Failover engine is unreachable right now."

    if response.status_code != 200:
        logger.warning("DeepSeek API status %s", response.status_code)
        return "❌ Failover engine returned an error. Please try again."

    try:
        message_node = response.json()["choices"][0]["message"]
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning("Unexpected DeepSeek response shape: %s", exc)
        return "❌ Failover engine returned an unexpected response."

    tool_calls = message_node.get("tool_calls") or []
    for call in tool_calls:
        func_name = call.get("function", {}).get("name", "")
        raw_args = call.get("function", {}).get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            args = {}
        logger.info("DeepSeek triggered tool '%s' with args %s", func_name, args)
        # Enforce the Household constraint for reminder tools.
        if func_name in ("add_apple_reminder", "fetch_apple_reminders"):
            args["list_name"] = "Household"
        return tool_registry.execute(func_name, **args)

    return (message_node.get("content") or "").strip()
