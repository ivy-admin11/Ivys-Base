"""Log picks and results to Google Sheets for tracking and analysis.

Integrates with Google Sheets API to append pick records and update results
in a shared spreadsheet for easy viewing and analysis.
"""

import json
import logging
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials
from google.auth import default
from googleapiclient.discovery import build

logger = logging.getLogger("ivy.sheets_logger")

# Google Sheets API configuration
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Sheet IDs (from URL: /spreadsheets/d/{SPREADSHEET_ID}/edit)
SPREADSHEET_ID = "1vxdAfvLyu3o3N-suV1qxX6KWbYZyCiQvNcYdOxePoHQ"
SHEET_NAME = "Sharp Picks"  # Dedicated tab for picks tracking


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
        rows = []
        for pick in picks:
            row = [
                report_date,
                pick.get("sport", ""),
                pick.get("matchup", ""),
                pick.get("side", ""),
                str(pick.get("odds", "")),
                pick.get("handicapper", ""),
                pick.get("confidence", ""),
                pick.get("game_day", ""),
                pick.get("start_time", ""),
                "",  # Result (empty for new picks)
                "",  # Notes
            ]
            rows.append(row)
        
        # Append rows to the sheet
        body = {"values": rows}
        service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{SHEET_NAME}!A:K",  # A=Date, B=Sport, C=Matchup, D=Side, E=Odds, F=Handicapper, G=Confidence, H=GameDay, I=StartTime, J=Result, K=Notes
            valueInputOption="USER_ENTERED",
            body=body,
        ).execute()
        
        logger.info(f"Logged {len(picks)} picks to Google Sheet")
    except Exception as e:
        logger.error(f"Failed to log picks to Google Sheet: {e}")


def update_result_in_sheet(matchup: str, side: str, result: str, notes: Optional[str] = None):
    """Update the result column for a specific pick in the sheet.
    
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
        # Read the current sheet to find the matching row
        result_obj = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{SHEET_NAME}!A:K",
        ).execute()
        
        values = result_obj.get("values", [])
        
        # Find the matching row (matchup in column C, side in column D)
        for row_idx, row in enumerate(values[1:], start=2):  # Skip header
            if len(row) >= 4 and row[2].lower() == matchup.lower() and row[3].lower() == side.lower():
                # Found matching pick; update result (column J = index 9) and notes (column K = index 10)
                update_range = f"{SHEET_NAME}!J{row_idx}"
                update_body = {"values": [[result]]}
                service.spreadsheets().values().update(
                    spreadsheetId=SPREADSHEET_ID,
                    range=update_range,
                    valueInputOption="USER_ENTERED",
                    body=update_body,
                ).execute()
                
                if notes:
                    notes_range = f"{SHEET_NAME}!K{row_idx}"
                    notes_body = {"values": [[notes]]}
                    service.spreadsheets().values().update(
                        spreadsheetId=SPREADSHEET_ID,
                        range=notes_range,
                        valueInputOption="USER_ENTERED",
                        body=notes_body,
                    ).execute()
                
                logger.info(f"Updated {matchup} {side} to {result}")
                return
        
        logger.warning(f"Pick not found in sheet: {matchup} {side}")
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
        
        # Count results in column J (index 9)
        wins = losses = pushes = pending = 0
        
        for row in values[1:]:  # Skip header
            # Handle rows with fewer columns
            result = row[9].upper().strip() if len(row) > 9 else ""
            
            if result == "W":
                wins += 1
            elif result == "L":
                losses += 1
            elif result == "P":
                pushes += 1
            else:  # Empty or unrecognized
                pending += 1
        
        hit_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
        
        return {
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "pending": pending,
            "hit_rate": hit_rate,
            "total": wins + losses + pushes + pending,
        }
    except Exception as e:
        logger.error(f"Failed to get sheet summary: {e}")
        return None
