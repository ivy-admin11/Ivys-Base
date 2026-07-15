"""Tool package: LangChain-compatible tools plus a runtime registry.

Importing this package wires up the singleton :data:`tool_registry` with every
enabled tool. ``main.py`` and the DeepSeek failover path both execute tools
through this registry rather than reaching into ``globals()``.
"""

from __future__ import annotations

from typing import List

from config import (
    ENABLE_CALENDAR_INTEGRATION,
    ENABLE_GROCERY_STAGING,
    ENABLE_READWISE_INTEGRATION,
    ENABLE_REMINDERS_INTEGRATION,
    ENABLE_SPORTS_PICKS,
)
from utils.applescript import AppleScriptRunner

from .base import BaseIvyTool
from .calendar_tool import CheckCalendarTool
from .grocery_tool import StageGroceriesTool
from .readwise_tool import FetchReadwiseTool
from .registry import ToolRegistry
from .sports_tool import RunSharpPicksTool
from .reminders_tool import AddReminderTool, FetchRemindersTool

# One AppleScript runner shared by every tool that shells out to osascript.
_runner = AppleScriptRunner()


def _build_enabled_tools() -> List[BaseIvyTool]:
    """Instantiate the tool objects whose feature flags are enabled."""
    tools: List[BaseIvyTool] = []
    if ENABLE_CALENDAR_INTEGRATION:
        tools.append(CheckCalendarTool(runner=_runner))
    if ENABLE_REMINDERS_INTEGRATION:
        tools.append(FetchRemindersTool(runner=_runner))
        tools.append(AddReminderTool(runner=_runner))
    if ENABLE_READWISE_INTEGRATION:
        tools.append(FetchReadwiseTool())
    if ENABLE_GROCERY_STAGING:
        tools.append(StageGroceriesTool())
    if ENABLE_SPORTS_PICKS:
        tools.append(RunSharpPicksTool())
    return tools


# Build once at import time.
ENABLED_TOOLS: List[BaseIvyTool] = _build_enabled_tools()

# Singleton registry, populated with the enabled tool objects.
tool_registry = ToolRegistry()
for _tool in ENABLED_TOOLS:
    tool_registry.register(_tool.name, _tool)


def get_enabled_tools() -> List[BaseIvyTool]:
    """Return the list of enabled LangChain tool objects (for the agent)."""
    return list(ENABLED_TOOLS)


__all__ = [
    "BaseIvyTool",
    "ToolRegistry",
    "CheckCalendarTool",
    "FetchRemindersTool",
    "AddReminderTool",
    "FetchReadwiseTool",
    "StageGroceriesTool",
    "RunSharpPicksTool",
    "tool_registry",
    "get_enabled_tools",
    "ENABLED_TOOLS",
]
