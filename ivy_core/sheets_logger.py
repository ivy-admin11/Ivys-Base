"""Log picks and results to Google Sheets for tracking and analysis.

Integrates with Google Sheets API to append pick records and update results
in a shared spreadsheet for easy viewing and analysis.
"""

import os
import logging
from pathlib import Path
from typing import Optional

from google.oauth2.service_account import Credentials
from google.auth import default
from googleapiclient.discovery import build

from ivy_core.pick_stats import UNVERIFIABLE, summarize

logger = logging.getLogger("ivy.sheets_logger")

# Google Sheets API configuration
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Sheet IDs (from URL: /spreadsheets/d/{SPREADSHEET_ID}/edit)
#
# .env.example has documented SPORTS_DASHBOARD_SPREADSHEET_ID and
# GOOGLE_SHEET_ID since the pipeline was written, but both modules that
# talk to Sheets hardcoded the ID instead, so setting either variable did
# nothing. Env now wins; the literal stays as the default so an unset
# environment behaves exactly as before.
SPREADSHEET_ID = (
    os.getenv("SPORTS_DASHBOARD_SPREADSHEET_ID")
    or os.getenv("GOOGLE_SHEET_ID")
    or "1vxdAfvLyu3o3N-suV1qxX6KWbYZyCiQvNcYdOxePoHQ"
)
SHEET_NAME = "Sharp Picks"  # Dedicated tab for picks tracking

# The sheet's column order, in one place.
#
# This used to be written down three times and three different ways.
# scripts/sync_picks_to_sheet.py creates the header below; log_picks_to_sheet
# appended rows shifted one column right (ReportDate first); and
# update_result_in_sheet assumed a "Sharps" column that does not exist, so it
# wrote each grade into FinalScore while get_sheet_summary read grades from
# Result. The net effect was that a correctly graded pick never showed up as
# graded -- the dashboard counted it pending forever.
#
# Anything that reads or writes this tab derives its indices from here.
COLUMNS = (
    "Sport", "Matchup", "Side", "Odds", "Handicapper", "Confidence",
    "GameDay", "StartTime", "ReportDate", "Result", "FinalScore",
)
COL = {name: i for i, name in enumerate(COLUMNS)}
LAST_COLUMN_LETTER = chr(ord("A") + len(COLUMNS) - 1)  # "K"


def column_letter(name: str) -> str:
    """Spreadsheet letter for a named column, e.g. "Result" -> "J"."""
    return chr(ord("A") + COL[name])


def row_for_pick(pick: dict, report_date: str) -> list:
    """Build one sheet row in COLUMNS order."""
    return [
        pick.get("sport", ""),
        pick.get("matchup", ""),
        pick.get("side", ""),
        str(pick.get("odds", "")),
        pick.get("handicapper", ""),
        pick.get("confidence", ""),
        pick.get("game_day", ""),
        pick.get("start_time", ""),
        report_date,
        "",  # Result -- filled in later by update_result_in_sheet
        "",  # FinalScore
    ]


def _get_sheets_service():
    """Get authenticated Google Sheets API service."""
    try:
        # Try service account first (for automated deployments)
        for cred_path in [
            Path("~/openclaw-admin/service-account-key.json").expanduser(),
            Path("~/ai-admin-api/service-account-key.json").expanduser(),
            Path("~/ai-admin-api/google_credentials.json").expanduser(),
            Path("~/openclaw-admin/google_credentials.json").expanduser(),
            Path("/Users/lexi/Ivys-Base/google_credentials.json"),
        ]:
            if cred_path.exists():
                credentials = Credentials.from_service_account_file(
                    str(cred_path), scopes=SCOPES
                )
                service = build("sheets", "v4", credentials=credentials)
                logger.debug(f"Authenticated via service account: {cred_path}")
                return service
        
        # Fallback to user OAuth (e.g., for local testing)
        credentials, project = default(scopes=SCOPES)
        service = build("sheets", "v4", credentials=credentials)
        logger.debug("Authenticated via default credentials")
        return service
    except Exception as e:
        logger.warning(f"Could not authenticate with Google Sheets: {e}")
        return None


