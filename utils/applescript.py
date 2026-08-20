"""Bounded, argv-only AppleScript runtime helpers.

All production helpers in this module pass dynamic values as ``osascript``
process arguments consumed by a fixed ``on run argv`` script.  The legacy
string-building helper remains for compatibility, but no convenience method
depends on it.

Each :class:`AppleScriptRunner` serializes its own calls, enforces a finite
timeout, returns the existing string result contract, and records only safe
last-call telemetry.  Raw subprocess errors are intentionally neither logged
nor returned because they can contain message content, contact identifiers,
or local paths.
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Optional, Sequence

from filelock import FileLock, Timeout as FileLockTimeout

logger = logging.getLogger("ivy.applescript")

# Default subprocess timeout (seconds) for a single osascript invocation.
DEFAULT_TIMEOUT_S = 30
OSASCRIPT_EXECUTABLE = "osascript"

ERROR_CATEGORY_TIMEOUT = "timeout"
ERROR_CATEGORY_EXECUTABLE_NOT_FOUND = "executable_not_found"
ERROR_CATEGORY_SUBPROCESS = "subprocess_error"
ERROR_CATEGORY_NONZERO_EXIT = "nonzero_exit"
ERROR_CATEGORY_APPLESCRIPT = "applescript_error"
ERROR_CATEGORY_BUSY = "automation_busy"

_ERROR_TIMEOUT = "ERROR: AppleScript execution timed out."
_ERROR_EXECUTION = "ERROR: AppleScript execution failed."
_ERROR_APPLESCRIPT = "ERROR: AppleScript failed."

_GLOBAL_CALL_LOCK = threading.RLock()
_LOCK_FILE = os.path.join(
    tempfile.gettempdir(),
    f"ivy-applescript-{getattr(os, 'getuid', lambda: 0)()}.lock",
)
_PROCESS_FILE_LOCK = FileLock(_LOCK_FILE)

# Every script below is static. Dynamic values are received only through argv.
CALENDAR_EVENTS_ARGV_SCRIPT = """
on run argv
    set calendarNameValue to item 1 of argv
    set totalEvents to ""
    set midnightToday to (current date)
    set hours of midnightToday to 0
    set minutes of midnightToday to 0
    set seconds of midnightToday to 0
    tell application "Calendar"
        try
            set targetCalendar to first calendar whose name is calendarNameValue
            set upcomingEvents to (every event of targetCalendar whose start date is greater than or equal to midnightToday)
            repeat with calendarEvent in upcomingEvents
                set eventDate to start date of calendarEvent
                set totalEvents to totalEvents & (summary of calendarEvent) & ":::" & (day of eventDate as text) & " " & (month of eventDate as text) & " " & (year of eventDate as text) & " at " & (time string of eventDate) & linefeed
            end repeat
            return totalEvents
        on error
            return "ERROR: APPLESCRIPT_FAILURE"
        end try
    end tell
end run
"""

FETCH_REMINDERS_ARGV_SCRIPT = """
on run argv
    set listNameValue to item 1 of argv
    tell application "Reminders"
        try
            set targetList to first list whose name is listNameValue
            set reminderNames to name of every reminder of targetList whose completed is false
            set previousDelimiters to AppleScript's text item delimiters
            set AppleScript's text item delimiters to ", "
            set renderedNames to reminderNames as text
            set AppleScript's text item delimiters to previousDelimiters
            return renderedNames
        on error
            return "ERROR: APPLESCRIPT_FAILURE"
        end try
    end tell
end run
"""

ADD_REMINDER_ARGV_SCRIPT = """
on run argv
    set listNameValue to item 1 of argv
    set reminderTitleValue to item 2 of argv
    tell application "Reminders"
        try
            set matchingLists to every list whose name is listNameValue
            if (count of matchingLists) is 0 then
                set targetList to make new list with properties {name:listNameValue}
            else
                set targetList to item 1 of matchingLists
            end if
            tell targetList
                make new reminder with properties {name:reminderTitleValue}
            end tell
            return "SUCCESS"
        on error
            return "ERROR: APPLESCRIPT_FAILURE"
        end try
    end tell
