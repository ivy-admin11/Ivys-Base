"""Post-send verification for iMessage attachments, read back from chat.db.

Why this exists
---------------
Every attachment send path we have is fire-and-forget from AppleScript's
point of view: the scripting verb (``send POSIX file … to buddy``) returns
without waiting for the upload, and the clipboard-paste UI automation
returns "SUCCESS" the moment its keystrokes were *issued* — even when the
screen was locked and the keystrokes went nowhere (2026-08-28: the Familia
Meal Plan text arrived, the PDF never did, and the log said SUCCESS).

The only ground truth on this Mac is ``~/Library/Messages/chat.db``: a real
send creates a ``message`` row (``is_from_me = 1``) joined to an
``attachment`` row whose ``transfer_state`` reaches the finished value and
whose message has ``error = 0``. A silently dropped file leaves either no
row at all, or a row with a non-zero ``error`` (25 = Messages refused the
file, see ivy_core.messaging).

Who can read chat.db
--------------------
Only a process with Full Disk Access — the gateway (com.lexi.ivy) has it;
job subprocesses launched by launchd generally do not. So the lookup tries a
direct read first and falls back to the gateway's ``/imessage/attachments``
endpoint over localhost. If neither works, verification is "unknown" and the
caller keeps the old ``submitted_unverified`` behaviour rather than
retrying blind (retrying blind risks duplicate attachments).
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ivy.attachment_verify")

CHAT_DB_PATH = os.path.expanduser(os.environ.get("CHAT_DB_PATH", "~/Library/Messages/chat.db"))
GATEWAY_BASE_URL = os.environ.get("IVY_GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")

# Seconds between the Unix epoch and Apple's 2001-01-01 reference date.
APPLE_EPOCH_OFFSET = 978_307_200

# attachment.transfer_state values seen for outgoing files. 5 = transfer
# finished (upload complete). Anything else is still in flight or never
# started. Kept as a set so a newly observed "done" value is a one-line change.
TRANSFER_STATE_DONE = frozenset({5})

# How long to wait for Messages to finish a small (< 1 MB) upload before
# giving up on confirmation. Real uploads of a few-KB PDF complete in ~2-5 s.
DEFAULT_VERIFY_TIMEOUT_S = 25.0
DEFAULT_VERIFY_INTERVAL_S = 2.5

_LOCKED_RE = re.compile(r"CGSSessionScreenIsLocked</key>\s*<true/>")


def screen_is_locked() -> Optional[bool]:
    """True when the console session's screen is locked (login window /
    lock screen showing), False when unlocked, None when undeterminable.

    Keystroke-based automation must never run against a locked screen: the
    keystrokes land in the password field, and the "send" never happens.
    """
    try:
        out = subprocess.run(
            ["ioreg", "-n", "Root", "-d1", "-a"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("ioreg lock probe failed: %s", exc)
        return None
    if "IOConsoleUsers" not in out:
        return None
    return bool(_LOCKED_RE.search(out))


def to_apple_ns(unix_ts: float) -> int:
    """Unix seconds -> chat.db ``message.date`` (nanoseconds since 2001)."""
    return int((unix_ts - APPLE_EPOCH_OFFSET) * 1_000_000_000)


def from_apple_ns(apple_ns: int) -> float:
    return apple_ns / 1_000_000_000 + APPLE_EPOCH_OFFSET


def _handle_suffix(handle: Optional[str]) -> Optional[str]:
    """Last 10 digits of a phone number, so '+1 (214) 733-4061' and
    '+12147334061' match the same chat.db handle."""
    if not handle:
        return None
    digits = re.sub(r"\D", "", handle)
    return digits[-10:] if digits else None


def fetch_outgoing_attachments(
    *,
    since_ts: float,
    filename: Optional[str] = None,
    handle: Optional[str] = None,
    limit: int = 20,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Read outgoing attachment rows straight from chat.db.

    Raises ``sqlite3.Error`` (including macOS's "authorization denied" when
    the calling process lacks Full Disk Access) — callers decide whether to
    fall back to the gateway.
    """
    path = db_path or CHAT_DB_PATH
    sql = (
        "SELECT m.ROWID AS message_rowid, m.date AS date_ns, m.is_sent, m.is_delivered, "
        "m.error, h.id AS handle, a.transfer_name, a.transfer_state, a.total_bytes "
        "FROM message m "
        "JOIN message_attachment_join maj ON maj.message_id = m.ROWID "
        "JOIN attachment a ON a.ROWID = maj.attachment_id "
        "LEFT JOIN handle h ON h.ROWID = m.handle_id "
        "WHERE m.is_from_me = 1 AND m.date >= ?"
    )
    params: List[Any] = [to_apple_ns(since_ts)]
    if filename:
        sql += " AND a.transfer_name = ?"
        params.append(filename)
    suffix = _handle_suffix(handle)
    if suffix:
        sql += " AND h.id LIKE ?"
        params.append(f"%{suffix}")
    sql += " ORDER BY m.ROWID DESC LIMIT ?"
    params.append(max(1, min(int(limit), 200)))

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["sent_at_unix"] = from_apple_ns(d["date_ns"]) if d.get("date_ns") else None
        d["state"] = classify(d)
        out.append(d)
    return out


