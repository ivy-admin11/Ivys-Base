"""Canonical Sharp Picks database: schema, migration, identity, persistence,
canonical queries, and statistics.

SQLite (data/picks.db) is the single source of truth. Google Sheets is a
derived snapshot written by :mod:`ivy_core.picks_sync` — this module never
performs Google API calls or Sheets I/O.
"""

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("ivy.picks_tracker")

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
PICKS_DB = DATA_DIR / "picks.db"

# Every official query, pending-result query, Sheets export, and statistic
# must filter to canonical rows — duplicates are marked, never deleted.
_CANONICAL_FILTER = "duplicate_of IS NULL"


# ---------------------------------------------------------------------------
# Normalization + canonical identity
# ---------------------------------------------------------------------------

def normalize_text(value) -> str:
    """Loose normalization for identity components: collapsed whitespace,
    lowercase. Word boundaries are preserved (unlike normalize_team_name)."""
    return " ".join(str(value or "").lower().split())


def normalize_team_name(value) -> str:
    """Strict normalization for team-name/matchup substring matching: also
    strips spaces and apostrophes so 'Kansas City Chiefs' and a
    differently-spaced/punctuated provider string compare equal. Shared by
    ivy_core.result_updater so a matchup string is always normalized the
    same way as the team names being searched for inside it."""
    return normalize_text(value).replace("'", "").replace(" ", "")


def _canonical_event_component(pick: Dict) -> str:
    """Canonical event-date component of a pick's identity.

    Prefers the scheduled start time (stable across re-sweeps of the same
    game). Documented fallback when no start time is known: report_date
    plus game_day — the two fields that were already reliably present on
    every historical pick."""
    start = pick.get("start_time") or pick.get("start")
    if start:
        return normalize_text(start)
    report_date = normalize_text(pick.get("report_date"))
    game_day = normalize_text(pick.get("game_day")) or "unknown"
    return f"{report_date}:{game_day}"


def compute_pick_key(pick: Dict) -> str:
    """Deterministic identity for a pick — independent of odds, confidence,
    handicappers, result, final score, and sharp count.

    Prefers the provider's event ID (when available) plus the normalized
    side, since an event ID is immune to matchup-text drift between sweeps.
    Otherwise falls back to normalized sport + matchup + side + the
    canonical event date/start time (see _canonical_event_component)."""
    event_id = pick.get("provider_event_id") or pick.get("event_id")
    side = normalize_text(pick.get("side"))
    if event_id:
        return f"evt:{normalize_text(event_id)}|{side}"
    sport = normalize_text(pick.get("sport"))
    matchup = normalize_text(pick.get("matchup"))
    when = _canonical_event_component(pick)
    return f"noevt:{sport}|{matchup}|{side}|{when}"


# ---------------------------------------------------------------------------
# Schema + repeatable additive migration
# ---------------------------------------------------------------------------

_ADDITIVE_COLUMNS = {
    "pick_key": "TEXT",
    "provider_event_id": "TEXT",
    "duplicate_of": "INTEGER",
    "first_seen_at": "TIMESTAMP",
    "last_seen_at": "TIMESTAMP",
}


def _existing_columns(conn: sqlite3.Connection, table: str) -> set:
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


