"""Google Sheets authentication and low-level snapshot writes.

No business logic here — schema/statistics/business rules live in
ivy_core.picks_tracker; orchestration lives in ivy_core.picks_sync. This
module only knows how to authenticate and write/clear ranges.
"""

import logging
from typing import Dict, List, Optional

import config

logger = logging.getLogger("ivy.sheets_logger")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _not_configured(reason: str) -> Dict:
    return {"status": "not_configured", "reason": reason}


def get_sheets_service():
    """Authenticated Sheets API client, or None if unavailable.

    Honors GOOGLE_SERVICE_ACCOUNT_KEY when set; otherwise falls back to
    Application Default Credentials. Never logs credential contents,
    authorization headers, or key lengths."""
    try:
        from google.oauth2.service_account import Credentials
        from google.auth import default
        from googleapiclient.discovery import build
    except ImportError:
        logger.warning("Google API client libraries not installed — Sheets sync unavailable.")
        return None

    key_path = config.GOOGLE_SERVICE_ACCOUNT_KEY
    try:
        if key_path:
            credentials = Credentials.from_service_account_file(key_path, scopes=SCOPES)
            logger.debug("Authenticated via configured service account key.")
        else:
            credentials, _project = default(scopes=SCOPES)
            logger.debug("Authenticated via Application Default Credentials.")
        return build("sheets", "v4", credentials=credentials)
    except Exception:
        logger.warning("Could not authenticate with Google Sheets.")
        return None


def _spreadsheet_and_tab_ready(service, spreadsheet_id: str, tab_name: str) -> bool:
    """Verify the configured spreadsheet and tab exist before writing."""
    try:
        spreadsheet = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    except Exception:
        logger.warning("Could not read configured spreadsheet to verify tab %r.", tab_name)
        return False
    return any(s["properties"]["title"] == tab_name for s in spreadsheet.get("sheets", []))


def write_snapshot(header: List[str], rows: List[List], tab_name: Optional[str] = None) -> Dict:
    """Overwrite the header + data rows of `tab_name` with RAW values (never
    USER_ENTERED — externally sourced pick text must never become a
    spreadsheet formula), then clear any stale trailing rows beyond what was
    just written. The new snapshot is written successfully before any
    clearing happens, so a failed write never destroys existing data."""
    spreadsheet_id = config.GOOGLE_SHEETS_SPREADSHEET_ID
    tab = tab_name or config.GOOGLE_SHEETS_PICKS_TAB
    if not spreadsheet_id:
        return _not_configured("GOOGLE_SHEETS_SPREADSHEET_ID is not set")

    service = get_sheets_service()
    if not service:
        return _not_configured("Google Sheets authentication is unavailable")

    if not _spreadsheet_and_tab_ready(service, spreadsheet_id, tab):
        return _not_configured(f"tab {tab!r} not found in configured spreadsheet")

    n_cols = len(header)
    last_col = chr(ord("A") + n_cols - 1) if n_cols <= 26 else "Z"
    all_values = [header] + rows

    try:
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'{tab}'!A1:{last_col}{len(all_values)}",
            valueInputOption="RAW",
            body={"values": all_values},
        ).execute()

        # Only clear stale trailing rows AFTER the new snapshot write succeeds.
        clear_from = len(all_values) + 1
        service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=f"'{tab}'!A{clear_from}:{last_col}",
        ).execute()
    except Exception as exc:
        logger.error("Sheets snapshot write failed for tab %r: %s", tab, exc)
        return {"status": "error", "reason": "sheets_write_failed"}

    return {"status": "success", "rows_written": len(rows), "tab": tab}


def write_summary(header: List[str], rows: List[List], tab_name: Optional[str] = None) -> Dict:
    """Overwrite the W/L/P summary tab with RAW values."""
    spreadsheet_id = config.GOOGLE_SHEETS_SPREADSHEET_ID
    tab = tab_name or config.GOOGLE_SHEETS_SUMMARY_TAB
    if not spreadsheet_id:
        return _not_configured("GOOGLE_SHEETS_SPREADSHEET_ID is not set")

    service = get_sheets_service()
    if not service:
        return _not_configured("Google Sheets authentication is unavailable")

    if not _spreadsheet_and_tab_ready(service, spreadsheet_id, tab):
        return _not_configured(f"tab {tab!r} not found in configured spreadsheet")

    n_cols = len(header)
    last_col = chr(ord("A") + n_cols - 1) if n_cols <= 26 else "Z"
    all_values = [header] + rows

    try:
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'{tab}'!A1:{last_col}{len(all_values)}",
            valueInputOption="RAW",
            body={"values": all_values},
        ).execute()
        clear_from = len(all_values) + 1
        service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=f"'{tab}'!A{clear_from}:{last_col}",
        ).execute()
    except Exception as exc:
        logger.error("Sheets summary write failed for tab %r: %s", tab, exc)
        return {"status": "error", "reason": "sheets_write_failed"}

    return {"status": "success", "rows_written": len(rows), "tab": tab}
