"""Runtime tool registry.

Holds a name -> tool mapping and executes tools uniformly. A registered value
may be either a plain callable *or* a LangChain ``BaseTool`` instance; the
registry dispatches to the right calling convention. This replaces the previous
``globals()[tool_name](**args)`` pattern, which allowed any module-level name to
be invoked by the LLM.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Union

logger = logging.getLogger("ivy.tools.registry")

ToolLike = Union[Callable[..., Any], Any]


class ToolRegistry:
    """A dictionary of executable tools keyed by name."""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolLike] = {}

    def register(self, name: str, fn: ToolLike) -> None:
        """Register a tool under ``name`` (overwrites any existing entry)."""
        if not name:
            raise ValueError("Tool name must be a non-empty string.")
        if name in self._tools:
            logger.warning("Overwriting already-registered tool '%s'.", name)
        self._tools[name] = fn

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> ToolLike:
        return self._tools[name]

    def execute(self, name: str, **kwargs: Any) -> str:
        """Execute the named tool with keyword args, returning a string result.

        Never raises for tool-level failures: errors are logged and returned as
        a user-safe ``Error: ...`` string so a single bad tool call cannot crash
        the worker loop.
        """
        tool = self._tools.get(name)
        if tool is None:
            logger.warning("Requested unknown tool '%s'.", name)
            return f"Error: Tool '{name}' is not registered."

        try:
            # LangChain BaseTool instances expose .run() and a .name attribute.
            if hasattr(tool, "run") and hasattr(tool, "name") and not _is_plain_function(tool):
                result = tool.run(kwargs)
            elif callable(tool):
                result = tool(**kwargs)
            else:  # pragma: no cover - defensive
                return f"Error: Tool '{name}' is not executable."
            return "" if result is None else str(result)
        except Exception as exc:
            logger.exception("Tool '%s' raised during execution: %s", name, exc)
            return f"Error: Tool '{name}' failed to execute."


def _is_plain_function(obj: Any) -> bool:
    """Heuristic: True for ordinary functions/lambdas/builtins, not tool objects."""
    import types

    return isinstance(
        obj,
        (types.FunctionType, types.BuiltinFunctionType, types.LambdaType, types.MethodType),
    )