def _create_base_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS picks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT NOT NULL,
            matchup TEXT NOT NULL,
            side TEXT NOT NULL,
            odds REAL,
            handicapper TEXT,
            confidence TEXT,
            game_day TEXT,
            start_time TEXT,
            reasoning TEXT,
            report_date TEXT NOT NULL,
            sharp_count INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pick_id INTEGER NOT NULL UNIQUE,
            result TEXT,
            final_score TEXT,
            resolved_at TIMESTAMP,
            FOREIGN KEY (pick_id) REFERENCES picks(id)
        )
    """)
    # Audit trail for historical duplicate groups with conflicting resolved
    # outcomes — never silently resolved by picking one.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pick_conflicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pick_key TEXT NOT NULL,
            canonical_id INTEGER,
            detail TEXT,
            detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    existing = _existing_columns(conn, "picks")
    for column, decl in _ADDITIVE_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE picks ADD COLUMN {column} {decl}")
            logger.info(f"picks_tracker migration: added column picks.{column}")


def _backfill_pick_keys(conn: sqlite3.Connection) -> None:
    """Compute pick_key for legacy rows that predate this column. Legacy
    rows predate provider_event_id, so they always use the
    report_date+game_day fallback identity (see compute_pick_key)."""
    rows = conn.execute(
        "SELECT id, sport, matchup, side, game_day, start_time, report_date "
        "FROM picks WHERE pick_key IS NULL"
    ).fetchall()
    for pick_id, sport, matchup, side, game_day, start_time, report_date in rows:
        key = compute_pick_key({
            "sport": sport, "matchup": matchup, "side": side,
            "game_day": game_day, "start_time": start_time, "report_date": report_date,
        })
        conn.execute("UPDATE picks SET pick_key = ? WHERE id = ?", (key, pick_id))
    if rows:
        logger.info(f"picks_tracker migration: backfilled pick_key for {len(rows)} legacy row(s)")


def _mark_duplicates_and_carry_results(conn: sqlite3.Connection) -> None:
    """Group canonical-candidate rows by pick_key, keep the earliest as
    canonical, and mark the rest duplicate_of=<canonical id>. Original rows
    are never deleted or altered beyond duplicate_of.

    If a duplicate group has exactly one distinct resolved outcome, it is
    carried to the canonical record (only if the canonical record doesn't
    already have a conflicting one). Groups with more than one distinct
    resolved outcome are recorded in pick_conflicts and left untouched —
    never silently resolved."""
    candidates = conn.execute(
        f"SELECT pick_key, id FROM picks WHERE {_CANONICAL_FILTER} "
        "ORDER BY pick_key, created_at, id"
    ).fetchall()

    groups: Dict[str, List[int]] = {}
    for pick_key, pick_id in candidates:
        groups.setdefault(pick_key, []).append(pick_id)

    for pick_key, ids in groups.items():
        if len(ids) <= 1:
            continue
        canonical_id, dup_ids = ids[0], ids[1:]

        placeholders = ",".join("?" * len(ids))
        resolved = [
            (pid, result, score)
            for pid, result, score in conn.execute(
                f"SELECT pick_id, result, final_score FROM results WHERE pick_id IN ({placeholders})",
                ids,
            ).fetchall()
            if result
        ]
        distinct_outcomes = {result for _, result, _ in resolved}

        for dup_id in dup_ids:
            conn.execute("UPDATE picks SET duplicate_of = ? WHERE id = ?", (canonical_id, dup_id))

        if len(distinct_outcomes) == 1:
            outcome_result = next(iter(distinct_outcomes))
            outcome_score = next((score for _, result, score in resolved if score), None)
            canonical_row = conn.execute(
                "SELECT result FROM results WHERE pick_id = ?", (canonical_id,)
            ).fetchone()
            if canonical_row and not canonical_row[0]:
                conn.execute(
                    "UPDATE results SET result = ?, final_score = COALESCE(final_score, ?), "
                    "resolved_at = CURRENT_TIMESTAMP WHERE pick_id = ?",
                    (outcome_result, outcome_score, canonical_id),
                )
        elif len(distinct_outcomes) > 1:
            conn.execute(
                "INSERT INTO pick_conflicts (pick_key, canonical_id, detail) VALUES (?, ?, ?)",
                (
                    pick_key, canonical_id,
                    f"{len(distinct_outcomes)} conflicting resolved outcomes "
                    f"({sorted(distinct_outcomes)}) across a {len(ids)}-row duplicate group",
                ),
            )
            logger.warning(
                "picks_tracker migration: conflicting outcomes for pick_key=%s "
                "across %d duplicate rows — recorded in pick_conflicts, canonical "
                "result left untouched",
                pick_key, len(ids),
            )


def _ensure_canonical_unique_index(conn: sqlite3.Connection) -> None:
    # Partial unique index: only one canonical record per pick_key. Must run
    # after dedup, or an un-migrated duplicate would fail this CREATE.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_picks_canonical_pick_key "
        f"ON picks(pick_key) WHERE {_CANONICAL_FILTER}"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_picks_pick_key ON picks(pick_key)")


def _init_db() -> None:
    """Create tables if missing, then perform a repeatable additive schema
    migration and one-time historical backfill/dedup. Idempotent — safe to
    call at the top of every public function in this module."""
    conn = sqlite3.connect(PICKS_DB)
    try:
        _create_base_tables(conn)
        _add_missing_columns(conn)
        conn.commit()
        _backfill_pick_keys(conn)
        _mark_duplicates_and_carry_results(conn)
        conn.commit()
        _ensure_canonical_unique_index(conn)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _extract_pick_fields(pick: Dict) -> Dict:
    """Normalize a raw or merged pick dict into canonical column values.
    Merged picks use 'start'/'handicappers'; raw picks use 'start_time'/'handicapper'."""
    start_time = pick.get("start_time") or pick.get("start")
    handicappers = pick.get("handicappers") or pick.get("handicapper")
    if isinstance(handicappers, list):
        sharp_count = len(handicappers)
        handicapper = ", ".join(handicappers) if handicappers else None
    else:
        sharp_count = 1 if handicappers else 0
        handicapper = handicappers
    return {
        "sport": pick.get("sport"),
        "matchup": pick.get("matchup"),
        "side": pick.get("side"),
        "odds": pick.get("odds"),
        "handicapper": handicapper,
        "confidence": pick.get("confidence"),
        "game_day": pick.get("game_day"),
        "start_time": start_time,
        "reasoning": pick.get("reasoning"),
        "sharp_count": sharp_count,
        "provider_event_id": pick.get("provider_event_id") or pick.get("event_id"),
    }


def save_picks(picks: List[Dict], report_date: str) -> Dict:
    """Idempotently upsert a batch of picks by canonical pick_key.

    A repeated observation of the same pick (same event/side) updates safe
    metadata (last_seen_at, missing fields, the highest sharp count seen)
    instead of inserting another official row. Never writes to Google
    Sheets — see ivy_core.picks_sync for the derived snapshot.

    Returns a structured summary: {"inserted": N, "updated": N, "total": N}.
    """
    _init_db()
    conn = sqlite3.connect(PICKS_DB)
    now = datetime.now(timezone.utc).isoformat()
    inserted = updated = 0
    try:
        for raw_pick in picks:
            fields = _extract_pick_fields(raw_pick)
            pick_key = compute_pick_key({**fields, "report_date": report_date})

            existing = conn.execute(
                f"SELECT id, sharp_count FROM picks WHERE pick_key = ? AND {_CANONICAL_FILTER}",
                (pick_key,),
            ).fetchone()

            if existing:
                pick_id, existing_sharp_count = existing
                new_sharp_count = max(existing_sharp_count or 0, fields["sharp_count"] or 0)
                conn.execute(
                    """
                    UPDATE picks SET
                        last_seen_at = ?,
                        sharp_count = ?,
                        odds = COALESCE(odds, ?),
                        confidence = COALESCE(confidence, ?),
                        reasoning = COALESCE(reasoning, ?),
                        handicapper = COALESCE(handicapper, ?),
                        start_time = COALESCE(start_time, ?),
                        provider_event_id = COALESCE(provider_event_id, ?)
                    WHERE id = ?
                    """,
                    (
                        now, new_sharp_count, fields["odds"], fields["confidence"],
                        fields["reasoning"], fields["handicapper"], fields["start_time"],
                        fields["provider_event_id"], pick_id,
                    ),
                )
                updated += 1
            else:
                insert_cursor = conn.execute(
                    """
                    INSERT INTO picks (
                        sport, matchup, side, odds, handicapper, confidence,
                        game_day, start_time, reasoning, report_date, sharp_count,
                        pick_key, provider_event_id, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fields["sport"], fields["matchup"], fields["side"], fields["odds"],
                        fields["handicapper"], fields["confidence"], fields["game_day"],
                        fields["start_time"], fields["reasoning"], report_date,
                        fields["sharp_count"], pick_key, fields["provider_event_id"], now, now,
                    ),
                )
                pick_id = insert_cursor.lastrowid
                conn.execute("INSERT INTO results (pick_id) VALUES (?)", (pick_id,))
                inserted += 1
        conn.commit()
    finally:
        conn.close()

    logger.info(f"save_picks: {inserted} inserted, {updated} updated (of {len(picks)} observed)")
    return {"inserted": inserted, "updated": updated, "total": len(picks)}


