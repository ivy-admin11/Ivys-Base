"""Apple Calendar tool: scans the local 'Hilla' calendar for events."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Type

from pydantic import BaseModel, Field

from utils.applescript import AppleScriptRunner
from utils.text import optimize_token_payload

from .base import BaseIvyTool

logger = logging.getLogger("ivy.tools.calendar")

_MONTHS_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

_CALENDAR_SCRIPT = "\n".join(
    [
        'set totalEvents to ""',
        "set midnightToday to (current date)",
        "set hours of midnightToday to 0",
        "set minutes of midnightToday to 0",
        "set seconds of midnightToday to 0",
        'tell application "Calendar"',
        "    try",
        '        set familyCal to calendar "Hilla"',
        "        set upcomingEvents to (every event of familyCal whose start date is greater than or equal to midnightToday)",
        "        repeat with e in upcomingEvents",
        "            set d to start date of e",
        '            set totalEvents to totalEvents & (summary of e) & ":::" & (day of d as text) & " " & (month of d as text) & " " & (year of d as text) & " at " & (time string of d) & "\\n"',
        "        end repeat",
        "    on error err",
        '        return "Error: " & err',
        "    end try",
        "end tell",
        "return totalEvents",
    ]
)


def check_apple_calendar(timeframe: str, runner: AppleScriptRunner) -> str:
    """Scan the local Mac 'Hilla' calendar for upcoming events."""
    raw_output = runner.run(_CALENDAR_SCRIPT)

    if raw_output.startswith("ERROR:") or "Error:" in raw_output:
        logger.warning("Calendar AppleScript error: %s", raw_output)
        return "❌ Could not read the Hilla calendar right now."
    if not raw_output:
        return "Your Hilla calendar has no upcoming events listed."

    now = datetime.now()
    parsed_events = []
    for line in raw_output.split("\n"):
        if ":::" not in line:
            continue
        summary, date_string = line.split(":::", 1)
        parts = date_string.split()
        if len(parts) < 3:
            continue
        try:
            ev_day = int(parts[0])
            ev_month = _MONTHS_MAP.get(parts[1].lower(), 1)
            ev_year = int(parts[2])
            ev_time = " ".join(parts[4:]) if "at" in parts else parts[3]
            event_dt = datetime(ev_year, ev_month, ev_day)
            parsed_events.append(
                {
                    "date": event_dt,
                    "display": f"- {event_dt.strftime('%A, %b %d')}: {summary} ({ev_time})",
                }
            )
        except Exception:
            continue

    parsed_events.sort(key=lambda x: x["date"])

    if timeframe.lower() in ["all", "full", "everything"]:
        formatted_list = "Complete Upcoming Schedule:\n" + "\n".join(
            e["display"] for e in parsed_events
        )
        return optimize_token_payload(formatted_list, max_chars=3500)

    target = now + timedelta(days=1) if timeframe.lower() == "tomorrow" else now
    day_matches = [
        e["display"] for e in parsed_events if e["date"].date() == target.date()
    ]
    if day_matches:
        return f"Schedule for {timeframe}:\n" + "\n".join(day_matches)

    next_up = parsed_events[0]["display"] if parsed_events else "None listed"
    return (
        f"Your Hilla calendar is clear for {timeframe}. "
        f"Next upcoming agenda item:\n{next_up}"
    )


class CalendarArgs(BaseModel):
    timeframe: str = Field(
        default="today",
        description="One of: today, tomorrow, or all/full/everything.",
    )


class CheckCalendarTool(BaseIvyTool):
    name: str = "check_apple_calendar"
    description: str = (
        "Scans the local Mac iCloud 'Hilla' calendar for upcoming events. "
        "Use when the user asks about their schedule, calendar, or agenda."
    )
    args_schema: Type[BaseModel] = CalendarArgs
    runner: AppleScriptRunner

    def _run(self, timeframe: str = "today", **_: object) -> str:
        return check_apple_calendar(timeframe=timeframe, runner=self.runner)
