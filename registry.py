"""Canonical tool specifications — single source of truth for both the
Gemini and DeepSeek function-calling schemas.

Replaces the two hand-synced lists that used to live in config.py
(GEMINI_TOOL_DECLARATIONS / DEEPSEEK_TOOL_SCHEMA), which had already drifted:
DeepSeek's list was missing fetch_apple_reminders entirely, so DeepSeek could
never satisfy a "read my reminders" request Gemini could handle.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple


@dataclass(frozen=True)
class ToolParam:
    name: str
    type: str  # JSON-schema primitive type: "string", "number", etc.
    description: str
    required: bool = False


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params: Tuple[ToolParam, ...] = ()


TOOL_SPECS: Tuple[ToolSpec, ...] = (
    ToolSpec(
        name="check_apple_calendar",
        description="Scans the local Mac iCloud 'Hilla' Calendar for upcoming events.",
        params=(
            ToolParam("timeframe", "string", "today, tomorrow, or all", required=True),
        ),
    ),
    ToolSpec(
        name="fetch_readwise_highlights",
        description="Connects to the Readwise REST API to retrieve saved articles and highlights.",
    ),
    ToolSpec(
        name="fetch_apple_reminders",
        description=(
            "Reads all uncompleted tasks and grocery entries currently listed "
            "inside a specific Mac Reminders list."
        ),
        params=(
            ToolParam(
                "list_name", "string",
                "The name of the list to read (e.g., 'Household')",
                required=True,
            ),
        ),
    ),
    ToolSpec(
        name="add_apple_reminder",
        description="Adds a new task item or grocery entry into specific Apple Reminders lists.",
        params=(
            ToolParam(
                "title", "string",
                "The description of the reminder task or ingredient with measurements",
                required=True,
            ),
            ToolParam("list_name", "string", "Must strictly be 'Household'", required=False),
        ),
    ),
    ToolSpec(
        name="run_job",
        description=(
            "Execute a background job on-demand. Available jobs: sharp_picks (daily picks), "
            "happy_hour (scout nearby happy hours), bravo_scout (reality TV monitor), "
            "weekly_planner (meal plan generator), brain (knowledge queries)."
        ),
        params=(
            ToolParam(
                "job_name", "string",
                "Job to run: 'sharp_picks', 'happy_hour', 'bravo_scout', 'weekly_planner', "
                "'brain', or natural language like 'picks', 'meals', 'scout'",
                required=True,
            ),
        ),
    ),
)


def to_gemini_declarations(specs: Tuple[ToolSpec, ...] = TOOL_SPECS) -> List[Dict[str, Any]]:
    """Render canonical specs into Gemini's function_declarations schema shape."""
    declarations = []
    for spec in specs:
        entry: Dict[str, Any] = {"name": spec.name, "description": spec.description}
        if spec.params:
            entry["parameters"] = {
                "type": "OBJECT",
                "properties": {
                    p.name: {"type": p.type.upper(), "description": p.description}
                    for p in spec.params
                },
                "required": [p.name for p in spec.params if p.required],
            }
        declarations.append(entry)
    return declarations


def to_deepseek_schema(specs: Tuple[ToolSpec, ...] = TOOL_SPECS) -> List[Dict[str, Any]]:
    """Render canonical specs into DeepSeek's OpenAI-compatible tools schema shape."""
    schema = []
    for spec in specs:
        function: Dict[str, Any] = {"name": spec.name, "description": spec.description}
        if spec.params:
            function["parameters"] = {
                "type": "object",
                "properties": {
                    p.name: {"type": p.type, "description": p.description}
                    for p in spec.params
                },
                "required": [p.name for p in spec.params if p.required],
            }
        schema.append({"type": "function", "function": function})
    return schema


GEMINI_TOOL_DECLARATIONS: List[Dict[str, Any]] = to_gemini_declarations()
DEEPSEEK_TOOL_SCHEMA: List[Dict[str, Any]] = to_deepseek_schema()
