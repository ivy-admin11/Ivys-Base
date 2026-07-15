"""LangChain tool-calling agent with Gemini -> DeepSeek failover.

``create_agent(provider)`` builds an ``AgentExecutor`` backed by the requested
LLM. ``run_with_failover(text)`` preserves the mandated dual-brain behavior:
try the Gemini-backed agent first, fall back to the DeepSeek-backed agent on any
failure.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Optional

from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from config import AGENT_MAX_ITERATIONS, AGENT_SYSTEM_INSTRUCTION
from tools import get_enabled_tools
from utils.exceptions import ExternalServiceError

logger = logging.getLogger("ivy.agent")

# Cache built executors by provider so we don't rebuild per message.
_EXECUTOR_CACHE: Dict[str, AgentExecutor] = {}


def _build_llm(provider: str):
    """Instantiate the chat model for the given provider."""
    if provider == "gemini":
        if not os.environ.get("GEMINI_API_KEY", "").strip():
            raise ExternalServiceError("Gemini is not configured.")
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError as exc:  # pragma: no cover
            raise ExternalServiceError("langchain-google-genai is not installed.") from exc
        return ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=os.environ["GEMINI_API_KEY"],
            temperature=0.1,
        )

    if provider == "deepseek":
        if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
            raise ExternalServiceError("DeepSeek is not configured.")
        try:
            from langchain_deepseek import ChatDeepSeek
        except ImportError as exc:  # pragma: no cover
            raise ExternalServiceError("langchain-deepseek is not installed.") from exc
        return ChatDeepSeek(
            model="deepseek-chat",
            api_key=os.environ["DEEPSEEK_API_KEY"],
            temperature=0.1,
        )

    raise ExternalServiceError(f"Unknown LLM provider '{provider}'.")


def create_agent(provider: str = "gemini") -> AgentExecutor:
    """Create (or return cached) AgentExecutor for ``provider``."""
    if provider in _EXECUTOR_CACHE:
        return _EXECUTOR_CACHE[provider]

    llm = _build_llm(provider)
    tools = get_enabled_tools()

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", AGENT_SYSTEM_INSTRUCTION),
            ("human", "{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ]
    )

    agent = create_tool_calling_agent(llm, tools, prompt)
    executor = AgentExecutor(
        agent=agent,
        tools=tools,
        max_iterations=AGENT_MAX_ITERATIONS,
        verbose=True,
        handle_parsing_errors=True,
    )
    _EXECUTOR_CACHE[provider] = executor
    return executor


def _run(provider: str, text: str) -> Optional[str]:
    """Run one agent invocation; return its output text or ``None`` on failure."""
    try:
        executor = create_agent(provider)
        result = executor.invoke({"input": text})
        output = (result or {}).get("output")
        return output.strip() if isinstance(output, str) and output.strip() else None
    except Exception as exc:
        logger.warning("Agent provider '%s' failed: %s", provider, exc)
        return None


def run_with_failover(text: str) -> Optional[str]:
    """Gemini-backed agent first, DeepSeek-backed agent as failover."""
    logger.info("🧠 Running Gemini-backed agent...")
    reply = _run("gemini", text)
    if reply:
        return reply

    logger.info("🛡️ Gemini agent unavailable — engaging DeepSeek failover agent...")
    return _run("deepseek", text)