end run
"""

SEND_TEXT_ARGV_SCRIPT = """
on run argv
    set recipientValue to item 1 of argv
    set messageValue to item 2 of argv
    tell application "Messages"
        try
            set targetService to first service whose service type is iMessage
            set targetBuddy to buddy recipientValue of targetService
            send messageValue to targetBuddy
            return "SUCCESS"
        on error
            return "ERROR: APPLESCRIPT_FAILURE"
        end try
    end tell
end run
"""

SEND_FILE_ARGV_SCRIPT = """
on run argv
    set recipientValue to item 1 of argv
    set filePathValue to item 2 of argv
    try
        -- Messages' scripting `send (POSIX file ...) to buddy` verb creates a
        -- local message/attachment record but unreliably triggers the actual
        -- upload when driven headlessly. Emulate a human paste instead.
        set the clipboard to (POSIX file filePathValue)
        tell application "Messages" to activate
        open location "imessage:" & recipientValue
        delay 1.5
        tell application "System Events"
            keystroke "v" using command down
            delay 1.5
            key code 36
        end tell
        return "SUCCESS"
    on error
        return "ERROR: APPLESCRIPT_FAILURE"
    end try
end run
"""


@dataclass(frozen=True)
class AppleScriptTelemetry:
    """Safe metadata for the most recently completed call."""

    duration_seconds: float = 0.0
    error_category: Optional[str] = None


def escape_applescript_string(value: str) -> str:
    """Escape a value for the retained legacy source-building API."""
    if value is None:
        return ""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _string_arg(value: object) -> str:
    """Return a subprocess-safe string without interpreting its contents."""
    return "" if value is None else str(value)


def _validated_timeout(value: float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("AppleScript timeout must be a finite positive number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("AppleScript timeout must be a finite positive number")
    return timeout


class AppleScriptRunner:
    """Run AppleScript calls serially with bounded, sanitized failure handling."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._call_lock = threading.RLock()
        self._timeout = _validated_timeout(timeout)
        self._last_telemetry = AppleScriptTelemetry()

    @property
    def timeout(self) -> float:
        with self._call_lock:
            return self._timeout

    @timeout.setter
    def timeout(self, value: float) -> None:
        validated = _validated_timeout(value)
        with self._call_lock:
            self._timeout = validated

    @property
    def last_telemetry(self) -> AppleScriptTelemetry:
        with self._call_lock:
            return self._last_telemetry

    @property
    def last_error_category(self) -> Optional[str]:
        return self.last_telemetry.error_category

    @property
    def last_duration_seconds(self) -> float:
        return self.last_telemetry.duration_seconds

    def _finish(self, started_at: float, error_category: Optional[str]) -> AppleScriptTelemetry:
        telemetry = AppleScriptTelemetry(
            duration_seconds=max(0.0, time.monotonic() - started_at),
            error_category=error_category,
        )
        self._last_telemetry = telemetry
        return telemetry

    def _execute(self, command: list[str]) -> str:
        """Execute one already-constructed command while holding the runner lock."""
        with _GLOBAL_CALL_LOCK, self._call_lock:
            started_at = time.monotonic()
            try:
                with _PROCESS_FILE_LOCK.acquire(timeout=self._timeout):
                    result = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        timeout=self._timeout,
                        check=False,
                        shell=False,
                    )
            except FileLockTimeout:
                telemetry = self._finish(started_at, ERROR_CATEGORY_BUSY)
                logger.error(
                    "AppleScript call failed category=%s duration_seconds=%.3f",
                    telemetry.error_category,
                    telemetry.duration_seconds,
                )
                return _ERROR_EXECUTION
            except subprocess.TimeoutExpired:
                telemetry = self._finish(started_at, ERROR_CATEGORY_TIMEOUT)
                logger.error(
                    "AppleScript call failed category=%s duration_seconds=%.3f",
                    telemetry.error_category,
                    telemetry.duration_seconds,
                )
                return _ERROR_TIMEOUT
            except FileNotFoundError:
                telemetry = self._finish(started_at, ERROR_CATEGORY_EXECUTABLE_NOT_FOUND)
                logger.error(
                    "AppleScript call failed category=%s duration_seconds=%.3f",
                    telemetry.error_category,
                    telemetry.duration_seconds,
                )
                return _ERROR_EXECUTION
            except Exception as exc:  # pragma: no cover - defensive catch is directly unit-tested
                telemetry = self._finish(started_at, ERROR_CATEGORY_SUBPROCESS)
                logger.error(
                    "AppleScript call failed category=%s exception_type=%s duration_seconds=%.3f",
                    telemetry.error_category,
                    type(exc).__name__,
                    telemetry.duration_seconds,
                )
                return _ERROR_EXECUTION

            stdout = (result.stdout or "").strip()
            if result.returncode != 0:
                telemetry = self._finish(started_at, ERROR_CATEGORY_NONZERO_EXIT)
                logger.warning(
                    "AppleScript call failed category=%s returncode=%s duration_seconds=%.3f",
                    telemetry.error_category,
                    result.returncode,
                    telemetry.duration_seconds,
                )
                return _ERROR_APPLESCRIPT

            # Fixed scripts return sentinel error strings for handled failures.
            # Never return or log the raw stdout body; it may contain sensitive
            # values returned by script-level errors.
            if stdout.upper().startswith("ERROR:"):
                telemetry = self._finish(started_at, ERROR_CATEGORY_APPLESCRIPT)
                logger.warning(
                    "AppleScript call failed category=%s duration_seconds=%.3f",
                    telemetry.error_category,
                    telemetry.duration_seconds,
                )
                return _ERROR_APPLESCRIPT

            self._finish(started_at, None)
            return stdout

    def run(self, script: str) -> str:
        """Execute legacy AppleScript source while preserving the string contract."""
        return self._execute([OSASCRIPT_EXECUTABLE, "-e", script])

    def run_argv(self, script_source: str, args: Sequence[str]) -> str:
        """Execute a fixed ``on run argv`` script with uninterpreted arguments."""
        command = [
            OSASCRIPT_EXECUTABLE,
            "-e",
            script_source,
            *[_string_arg(arg) for arg in args],
        ]
        return self._execute(command)

    def fetch_calendar_events(self, calendar_name: str) -> str:
        """Return upcoming events from ``calendar_name`` using a fixed argv script."""
        return self.run_argv(CALENDAR_EVENTS_ARGV_SCRIPT, [_string_arg(calendar_name)])

    def fetch_reminders(self, list_name: str) -> str:
        """Return incomplete reminder names from ``list_name`` using argv."""
        return self.run_argv(FETCH_REMINDERS_ARGV_SCRIPT, [_string_arg(list_name)])

    def add_reminder(self, list_name: str, title: str) -> str:
        """Add ``title`` to ``list_name`` using argv for both dynamic values."""
        return self.run_argv(
            ADD_REMINDER_ARGV_SCRIPT,
            [_string_arg(list_name), _string_arg(title)],
        )

    def build_imessage_send_script(self, recipient: str, body: str) -> str:
        """Build escaped source for legacy callers; new code should use argv methods."""
        recipient_value = _string_arg(recipient)
        target = "me" if recipient_value.lower() == "me" else recipient_value
        safe_recipient = escape_applescript_string(target)
        safe_body = escape_applescript_string(_string_arg(body))
        return "\n".join(
            [
                'tell application "Messages"',
                "    try",
                "        set targetService to first service whose service type is iMessage",
                f'        set targetBuddy to buddy "{safe_recipient}" of targetService',
                f'        send "{safe_body}" to targetBuddy',
                '        return "SUCCESS"',
                "    on error",
                '        return "ERROR: APPLESCRIPT_FAILURE"',
                "    end try",
                "end tell",
            ]
        )

    def send_imessage(self, recipient: str, body: str) -> str:
        """Compatibility convenience method, now routed through the argv path."""
        return self.send_imessage_argv(recipient, body)

    def send_imessage_argv(self, recipient: str, body: str) -> str:
        """Send text with recipient and body passed as process argv."""
        recipient_value = _string_arg(recipient)
        target = "me" if recipient_value.lower() == "me" else recipient_value
        return self.run_argv(SEND_TEXT_ARGV_SCRIPT, [target, _string_arg(body)])

    def send_imessage_file_argv(self, recipient: str, file_path: str) -> str:
        """Send a file with recipient and path passed as process argv."""
        recipient_value = _string_arg(recipient)
        target = "me" if recipient_value.lower() == "me" else recipient_value
        return self.run_argv(SEND_FILE_ARGV_SCRIPT, [target, _string_arg(file_path)])