def classify(row: Dict[str, Any]) -> str:
    """Map one chat.db row to delivered / failed / pending."""
    if row.get("error"):
        return "failed"
    if row.get("transfer_state") in TRANSFER_STATE_DONE and row.get("is_sent"):
        return "delivered"
    return "pending"


def _lookup(
    *, since_ts: float, filename: Optional[str], handle: Optional[str]
) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """Rows from chat.db directly, else via the gateway; (None, reason) if neither."""
    try:
        return fetch_outgoing_attachments(since_ts=since_ts, filename=filename, handle=handle), "local"
    except sqlite3.Error as exc:
        logger.debug("Direct chat.db read unavailable (%s); trying gateway", exc)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Direct chat.db read failed (%s); trying gateway", exc)

    try:
        import requests
        from config import ADMIN_SECRET

        resp = requests.get(
            f"{GATEWAY_BASE_URL}/imessage/attachments",
            params={"since": since_ts, "filename": filename or "", "handle": handle or ""},
            headers={"X-API-Key": ADMIN_SECRET},
            timeout=5,
        )
        if resp.status_code == 200:
            return resp.json().get("attachments", []), "gateway"
        logger.debug("Gateway attachment lookup returned HTTP %s", resp.status_code)
        return None, f"gateway HTTP {resp.status_code}"
    except Exception as exc:
        logger.debug("Gateway attachment lookup failed: %s", exc)
        return None, "unavailable"


def wait_for_attachment_outcome(
    filename: str,
    since_ts: float,
    *,
    handle: Optional[str] = None,
    timeout: float = DEFAULT_VERIFY_TIMEOUT_S,
    interval: float = DEFAULT_VERIFY_INTERVAL_S,
) -> Tuple[str, Dict[str, Any]]:
    """Poll chat.db until the attachment named ``filename`` sent after
    ``since_ts`` is confirmed.

    Returns ``(outcome, details)`` where outcome is one of:
      delivered — row exists, upload finished, no error
      failed    — row exists with a Messages error (file was refused)
      missing   — no row appeared within ``timeout`` (send never happened)
      pending   — row exists but did not finish within ``timeout``
      unknown   — chat.db is unreadable here AND the gateway is unreachable
    """
    deadline = time.monotonic() + timeout
    last_row: Optional[Dict[str, Any]] = None
    source = "unavailable"
    while True:
        rows, source = _lookup(since_ts=since_ts, filename=filename, handle=handle)
        if rows is None:
            return "unknown", {"source": source}
        if rows:
            last_row = rows[0]
            state = classify(last_row)
            if state in ("delivered", "failed"):
                return state, {"source": source, "row": last_row}
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)
    if last_row is None:
        return "missing", {"source": source}
    return "pending", {"source": source, "row": last_row}
