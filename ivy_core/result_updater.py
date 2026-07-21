"""Automated result tracking for Sharp Picks.

Queries completed games, matches them against saved picks,
determines outcomes (W/L/P), and updates the database + Google Sheets.
"""

import sqlite3
import logging
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional, Dict, Tuple
from decimal import Decimal, InvalidOperation

import requests

logger = logging.getLogger("ivy.result_updater")

DATA_DIR = Path(__file__).parent.parent / "data"
PICKS_DB = DATA_DIR / "picks.db"

# The Odds API base URL
ODDS_API_BASE = "https://api.the-odds-api.com/v4"


def _get_odds_api_key():
    """Get The Odds API key from environment."""
    import os
    return os.environ.get("ODDS_API_KEY", "")


def parse_score(value: Any) -> Optional[Decimal]:
    """Safely parse scores to Decimal for type safety and precision.
    
    Handles int, float, string, and None values gracefully.
    Returns None if parsing fails.
    
    Args:
        value: Raw score value from API (int, float, string, or None)
    
    Returns:
        Decimal or None if unparseable
    """
    if value is None:
        return None
    
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        logger.debug(f"Failed to parse score: {value!r}")
        return None


def get_completed_games(sport_key: str = None, hours_back: int = 48) -> list:
    """Fetch completed games from The Odds API.
    
    Args:
        sport_key: Optional sport to filter (e.g., 'baseball_mlb'). If None, fetch all.
        hours_back: How many hours back to search for completed games.
    
    Returns:
        List of completed game dicts with scores.
    """
    api_key = _get_odds_api_key()
    if not api_key:
        logger.warning("ODDS_API_KEY not set; cannot fetch game results")
        return []
    
    # Get list of sports
    try:
        resp = requests.get(
            f"{ODDS_API_BASE}/sports",
            params={"api_key": api_key},
            timeout=10
        )
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to fetch sports list: {e}")
        return []
    
    sports = resp.json()
    if not sports:
        logger.debug("No sports returned from API")
        return []
    
    completed_games = []
    
    # Query each sport for completed games
    for sport in sports:
        sport_key_i = sport.get("key")
        group = sport.get("group", "")
        
        # Skip if user filtered to different sport
        if sport_key and sport_key_i != sport_key:
            continue
        
        try:
            resp = requests.get(
                f"{ODDS_API_BASE}/sports/{sport_key_i}/scores",
                params={"api_key": api_key, "daysFrom": 2},
                timeout=10
            )
            resp.raise_for_status()
            games = resp.json()
            
            # Filter for completed games only
            for game in games:
                if game.get("completed"):
                    game["sport_key"] = sport_key_i
                    game["sport_group"] = group
                    completed_games.append(game)
        except Exception as e:
            logger.debug(f"Could not fetch games for {sport_key_i}: {e}")
            continue
    
    logger.info(f"Fetched {len(completed_games)} completed games")
    return completed_games


def _normalize_team(name: str) -> str:
    """Normalize team name for matching (strip apostrophes, spaces, lowercase)."""
    if not name:
        return ""
    return name.replace("'", "").replace(" ", "").lower()


def _match_teams_in_matchup(matchup: str, home: str, away: str) -> bool:
    """Verify both teams appear in matchup string.
    
    Ensures we match the correct game, not just any game with one matching team.
    
    Args:
        matchup: Matchup description (e.g., "Miami @ Texas")
        home: Home team name
        away: Away team name
    
    Returns:
        True if both teams are found in matchup
    """
    norm_matchup = matchup.lower()
    home_norm = _normalize_team(home)
    away_norm = _normalize_team(away)
    
    has_home = home_norm in norm_matchup if home_norm else False
    has_away = away_norm in norm_matchup if away_norm else False
    
    return has_home and has_away


def _extract_moneyline_result(game: dict, pick_side: str) -> Optional[str]:
    """Determine W/L/P from a moneyline game and pick side."""
    home_team = game.get("home_team", "")
    away_team = game.get("away_team", "")
    scores = game.get("scores", [])
    
    if not scores or len(scores) < 2:
        return None
    
    home_score = parse_score(scores[0].get("score"))
    away_score = parse_score(scores[1].get("score"))
    
    if home_score is None or away_score is None:
        return None
    
    # Determine winner
    if home_score > away_score:
        winner = "home"
    elif away_score > home_score:
        winner = "away"
    else:
        return "P"  # Push/tie
    
    # Check if pick won
    pick_norm = _normalize_team(pick_side)
    home_norm = _normalize_team(home_team)
    away_norm = _normalize_team(away_team)
    
    # Pick could be "Team ML" or "Team Moneyline"
    for team_norm, team_winner in [(home_norm, "home"), (away_norm, "away")]:
        if team_norm in pick_norm:
            if team_winner == winner:
                return "W"
            else:
                return "L"
    
    return None


