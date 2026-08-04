"""Durable, truthful lifecycle receipts for Ivy jobs.

The execution row describes the lifecycle of the job worker, while ``outcome``
describes the domain result returned by the agent and ``delivery_status``
describes messaging separately.  A detached process being spawned is never a
completed execution.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

DB_PATH = Path(
    os.environ.get(
        "IVY_RECEIPTS_DB",
        str(Path(__file__).resolve().parent.parent / "logs" / "executions.db"),
    )
).expanduser()

SCHEMA_VERSION = 3

ACTIVE_STATUSES = frozenset({"queued", "dispatched", "running"})
TERMINAL_STATUSES = frozenset({
    "completed",
    "failed",
    "skipped",
    "dispatch_failed",
    "unavailable",
    "not_found",
    "completion_unknown",
    "triggered_unobserved",
    "timed_out",
})
DELIVERY_STATUSES = frozenset({
    "not_requested",
    "not_attempted",
    "submitted_unverified",
    "partial",
    "failed",
    "unknown",
    "verified_delivered",
})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    job_name TEXT NOT NULL,
    requester TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    detail TEXT,
    executor TEXT,
    worker_started_at TEXT,
    heartbeat_at TEXT,
    updated_at TEXT,
    pid INTEGER,
    log_path TEXT,
    exit_code INTEGER,
    outcome TEXT,
    result_json TEXT,
    delivery_status TEXT NOT NULL DEFAULT 'not_attempted',
    report_ids_json TEXT
)
"""

_ADDITIVE_COLUMNS = {
    "executor": "TEXT",
    "worker_started_at": "TEXT",
    "heartbeat_at": "TEXT",
    "updated_at": "TEXT",
    "pid": "INTEGER",
    "log_path": "TEXT",
    "exit_code": "INTEGER",
    "outcome": "TEXT",
    "result_json": "TEXT",
    "delivery_status": "TEXT NOT NULL DEFAULT 'not_attempted'",
    "report_ids_json": "TEXT",
}


class ExecutionAlreadyActive(RuntimeError):
    """Raised when an atomic single-active-job guard rejects a new run."""

    def __init__(self, job_name: str, execution_id: str):
        self.job_name = job_name
        self.execution_id = execution_id
        super().__init__(f"Job '{job_name}' already has active execution {execution_id}")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utcnow().isoformat()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(executions)").fetchall()
    }
    for name, declaration in _ADDITIVE_COLUMNS.items():
        if name not in existing:
            try:
                conn.execute(f"ALTER TABLE executions ADD COLUMN {name} {declaration}")
            except sqlite3.OperationalError:
                # A second Ivy process may have completed the same additive
                # migration after our initial PRAGMA snapshot.  Only suppress
                # the race when the requested column is now observably present.
                refreshed = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(executions)").fetchall()
                }
                if name not in refreshed:
                    raise

    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version < 2:
        now = _iso_now()
        # Historical "success" rows only proved dispatch.  Their eventual
        # outcome cannot be reconstructed, so migration must not preserve the
        # false completion claim.  Brain is a launchctl trigger and is labelled
        # explicitly as unobserved; other legacy accepted runs are unknown.
        conn.execute(
            """
            UPDATE executions
               SET status = CASE
                       WHEN status = 'success' AND job_name = 'brain'
                           THEN 'triggered_unobserved'
                       WHEN status IN ('success', 'started')
                           THEN 'completion_unknown'
                       WHEN status = 'error'
                           THEN 'dispatch_failed'
                       WHEN status = 'already_running'
                           THEN 'skipped'
                       ELSE status
                   END,
                   outcome = CASE
                       WHEN status = 'success' THEN 'legacy_dispatch_success'
                       WHEN status = 'started' THEN 'legacy_started_without_completion'
                       WHEN status = 'already_running' THEN 'already_running'
                       ELSE outcome
                   END,
                   delivery_status = CASE
                       WHEN status IN ('success', 'started') THEN 'unknown'
                       ELSE COALESCE(delivery_status, 'not_attempted')
                   END,
                   finished_at = COALESCE(finished_at, ?),
                   updated_at = COALESCE(updated_at, finished_at, started_at, ?)
             WHERE status IN ('success', 'started', 'error', 'already_running')
            """,
            (now, now),
        )
    if version < 3:
        # The active-set definition gained the explicit ``dispatched`` state.
        # SQLite cannot alter a partial-index predicate in place.
        conn.execute("DROP INDEX IF EXISTS executions_one_active_job")

    if version < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS executions_one_active_job
            ON executions(job_name)
         WHERE status IN ('queued', 'dispatched', 'running')
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS executions_job_started
            ON executions(job_name, started_at DESC)
        """
    )


def _connect() -> sqlite3.Connection:
    if DB_PATH.is_symlink():
        raise RuntimeError("receipts database must not be a symlink")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    DB_PATH.parent.chmod(0o700)
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    DB_PATH.chmod(0o600)
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        # Some special SQLite targets do not support WAL.  Receipt correctness
        # does not depend on it; the busy timeout still protects short races.
        pass
    _ensure_schema(conn)
    conn.commit()
    for sqlite_sidecar in (
        DB_PATH.with_name(DB_PATH.name + "-wal"),
        DB_PATH.with_name(DB_PATH.name + "-shm"),
    ):
        if sqlite_sidecar.exists() and not sqlite_sidecar.is_symlink():
            sqlite_sidecar.chmod(0o600)
    return conn


def _json_dump(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_load(raw: Optional[str], fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    record = dict(row)
    record["result"] = _json_load(record.get("result_json"), None)
    record["report_ids"] = _json_load(record.get("report_ids_json"), [])
    record["terminal"] = record.get("status") in TERMINAL_STATUSES
    return record


def record_start(
    job_name: str,
    requester: Optional[str] = None,
    *,
    executor: Optional[str] = None,
    delivery_status: str = "not_attempted",
) -> str:
    """Atomically queue a job and return its new execution ID.

    The partial unique index is the source of truth for the single-active-job
    guard.  A check followed by an insert would be racy across gateway/CLI
    processes, so conflicts raise :class:`ExecutionAlreadyActive` with the
    existing execution ID.
    """
    if delivery_status not in DELIVERY_STATUSES:
        raise ValueError(f"Invalid delivery status: {delivery_status}")

    reconcile_stale()
    execution_id = str(uuid.uuid4())
    now = _iso_now()
    try:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO executions (
                    execution_id, job_name, requester, status, started_at,
                    executor, updated_at, delivery_status
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    job_name,
                    requester,
                    now,
                    executor,
                    now,
                    delivery_status,
                ),
            )
    except sqlite3.IntegrityError as exc:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT execution_id FROM executions
                 WHERE job_name = ? AND status IN ('queued', 'dispatched', 'running')
                 ORDER BY started_at DESC LIMIT 1
                """,
                (job_name,),
            ).fetchone()
        if row:
            raise ExecutionAlreadyActive(job_name, row[0]) from exc
        raise
    return execution_id


