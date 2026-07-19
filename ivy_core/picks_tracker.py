"""Track wins, losses, and pushes from Sharp Picks reports.

Stores picks with their outcomes in a SQLite database, enabling:
- Historical record of all picks
- Win/loss/push tallies by sport, handicapper, confidence level
- Season-to-date ROI and hit rate calculations
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

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
    """Save a batch of picks from a report."""
    _init_db()
    conn = sqlite3.connect(PICKS_DB)
    cursor = conn.cursor()
    
    for pick in picks:
        cursor.execute("""
            INSERT INTO picks (
                sport, matchup, side, odds, handicapper, confidence,
                game_day, start_time, reasoning, report_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            pick.get("sport"),
            pick.get("matchup"),
            pick.get("side"),
            pick.get("odds"),
            pick.get("handicapper"),
            pick.get("confidence"),
            pick.get("game_day"),
            pick.get("start_time"),
            pick.get("reasoning"),
            report_date,
        ))
        pick_id = cursor.lastrowid
        cursor.execute("INSERT INTO results (pick_id) VALUES (?)", (pick_id,))
    
    conn.commit()
    conn.close()
    logger.info(f"Saved {len(picks)} picks to database")


def update_pick_result(pick_id: int, result: str, final_score: Optional[str] = None):
    """Update the result of a pick (W/L/P)."""
    _init_db()
    conn = sqlite3.connect(PICKS_DB)
    cursor = conn.cursor()
    
    cursor.execute("""
        UPDATE results
        SET result = ?, final_score = ?, resolved_at = CURRENT_TIMESTAMP
        WHERE pick_id = ?
    """, (result, final_score, pick_id))
    
    conn.commit()
    conn.close()


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
