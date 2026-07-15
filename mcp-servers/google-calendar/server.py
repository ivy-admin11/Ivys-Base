"""Local Google Calendar MCP server.

Tools:
    - view_schedule(range="day", date=None, calendar_id="primary")
    - create_event(summary, start_time, end_time, description="",
                   calendar_id="primary", timezone="America/Los_Angeles")

Security model:
    - Authenticates with the repo's service-account-key.json (no user OAuth).
    - Scope is locked to https://www.googleapis.com/auth/calendar ONLY.
      No Gmail, Drive, or financial APIs are imported or requested.
    - The target calendar must be explicitly shared with the service account
      client_email; the default "primary" only works when a calendar with
      that alias has been shared with the service account.
"""

from __future__ import annotations

import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_ACCOUNT_PATH = REPO_ROOT / "service-account-key.json"
CALENDAR_SCOPES = ("https://www.googleapis.com/auth/calendar",)

mcp = FastMCP("google-calendar")


def _check_service_account() -> None:
    """Fail loud at startup if the key file is missing or malformed."""
    if not SERVICE_ACCOUNT_PATH.exists():
        print(
            f"service-account-key.json not found at {SERVICE_ACCOUNT_PATH}.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        service_account.Credentials.from_service_account_file(
            str(SERVICE_ACCOUNT_PATH), scopes=list(CALENDAR_SCOPES)
        )
    except (ValueError, OSError) as exc:
        print(f"Failed to load service account credentials: {exc}", file=sys.stderr)
        sys.exit(1)


def _calendar_service():
    creds = service_account.Credentials.from_service_account_file(
        str(SERVICE_ACCOUNT_PATH), scopes=list(CALENDAR_SCOPES)
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _parse_iso(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty ISO 8601 string.")
    raw = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(
            f"{field} is not valid ISO 8601 (e.g. '2026-05-28T15:00:00-07:00'): {exc}"
        ) from None


def _parse_date(value: str | None) -> datetime:
    if value is None or not str(value).strip():
        return datetime.now().astimezone()
    return _parse_iso(value, "date")


@mcp.tool()
def view_schedule(
    range: str = "day",
    date: str | None = None,
    calendar_id: str = "primary",
) -> list[dict]:
    """Return calendar events for a day or week.

    Args:
        range: "day" (default) or "week".
        date: ISO 8601 date or datetime anchoring the window. Defaults to now.
              For "day", the window is that local calendar day.
              For "week", the window is the 7 days starting at that instant.
        calendar_id: Calendar to read. The calendar must be shared with the
                     service account client_email. Defaults to "primary".
    """
    mode = (range or "day").strip().lower()
    if mode not in {"day", "week"}:
        raise ValueError("range must be 'day' or 'week'.")

    anchor = _parse_date(date)
    if anchor.tzinfo is None:
        anchor = anchor.astimezone()

    if mode == "day":
        start = datetime.combine(anchor.date(), time.min, tzinfo=anchor.tzinfo)
        end = start + timedelta(days=1)
    else:
        start = anchor
        end = anchor + timedelta(days=7)

    try:
        events = (
            _calendar_service()
            .events()
            .list(
                calendarId=calendar_id,
                timeMin=start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                timeMax=end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
            )
            .execute()
        )
    except HttpError as exc:
        raise RuntimeError(f"Calendar API error: {exc}") from None

    results = []
    for item in events.get("items", []):
        results.append(
            {
                "id": item.get("id"),
                "summary": item.get("summary", ""),
                "description": item.get("description", ""),
                "start": (item.get("start") or {}).get("dateTime")
                or (item.get("start") or {}).get("date"),
                "end": (item.get("end") or {}).get("dateTime")
                or (item.get("end") or {}).get("date"),
                "location": item.get("location", ""),
                "htmlLink": item.get("htmlLink", ""),
            }
        )
    return results


@mcp.tool()
def create_event(
    summary: str,
    start_time: str,
    end_time: str,
    description: str = "",
    calendar_id: str = "primary",
    timezone: str = "America/Los_Angeles",
) -> dict:
    """Create a calendar event (card drops, show times, auctions, etc.).

    Args:
        summary: Event title.
        start_time: ISO 8601 datetime. If no offset, `timezone` is applied.
        end_time: ISO 8601 datetime. If no offset, `timezone` is applied.
        description: Optional event notes.
        calendar_id: Target calendar; must be shared with the service account.
        timezone: IANA tz name used when start/end lack an offset.
    """
    if not summary or not summary.strip():
        raise ValueError("summary must be non-empty.")
    start_dt = _parse_iso(start_time, "start_time")
    end_dt = _parse_iso(end_time, "end_time")
    if end_dt <= start_dt:
        raise ValueError("end_time must be after start_time.")

    body = {
        "summary": summary.strip(),
        "description": description or "",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": timezone},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": timezone},
    }

    try:
        event = (
            _calendar_service()
            .events()
            .insert(calendarId=calendar_id, body=body)
            .execute()
        )
    except HttpError as exc:
        raise RuntimeError(f"Calendar API error: {exc}") from None

    return {
        "id": event.get("id"),
        "summary": event.get("summary"),
        "start": (event.get("start") or {}).get("dateTime"),
        "end": (event.get("end") or {}).get("dateTime"),
        "htmlLink": event.get("htmlLink"),
        "status": event.get("status"),
    }


if __name__ == "__main__":
    _check_service_account()
    mcp.run()
