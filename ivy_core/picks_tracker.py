"""Track wins, losses, and pushes from Sharp Picks reports.

Stores picks with their outcomes in a SQLite database, enabling:
- Historical record of all picks
- Win/loss/push tallies by sport, handicapper, confidence level
- Season-to-date ROI and hit rate calculations
- Google Sheets logging for shared visibility
"""

import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

from ivy_core.pick_stats import (  # noqa: F401  (re-exported for callers)
    MIN_DECIDED_FOR_RATE,
    UNVERIFIABLE,
    format_hit_rate,
    resolve_pick_date,
    summarize,
)
from ivy_core.sheets_logger import log_picks_to_sheet, update_result_in_sheet

logger = logging.getLogger("ivy.picks_tracker")

# Use data/picks.db for persistent storage
DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
PICKS_DB = DATA_DIR / "picks.db"


def _init_db():
    """Create picks and results tables if they don't exist."""
    conn = sqlite3.connect(PICKS_DB)
    cursor = conn.cursor()
    
    # picks table: stores each pick as it was reported
    cursor.execute("""
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
    
    # results table: stores the outcome after the game is played
    # result: 'W' (win), 'L' (loss), 'P' (push), or NULL (not yet played/resolved)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pick_id INTEGER NOT NULL UNIQUE,
            result TEXT,
            final_score TEXT,
            resolved_at TIMESTAMP,
            FOREIGN KEY (pick_id) REFERENCES picks(id)
        )
    """)
    
    # Whether this grade also reached the spreadsheet. A grade can land in the
    # database and fail to reach the sheet -- that happened live on 5 Sep when
    # four picks graded while every Sheets call returned 404 -- and until this
    # column existed the only trace was a log line. 0 means the two stores
    # disagree and nothing has reconciled them yet.
    cursor.execute("PRAGMA table_info(results)")
    if "sheet_synced" not in {row[1] for row in cursor.fetchall()}:
        cursor.execute("ALTER TABLE results ADD COLUMN sheet_synced INTEGER DEFAULT 0")

    conn.commit()
    conn.close()


