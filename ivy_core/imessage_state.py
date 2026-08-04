"""Durable, privacy-minimizing state for Ivy's iMessage worker.

The Apple Messages database remains read-only.  This module stores only ROWIDs,
state transitions, timestamps, and sanitized outcome categories in Ivy's own
SQLite database.  Message text and sender identifiers are deliberately never
persisted here.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


DEFAULT_DB_PATH = Path(
    os.environ.get(
        "IVY_IMESSAGE_STATE_DB",
        str(Path(__file__).resolve().parent.parent / "logs" / "imessage_worker.db"),
    )
).expanduser()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS worker_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbound_messages (
    message_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'unclassified',
    collected_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    outcome TEXT,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_inbound_messages_status
ON inbound_messages(status, message_id);
"""

_ACTIVE_STATUSES = frozenset({"queued", "processing", "sending"})
_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "blocked", "superseded", "completion_unknown"}
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_detail(detail: Optional[str]) -> Optional[str]:
    if not detail:
        return None
    # State diagnostics accept categories, not raw user/provider payloads.
    cleaned = " ".join(str(detail).split())
    return cleaned[:200]


@dataclass(frozen=True)
class InboundMessage:
    """A message held in memory while it moves through bounded queues."""

    message_id: int
    text: str
    sender: str
    collected_monotonic: float