def _extract_spread_result(game: dict, pick_side: str) -> Optional[str]:
    """Determine W/L/P from a spread pick."""
    home_team = game.get("home_team", "")
    away_team = game.get("away_team", "")
    scores = game.get("scores", [])
    
    if not scores or len(scores) < 2:
        return None
    
    home_score = parse_score(scores[0].get("score"))
    away_score = parse_score(scores[1].get("score"))
    
    if home_score is None or away_score is None:
        return None
    
    # Extract spread from pick_side (e.g., "Team -2.5" or "Team +3")
    # More specific: must have +/- immediately before the number
    match = re.search(r'([+-]\d+(?:\.\d+)?)', pick_side)
    if not match:
        return None
    
    try:
        spread = Decimal(match.group(1))
    except (ValueError, InvalidOperation):
        return None
    
    home_norm = _normalize_team(home_team)
    away_norm = _normalize_team(away_team)
    
    # Determine which team the pick is on
    pick_norm = _normalize_team(pick_side)
    
    if home_norm in pick_norm:
        # Home team spread pick
        adjusted_score = home_score - spread
        if adjusted_score > away_score:
            return "W"
        elif adjusted_score < away_score:
            return "L"
        else:
            return "P"
    elif away_norm in pick_norm:
        # Away team spread pick
        adjusted_score = away_score + spread
        if adjusted_score > home_score:
            return "W"
        elif adjusted_score < home_score:
            return "L"
        else:
            return "P"
    
    return None


def _extract_over_under_result(game: dict, pick_side: str) -> Optional[str]:
    """Determine W/L/P from an Over/Under pick."""
    scores = game.get("scores", [])
    
    if not scores or len(scores) < 2:
        return None
    
    home_score = parse_score(scores[0].get("score"))
    away_score = parse_score(scores[1].get("score"))
    
    if home_score is None or away_score is None:
        return None
    
    total_points = home_score + away_score
    
    # Extract the total from pick_side (e.g., "Over 9.5" or "Under 45.5")
    # More specific: look for "over"/"under" keyword first, then the number
    match = re.search(r'(?:over|under)\s*(\d+(?:\.\d+)?)', pick_side, re.IGNORECASE)
    if not match:
        return None
    
    try:
        ou_line = Decimal(match.group(1))
    except (ValueError, InvalidOperation):
        return None
    
    pick_lower = pick_side.lower()
    
    if "over" in pick_lower:
        if total_points > ou_line:
            return "W"
        elif total_points < ou_line:
            return "L"
        else:
            # Exact match treated as PUSH (betting lines are typically X.5, so exact match is rare)
            return "P"
    elif "under" in pick_lower:
        if total_points < ou_line:
            return "W"
        elif total_points > ou_line:
            return "L"
        else:
            return "P"
    
    return None


def match_pick_to_game(pick: Dict, completed_games: list) -> Tuple[Optional[str], Optional[str]]:
    """Match a pick to a completed game and determine result.
    
    Args:
        pick: Pick dict with sport, matchup, side, game_day
        completed_games: List of completed games from API
    
    Returns:
        Tuple of (result: 'W'/'L'/'P' or None, final_score: str or None)
    """
    sport = pick.get("sport", "").lower()
    matchup = pick.get("matchup", "").lower()
    side = pick.get("side", "").lower()
    
    # Map sport to API key
    sport_map = {
        "mlb": "baseball_mlb",
        "nba": "basketball_nba",
        "nfl": "americanfootball_nfl",
        "nhl": "icehockey_nhl",
        "soccer": "soccer_uefa_champs",
        "world cup": "soccer_fifa_wc",
    }
    sport_key = sport_map.get(sport)
    
    # Find matching game
    for game in completed_games:
        if game.get("sport_key") != sport_key:
            continue
        
        home_team = game.get("home_team", "").lower()
        away_team = game.get("away_team", "").lower()
        
        # Require BOTH teams to be in matchup (more strict matching)
        if not _match_teams_in_matchup(matchup, home_team, away_team):
            continue
        
        # Found matching game
        scores = game.get("scores", [])
        if scores and len(scores) >= 2:
            home_score = parse_score(scores[0].get("score"))
            away_score = parse_score(scores[1].get("score"))
            if home_score is not None and away_score is not None:
                final_score = f"{away_team} {away_score} vs {home_team} {home_score}"
            else:
                final_score = None
        else:
            final_score = None
        
        # Determine result based on pick type
        result = None
        if "moneyline" in side or " ml" in side:
            result = _extract_moneyline_result(game, side)
        elif " " in side and ("-" in side or "+" in side):
            result = _extract_spread_result(game, side)
        elif "over" in side or "under" in side:
            result = _extract_over_under_result(game, side)
        
        return result, final_score
    
    return None, None