def save_picks(picks: List[Dict], report_date: str):
    """Save a batch of picks from a report to SQLite and Google Sheets."""
    _init_db()
    conn = sqlite3.connect(PICKS_DB)
    cursor = conn.cursor()
    skipped = 0
    
    for pick in picks:
        # Normalize field names: merged picks use "start"/"handicappers", raw picks use "start_time"/"handicapper"
        start_time = pick.get("start_time") or pick.get("start")

        # game_day comes from a model reading free-form posts and is routinely
        # not a date -- "today" appears in 79 of the first 88 picks. Storing
        # the string keeps a value that no consumer can use and that shadows
        # the usable report_date. Store nothing instead; date resolution falls
        # back to report_date, which is always real.
        raw_game_day = pick.get("game_day")
        game_day = resolve_pick_date(raw_game_day, None)
        if raw_game_day and not game_day:
            logger.info(
                "Pick game_day %r is not a date; storing none and relying on "
                "report_date", raw_game_day,
            )
        handicappers = pick.get("handicappers") or pick.get("handicapper")
        
        # Count the number of sharps backing this pick
        if isinstance(handicappers, list):
            sharp_count = len(handicappers)
            handicapper = ", ".join(handicappers) if handicappers else None
        else:
            sharp_count = 1 if handicappers else 0
            handicapper = handicappers
        
        # sport/matchup/side are NOT NULL. Picks are parsed out of free-form
        # posts, so one of them arriving incomplete is routine -- and letting
        # that IntegrityError escape used to discard the whole batch before
        # the commit, losing every good pick alongside the bad one.
        try:
            cursor.execute("""
                INSERT INTO picks (
                    sport, matchup, side, odds, handicapper, confidence,
                    game_day, start_time, reasoning, report_date, sharp_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pick.get("sport"),
                pick.get("matchup"),
                pick.get("side"),
                pick.get("odds"),
                handicapper,
                pick.get("confidence"),
                game_day,
                start_time,
                pick.get("reasoning"),
                report_date,
                sharp_count,
            ))
        except sqlite3.IntegrityError as exc:
            skipped += 1
            logger.warning(
                "Skipping unsaveable pick (%s): %s | %s",
                exc, pick.get("matchup") or "no matchup", pick.get("side") or "no side",
            )
            continue
        pick_id = cursor.lastrowid
        cursor.execute("INSERT INTO results (pick_id) VALUES (?)", (pick_id,))
    
    conn.commit()
    conn.close()
    saved = len(picks) - skipped
    if skipped:
        logger.warning("Saved %d of %d picks; %d could not be stored", saved, len(picks), skipped)
    else:
        logger.info("Saved %d picks to database", saved)
    
    # Also log to Google Sheets for shared visibility
    try:
        log_picks_to_sheet(picks, report_date)
    except Exception as e:
        logger.warning(f"Could not log picks to Google Sheets: {e}")
    
    # Auto-sync to export sheet
    try:
        auto_sync_to_export_sheet()
    except Exception as e:
        logger.warning(f"Could not sync picks to export sheet: {e}")


def update_pick_result(pick_id: int, result: str, final_score: Optional[str] = None):
    """Update the result of a pick (W/L/P) in database and Google Sheets."""
    _init_db()
    conn = sqlite3.connect(PICKS_DB)
    cursor = conn.cursor()
    
    # Get the pick details for sheet update
    cursor.execute(
        "SELECT matchup, side FROM picks WHERE id = ?",
        (pick_id,)
    )
    pick_data = cursor.fetchone()
    
    cursor.execute("""
        UPDATE results
        SET result = ?, final_score = ?, resolved_at = CURRENT_TIMESTAMP
        WHERE pick_id = ?
    """, (result, final_score, pick_id))
    
    conn.commit()
    conn.close()
    
    # Also update in Google Sheets, and record whether it actually landed.
    if pick_data:
        matchup, side = pick_data
        try:
            synced = bool(update_result_in_sheet(matchup, side, result, notes=final_score))
        except Exception as e:
            synced = False
            logger.warning(f"Could not update result in Google Sheets: {e}")
        if not synced:
            logger.warning(
                "Grade recorded in the database but NOT on the sheet: %s %s -> %s. "
                "Run scripts/repair_dashboard.py to reconcile.",
                matchup, side, result,
            )
        conn = sqlite3.connect(PICKS_DB)
        try:
            conn.execute(
                "UPDATE results SET sheet_synced = ? WHERE pick_id = ?",
                (1 if synced else 0, pick_id),
            )
            conn.commit()
        finally:
            conn.close()


def _split_handicappers(stored: Optional[str]) -> List[str]:
    """Unpack the comma-joined handicapper column into individual handles.

    save_picks writes ", ".join(handicappers) for a consensus pick. Handles
    never contain commas, so splitting on them is safe and reversible.
    """
    if not stored:
        return ["unattributed"]
    names = [part.strip() for part in stored.split(",")]
    return [n for n in names if n] or ["unattributed"]


def mark_all_synced() -> int:
    """Record that every grade is now on the sheet.

    Only correct after a full rebuild, which rewrites every row from this
    database -- scripts/sync_picks_to_sheet.py. Called there rather than
    assumed, so the flag reflects a write that actually happened.
    """
    _init_db()
    conn = sqlite3.connect(PICKS_DB)
    try:
        cur = conn.execute(
            "UPDATE results SET sheet_synced = 1 WHERE result IS NOT NULL")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def unsynced_grades() -> List[Dict]:
    """Grades in the database that are not known to be on the sheet.

    This is the divergence that used to be invisible. A non-empty result means
    the dashboard is understating the record -- the grade exists, the sheet
    does not show it.
    """
    _init_db()
    conn = sqlite3.connect(PICKS_DB)
    try:
        rows = conn.execute("""
            SELECT p.id, p.matchup, p.side, r.result, r.final_score
            FROM picks p
            JOIN results r ON r.pick_id = p.id
            WHERE r.result IS NOT NULL
              AND r.result != ?
              AND COALESCE(r.sheet_synced, 0) = 0
            ORDER BY p.id
        """, (UNVERIFIABLE,)).fetchall()
    finally:
        conn.close()
    return [
        {"pick_id": r[0], "matchup": r[1], "side": r[2],
         "result": r[3], "final_score": r[4]}
        for r in rows
    ]


def resync_grades() -> Dict[str, int]:
    """Retry every grade the sheet is missing. Safe to run repeatedly."""
    pending = unsynced_grades()
    fixed = failed = 0
    for row in pending:
        try:
            ok = bool(update_result_in_sheet(
                row["matchup"], row["side"], row["result"], notes=row["final_score"]
            ))
        except Exception as exc:
            ok = False
            logger.warning("Resync failed for pick %s: %s", row["pick_id"], exc)
        if ok:
            fixed += 1
            conn = sqlite3.connect(PICKS_DB)
            try:
                conn.execute("UPDATE results SET sheet_synced = 1 WHERE pick_id = ?",
                             (row["pick_id"],))
                conn.commit()
            finally:
                conn.close()
        else:
            failed += 1
    return {"attempted": len(pending), "fixed": fixed, "still_missing": failed}


def get_stats_by_handicapper(days_back: int = 30) -> Dict[str, Dict]:
    """Get win/loss/push stats grouped by handicapper (last N days)."""
    _init_db()
    conn = sqlite3.connect(PICKS_DB)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT 
            p.handicapper,
            COUNT(*) as total,
            SUM(CASE WHEN r.result = 'W' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN r.result = 'L' THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN r.result = 'P' THEN 1 ELSE 0 END) as pushes,
            SUM(CASE WHEN r.result IS NULL THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN r.result = 'U' THEN 1 ELSE 0 END) as unverifiable
        FROM picks p
        LEFT JOIN results r ON p.id = r.pick_id
        WHERE datetime(p.created_at) >= datetime('now', '-' || ? || ' days')
        GROUP BY p.handicapper
        ORDER BY wins DESC
    """, (days_back,))
    
    # save_picks stores a consensus pick's backers as one comma-joined string,
    # so grouping on that column alone invents a composite handicapper
    # ("@a, @b") and credits neither real handle. Every pick a handicapper was
    # part of counts toward that handicapper's record, which is the whole
    # point of tracking them -- and consensus picks are the ones worth judging
    # a roster on. Split the group key back apart and accumulate per handle.
    tallies: Dict[str, Dict] = {}
    for row in cursor.fetchall():
        handicapper, total, wins, losses, pushes, pending, unverifiable = row
        for name in _split_handicappers(handicapper):
            bucket = tallies.setdefault(
                name,
                {"total": 0, "wins": 0, "losses": 0, "pushes": 0,
                 "pending": 0, "unverifiable": 0},
            )
            bucket["total"] += total or 0
            bucket["wins"] += wins or 0
            bucket["losses"] += losses or 0
            bucket["pushes"] += pushes or 0
            bucket["pending"] += pending or 0
            bucket["unverifiable"] += unverifiable or 0
    
    stats = {name: summarize(**t) for name, t in tallies.items()}
    # Restore the ORDER BY the SQL intended, now that rows have been merged.
    stats = dict(sorted(stats.items(), key=lambda kv: kv[1]["wins"], reverse=True))
    
    conn.close()
    return stats