class InboxStateStore:
    """Small SQLite journal for cursor and processing-state recovery."""

    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        if self.path.is_symlink():
            raise RuntimeError("iMessage state database must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        conn = sqlite3.connect(str(self.path), timeout=5)
        self.path.chmod(0o600)
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.executescript(_SCHEMA)
        return conn

    def initialize_cursor(self, current_max: int) -> int:
        """Return the durable collector cursor, bootstrapping on first use.

        A brand-new installation starts at the current Messages high-water
        mark so Ivy does not replay an entire historical database.  Once
        created, the cursor survives gateway restarts.
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM worker_meta WHERE key = 'collector_cursor'"
            ).fetchone()
            if row is not None:
                try:
                    return max(0, int(row[0]))
                except (TypeError, ValueError):
                    pass
            cursor = max(0, int(current_max))
            conn.execute(
                "INSERT INTO worker_meta(key, value) VALUES('collector_cursor', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(cursor),),
            )
            return cursor

    def get_cursor(self) -> Optional[int]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM worker_meta WHERE key = 'collector_cursor'"
            ).fetchone()
        if row is None:
            return None
        try:
            return max(0, int(row[0]))
        except (TypeError, ValueError):
            return None

    def advance_cursor(self, message_id: int) -> None:
        """Advance the cursor monotonically after durable queue reservation."""
        message_id = max(0, int(message_id))
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM worker_meta WHERE key = 'collector_cursor'"
            ).fetchone()
            current = int(row[0]) if row and str(row[0]).isdigit() else 0
            if message_id > current:
                conn.execute(
                    "INSERT INTO worker_meta(key, value) VALUES('collector_cursor', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(message_id),),
                )

    def reset_cursor(self, message_id: int) -> None:
        """Reset the cursor after verified chat.db replacement/ROWID rotation."""
        message_id = max(0, int(message_id))
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO worker_meta(key, value) VALUES('collector_cursor', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(message_id),),
            )

    def reserve(self, message_id: int, category: str = "unclassified") -> bool:
        """Reserve a ROWID exactly once before placing it on an in-memory queue."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO inbound_messages "
                "(message_id, status, category, collected_at) VALUES (?, 'queued', ?, ?)",
                (int(message_id), category[:40], _utc_now()),
            )
            return cursor.rowcount == 1

    def release_reservation(self, message_id: int) -> None:
        """Undo a reservation when bounded queue insertion did not succeed."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM inbound_messages WHERE message_id = ? AND status = 'queued'",
                (int(message_id),),
            )

    def update_category(self, message_ids: Iterable[int], category: str) -> None:
        ids = tuple(int(value) for value in message_ids)
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE inbound_messages SET category = ? "  # nosec B608 - placeholders only
                f"WHERE message_id IN ({placeholders}) AND status = 'queued'",
                (category[:40], *ids),
            )

    def mark_processing(self, message_ids: Iterable[int], category: str) -> bool:
        ids = tuple(int(value) for value in message_ids)
        if not ids:
            return False
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE inbound_messages SET status = 'processing', category = ?, "  # nosec B608 - placeholders only
                f"started_at = ? WHERE message_id IN ({placeholders}) AND status = 'queued'",
                (category[:40], _utc_now(), *ids),
            )
            return cursor.rowcount == len(ids)

    def mark_sending(self, message_ids: Iterable[int]) -> None:
        ids = tuple(int(value) for value in message_ids)
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE inbound_messages SET status = 'sending' "  # nosec B608 - placeholders only
                f"WHERE message_id IN ({placeholders}) AND status = 'processing'",
                ids,
            )

    def mark_terminal(
        self,
        message_ids: Iterable[int],
        status: str,
        *,
        outcome: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        if status not in _TERMINAL_STATUSES:
            raise ValueError(f"Unsupported terminal inbox status: {status}")
        ids = tuple(int(value) for value in message_ids)
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        active = tuple(sorted(_ACTIVE_STATUSES))
        active_placeholders = ",".join("?" for _ in active)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE inbound_messages SET status = ?, finished_at = ?, outcome = ?, detail = ? "  # nosec B608 - placeholders only
                f"WHERE message_id IN ({placeholders}) AND status IN ({active_placeholders})",
                (
                    status,
                    _utc_now(),
                    (outcome or status)[:40],
                    _safe_detail(detail),
                    *ids,
                    *active,
                ),
            )

    def recover_after_restart(self) -> list[int]:
        """Return never-started ROWIDs; quarantine ambiguous in-flight work.

        Retrying a message that was already executing could duplicate a job,
        reminder, or outbound message.  Queued rows are safe to rehydrate from
        read-only chat.db; processing/sending rows become completion_unknown.
        """
        now = _utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE inbound_messages SET status = 'completion_unknown', "
                "finished_at = ?, outcome = 'restart_during_processing', "
                "detail = 'manual review required after restart' "
                "WHERE status IN ('processing', 'sending')",
                (now,),
            )
            rows = conn.execute(
                "SELECT message_id FROM inbound_messages WHERE status = 'queued' "
                "ORDER BY message_id ASC"
            ).fetchall()
        return [int(row[0]) for row in rows]

    def recent_counts(self) -> dict[str, int]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM inbound_messages GROUP BY status"
            ).fetchall()
        return {str(status): int(count) for status, count in rows}

    def prune_terminal(self, keep: int = 5000) -> int:
        """Bound the privacy-minimizing ROWID dedup journal."""
        keep = max(100, min(int(keep), 100_000))
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM inbound_messages WHERE message_id IN ("
                "SELECT message_id FROM inbound_messages "
                "WHERE status IN ('completed', 'failed', 'blocked', 'superseded', "
                "'completion_unknown') ORDER BY message_id DESC LIMIT -1 OFFSET ?"
                ")",
                (keep,),
            )
            return max(0, cursor.rowcount)


class WorkerRuntimeMetrics:
    """Thread-safe, non-sensitive in-memory runtime metrics."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_monotonic = time.monotonic()
        self._last_poll_monotonic: Optional[float] = None
        self._last_response_monotonic: Optional[float] = None
        self._last_response_latency_ms: Optional[int] = None
        self._last_handler_category: Optional[str] = None
        self._last_error_category: Optional[str] = None
        self._oldest_queued_monotonic: Optional[float] = None
        self._queue_depth = 0
        self._slow_queue_depth = 0
        self._collector_alive = False
        self._dispatcher_alive = False
        self._slow_worker_alive = False
        self._apple_event_timeouts = 0

    def set_thread_state(self, name: str, alive: bool) -> None:
        with self._lock:
            setattr(self, f"_{name}_alive", bool(alive))

    def record_poll(self) -> None:
        with self._lock:
            self._last_poll_monotonic = time.monotonic()
            self._last_error_category = None

    def update_queues(
        self,
        queue_depth: int,
        slow_queue_depth: int,
        oldest_queued_monotonic: Optional[float],
    ) -> None:
        with self._lock:
            self._queue_depth = max(0, int(queue_depth))
            self._slow_queue_depth = max(0, int(slow_queue_depth))
            self._oldest_queued_monotonic = oldest_queued_monotonic

    def record_response(self, category: str, total_latency_ms: int) -> None:
        with self._lock:
            self._last_response_monotonic = time.monotonic()
            self._last_response_latency_ms = max(0, int(total_latency_ms))
            self._last_handler_category = category[:40]
            self._last_error_category = None

    def record_error(self, category: str) -> None:
        with self._lock:
            self._last_error_category = category[:80]

    def record_apple_event_timeout(self) -> None:
        with self._lock:
            self._apple_event_timeouts += 1

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()

        def age(value: Optional[float]) -> Optional[float]:
            return None if value is None else round(max(0.0, now - value), 3)

        with self._lock:
            return {
                "collector_alive": self._collector_alive,
                "dispatcher_alive": self._dispatcher_alive,
                "slow_worker_alive": self._slow_worker_alive,
                "queue_depth": self._queue_depth,
                "slow_queue_depth": self._slow_queue_depth,
                "oldest_queued_age_seconds": age(self._oldest_queued_monotonic),
                "last_poll_age_seconds": age(self._last_poll_monotonic),
                "last_response_age_seconds": age(self._last_response_monotonic),
                "last_response_latency_ms": self._last_response_latency_ms,
                "last_handler_category": self._last_handler_category,
                "last_error_category": self._last_error_category,
                "apple_event_timeouts": self._apple_event_timeouts,
                "uptime_seconds": round(now - self._started_monotonic, 3),
            }


runtime_metrics = WorkerRuntimeMetrics()