def record_spawned(
    execution_id: str,
    *,
    pid: Optional[int] = None,
    log_path: Optional[str] = None,
) -> bool:
    """Record a successful process spawn as dispatched, never completed.

    Metadata may arrive after a very fast worker has already claimed or
    finalized the row.  The metadata update is allowed, but the lifecycle
    transition is compare-and-set and cannot overwrite either state.
    """
    now = _iso_now()
    with _connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM executions WHERE execution_id = ?", (execution_id,)
        ).fetchone()
        if not exists:
            return False
        conn.execute(
            """
            UPDATE executions
               SET pid = COALESCE(pid, ?),
                   log_path = COALESCE(log_path, ?)
             WHERE execution_id = ?
            """,
            (pid, log_path, execution_id),
        )
        conn.execute(
            """
            UPDATE executions
               SET status = 'dispatched',
                   updated_at = ?
             WHERE execution_id = ? AND status = 'queued'
            """,
            (now, execution_id),
        )
        return True


def record_running(
    execution_id: str,
    *,
    pid: Optional[int] = None,
    log_path: Optional[str] = None,
) -> bool:
    """Claim an execution for one worker PID and mark it running.

    The dispatcher normally records the just-spawned PID first.  The matching
    child may then claim that row idempotently, while a different process using
    the same execution ID is rejected before it can run the agent twice.
    """
    now = _iso_now()
    with _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE executions
               SET status = 'running',
                   pid = COALESCE(pid, ?),
                   log_path = COALESCE(log_path, ?),
                   worker_started_at = COALESCE(worker_started_at, ?),
                   heartbeat_at = ?,
                   updated_at = ?
             WHERE execution_id = ?
               AND status IN ('queued', 'dispatched')
               AND (pid IS NULL OR ? IS NULL OR pid = ?)
            """,
            (pid, log_path, now, now, now, execution_id, pid, pid),
        )
        if cursor.rowcount == 1:
            return True

        if pid is None:
            cursor = conn.execute(
                """
                UPDATE executions
                   SET pid = COALESCE(pid, ?),
                       log_path = COALESCE(log_path, ?),
                       worker_started_at = COALESCE(worker_started_at, ?),
                       heartbeat_at = ?,
                       updated_at = ?
                 WHERE execution_id = ?
                   AND status = 'running'
                   AND pid IS NULL
                """,
                (None, log_path, now, now, now, execution_id),
            )
        else:
            cursor = conn.execute(
                """
                UPDATE executions
                   SET pid = COALESCE(pid, ?),
                       log_path = COALESCE(log_path, ?),
                       worker_started_at = COALESCE(worker_started_at, ?),
                       heartbeat_at = ?,
                       updated_at = ?
                 WHERE execution_id = ?
                   AND status = 'running'
                   AND (pid IS NULL OR pid = ?)
                """,
                (pid, log_path, now, now, now, execution_id, pid),
            )
        return cursor.rowcount == 1


def record_heartbeat(execution_id: str) -> bool:
    """Refresh a running worker lease; terminal rows are immutable."""
    now = _iso_now()
    with _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE executions
               SET heartbeat_at = ?, updated_at = ?
             WHERE execution_id = ? AND status = 'running'
            """,
            (now, now, execution_id),
        )
        return cursor.rowcount == 1


