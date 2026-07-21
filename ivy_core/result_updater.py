"""Automated result reconciliation for Sharp Picks.

Queries completed games from The Odds API, matches them against pending
canonical picks, determines outcomes (W/L/P), batches the database updates
in one transaction, and performs one canonical Sheets sync for the whole
batch — never a sync per pick plus another append-all pass.
"""

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

import requests

import config
from ivy_core import picks_tracker, picks_sync

logger = logging.getLogger("ivy.result_updater")

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Centralized sport-key map — the one place Sharp Picks sport labels map to
# Odds API sport keys, so this never drifts out of sync with the map used
# to fetch the live slate in proactive_agents/sports_bettor.py.
SPORT_KEY_MAP = {
    "mlb": "baseball_mlb",
    "nba": "basketball_nba",
    "nfl": "americanfootball_nfl",
    "nhl": "icehockey_nhl",
    "epl": "soccer_epl",
    "la liga": "soccer_spain_la_liga",
    "bundesliga": "soccer_germany_bundesliga",
    "serie a": "soccer_italy_serie_a",
    "kbo": "baseball_kbo",
    "world cup": "soccer_fifa_world_cup",
}


class OddsApiAuthenticationError(Exception):
    """Raised when the Odds API rejects credentials (401/403) — distinct
    from a legitimate empty result set, which is not an error."""


