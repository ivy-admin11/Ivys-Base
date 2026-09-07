#!/usr/bin/env python3
"""Show what ESPN actually returns, so the parser stops being guesswork.

The backfill reports "0 gradeable, 75 no match found" with no errors, which
could mean any of: no events came back, events came back but none looked
completed, or they looked completed but their team names do not match the
matchup strings in the database. Those need different fixes, and nothing in
the sandbox this was written in can reach ESPN to tell them apart.

Prints the raw shape and stops. It writes nothing.

    ./scripts/diagnose_espn.py                 # a date from the real backlog
    ./scripts/diagnose_espn.py MLB 2026-07-19
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402,F401
from ivy_core.historical_scores import (  # noqa: E402
    ESPN_HEADERS,
    ESPN_HOSTS,
    SPORT_ROUTES,
    normalise_event,
)
from ivy_core.picks_tracker import PICKS_DB  # noqa: E402


def pick_a_target():
    """A sport/date that actually has ungraded picks."""
    conn = sqlite3.connect(PICKS_DB)
    try:
        row = conn.execute("""
            SELECT p.sport, p.report_date, COUNT(*)
            FROM picks p LEFT JOIN results r ON r.pick_id = p.id
            WHERE (r.result IS NULL OR r.result = 'U') AND LOWER(p.sport) = 'mlb'
            GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 1
        """).fetchone()
    finally:
        conn.close()
    from ivy_core.pick_stats import resolve_pick_date

    if row:
        # The first version of this returned game_day unchecked and asked ESPN
        # for ?dates=today, which is a 400. That is how the real bug surfaced.
        day = resolve_pick_date(row[1], None)
        if day:
            return row[0], day
    conn = sqlite3.connect(PICKS_DB)
    try:
        alt = conn.execute("""
            SELECT p.sport, p.report_date FROM picks p
            LEFT JOIN results r ON r.pick_id = p.id
            WHERE (r.result IS NULL OR r.result = 'U') AND LOWER(p.sport) = 'mlb'
            ORDER BY p.id LIMIT 1
        """).fetchone()
    finally:
        conn.close()
    if alt and resolve_pick_date(alt[1], None):
        return alt[0], resolve_pick_date(alt[1], None)
    return "MLB", "2026-07-19"


def matchups_for(sport, day):
    conn = sqlite3.connect(PICKS_DB)
    try:
        return [r[0] for r in conn.execute("""
            SELECT DISTINCT p.matchup FROM picks p
            LEFT JOIN results r ON r.pick_id = p.id
            WHERE (r.result IS NULL OR r.result = 'U')
              AND LOWER(p.sport) = LOWER(?)
              AND COALESCE(NULLIF(p.game_day,''), p.report_date) LIKE ?
        """, (sport, f"{day}%"))]
    finally:
        conn.close()


def main() -> int:
    import requests

    if len(sys.argv) >= 3:
        sport, day = sys.argv[1], sys.argv[2]
    else:
        sport, day = pick_a_target()
    print(f"Sport: {sport}   Date: {day}")

    route = SPORT_ROUTES.get(sport.lower())
    if not route:
        print(f"  no route for {sport!r}; known: {', '.join(sorted(SPORT_ROUTES))}")
        return 1
    espn_sport, league, sport_key = route

    for base in ESPN_HOSTS:
        url = f"{base}/{espn_sport}/{league}/scoreboard"
        print(f"\n--- {url}?dates={day.replace('-', '')}")
        try:
            r = requests.get(url, params={"dates": day.replace("-", "")},
                             headers=ESPN_HEADERS, timeout=20)
            print(f"    HTTP {r.status_code}, {len(r.content)} bytes")
            r.raise_for_status()
            payload = r.json()
        except Exception as exc:
            print(f"    FAILED: {type(exc).__name__}: {exc}")
            continue

        print(f"    top-level keys: {sorted(payload)[:12]}")
        events = payload.get("events") or []
        print(f"    events: {len(events)}")
        if not events:
            print("    -> nothing came back for this date. Either the date format")
            print("       is wrong or that league had no games that day.")
            continue

        ev = events[0]
        print(f"    first event keys: {sorted(ev)}")
        print(f"    status block: {json.dumps(ev.get('status'), indent=6)[:400]}")
        comps = (ev.get("competitions") or [{}])[0].get("competitors") or []
        for c in comps:
            print(f"      competitor: homeAway={c.get('homeAway')!r} "
                  f"score={c.get('score')!r} "
                  f"name={(c.get('team') or {}).get('displayName')!r}")

        normalised = [normalise_event(e, sport_key) for e in events]
        good = [g for g in normalised if g]
        print(f"\n    normalise_event accepted {len(good)} of {len(events)}")
        if good:
            print(f"    example: {good[0]['away_team']} @ {good[0]['home_team']} "
                  f"{[s['score'] for s in good[0]['scores']]}")

        print("\n    ESPN names that day:")
        for g in good[:8]:
            print(f"      {g['away_team']} @ {g['home_team']}")
        print("    matchups in the database that day:")
        for m in matchups_for(sport, day)[:8]:
            print(f"      {m}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