def get_pending_picks() -> List[Dict]:
    """Canonical picks with no resolved result yet."""
    _init_db()
    conn = sqlite3.connect(PICKS_DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f"""
            SELECT p.id, p.sport, p.matchup, p.side, p.game_day, p.start_time,
                   p.sharp_count, p.handicapper, p.report_date
            FROM picks p
            LEFT JOIN results r ON p.id = r.pick_id
            WHERE {_CANONICAL_FILTER} AND (r.result IS NULL OR r.result = '')
            ORDER BY p.created_at DESC
        """).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def update_pick_result(pick_id: int, result: str, final_score: Optional[str] = None) -> bool:
    """Update one pick's outcome in the database only. No Sheets I/O —
    call ivy_core.picks_sync.sync_canonical_snapshot() separately."""
    _init_db()
    conn = sqlite3.connect(PICKS_DB)
    try:
        if not conn.execute("SELECT id FROM picks WHERE id = ?", (pick_id,)).fetchone():
            return False
        conn.execute(
            "UPDATE results SET result = ?, final_score = ?, resolved_at = ? WHERE pick_id = ?",
            (result, final_score, datetime.now(timezone.utc).isoformat(), pick_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def batch_update_results(updates: List[Dict]) -> int:
    """Apply many {"pick_id", "result", "final_score"} updates in a single
    transaction. Returns the number of rows actually updated."""
    if not updates:
        return 0
    _init_db()
    conn = sqlite3.connect(PICKS_DB)
    now = datetime.now(timezone.utc).isoformat()
    applied = 0
    try:
        for u in updates:
            cursor = conn.execute(
                "UPDATE results SET result = ?, final_score = ?, resolved_at = ? WHERE pick_id = ?",
                (u["result"], u.get("final_score"), now, u["pick_id"]),
            )
            applied += cursor.rowcount
        conn.commit()
    finally:
        conn.close()
    return applied


def get_conflicts() -> List[Dict]:
    """Recorded duplicate groups with conflicting historical outcomes —
    never silently resolved during migration."""
    _init_db()
    conn = sqlite3.connect(PICKS_DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT pick_key, canonical_id, detail, detected_at FROM pick_conflicts"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_canonical_snapshot_rows() -> List[Dict]:
    """One row per canonical pick, in deterministic order, for the Sheets
    snapshot sync (see ivy_core.picks_sync). Never touches Google Sheets."""
    _init_db()
    conn = sqlite3.connect(PICKS_DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f"""
            SELECT p.id, p.sport, p.matchup, p.side, p.odds, p.handicapper,
                   p.confidence, p.game_day, p.start_time, p.report_date,
                   p.sharp_count, p.pick_key, r.result, r.final_score
            FROM picks p
            LEFT JOIN results r ON p.id = r.pick_id
            WHERE {_CANONICAL_FILTER}
            ORDER BY p.id ASC
        """).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def _ratio(numerator: int, denominator: int) -> float:
    return (numerator / denominator) if denominator else 0.0


def _record_from_counts(wins: Optional[int], losses: Optional[int],
                         pushes: Optional[int], pending: Optional[int]) -> Dict:
    wins, losses, pushes, pending = wins or 0, losses or 0, pushes or 0, pending or 0
    resolved = wins + losses + pushes
    return {
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "pending": pending,
        "resolved": resolved,
        "record": f"{wins}-{losses}-{pushes}",
        "win_ratio": _ratio(wins, resolved),
        "loss_ratio": _ratio(losses, resolved),
        "push_ratio": _ratio(pushes, resolved),
        "decisive_hit_rate": _ratio(wins, wins + losses),
    }


def get_stats_overall() -> Dict:
    """All-time canonical W/L/P statistics with ratios (0.0 when a
    denominator is zero — never a division error)."""
    _init_db()
    conn = sqlite3.connect(PICKS_DB)
    try:
        row = conn.execute(f"""
            SELECT
                SUM(CASE WHEN r.result = 'W' THEN 1 ELSE 0 END),
                SUM(CASE WHEN r.result = 'L' THEN 1 ELSE 0 END),
                SUM(CASE WHEN r.result = 'P' THEN 1 ELSE 0 END),
                SUM(CASE WHEN r.result IS NULL OR r.result = '' THEN 1 ELSE 0 END)
            FROM picks p
            LEFT JOIN results r ON p.id = r.pick_id
            WHERE {_CANONICAL_FILTER}
        """).fetchone()
    finally:
        conn.close()
    return _record_from_counts(*row)


def get_stats_by_sport() -> Dict[str, Dict]:
    """All-time canonical W/L/P statistics with ratios, grouped by sport."""
    _init_db()
    conn = sqlite3.connect(PICKS_DB)
    try:
        rows = conn.execute(f"""
            SELECT p.sport,
                SUM(CASE WHEN r.result = 'W' THEN 1 ELSE 0 END),
                SUM(CASE WHEN r.result = 'L' THEN 1 ELSE 0 END),
                SUM(CASE WHEN r.result = 'P' THEN 1 ELSE 0 END),
                SUM(CASE WHEN r.result IS NULL OR r.result = '' THEN 1 ELSE 0 END)
            FROM picks p
            LEFT JOIN results r ON p.id = r.pick_id
            WHERE {_CANONICAL_FILTER}
            GROUP BY p.sport
        """).fetchall()
    finally:
        conn.close()
    return {sport: _record_from_counts(w, l, pu, pe) for sport, w, l, pu, pe in rows}
