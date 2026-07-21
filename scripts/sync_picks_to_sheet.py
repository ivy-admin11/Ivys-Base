#!/usr/bin/env python3
"""Thin CLI around the canonical Sharp Picks -> Google Sheets snapshot sync.

Never writes directly to Sheets itself — delegates entirely to
ivy_core.picks_sync.sync_canonical_snapshot(), which is also invoked
automatically from the scheduled Sharp Picks pipeline and from
ivy_core.result_updater after a result reconciliation batch.
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from ivy_core import picks_tracker, picks_sync  # noqa: E402


def _dry_run() -> int:
    rows = picks_tracker.get_canonical_snapshot_rows()
    overall = picks_tracker.get_stats_overall()
    print(f"Dry run — no network writes.")
    print(f"Configured spreadsheet: {config.GOOGLE_SHEETS_SPREADSHEET_ID or '(not configured)'}")
    print(f"Picks tab: {config.GOOGLE_SHEETS_PICKS_TAB}")
    print(f"Summary tab: {config.GOOGLE_SHEETS_SUMMARY_TAB}")
    print(f"Canonical picks: {len(rows)}")
    print(f"Overall record: {overall['record']} (resolved={overall['resolved']}, pending={overall['pending']})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync canonical Sharp Picks to Google Sheets")
    parser.add_argument("--dry-run", action="store_true", help="Validate local data only — no network writes")
    args = parser.parse_args()

    if args.dry_run:
        return _dry_run()

    result = picks_sync.sync_canonical_snapshot()
    print(result)
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
