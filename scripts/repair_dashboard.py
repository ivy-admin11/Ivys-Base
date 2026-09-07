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
import subprocess
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
    ap.add_argument("--no-backfill", action="store_true",
                    help="skip grading old picks from historical scoreboards")
    ap.add_argument("--no-sheet", action="store_true",
                    help="skip rebuilding the Google Sheet")
    ap.add_argument("--window-days", type=int, default=SCORES_WINDOW_DAYS)
    ap.add_argument("--today", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    help="override today's date (testing)")
    args = ap.parse_args()

    sheet_ok = True
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

    # ---- 2a. backfill old picks from historical scoreboards ---------------
    if args.no_backfill:
        print("\n2b. Backfill from historical scoreboards: skipped (--no-backfill)")
    else:
        print("\n2b. Backfill from historical scoreboards")
        conn = sqlite3.connect(PICKS_DB)
        try:
            rows = conn.execute("""
                SELECT p.id, p.sport, p.matchup, p.side, p.game_day, p.report_date
                FROM picks p LEFT JOIN results r ON r.pick_id = p.id
                WHERE r.result IS NULL OR r.result = ?
                ORDER BY p.id
            """, (UNVERIFIABLE,)).fetchall()
        finally:
            conn.close()

        from ivy_core.historical_scores import SPORT_ROUTES
        from ivy_core.result_updater import backfill_pick
        from ivy_core.picks_tracker import update_pick_result

        cache, graded, unreachable, unsupported = {}, 0, 0, 0
        for pid, sport, matchup, side, game_day, report_date in rows:
            if (sport or "").strip().lower() not in SPORT_ROUTES:
                unsupported += 1
                continue
            pick = {"sport": sport, "matchup": matchup, "side": side,
                    "game_day": game_day, "report_date": report_date}
            try:
                result, final = backfill_pick(pick, cache)
            except Exception as exc:
                print(f"   #{pid}: {type(exc).__name__}: {exc}")
                result = None
            if not result:
                unreachable += 1
                continue
            graded += 1
            if args.apply:
                update_pick_result(pid, result, final)
            print(f"   #{pid} {matchup} — {side} = {result}"
                  + ("" if args.apply else "  (dry run)"))

        print(f"   {graded} gradeable, {unreachable} no match found, "
              f"{unsupported} in a sport with no scoreboard route")
        if unsupported:
            print(f"   supported: {', '.join(sorted(SPORT_ROUTES))}")

    # ---- 2b. reconcile grades the sheet never received --------------------
    from ivy_core.picks_tracker import resync_grades, unsynced_grades

    missing = unsynced_grades()
    print(f"\n3b. Grades recorded but missing from the sheet: {len(missing)}")
    for row in missing[:5]:
        print(f"      #{row['pick_id']} {row['matchup']} — {row['side']} = {row['result']}")
    if len(missing) > 5:
        print(f"      ... and {len(missing) - 5} more")
    if missing and not args.no_sheet:
        # The rebuild below rewrites every row from this database, so it
        # resolves all of these at once; a targeted retry would be wasted work.
        print("   the rebuild in step 4 rewrites every row, which covers these")
    elif missing and args.apply:
        outcome = resync_grades()
        print(f"   reconciled {outcome['fixed']}; {outcome['still_missing']} still missing")
        if outcome["still_missing"]:
            sheet_ok = False
    elif missing:
        print("   would retry each against the sheet")

    # ---- 3. rebuild the sheet from the database ---------------------------
    if args.no_sheet:
        print("\n4. Rebuild sheet: skipped (--no-sheet)")
    else:
        print("\n4. Rebuild sheet from the database")
        if not args.apply:
            print("   would clear the tab and rewrite every row from the database")
        else:
            # scripts/sync_picks_to_sheet.py is the rebuild: it clears the tab
            # and rewrites header and rows. picks_tracker.auto_sync_to_export_sheet
            # only appends, and swallows its own failures -- calling that here
            # is what let two runs report a rebuild that had 404'd.
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parent / "sync_picks_to_sheet.py")],
                cwd=str(Path(__file__).resolve().parents[1]),
                capture_output=True, text=True,
            )
            for line in (proc.stdout or "").splitlines():
                print(f"   {line}")
            if proc.returncode == 0:
                from ivy_core.picks_tracker import mark_all_synced
                marked = mark_all_synced()
                print(f"   sheet rewritten from the database "
                      f"({marked} grade(s) confirmed present)")
            else:
                sheet_ok = False
                print(f"   REBUILD FAILED (exit {proc.returncode}) — the sheet was NOT updated")
                for line in (proc.stderr or "").splitlines()[-6:]:
                    print(f"   {line}")

    if not args.apply:
        print(f"\nDry run. {marked} pick(s) would be marked unverifiable. Re-run with --apply.")
    else:
        print(f"\nDone. {marked} pick(s) marked unverifiable; "
              f"{overall['wins']}W-{overall['losses']}L-{overall['pushes']}P stands as the real record.")
        if not sheet_ok:
            print("The database is correct but the SHEET IS STALE — fix the error above and re-run.")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