def get_stats_overall(days_back: int = 30) -> Dict:
    """Get overall win/loss/push stats (last N days)."""
    _init_db()
    conn = sqlite3.connect(PICKS_DB)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN r.result = 'W' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN r.result = 'L' THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN r.result = 'P' THEN 1 ELSE 0 END) as pushes,
            SUM(CASE WHEN r.result IS NULL THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN r.result = 'U' THEN 1 ELSE 0 END) as unverifiable
        FROM picks p
        LEFT JOIN results r ON p.id = r.pick_id
        WHERE datetime(p.created_at) >= datetime('now', '-' || ? || ' days')
    """, (days_back,))
    
    total, wins, losses, pushes, pending, unverifiable = cursor.fetchone()
    conn.close()
    
    stats = summarize(wins, losses, pushes, pending, unverifiable, total=total)
    stats["roi"] = 0  # TODO: Calculate ROI based on odds if odds are tracked
    return stats


def get_stats_by_sport(days_back: int = 30) -> Dict[str, Dict]:
    """Get win/loss/push stats grouped by sport (last N days)."""
    _init_db()
    conn = sqlite3.connect(PICKS_DB)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT 
            p.sport,
            COUNT(*) as total,
            SUM(CASE WHEN r.result = 'W' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN r.result = 'L' THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN r.result = 'P' THEN 1 ELSE 0 END) as pushes,
            SUM(CASE WHEN r.result IS NULL THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN r.result = 'U' THEN 1 ELSE 0 END) as unverifiable
        FROM picks p
        LEFT JOIN results r ON p.id = r.pick_id
        WHERE datetime(p.created_at) >= datetime('now', '-' || ? || ' days')
        GROUP BY p.sport
        ORDER BY wins DESC
    """, (days_back,))
    
    stats = {}
    for row in cursor.fetchall():
        sport, total, wins, losses, pushes, pending, unverifiable = row
        stats[sport] = summarize(wins, losses, pushes, pending, unverifiable, total=total)
    
    conn.close()
    return stats