def record_finish(
    execution_id: str,
    status: str,
    detail: Optional[str] = None,
    *,
    outcome: Optional[str] = None,
    exit_code: Optional[int] = None,
    result: Any = None,
    delivery_status: Optional[str] = None,
    report_ids: Optional[Sequence[str]] = None,
) -> bool:
    """Compare-and-set an active execution to a terminal state.

    Returns ``False`` when the ID does not exist or another actor already made
    the row terminal.  First terminal result wins.
    """
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"Invalid terminal execution status: {status}")
    if delivery_status is not None and delivery_status not in DELIVERY_STATUSES:
        raise ValueError(f"Invalid delivery status: {delivery_status}")

    now = _iso_now()
    with _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE executions
               SET status = ?,
                   finished_at = ?,
                   detail = ?,
                   outcome = ?,
                   exit_code = ?,
                   result_json = COALESCE(?, result_json),
                   delivery_status = COALESCE(?, delivery_status),
                   report_ids_json = COALESCE(?, report_ids_json),
                   updated_at = ?
             WHERE execution_id = ?
               AND status IN ('queued', 'dispatched', 'running')
            """,
            (
                status,
                now,
                detail,
                outcome,
                exit_code,
                _json_dump(result),
                delivery_status,
                _json_dump(list(report_ids)) if report_ids is not None else None,
                now,
                execution_id,
            ),
        )
        return cursor.rowcount == 1


def _pid_exists(pid: Optional[int]) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _parse_timestamp(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def reconcile_stale(
    *,
    max_age_seconds: Optional[float] = None,
    now: Optional[datetime] = None,
    pid_checker: Optional[Callable[[Optional[int]], bool]] = None,
) -> List[str]:
    """Finalize abandoned active rows as ``completion_unknown``.

    A stale running row is left alone while its recorded PID is alive.  Once
    the worker lease is stale *and* no worker is observable, claiming either
    success or failure would be fabrication, so the terminal state is unknown.
    """
    if max_age_seconds is None:
        try:
            max_age_seconds = float(os.environ.get("IVY_JOB_STALE_SECONDS", "300"))
        except ValueError:
            max_age_seconds = 300.0
    current = now or _utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    check_pid = pid_checker or _pid_exists

    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT execution_id, status, pid, heartbeat_at, worker_started_at,
                   updated_at, started_at, delivery_status
              FROM executions
             WHERE status IN ('queued', 'dispatched', 'running')
            """
        ).fetchall()

    reconciled: List[str] = []
    for row in rows:
        observed = (
            _parse_timestamp(row["heartbeat_at"])
            or _parse_timestamp(row["worker_started_at"])
            or _parse_timestamp(row["updated_at"])
            or _parse_timestamp(row["started_at"])
        )
        if observed is None or (current - observed).total_seconds() <= max_age_seconds:
            continue
        if row["status"] == "running" and check_pid(row["pid"]):
            continue
        # Finalize only the exact lease observation assessed above.  A worker
        # heartbeat, spawn update, or PID claim racing after our SELECT changes
        # at least one predicate and prevents a live execution being clobbered.
        finished_at = _iso_now()
        with _connect() as conn:
            cursor = conn.execute(
                """
                UPDATE executions
                   SET status = 'completion_unknown',
                       finished_at = ?,
                       detail = ?,
                       outcome = 'worker_lost',
                       delivery_status = ?,
                       updated_at = ?
                 WHERE execution_id = ?
                   AND status = ?
                   AND pid IS ?
                   AND heartbeat_at IS ?
                   AND worker_started_at IS ?
                   AND updated_at IS ?
                """,
                (
                    finished_at,
                    "Worker stopped reporting before a terminal result was recorded.",
                    (
                        "not_requested"
                        if row["delivery_status"] == "not_requested"
                        else "unknown"
                    ),
                    finished_at,
                    row["execution_id"],
                    row["status"],
                    row["pid"],
                    row["heartbeat_at"],
                    row["worker_started_at"],
                    row["updated_at"],
                ),
            )
        if cursor.rowcount == 1:
            reconciled.append(row["execution_id"])
    return reconciled


def get_execution(
    execution_id: str,
    *,
    reconcile: bool = True,
) -> Optional[Dict[str, Any]]:
    if reconcile:
        reconcile_stale()
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_recent(
    limit: int = 50,
    job_name: Optional[str] = None,
    *,
    reconcile: bool = True,
) -> List[Dict[str, Any]]:
    if reconcile:
        reconcile_stale()
    safe_limit = max(0, min(int(limit), 500))
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        if job_name:
            rows = conn.execute(
                "SELECT * FROM executions WHERE job_name = ? ORDER BY started_at DESC LIMIT ?",
                (job_name, safe_limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM executions ORDER BY started_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
    return [_row_to_dict(row) for row in rows]
