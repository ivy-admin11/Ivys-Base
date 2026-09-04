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

SEND_FILE_SCRIPTING_ARGV_SCRIPT = """
on run argv
    set recipientValue to item 1 of argv
    set filePathValue to item 2 of argv
    set theFile to POSIX file filePathValue
    tell application "Messages"
        try
            set targetAccount to 1st account whose service type = iMessage
            set targetParticipant to participant recipientValue of targetAccount
            send theFile to targetParticipant
            return "SUCCESS"
        on error errMsg
            return "ERROR: " & errMsg
        end try
    end tell
end run
"""

# Headless attachment send through Messages' own scripting verb — no
# keystrokes, so it works with the screen locked and the display asleep.
#
# It MUST address the recipient as `participant … of account` (the modern
# Messages scripting model), not the legacy `buddy … of service` used for
# text. Verified 2026-09-01 against chat.db: the buddy/service form returns
# "SUCCESS" and creates no message row at all (the file is silently
# dropped); the participant/account form creates the row with
# transfer_state=5 / is_sent=1 within ~3 s. Callers still verify the outcome
# (ivy_core.attachment_verify) rather than trust the return value.

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


# Reminders access. `list`/`title` arrive as argv, so a name containing a double
# quote is inert data instead of script text. The f-string versions these
# replaced built `list "<name>"` directly into the source, which any inbound
# iMessage could close early (verified 2026-09-02).
#
# Both address the list by variable (`list listNameValue`), which compiles and
# behaves identically to the literal form.

REMINDERS_FETCH_ARGV_SCRIPT = """
on run argv
    set listNameValue to item 1 of argv
    tell application "Reminders"
        try
            set targetList to list listNameValue
            tell targetList
                set remNames to name of every reminder whose completed is false
                set AppleScript's text item delimiters to ", "
                return remNames as text
            end tell
        on error errMsg
            return "ERROR: " & errMsg
        end try
    end tell
end run
"""

REMINDERS_ADD_ARGV_SCRIPT = """
on run argv
    set listNameValue to item 1 of argv
    set titleValue to item 2 of argv
    tell application "Reminders"
        try
            if not (exists list listNameValue) then
                make new list with properties {name:listNameValue}
            end if
            set targetList to list listNameValue
            tell targetList
                make new reminder with properties {name:titleValue}
            end tell
            return "SUCCESS"
        on error err
            return "ERROR: " & err
        end try
    end tell
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
                # "--" terminates osascript's own option parsing. Without it an
                # argument that begins with "-" is read as an option, not as
                # data: a value of "-e" makes the NEXT argument a second script
                # fragment (osascript documents that multiple -e build up one
                # script), and an AppleScript `property` initializer runs at
                # load time — before the run handler — so `do shell script`
                # would execute. Verified 2026-09-03; argv passing alone was
                # not sufficient, it only moved the injection to the command
                # line.
                ["osascript", "-e", script_source, "--", *args],
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

    def send_imessage_file_scripting_argv(self, recipient: str, file_path: str) -> str:
        """Send a file via Messages' scripting verb (no UI automation, safe when
        the screen is locked). Outcome must be verified in chat.db."""
        target = "me" if (recipient or "").lower() == "me" else recipient
        return self.run_argv(SEND_FILE_SCRIPTING_ARGV_SCRIPT, [target, file_path])

    def fetch_reminders_argv(self, list_name: str) -> str:
        """Names of uncompleted reminders in ``list_name``, comma-separated.

        Returns "ERROR: ..." when the list is missing or Reminders refused.
        """
        return self.run_argv(REMINDERS_FETCH_ARGV_SCRIPT, [list_name])

    def add_reminder_argv(self, list_name: str, title: str) -> str:
        """Create ``title`` in ``list_name``, creating the list if absent.

        Returns exactly "SUCCESS", or "ERROR: ...".
        """
        return self.run_argv(REMINDERS_ADD_ARGV_SCRIPT, [list_name, title])

    def send_imessage_file_argv(self, recipient: str, file_path: str) -> str:
        """Send a file attachment by emulating a human paste into the Messages
        compose field (clipboard + Cmd-V + Return via System Events).

        Requires an unlocked, interactive session: with the screen locked the
        keystrokes go to the lock screen and nothing is sent, yet this still
        returns "SUCCESS". Callers must gate on
        ``ivy_core.attachment_verify.screen_is_locked()`` and verify the
        outcome in chat.db.
        """
        target = "me" if (recipient or "").lower() == "me" else recipient
        return self.run_argv(SEND_FILE_ARGV_SCRIPT, [target, file_path])