def format_stats_for_pdf(days_back: int = 30) -> str:
    """Format stats as a readable string for inclusion in PDF."""
    overall = get_stats_overall(days_back)
    by_sport = get_stats_by_sport(days_back)
    by_hand = get_stats_by_handicapper(days_back)
    
    def record(stats: Dict) -> str:
        return (
            f"{stats['wins']}W-{stats['losses']}L-{stats['pushes']}P"
            f" · {format_hit_rate(stats)}"
        )
    
    lines = [
        f"📊 Sharp Picks Record (Last {days_back} Days)",
        f"Overall: {record(overall)}",
    ]
    
    # Pending and unverifiable are different claims and are never merged: one
    # is a result still coming, the other is a result that never will.
    tail = []
    if overall["pending"]:
        tail.append(f"{overall['pending']} awaiting results")
    if overall["unverifiable"]:
        tail.append(f"{overall['unverifiable']} ungradeable (game outside the scores window)")
    if tail:
        lines.append("  " + " · ".join(tail))
    
    behind = len(unsynced_grades())
    if behind:
        lines.append(
            f"  ⚠️  {behind} grade(s) recorded but missing from the sheet — "
            f"the dashboard is understating the record"
        )
    
    if by_sport:
        lines.append("\nBy Sport:")
        for sport, stats in sorted(by_sport.items(), key=lambda x: x[1]['wins'], reverse=True):
            lines.append(f"  {sport}: {record(stats)}")
    
    if by_hand:
        lines.append("\nTop Handicappers:")
        for hand, stats in list(sorted(by_hand.items(), key=lambda x: x[1]['wins'], reverse=True))[:5]:
            lines.append(f"  {hand}: {record(stats)}")
    
    return "\n".join(lines)


