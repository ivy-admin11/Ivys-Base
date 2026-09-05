#!/usr/bin/env python3
"""Make the picks dashboard say only what is actually known.

The grading pipeline was broken at five points (see
claude/grading-pipeline-repair.md). The code is fixed, but the record it
already wrote is not, and most of it cannot be recovered: The Odds API's
scores endpoint serves roughly the last two days, so a pick whose game is
older than that can never be graded, by this or any other tool.

So this does not try to reconstruct the record. It makes the dashboard stop
implying it has one:

  1. Re-grade what is still inside the scores window (optional, needs network
     and ODDS_API_KEY -- skipped with --no-regrade).
  2. Mark every older ungraded pick "U" (unverifiable) with the reason, so it
     stops being counted as a result that is still coming.
  3. Rebuild the sheet from the database, which is the authoritative store,
     in the one column layout everything now agrees on.

Dry run by default. Nothing is written without --apply.

Usage:
    ./scripts/repair_dashboard.py                  # show what would change
    ./scripts/repair_dashboard.py --apply
    ./scripts/repair_dashboard.py --apply --no-regrade --no-sheet
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Importing config is what loads .env -- it owns the load_dotenv call. Nothing
# under ivy_core does it, so a script that skips this import runs with an
# empty environment and reports "ODDS_API_KEY not set" while the key is
# sitting in .env. That is exactly what happened on the first real run.
import config  # noqa: E402,F401  (imported for its load_dotenv side effect)
from ivy_core.pick_stats import UNVERIFIABLE  # noqa: E402
from ivy_core.picks_tracker import PICKS_DB  # noqa: E402

# The scores endpoint is called with daysFrom=2. Three days of slack keeps a
# pick gradeable through a late finish and a missed run before it is written
# off; anything older has no source of truth left.
SCORES_WINDOW_DAYS = 3

UNVERIFIABLE_NOTE = "ungradeable: game outside the scores window when grading was repaired"


def pick_date(game_day: str | None, report_date: str | None) -> str | None:
    """The date a pick's game was played, as best the row records it."""
    for value in (game_day, report_date):
        if not value:
            continue
        text = str(value).strip()[:10]
        try:
            datetime.strptime(text, "%Y-%m-%d")
            return text
        except ValueError:
            continue
    return None


def find_unverifiable(conn, *, today: str, window_days: int = SCORES_WINDOW_DAYS):
    """Ungraded picks whose games are too old to ever be scored."""
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=window_days)).date()
    rows = conn.execute("""
        SELECT p.id, p.report_date, p.game_day, p.sport, p.matchup, p.side
        FROM picks p
        LEFT JOIN results r ON r.pick_id = p.id
        WHERE r.result IS NULL
        ORDER BY p.id
    """).fetchall()

    stale, recent, undated = [], [], []
    for row in rows:
        when = pick_date(row[2], row[1])
        if when is None:
            undated.append(row)
        elif datetime.strptime(when, "%Y-%m-%d").date() < cutoff:
            stale.append(row)
        else:
            recent.append(row)
    return stale, recent, undated


def mark_unverifiable(conn, rows, *, apply: bool) -> int:
    if not apply:
        return len(rows)
    conn.executemany(
        """UPDATE results
           SET result = ?, final_score = COALESCE(final_score, ?),
               resolved_at = CURRENT_TIMESTAMP
           WHERE pick_id = ? AND result IS NULL""",
        [(UNVERIFIABLE, UNVERIFIABLE_NOTE, r[0]) for r in rows],
    )
    conn.commit()
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually write changes")
    ap.add_argument("--no-regrade", action="store_true",
                    help="skip the API re-grade of recent picks")
    ap.add_argument("--no-sheet", action="store_true",
                    help="skip rebuilding the Google Sheet")
    ap.add_argument("--window-days", type=int, default=SCORES_WINDOW_DAYS)
    ap.add_argument("--today", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    help="override today's date (testing)")
    args = ap.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN (pass --apply to write)"
    print(f"Dashboard repair — {mode}")
    print(f"  database: {PICKS_DB}")

    if not Path(PICKS_DB).exists():
        print("  no database; nothing to repair")
        return 0

    # ---- 1. re-grade what is still reachable ------------------------------
    if args.no_regrade:
        print("\n1. Re-grade recent picks: skipped (--no-regrade)")
    else:
        print("\n1. Re-grade recent picks")
        try:
            from ivy_core.result_updater import auto_update_results
            if args.apply:
                outcome = auto_update_results()
                print(f"   graded {outcome.get('updated', 0)}; "
                      f"{outcome.get('pending', 0)} still awaiting results")
            else:
                print("   would call auto_update_results() with the repaired grader")
        except Exception as exc:
            print(f"   could not re-grade: {exc}")
            print("   (needs network and ODDS_API_KEY; the rest of the repair still runs)")
        else:
            if args.apply and not os.getenv("ODDS_API_KEY"):
                print("   NOTE: ODDS_API_KEY is not set even after loading .env —")
                print("         nothing can be graded until it is. If the key was")
                print("         rotated, put the new one in .env and re-run.")

    # ---- 2. write off what can never be graded ----------------------------
    conn = sqlite3.connect(PICKS_DB)
    try:
        stale, recent, undated = find_unverifiable(
            conn, today=args.today, window_days=args.window_days
        )
        print(f"\n2. Ungraded picks, as of {args.today}")
        print(f"   {len(recent)} still inside the {args.window_days}-day scores window — left pending")
        print(f"   {len(undated)} with no usable date — left pending")
        print(f"   {len(stale)} older than the window — {'marking' if args.apply else 'would mark'} unverifiable")
        for row in stale[:5]:
            print(f"      #{row[0]} {pick_date(row[2], row[1])} {row[4]} — {row[5]}")
        if len(stale) > 5:
            print(f"      ... and {len(stale) - 5} more")
        marked = mark_unverifiable(conn, stale, apply=args.apply)

        from ivy_core.picks_tracker import format_stats_for_pdf, get_stats_overall
        overall = get_stats_overall(days_back=3650)
        print("\n3. Record after repair" if args.apply else "\n3. Record as it stands now")
        for line in format_stats_for_pdf(days_back=3650).splitlines():
            print(f"   {line}")
    finally:
        conn.close()

    # ---- 3. rebuild the sheet from the database ---------------------------
    if args.no_sheet:
        print("\n4. Rebuild sheet: skipped (--no-sheet)")
    else:
        print("\n4. Rebuild sheet from the database")
        try:
            from ivy_core.picks_tracker import auto_sync_to_export_sheet
            if args.apply:
                auto_sync_to_export_sheet()
                print("   sheet rewritten from the database")
            else:
                print("   would clear the tab and rewrite every row from the database")
        except Exception as exc:
            print(f"   could not rebuild: {exc}")
            print("   (needs network and Google credentials)")

    if not args.apply:
        print(f"\nDry run. {marked} pick(s) would be marked unverifiable. Re-run with --apply.")
    else:
        print(f"\nDone. {marked} pick(s) marked unverifiable; "
              f"{overall['wins']}W-{overall['losses']}L-{overall['pushes']}P stands as the real record.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
