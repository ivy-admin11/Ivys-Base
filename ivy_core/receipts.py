"""Persistent execution receipts — the runtime's source of truth for whether
a job was actually dispatched, not something an LLM gets to assert on its
own. Backed by SQLite at logs/executions.db (gitignored).
"""

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "logs" / "executions.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    job_name TEXT NOT NULL,
    requester TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    detail TEXT
)
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.execute(_SCHEMA)
    return conn


def record_start(job_name: str, requester: Optional[str] = None) -> str:
    """Record that a job was dispatched. Returns a new execution_id."""
    execution_id = str(uuid.uuid4())
    with _connect() as conn:
        conn.execute(
            "INSERT INTO executions (execution_id, job_name, requester, status, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (execution_id, job_name, requester, "started", datetime.now(timezone.utc).isoformat()),
        )
    return execution_id


def record_finish(execution_id: str, status: str, detail: Optional[str] = None) -> None:
    """Record the terminal status of a previously-started execution."""
    with _connect() as conn:
        conn.execute(
            "UPDATE executions SET status = ?, finished_at = ?, detail = ? WHERE execution_id = ?",
            (status, datetime.now(timezone.utc).isoformat(), detail, execution_id),
        )


def get_execution(execution_id: str) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
        ).fetchone()
    return dict(row) if row else None


def list_recent(limit: int = 50, job_name: Optional[str] = None) -> List[Dict[str, Any]]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        if job_name:
            rows = conn.execute(
                "SELECT * FROM executions WHERE job_name = ? ORDER BY started_at DESC LIMIT ?",
                (job_name, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM executions ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(row) for row in rows]
