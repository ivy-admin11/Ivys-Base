"""Apple Reminders tools: read uncompleted reminders and add new ones."""

from __future__ import annotations

import logging
from typing import Type

from pydantic import BaseModel, Field

from utils.applescript import AppleScriptRunner, escape_applescript_string

from .base import BaseIvyTool

logger = logging.getLogger("ivy.tools.reminders")


def fetch_apple_reminders(list_name: str, runner: AppleScriptRunner) -> str:
    """Read uncompleted reminder names from an Apple Reminders list."""
    safe_list = escape_applescript_string(list_name)
    script = "\n".join(
        [
            'tell application "Reminders"',
            "    try",
            f'        tell list "{safe_list}"',
            "            set remNames to name of every reminder whose completed is false",
            '            set AppleScript\'s text item delimiters to ", "',
            "            return remNames as text",
            "        end tell",
            "    on error errMsg",
            '        return "ERROR: " & errMsg',
            "    end try",
            "end tell",
        ]
    )
    output = runner.run(script)
    if output.startswith("ERROR:"):
        logger.warning("fetch_apple_reminders error: %s", output)
        return f"No reminders could be read from '{list_name}'."
    return output if output else "No active reminders found."


def add_apple_reminder(title: str, list_name: str, runner: AppleScriptRunner) -> str:
    """Add a task to an Apple Reminders list, auto-categorizing by keyword."""
    resolved = list_name
    lowered = list_name.lower()
    if any(w in lowered for w in ["meal", "food", "dinner", "recipe", "taco"]):
        resolved = "Meal Plan"
    elif any(w in lowered for w in ["house", "chore", "clean", "task"]):
        resolved = "Household"

    safe_list = escape_applescript_string(resolved)
    safe_title = escape_applescript_string(title)
    script = "\n".join(
        [
            'tell application "Reminders"',
            "    try",
            f'        if not (exists list "{safe_list}") then',
            f'            make new list with properties {{name:"{safe_list}"}}',
            "        end if",
            f'        set targetList to list "{safe_list}"',
            "        tell targetList",
            f'            make new reminder with properties {{name:"{safe_title}"}}',
            "        end tell",
            '        return "SUCCESS"',
            "    on error err",
            '        return "ERROR: " & err',
            "    end try",
            "end tell",
        ]
    )
    output = runner.run(script)
    if "SUCCESS" in output:
        return f"✅ Added to your '{resolved}' list: {title}"
    logger.warning("add_apple_reminder error: %s", output)
    return f"❌ Could not add '{title}' to your reminders."


class FetchRemindersArgs(BaseModel):
    list_name: str = Field(
        default="Household",
        description="Name of the Reminders list to read (e.g. 'Household').",
    )


class AddReminderArgs(BaseModel):
    title: str = Field(
        description=(
            "The reminder/task text. For ingredients, include exact measurements, "
            "e.g. 'Flank steak (1.5 lbs)'."
        )
    )
    list_name: str = Field(
        default="Household",
        description="Target list: 'Household' or 'Meal Plan'.",
    )


class FetchRemindersTool(BaseIvyTool):
    name: str = "fetch_apple_reminders"
    description: str = (
        "Reads uncompleted tasks/groceries from a Mac Reminders list. Call this "
        "first when the user asks about what is already on their list."
    )
    args_schema: Type[BaseModel] = FetchRemindersArgs
    runner: AppleScriptRunner

    def _run(self, list_name: str = "Household", **_: object) -> str:
        return fetch_apple_reminders(list_name=list_name, runner=self.runner)


class AddReminderTool(BaseIvyTool):
    name: str = "add_apple_reminder"
    description: str = (
        "Adds a task or grocery entry into an Apple Reminders list. Include exact "
        "measurements in the title for ingredients."
    )
    args_schema: Type[BaseModel] = AddReminderArgs
    runner: AppleScriptRunner

    def _run(self, title: str, list_name: str = "Household", **_: object) -> str:
        return add_apple_reminder(title=title, list_name=list_name, runner=self.runner)