def parse_score(value: Any) -> Optional[Decimal]:
    """Safely parse a score to Decimal; None if unparseable."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _request_odds_api(path: str, params: Dict) -> Optional[list]:
    """One centralized request helper — uses the provider's documented
    `apiKey` parameter consistently (the old module mixed `api_key` and
    `apiKey` across functions)."""
    api_key = config.ODDS_API_KEY
    if not api_key:
        logger.info("ODDS_API_KEY not set; result reconciliation skipped.")
        return None

    try:
        resp = requests.get(
            f"{ODDS_API_BASE}{path}",
            params={**params, "apiKey": api_key},
            timeout=12,
        )
    except requests.exceptions.RequestException as exc:
        logger.warning("Odds API request failed (%s): %s", path, exc)
        return None

    if resp.status_code in (401, 403):
        raise OddsApiAuthenticationError(f"Odds API credentials rejected (HTTP {resp.status_code})")
    if resp.status_code != 200:
        # Not an auth failure — treat as a legitimate empty/unavailable result.
        logger.info("Odds API returned HTTP %s for %s; treating as no data.", resp.status_code, path)
        return None

    try:
        return resp.json() or []
    except ValueError:
        return None


def get_completed_games(hours_back: int = 48) -> list:
    """Fetch completed games across all tracked sports from The Odds API."""
    try:
        sports = _request_odds_api("/sports", {})
    except OddsApiAuthenticationError as exc:
        logger.error("Odds API authentication failed: %s", exc)
        return []
    if not sports:
        return []

    completed_games = []
    for sport in sports:
        sport_key = sport.get("key")
        if not sport_key or sport_key not in SPORT_KEY_MAP.values():
            continue
        try:
            games = _request_odds_api(f"/sports/{sport_key}/scores", {"daysFrom": 3}) or []
        except OddsApiAuthenticationError as exc:
            logger.error("Odds API authentication failed: %s", exc)
            return completed_games
        for game in games:
            if game.get("completed"):
                game["sport_key"] = sport_key
                completed_games.append(game)

    logger.info("Fetched %d completed game(s)", len(completed_games))
    return completed_games


def _match_teams_in_matchup(matchup: str, home: str, away: str) -> bool:
    """Both team names must appear in the matchup, normalized the SAME way
    (strip spaces/apostrophes, lowercase) on both sides of the comparison —
    otherwise 'Kansas City Chiefs' (with spaces) never matches inside a
    matchup string that's only .lower()'d, not fully normalized."""
    norm_matchup = picks_tracker.normalize_team_name(matchup)
    home_norm = picks_tracker.normalize_team_name(home)
    away_norm = picks_tracker.normalize_team_name(away)
    has_home = bool(home_norm) and home_norm in norm_matchup
    has_away = bool(away_norm) and away_norm in norm_matchup
    return has_home and has_away


def _scores_by_name(game: dict) -> Dict[str, Decimal]:
    """Map team name -> score by matching each score entry's `name` field —
    never assumes array order."""
    out = {}
    for entry in game.get("scores") or []:
        name = entry.get("name")
        score = parse_score(entry.get("score"))
        if name and score is not None:
            out[name] = score
    return out


def _extract_moneyline_result(game: dict, pick_side: str) -> Optional[str]:
    home_team, away_team = game.get("home_team", ""), game.get("away_team", "")
    scores = _scores_by_name(game)
    home_score, away_score = scores.get(home_team), scores.get(away_team)
    if home_score is None or away_score is None:
        return None
    if home_score == away_score:
        return "P"
    winner = "home" if home_score > away_score else "away"

    pick_norm = picks_tracker.normalize_team_name(pick_side)
    for team_norm, team_side in (
        (picks_tracker.normalize_team_name(home_team), "home"),
        (picks_tracker.normalize_team_name(away_team), "away"),
    ):
        if team_norm and team_norm in pick_norm:
            return "W" if team_side == winner else "L"
    return None


def _extract_spread_result(game: dict, pick_side: str) -> Optional[str]:
    home_team, away_team = game.get("home_team", ""), game.get("away_team", "")
    scores = _scores_by_name(game)
    home_score, away_score = scores.get(home_team), scores.get(away_team)
    if home_score is None or away_score is None:
        return None

    match = re.search(r"([+-]\d+(?:\.\d+)?)", pick_side)
    if not match:
        return None
    try:
        quoted_spread = Decimal(match.group(1))
    except (ValueError, InvalidOperation):
        return None

    pick_norm = picks_tracker.normalize_team_name(pick_side)
    home_norm = picks_tracker.normalize_team_name(home_team)
    away_norm = picks_tracker.normalize_team_name(away_team)

    if home_norm and home_norm in pick_norm:
        selected_score, opponent_score = home_score, away_score
    elif away_norm and away_norm in pick_norm:
        selected_score, opponent_score = away_score, home_score
    else:
        return None

    adjusted = selected_score + quoted_spread
    if adjusted > opponent_score:
        return "W"
    if adjusted < opponent_score:
        return "L"
    return "P"


def _extract_over_under_result(game: dict, pick_side: str) -> Optional[str]:
    scores = _scores_by_name(game)
    values = list(scores.values())
    if len(values) < 2:
        return None
    total_points = sum(values)

    match = re.search(r"(?:over|under)\s*(\d+(?:\.\d+)?)", pick_side, re.IGNORECASE)
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
        if total_points < ou_line:
            return "L"
        return "P"
    if "under" in pick_lower:
        if total_points < ou_line:
            return "W"
        if total_points > ou_line:
            return "L"
        return "P"
    return None


def match_pick_to_game(pick: Dict, completed_games: list) -> Tuple[Optional[str], Optional[str]]:
    """Match a canonical pending pick to a completed game and determine its
    outcome. Returns (result: 'W'/'L'/'P' or None, final_score: str or None)."""
    sport = (pick.get("sport") or "").strip().lower()
    matchup = pick.get("matchup") or ""
    side = pick.get("side") or ""
    sport_key = SPORT_KEY_MAP.get(sport)
    if not sport_key:
        return None, None

    for game in completed_games:
        if game.get("sport_key") != sport_key:
            continue
        home_team, away_team = game.get("home_team", ""), game.get("away_team", "")
        if not _match_teams_in_matchup(matchup, home_team, away_team):
            continue

        scores = _scores_by_name(game)
        home_score, away_score = scores.get(home_team), scores.get(away_team)
        final_score = (
            f"{away_team} {away_score} vs {home_team} {home_score}"
            if home_score is not None and away_score is not None else None
        )

        side_lower = side.lower()
        if "moneyline" in side_lower or " ml" in side_lower:
            result = _extract_moneyline_result(game, side)
        elif "over" in side_lower or "under" in side_lower:
            result = _extract_over_under_result(game, side)
        elif re.search(r"[+-]\d", side):
            result = _extract_spread_result(game, side)
        else:
            result = None

        return result, final_score

    return None, None


def auto_update_results() -> Dict:
    """Reconcile all pending canonical picks against completed games, apply
    every result in one database transaction, then perform exactly one
    canonical Sheets sync for the whole batch. Returns the actual sync
    status — never claims success it didn't achieve."""
    games = get_completed_games()
    pending = picks_tracker.get_pending_picks()

    if not games or not pending:
        return {"updated": 0, "pending": len(pending), "sheet_sync": {"status": "skipped", "reason": "nothing to reconcile"}}

    updates = []
    for pick in pending:
        result, final_score = match_pick_to_game(pick, games)
        if result:
            updates.append({"pick_id": pick["id"], "result": result, "final_score": final_score})

    updated = picks_tracker.batch_update_results(updates)
    logger.info("Applied %d result update(s) out of %d pending pick(s)", updated, len(pending))

    sync_result = picks_sync.sync_canonical_snapshot() if updated else {"status": "skipped", "reason": "no changes"}

    return {
        "updated": updated,
        "pending": len(pending) - updated,
        "total_games_checked": len(games),
        "sheet_sync": sync_result,
    }


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    result = auto_update_results()
    print(f"Result update complete: {result['updated']} updated, {result['pending']} still pending, "
          f"sheet_sync={result['sheet_sync'].get('status')}")
    sys.exit(0)
