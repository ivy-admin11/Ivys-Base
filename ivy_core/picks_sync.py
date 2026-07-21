"""Orchestrates the canonical Sharp Picks database -> Google Sheets snapshot.

Queries ivy_core.picks_tracker for canonical rows + statistics, then calls
ivy_core.sheets_logger for the low-level writes. Serialized with filelock so
concurrent callers (a scheduled run + an ad-hoc CLI sync) can't race.
"""

import logging
import os
from pathlib import Path
from typing import Dict

from filelock import FileLock, Timeout

from ivy_core import picks_tracker, sheets_logger

logger = logging.getLogger("ivy.picks_sync")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOCK_PATH = PROJECT_ROOT / "data" / "sheets_sync.lock"

PICKS_HEADER = [
    "Sport", "Matchup", "Side", "Odds", "Handicapper", "Confidence",
    "GameDay", "StartTime", "ReportDate", "Sharps", "Result", "FinalScore",
    "DatabaseID", "PickKey",
]

SUMMARY_HEADER = [
    "Scope", "Wins", "Losses", "Pushes", "Pending", "Resolved", "Record",
    "WinRatio", "LossRatio", "PushRatio", "DecisiveHitRate",
]


def _picks_rows():
    rows = []
    for r in picks_tracker.get_canonical_snapshot_rows():
        rows.append([
            r.get("sport") or "",
            r.get("matchup") or "",
            r.get("side") or "",
            str(r.get("odds")) if r.get("odds") is not None else "",
            r.get("handicapper") or "",
            r.get("confidence") or "",
            r.get("game_day") or "",
            r.get("start_time") or "",
            r.get("report_date") or "",
            str(r.get("sharp_count") or 1),
            r.get("result") or "",
            r.get("final_score") or "",
            r.get("id"),
            r.get("pick_key") or "",
        ])
    return rows


def _summary_row(scope: str, stats: Dict):
    return [
        scope, stats["wins"], stats["losses"], stats["pushes"], stats["pending"],
        stats["resolved"], stats["record"], stats["win_ratio"], stats["loss_ratio"],
        stats["push_ratio"], stats["decisive_hit_rate"],
    ]


def _summary_rows():
    rows = [_summary_row("Overall", picks_tracker.get_stats_overall())]
    for sport, stats in sorted(picks_tracker.get_stats_by_sport().items()):
        rows.append(_summary_row(sport or "Unknown", stats))
    return rows


def sync_canonical_snapshot() -> Dict:
    """Write the one canonical picks snapshot + W/L/P summary. Never reports
    success after catching an exception — any failure returns a structured
    error/not_configured result instead."""
    os.makedirs(_LOCK_PATH.parent, exist_ok=True)
    lock = FileLock(str(_LOCK_PATH), timeout=30)
    try:
        lock.acquire()
    except Timeout:
        return {"status": "skipped", "reason": "another sync already in progress"}

    try:
        picks_rows = _picks_rows()
        picks_result = sheets_logger.write_snapshot(PICKS_HEADER, picks_rows)
        if picks_result.get("status") != "success":
            return {
                "status": picks_result.get("status", "error"),
                "reason": picks_result.get("reason"),
                "canonical_row_count": len(picks_rows),
                "summary_status": "skipped",
            }

        summary_rows = _summary_rows()
        summary_result = sheets_logger.write_summary(SUMMARY_HEADER, summary_rows)

        return {
            "status": "success",
            "canonical_row_count": len(picks_rows),
            "summary_status": summary_result.get("status", "error"),
        }
    except Exception as exc:
        logger.error("sync_canonical_snapshot failed: %s", exc)
        return {"status": "error", "reason": "unexpected_exception"}
    finally:
        lock.release()