def update_pick_result(pick_id: int, result: str, final_score: Optional[str] = None):
    """Update a pick's result in the database and Google Sheets."""
    conn = sqlite3.connect(PICKS_DB)
    cursor = conn.cursor()
    
    # Get the pick details
    cursor.execute(
        "SELECT matchup, side FROM picks WHERE id = ?",
        (pick_id,)
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False
    
    matchup, side = row
    
    # Update database
    cursor.execute(
        "UPDATE results SET result = ?, final_score = ?, resolved_at = ? WHERE pick_id = ?",
        (result, final_score, datetime.now(timezone.utc).isoformat(), pick_id)
    )
    conn.commit()
    conn.close()
    
    # Update Google Sheets
    try:
        from ivy_core.sheets_logger import update_result_in_sheet
        update_result_in_sheet(matchup, side, result)
    except Exception as e:
        logger.warning(f"Could not update Google Sheets: {e}")
    
    logger.info(f"Updated pick {pick_id}: {result}")
    return True


def auto_update_results():
    """Automatically update results for all pending picks.
    
    Fetches completed games, matches them to pending picks, and updates
    the database + Google Sheets.
    
    Returns:
        Dict with update summary.
    """
    logger.info("Starting auto-result update...")
    
    # Get completed games
    games = get_completed_games()
    if not games:
        logger.info("No completed games found")
        return {"updated": 0, "pending": 0}
    
    # Get all pending picks
    conn = sqlite3.connect(PICKS_DB)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT p.id, p.sport, p.matchup, p.side, p.game_day, p.sharp_count, p.handicapper
        FROM picks p
        LEFT JOIN results r ON p.id = r.pick_id
        WHERE r.result IS NULL
        ORDER BY p.created_at DESC
    """)
    
    pending_picks = cursor.fetchall()
    conn.close()
    
    logger.info(f"Found {len(pending_picks)} pending picks")
    
    updated = 0
    for pick_id, sport, matchup, side, game_day, sharp_count, handicapper in pending_picks:
        pick = {
            "sport": sport,
            "matchup": matchup,
            "side": side,
            "game_day": game_day,
            "sharp_count": sharp_count or 1,
            "handicapper": handicapper
        }
        
        result, final_score = match_pick_to_game(pick, games)
        
        if result:
            if update_pick_result(pick_id, result, final_score):
                # Log with sharp count
                sharp_info = f" ({sharp_count} {'sharp' if sharp_count == 1 else 'sharps'})" if sharp_count else ""
                logger.info(f"Pick {pick_id} {result}{sharp_info}: {matchup} {side}")
                updated += 1
    
    logger.info(f"Updated {updated} pick results")
    
    # Sync all results back to the export sheet
    if updated > 0:
        try:
            logger.info("Syncing results to Google Sheets...")
            from ivy_core.picks_tracker import auto_sync_to_export_sheet
            auto_sync_to_export_sheet()
            logger.info("✅ Export sheet synced with latest results")
        except Exception as e:
            logger.error(f"Failed to sync export sheet: {e}")
    
    return {
        "updated": updated,
        "pending": len(pending_picks) - updated,
        "total_games_checked": len(games)
    }


if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    
    result = auto_update_results()
    print(f"\n✅ Result update complete: {result['updated']} updated, {result['pending']} still pending")
    sys.exit(0 if result["updated"] > 0 or result["pending"] == 0 else 1)