def auto_sync_to_export_sheet():
    """Auto-sync all picks to the export sheet in Google Sheets.
    
    Called automatically after picks are saved. This populates the
    "Sharp Picks" sheet in the export tab for easy sharing and analysis.
    """
    try:
        # One import, at the point of use. sheets_logger owns every fact about
        # the spreadsheet -- its id, its tab, and its column layout -- so none
        # of them can drift apart across the modules that write to it.
        from ivy_core.sheets_logger import (
            COL,
            COLUMNS,
            LAST_COLUMN_LETTER,
            SPREADSHEET_ID,
            TARGET_SHEET_GID,
            _get_sheets_service,
        )
        
        service = _get_sheets_service()
        if not service:
            logger.debug("Skipping auto-sync to export sheet: no Google Sheets access")
            return False
        
        # Find the target sheet
        spreadsheet = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
        target_sheet_name = None
        for sheet in spreadsheet['sheets']:
            if sheet['properties']['sheetId'] == TARGET_SHEET_GID:
                target_sheet_name = sheet['properties']['title']
                break
        
        if not target_sheet_name:
            logger.debug(f"Target sheet (gid={TARGET_SHEET_GID}) not found for export")
            return False
        
        # Get all picks from database
        conn = sqlite3.connect(PICKS_DB)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                p.id,
                p.sport,
                p.matchup,
                p.side,
                p.odds,
                p.handicapper,
                p.confidence,
                p.game_day,
                p.start_time,
                p.report_date,
                p.sharp_count,
                COALESCE(r.result, '') as result,
                COALESCE(r.final_score, '') as final_score
            FROM picks p
            LEFT JOIN results r ON p.id = r.pick_id
            ORDER BY p.created_at
        """)
        
        picks = cursor.fetchall()
        conn.close()
        
        # Format for sheet
        rows = []
        header = list(COLUMNS)
        
        for pick in picks:
            row = [
                pick[1],  # sport
                pick[2],  # matchup
                pick[3],  # side
                str(pick[4]) if pick[4] else "",  # odds
                pick[5],  # handicapper
                pick[6],  # confidence
                pick[7],  # game_day
                pick[8],  # start_time
                pick[9],  # report_date
                str(pick[10]) if pick[10] else "1",  # sharp_count
                pick[11],  # result
                pick[12],  # final_score
            ]
            rows.append(row)
        
        # Append-only: do not clear existing data, only add new picks.
        # This preserves rows from old job runs that are no longer in the
        # database -- but "new" has to mean new. The query above selects every
        # pick, and this used to append all of them on every run, so each
        # save_picks re-appended the entire history. A sheet rebuilt to 88 rows
        # would have gone to 176 on the next picks job, then 264, with every
        # row duplicated. Read what is already there and append only the rest.
        existing = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{target_sheet_name}!A:{LAST_COLUMN_LETTER}"
        ).execute().get("values", [])
        
        def key(row):
            """Identity of a pick: same game, same side, same report."""
            def cell(i):
                return (row[i] or "").strip().lower() if len(row) > i else ""
            return (cell(COL["Matchup"]), cell(COL["Side"]), cell(COL["ReportDate"]))
        
        already = {key(r) for r in existing[1:]} if len(existing) > 1 else set()
        before = len(rows)
        rows = [r for r in rows if key(r) not in already]
        skipped = before - len(rows)
        
        # Initialize header if empty
        current = existing[:1]
        
        if not current:
            service.spreadsheets().values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{target_sheet_name}!A1:{LAST_COLUMN_LETTER}1",
                valueInputOption="USER_ENTERED",
                body={"values": [header]}
            ).execute()
        
        if rows:
            service.spreadsheets().values().append(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{target_sheet_name}!A2:{LAST_COLUMN_LETTER}",
                valueInputOption="USER_ENTERED",
                body={"values": rows}
            ).execute()
        
        logger.info(
            "Auto-synced %d new pick(s) to export sheet; %d already present",
            len(rows), skipped,
        )
        return True
    except Exception as e:
        # Returned, not just logged: a caller that reports "sheet updated"
        # off a swallowed failure is how a 404 went unnoticed through two
        # full repair runs.
        logger.warning(f"Failed to auto-sync picks to export sheet: {e}")
        return False
