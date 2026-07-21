"""Track wins, losses, and pushes from Sharp Picks reports.

Stores picks with their outcomes in a SQLite database, enabling:
- Historical record of all picks
- Win/loss/push tallies by sport, handicapper, confidence level
- Season-to-date ROI and hit rate calculations
- Google Sheets logging for shared visibility
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

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
    
    conn.commit()
    conn.close()


def save_picks(picks: List[Dict], report_date: str):
    """Save a batch of picks from a report to SQLite and Google Sheets."""
    _init_db()
    conn = sqlite3.connect(PICKS_DB)
    cursor = conn.cursor()
    
    for pick in picks:
        # Normalize field names: merged picks use "start"/"handicappers", raw picks use "start_time"/"handicapper"
        start_time = pick.get("start_time") or pick.get("start")
        handicappers = pick.get("handicappers") or pick.get("handicapper")
        
        # Count the number of sharps backing this pick
        if isinstance(handicappers, list):
            sharp_count = len(handicappers)
            handicapper = ", ".join(handicappers) if handicappers else None
        else:
            sharp_count = 1 if handicappers else 0
            handicapper = handicappers
        
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
            pick.get("game_day"),
            start_time,
            pick.get("reasoning"),
            report_date,
            sharp_count,
        ))
        pick_id = cursor.lastrowid
        cursor.execute("INSERT INTO results (pick_id) VALUES (?)", (pick_id,))
    
    conn.commit()
    conn.close()
    logger.info(f"Saved {len(picks)} picks to database")
    
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
    
    # Also update in Google Sheets
    if pick_data:
        matchup, side = pick_data
        try:
            update_result_in_sheet(matchup, side, result, notes=final_score)
        except Exception as e:
            logger.warning(f"Could not update result in Google Sheets: {e}")


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
            SUM(CASE WHEN r.result IS NULL THEN 1 ELSE 0 END) as pending
        FROM picks p
        LEFT JOIN results r ON p.id = r.pick_id
        WHERE datetime(p.created_at) >= datetime('now', '-' || ? || ' days')
        GROUP BY p.handicapper
        ORDER BY wins DESC
    """, (days_back,))
    
    stats = {}
    for row in cursor.fetchall():
        handicapper, total, wins, losses, pushes, pending = row
        wins = wins or 0
        losses = losses or 0
        pushes = pushes or 0
        pending = pending or 0
        
        hit_rate = (wins / (wins + losses)) * 100 if (wins + losses) > 0 else 0
        
        stats[handicapper] = {
            "total": total,
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "pending": pending,
            "hit_rate": hit_rate,
        }
    
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
            SUM(CASE WHEN r.result IS NULL THEN 1 ELSE 0 END) as pending
        FROM picks p
        LEFT JOIN results r ON p.id = r.pick_id
        WHERE datetime(p.created_at) >= datetime('now', '-' || ? || ' days')
    """, (days_back,))
    
    row = cursor.fetchone()
    total, wins, losses, pushes, pending = row
    wins = wins or 0
    losses = losses or 0
    pushes = pushes or 0
    pending = pending or 0
    
    hit_rate = (wins / (wins + losses)) * 100 if (wins + losses) > 0 else 0
    roi = 0  # TODO: Calculate ROI based on odds if odds are tracked
    
    conn.close()
    
    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "pending": pending,
        "hit_rate": hit_rate,
        "roi": roi,
    }


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
            SUM(CASE WHEN r.result IS NULL THEN 1 ELSE 0 END) as pending
        FROM picks p
        LEFT JOIN results r ON p.id = r.pick_id
        WHERE datetime(p.created_at) >= datetime('now', '-' || ? || ' days')
        GROUP BY p.sport
        ORDER BY wins DESC
    """, (days_back,))
    
    stats = {}
    for row in cursor.fetchall():
        sport, total, wins, losses, pushes, pending = row
        wins = wins or 0
        losses = losses or 0
        pushes = pushes or 0
        pending = pending or 0
        
        hit_rate = (wins / (wins + losses)) * 100 if (wins + losses) > 0 else 0
        
        stats[sport] = {
            "total": total,
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "pending": pending,
            "hit_rate": hit_rate,
        }
    
    conn.close()
    return stats


def format_stats_for_pdf(days_back: int = 30) -> str:
    """Format stats as a readable string for inclusion in PDF."""
    overall = get_stats_overall(days_back)
    by_sport = get_stats_by_sport(days_back)
    by_hand = get_stats_by_handicapper(days_back)
    
    lines = [
        f"📊 Sharp Picks Record (Last {days_back} Days)",
        f"Overall: {overall['wins']}W-{overall['losses']}L-{overall['pushes']}P ({overall['hit_rate']:.1f}% hit rate) — {overall['pending']} pending",
    ]
    
    if by_sport:
        lines.append("\nBy Sport:")
        for sport, stats in sorted(by_sport.items(), key=lambda x: x[1]['wins'], reverse=True):
            lines.append(f"  {sport}: {stats['wins']}W-{stats['losses']}L-{stats['pushes']}P ({stats['hit_rate']:.1f}%)")
    
    if by_hand:
        lines.append("\nTop Handicappers:")
        for hand, stats in list(sorted(by_hand.items(), key=lambda x: x[1]['wins'], reverse=True))[:5]:
            lines.append(f"  {hand}: {stats['wins']}W-{stats['losses']}L-{stats['pushes']}P ({stats['hit_rate']:.1f}%)")
    
    return "\n".join(lines)


def auto_sync_to_export_sheet():
    """Auto-sync all picks to the export sheet in Google Sheets.
    
    Called automatically after picks are saved. This populates the
    "Sharp Picks" sheet in the export tab for easy sharing and analysis.
    """
    try:
        from ivy_core.sheets_logger import _get_sheets_service
        
        service = _get_sheets_service()
        if not service:
            logger.debug("Skipping auto-sync to export sheet: no Google Sheets access")
            return
        
        SPREADSHEET_ID = "1vxdAfvLyu3o3N-suV1qxX6KWbYZyCiQvNcYdOxePoHQ"
        TARGET_SHEET_GID = 1305096861
        
        # Find the target sheet
        spreadsheet = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
        target_sheet_name = None
        for sheet in spreadsheet['sheets']:
            if sheet['properties']['sheetId'] == TARGET_SHEET_GID:
                target_sheet_name = sheet['properties']['title']
                break
        
        if not target_sheet_name:
            logger.debug(f"Target sheet (gid={TARGET_SHEET_GID}) not found for export")
            return
        
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
        header = ["Sport", "Matchup", "Side", "Odds", "Handicapper", "Confidence", "GameDay", "StartTime", "ReportDate", "Sharps", "Result", "FinalScore"]
        
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
        
        # Append-only: do not clear existing data, only add new picks
        # This prevents losing picks from old job runs that aren't in the current database
        
        # Initialize header if empty
        current = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{target_sheet_name}!A1:L1"
        ).execute()
        
        if not current.get('values'):
            service.spreadsheets().values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{target_sheet_name}!A1:L1",
                valueInputOption="USER_ENTERED",
                body={"values": [header]}
            ).execute()
        
        if rows:
            service.spreadsheets().values().append(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{target_sheet_name}!A2:L",
                valueInputOption="USER_ENTERED",
                body={"values": rows}
            ).execute()
        
        logger.info(f"Auto-synced {len(picks)} picks to export sheet (append-only mode)")
    except Exception as e:
        logger.warning(f"Failed to auto-sync picks to export sheet: {e}")
