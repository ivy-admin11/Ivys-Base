"""AppleScript execution helper.

Centralizes all ``osascript`` subprocess invocation so that timeouts, error
handling and — critically — string escaping live in one place. Building
AppleScript by naive f-string interpolation is an injection vector: a message
body containing a double quote (or a backslash) can terminate the string
literal and inject arbitrary AppleScript. :meth:`AppleScriptRunner.build_imessage_send_script`
escapes untrusted input before embedding it.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Optional

logger = logging.getLogger("ivy.applescript")

# Default subprocess timeout (seconds) for a single osascript invocation.
DEFAULT_TIMEOUT_S = 30


def escape_applescript_string(value: str) -> str:
    """Escape a Python string for safe embedding inside an AppleScript string literal.

    AppleScript string literals are delimited by double quotes. We must escape
    backslashes first (so we do not double-escape the escapes we add next) and
    then double quotes.
    """
    if value is None:
        return ""
    return value.replace("\\", "\\\\").replace('"', '\\"')


class AppleScriptRunner:
    """Runs AppleScript via ``osascript`` with a timeout and uniform errors."""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT_S) -> None:
        self.timeout = timeout

    def run(self, script: str) -> str:
        """Execute an AppleScript source string and return trimmed stdout.

        On timeout or subprocess failure a sanitized ``ERROR: ...`` string is
        returned (never raised) so callers can degrade gracefully. Internal
        details are logged, not returned.
        """
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            logger.error("AppleScript timed out after %ss", self.timeout)
            return "ERROR: AppleScript execution timed out."
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("AppleScript subprocess failed: %s", exc)
            return "ERROR: AppleScript execution failed."

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        if result.returncode != 0:
            # osascript writes the human-readable error to stderr.
            logger.warning(
                "AppleScript returned code %s: %s", result.returncode, stderr
            )
            return f"ERROR: {stderr}" if stderr else "ERROR: AppleScript failed."
        return stdout

    def build_imessage_send_script(self, recipient: str, body: str) -> str:
        """Build an AppleScript that sends ``body`` to ``recipient`` over iMessage.

        Both ``recipient`` and ``body`` are escaped to prevent AppleScript
        injection. ``"me"`` (case-insensitive) is treated as the local user.
        """
        target = "me" if (recipient or "").lower() == "me" else recipient
        safe_recipient = escape_applescript_string(target)
        safe_body = escape_applescript_string(body)
        return "\n".join(
            [
                'tell application "Messages"',
                "    try",
                "        set targetService to first service whose service type is iMessage",
                f'        set targetBuddy to buddy "{safe_recipient}" of targetService',
                f'        send "{safe_body}" to targetBuddy',
                '        return "SUCCESS"',
                "    on error errMsg",
                '        return "ERROR: " & errMsg',
                "    end try",
                "end tell",
            ]
        )

    def send_imessage(self, recipient: str, body: str) -> str:
        """Convenience: build and run an outbound iMessage send."""
        return self.run(self.build_imessage_send_script(recipient, body))
