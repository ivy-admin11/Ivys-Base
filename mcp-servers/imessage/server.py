"""Local iMessage MCP server with a Human-in-the-Loop whitelist gate.

Tools:
    - get_recent_messages(number, limit=20)
    - send_imessage(number, text)

Security model:
    - chat.db is opened read-only via sqlite3 URI (mode=ro).
    - Every tool call re-reads the favorites allowlist and rejects any number
      not present.
    - AppleScript receives the number/text as argv items, not interpolated, so
      message bodies cannot break out into the script.
    - On startup the server verifies the terminal has Full Disk Access; without
      it, reads of chat.db would silently return empty data, so we fail loud.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

BASE_DIR = Path(__file__).resolve().parent
FAVORITES_PATH = Path(os.path.expanduser(os.environ.get("IVY_FAVORITES_FILE", str(BASE_DIR / "favorites.json"))))
CHAT_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"
CHAT_DB_URI = f"file:{CHAT_DB_PATH}?mode=ro"

mcp = FastMCP("imessage")


def _check_full_disk_access() -> None:
    """Abort startup if the host terminal cannot read chat.db.

    macOS silently returns an empty DB to processes without Full Disk Access,
    so an explicit probe is the only reliable signal.
    """
    if not CHAT_DB_PATH.exists():
        print(
            f"❌ chat.db not found at {CHAT_DB_PATH}.\n"
            "   Grant Full Disk Access to the terminal running this server: "
            "System Settings → Privacy & Security → Full Disk Access.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        with sqlite3.connect(CHAT_DB_URI, uri=True) as conn:
            conn.execute("SELECT COUNT(*) FROM message LIMIT 1").fetchone()
    except sqlite3.OperationalError as exc:
        print(
            f"❌ chat.db is unreadable ({exc}).\n"
            "   Grant Full Disk Access to this terminal and restart the server.",
            file=sys.stderr,
        )
        sys.exit(1)


def _load_favorites() -> set[str]:
    """Re-read the favorites allowlist on every request so whitelist edits apply live."""
    if not FAVORITES_PATH.exists():
        raise FileNotFoundError(
            f"Favorites allowlist missing at {FAVORITES_PATH}. Create it with a JSON array "
            "(see favorites.example.json), or set IVY_FAVORITES_FILE."
        )
    with FAVORITES_PATH.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError("Favorites allowlist must be a JSON array of phone numbers/emails.")
    return {str(entry).strip() for entry in data if str(entry).strip()}


def _authorize(number: str) -> str:
    target = (number or "").strip()
    if not target:
        raise PermissionError("Unauthorized Contact: empty number.")
    if target not in _load_favorites():
        raise PermissionError(f"Unauthorized Contact: {target} is not in the favorites allowlist.")
    return target


@mcp.tool()
def get_recent_messages(number: str, limit: int = 20) -> list[dict]:
    """Return the most recent iMessages with `number` (newest first).

    `number` must match an entry in the favorites allowlist exactly (e.g.
    "+15551234567" or "user@example.com"). `limit` is capped at 200.
    """
    target = _authorize(number)
    capped = max(1, min(int(limit), 200))
    query = """
        SELECT
            message.ROWID AS rowid,
            datetime(
                message.date / 1000000000 + strftime('%s', '2001-01-01'),
                'unixepoch', 'localtime'
            ) AS sent_at,
            CASE message.is_from_me WHEN 1 THEN 'me' ELSE 'them' END AS direction,
            message.text AS text
        FROM message
        JOIN handle ON message.handle_id = handle.ROWID
        WHERE handle.id = ?
        ORDER BY message.date DESC
        LIMIT ?
    """
    with sqlite3.connect(CHAT_DB_URI, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, (target, capped)).fetchall()
    return [dict(row) for row in rows]


@mcp.tool()
def send_imessage(number: str, text: str) -> str:
    """Send `text` to `number` via the Messages app. Requires whitelist."""
    target = _authorize(number)
    if not text:
        raise ValueError("text must be non-empty.")

    # Pass values as argv so the AppleScript source itself is static — this
    # prevents the message body from breaking out of the string literal.
    script = (
        'on run argv\n'
        '    set targetNumber to item 1 of argv\n'
        '    set messageText to item 2 of argv\n'
        '    tell application "Messages"\n'
        '        try\n'
        '            set targetService to first service whose service type is iMessage\n'
        '            set targetBuddy to buddy targetNumber of targetService\n'
        '            send messageText to targetBuddy\n'
        '            return "SUCCESS"\n'
        '        on error errMsg\n'
        '            return "ERROR: " & errMsg\n'
        '        end try\n'
        '    end tell\n'
        'end run\n'
    )
    result = subprocess.run(
        ["osascript", "-e", script, target, text],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    output = (result.stdout or "").strip() or (result.stderr or "").strip()
    if result.returncode != 0 or output.startswith("ERROR"):
        raise RuntimeError(output or "osascript failed without output.")
    return output or "SUCCESS"


if __name__ == "__main__":
    _check_full_disk_access()
    mcp.run()
