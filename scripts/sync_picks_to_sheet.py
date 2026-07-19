#!/usr/bin/env python3
"""Extract all Sharp Picks from database and sync to Google Sheets.

This script reads the picks database (data/picks.db) and populates a target
Google Sheet with all picks for easy sharing and analysis.
"""

import sqlite3
import sys
from pathlib import Path
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SPREADSHEET_ID = "1vxdAfvLyu3o3N-suV1qxX6KWbYZyCiQvNcYdOxePoHQ"
TARGET_SHEET_GID = 1305096861  # From URL: gid=1305096861


def get_sheets_service():
    """Authenticate with Google Sheets API."""
    try:
        cred_path = Path("~/openclaw-admin/service-account-key.json").expanduser()
        if not cred_path.exists():
            cred_path = Path("~/ai-admin-api/service-account-key.json").expanduser()
        
        credentials = Credentials.from_service_account_file(str(cred_path), scopes=SCOPES)
        return build("sheets", "v4", credentials=credentials)
    except Exception as e:
        print(f"❌ Failed to authenticate with Google Sheets: {e}")
        return None


def get_picks_from_db(db_path="data/picks.db"):
    """Extract all picks from the database."""
    conn = sqlite3.connect(db_path)
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
            COALESCE(r.result, '') as result,
            COALESCE(r.final_score, '') as final_score
        FROM picks p
        LEFT JOIN results r ON p.id = r.pick_id
        ORDER BY p.created_at
    """)
    
    picks = cursor.fetchall()
    conn.close()
    return picks


def format_picks_for_sheet(picks):
    """Convert database picks to sheet rows."""
    rows = []
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
            pick[10],  # result
            pick[11],  # final_score
        ]
        rows.append(row)
    return rows


def find_target_sheet(service, spreadsheet_id, target_gid):
    """Find the sheet name matching the target gid."""
    try:
        spreadsheet = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        for sheet in spreadsheet['sheets']:
            if sheet['properties']['sheetId'] == target_gid:
                return sheet['properties']['title']
    except Exception as e:
        print(f"❌ Failed to get spreadsheet: {e}")
    return None


def sync_picks_to_sheet(service, spreadsheet_id, sheet_name, rows):
    """Write picks to the Google Sheet."""
    try:
        header = ["Sport", "Matchup", "Side", "Odds", "Handicapper", "Confidence", "GameDay", "StartTime", "ReportDate", "Result", "FinalScore"]
        
        # Clear existing data (keep header)
        service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!A2:K"
        ).execute()
        
        # Write header
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!A1:K1",
            valueInputOption="USER_ENTERED",
            body={"values": [header]}
        ).execute()
        
        # Write all picks
        if rows:
            service.spreadsheets().values().append(
                spreadsheetId=spreadsheet_id,
                range=f"{sheet_name}!A2:K",
                valueInputOption="USER_ENTERED",
                body={"values": rows}
            ).execute()
            return len(rows)
        return 0
    except Exception as e:
        print(f"❌ Failed to sync picks to sheet: {e}")
        return None


def main():
    """Extract picks and sync to Google Sheets."""
    print("🔄 Extracting Sharp Picks...")
    
    # Get picks from database
    picks = get_picks_from_db()
    print(f"📊 Found {len(picks)} picks in database")
    
    if not picks:
        print("⚠️ No picks to export")
        return 1
    
    # Format for sheet
    rows = format_picks_for_sheet(picks)
    
    # Authenticate with Google Sheets
    service = get_sheets_service()
    if not service:
        return 1
    
    # Find target sheet
    sheet_name = find_target_sheet(service, SPREADSHEET_ID, TARGET_SHEET_GID)
    if not sheet_name:
        print(f"❌ Sheet with gid {TARGET_SHEET_GID} not found")
        return 1
    
    print(f"✅ Target sheet: {sheet_name}")
    
    # Sync to sheet
    count = sync_picks_to_sheet(service, SPREADSHEET_ID, sheet_name, rows)
    if count is not None:
        print(f"✅ Populated {count} picks into {sheet_name}")
        
        # Summary stats
        sports_count = {}
        for pick in picks:
            sport = pick[1]
            sports_count[sport] = sports_count.get(sport, 0) + 1
        
        print("\n📈 Breakdown by sport:")
        for sport, count in sorted(sports_count.items(), key=lambda x: x[1], reverse=True):
            print(f"   {sport}: {count}")
        
        return 0
    else:
        return 1


if __name__ == "__main__":
    sys.exit(main())
