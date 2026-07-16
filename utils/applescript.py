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
from typing import List

logger = logging.getLogger("ivy.applescript")

# Default subprocess timeout (seconds) for a single osascript invocation.
DEFAULT_TIMEOUT_S = 30

# `on run argv` scripts for iMessage send — untrusted content (recipient,
# message body, attachment path) is passed as process argv, never
# interpolated into AppleScript source text, so no escaping function can be
# bypassed by a crafted input.
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
        on error errMsg
            return "ERROR: " & errMsg
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
        -- upload when driven headlessly — the receiving side ends up with an
        -- unopenable placeholder. Emulate an actual human paste instead: put
        -- the file on the clipboard the same way Finder's Cmd+C does, deep-link
        -- to the specific conversation's compose field via the imessage: URL
        -- scheme (focuses it reliably, unlike GUI sidebar navigation), paste,
        -- then send — this goes through the same code path a real attach does.
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
    on error errMsg
        return "ERROR: " & errMsg
    end try
end run
"""


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

    def run_argv(self, script_source: str, args: List[str]) -> str:
        """Execute an ``on run argv`` AppleScript with ``args`` passed as process argv.

        Unlike :meth:`run`, the caller's content is never embedded in the
        AppleScript source string — it's passed as ``osascript`` process
        arguments, which ``on run argv`` receives as ``item N of argv``. This
        is immune to AppleScript string-literal injection regardless of what
        characters ``args`` contains.
        """
        try:
            result = subprocess.run(
                ["osascript", "-e", script_source, *args],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            logger.error("AppleScript (argv) timed out after %ss", self.timeout)
            return "ERROR: AppleScript execution timed out."
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("AppleScript (argv) subprocess failed: %s", exc)
            return "ERROR: AppleScript execution failed."

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        if result.returncode != 0:
            logger.warning(
                "AppleScript (argv) returned code %s: %s", result.returncode, stderr
            )
            return f"ERROR: {stderr}" if stderr else "ERROR: AppleScript failed."
        return stdout

    def send_imessage_argv(self, recipient: str, body: str) -> str:
        """Send an iMessage with recipient/body passed as argv, not interpolated source."""
        target = "me" if (recipient or "").lower() == "me" else recipient
        return self.run_argv(SEND_TEXT_ARGV_SCRIPT, [target, body])

    def send_imessage_file_argv(self, recipient: str, file_path: str) -> str:
        """Send a file attachment with recipient/path passed as argv, not interpolated source."""
        target = "me" if (recipient or "").lower() == "me" else recipient
        return self.run_argv(SEND_FILE_ARGV_SCRIPT, [target, file_path])