def log_picks_to_sheet(picks: list, report_date: str):
    """Append picks to the Google Sheet for record tracking.
    
    Args:
        picks: List of pick dicts with sport, matchup, side, odds, handicapper, etc.
        report_date: Date the picks were reported (YYYY-MM-DD)
    """
    service = _get_sheets_service()
    if not service:
        logger.warning("Skipping Google Sheets logging: no authentication available")
        return
    
    try:
        # Prepare rows for the sheet
        rows = [row_for_pick(pick, report_date) for pick in picks]
        
        # Append rows to the sheet
        body = {"values": rows}
        service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{SHEET_NAME}!A:{LAST_COLUMN_LETTER}",
            valueInputOption="USER_ENTERED",
            body=body,
        ).execute()
        
        logger.info(f"Logged {len(picks)} picks to Google Sheet")
    except Exception as e:
        logger.error(f"Failed to log picks to Google Sheet: {e}")


def update_result_in_sheet(matchup: str, side: str, result: str, notes: Optional[str] = None):
    """Update the result column for a specific pick in the export sheet.
    
    Args:
        matchup: The matchup identifier (e.g., "Kansas City Chiefs @ Buffalo Bills")
        side: The pick side (e.g., "Kansas City Chiefs -2.5")
        result: The outcome (W/L/P)
        notes: Optional notes (final score, reason, etc.)
    """
    service = _get_sheets_service()
    if not service:
        logger.warning("Skipping Google Sheets update: no authentication available")
        return
    
    try:
        # Update export sheet (Sharp Picks tab, gid=1305096861)
        export_sheet = "Sharp Picks"
        
        # Read the current sheet to find the matching row
        result_obj = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{export_sheet}'!A:{LAST_COLUMN_LETTER}",
        ).execute()
        
        values = result_obj.get("values", [])
        
        # Find the matching row (indices come from COLUMNS)
        for row_idx, row in enumerate(values[1:], start=2):  # Skip header
            if len(row) >= 3:
                row_matchup = row[COL["Matchup"]].lower() if len(row) > COL["Matchup"] else ""
                row_side = row[COL["Side"]].lower() if len(row) > COL["Side"] else ""
                
                if row_matchup == matchup.lower() and row_side == side.lower():
                    update_range = f"'{export_sheet}'!{column_letter('Result')}{row_idx}"
                    update_body = {"values": [[result]]}
                    service.spreadsheets().values().update(
                        spreadsheetId=SPREADSHEET_ID,
                        range=update_range,
                        valueInputOption="USER_ENTERED",
                        body=update_body,
                    ).execute()
                    
                    if notes:
                        notes_range = f"'{export_sheet}'!{column_letter('FinalScore')}{row_idx}"
                        notes_body = {"values": [[notes]]}
                        service.spreadsheets().values().update(
                            spreadsheetId=SPREADSHEET_ID,
                            range=notes_range,
                            valueInputOption="USER_ENTERED",
                            body=notes_body,
                        ).execute()
                    
                    logger.info(f"Updated export sheet: {matchup} {side} to {result}")
                    return
        
        logger.warning(f"Pick not found in export sheet: {matchup} {side}")
    except Exception as e:
        logger.error(f"Failed to update result in Google Sheet: {e}")


def get_sheet_summary():
    """Get summary stats from the Google Sheet."""
    service = _get_sheets_service()
    if not service:
        logger.warning("Could not authenticate for sheet summary")
        return None
    
    try:
        result_obj = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{SHEET_NAME}!A:K",
        ).execute()
        
        values = result_obj.get("values", [])
        if not values or len(values) < 2:  # Header + at least one data row
            return None
        
        # Count results in the Result column
        result_idx = COL["Result"]
        tally = {"wins": 0, "losses": 0, "pushes": 0, "pending": 0, "unverifiable": 0}
        key = {"W": "wins", "L": "losses", "P": "pushes", UNVERIFIABLE: "unverifiable"}
        
        for row in values[1:]:  # Skip header
            # Rows arrive ragged: Sheets omits trailing empty cells.
            result = row[result_idx].upper().strip() if len(row) > result_idx else ""
            tally[key.get(result, "pending")] += 1
        
        return summarize(**tally)
    except Exception as e:
        logger.error(f"Failed to get sheet summary: {e}")
        return None
