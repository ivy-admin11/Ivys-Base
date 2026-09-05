#!/usr/bin/env python3
"""Vet candidate X handicapper handles before adding them to the sweep.

Why this exists
---------------
TARGET_X_ACCOUNTS was once a 16-handle list assembled from public "best betting
accounts" articles. It returned 0 picks every run for weeks: two handles no
longer existed, one was a stock trader, several were reporters who never bet,
and the rest were dormant. The fix was to confirm each handle actually returns
bettable picks through the real sweep before trusting it — this script is that
check, made repeatable.

It runs the SAME code path as the job (`_sweep_chunk`, which now sweeps every
sport rather than a league list), so a
handle that produces picks here will produce them in production. It sends
nothing: no iMessage, no outbox entry, no state written.

Usage
-----
    .venv/bin/python scripts/vet_x_handles.py --current       # what the 14 already cover
    .venv/bin/python scripts/vet_x_handles.py --candidates    # the CFB shortlist below
    .venv/bin/python scripts/vet_x_handles.py handleA handleB # your own list

`--current` is the one to reach for first when asking "does anyone I already
follow post <sport>?" — it prints the leagues each existing handle returned,
which is cheaper and more honest than adding handles on a hunch.

Or directly:
    .venv/bin/python scripts/vet_x_handles.py --candidates
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from proactive_agents.sports_bettor import (  # noqa: E402
    TARGET_X_ACCOUNTS,
    X_ACCOUNT_CHUNK_SIZE,
    _chunk_accounts,
    _sweep_chunk,
)

# Candidates sourced from public handicapper round-ups (RotoGrinders, Boyd's
# Bets) on 2026-09-05 and filtered to accounts those sources describe as posting
# FREE picks and covering college football. Sourcing like this is exactly what
# produced the dead 16-handle list, so nothing here is trusted until it shows up
# in this script's output with a real pick.
CANDIDATES = [
    "_Collin1",          # Collin Wilson — CFB matchup projections, occasional free plays
    "kylehunterpicks",   # Kyle Hunter — CFB-first, free content plus premium
    "bradpowers7",       # Brad Powers — CFB, free leans via podcast/radio
    "Jonny_Reno",        # NCAAF/NBA/MLB/NCAAB, free picks
    "Stuckey2",          # multi-sport incl. CFB, occasional free picks
    "betfirmsjack",      # daily free picks with analysis, general
]

# Deliberately NOT included, and why — so the next person doesn't re-add them:
#   sharpfootball    analytics, not picks
#   ToddFuhrman      insights without free picks
#   vegasbedwards    injury news and previews rather than picks
#   adamchernoff     previews and news
#   robpizzola       NFL/NHL, little CFB
#   The_Oddsmaker    NFL/MLB props
#   stevejanus       primarily paywalled


def vet(handles, chunk_size=X_ACCOUNT_CHUNK_SIZE, sweep=_sweep_chunk):
    """Run the real sweep over `handles`; return {handle: [picks]}.

    Handles that return nothing are reported too — a silent zero is the whole
    signal this script exists to surface.
    """
    found = defaultdict(list)
    for batch in _chunk_accounts(list(handles), chunk_size):
        print(f"🔎 Sweeping {len(batch)} handle(s): {', '.join(batch)}", flush=True)
        picks = sweep(batch, "")
        print(f"   ↳ {len(picks)} raw pick(s) returned.", flush=True)
        for p in picks:
            who = (p.get("handicapper") or "").lstrip("@")
            # Grok occasionally returns a differently-cased handle.
            match = next((h for h in batch if h.lower() == who.lower()), None)
            if match:
                found[match].append(p)
    return {h: found.get(h, []) for h in handles}


def report(results, auditing=False):
    """Print a per-handle verdict. Returns the handles that produced picks.

    ``auditing`` switches the wording for handles already in the sweep: a zero
    there means "posted nothing bettable in this window", not "don't add".
    """
    keep = []
    print("\n" + "=" * 64)
    print("VERDICT")
    print("=" * 64)
    for handle, picks in results.items():
        if not picks:
            note = "nothing bettable in this window" if auditing else "no bettable picks returned — do not add"
            print(f"  ✗ @{handle:<20} {note}")
            continue
        keep.append(handle)
        sports = sorted({(p.get("sport") or "?") for p in picks})
        ex = picks[0]
        print(f"  ✓ @{handle:<20} {len(picks)} pick(s) · {', '.join(sports)}")
        print(f"      e.g. {ex.get('matchup')} — {ex.get('side')}")
    print()
    if auditing:
        leagues = sorted({(p.get("sport") or "?") for picks in results.values() for p in picks})
        print(f"Leagues your current handles are actually posting: {', '.join(leagues) or 'none'}")
        return keep
    if keep:
        print("Add to TARGET_X_ACCOUNTS in proactive_agents/sports_bettor.py:")
        print("    " + ", ".join(f'"{h}"' for h in keep))
    else:
        print("Nothing cleared the bar. Try again closer to game day —")
        print("college handles post most of their slate on Thursday and Friday.")
    return keep


def main(argv=None):
    ap = argparse.ArgumentParser(description="Vet X handicapper handles against the real sweep.")
    ap.add_argument("handles", nargs="*", help="handles to test (no @)")
    ap.add_argument("--candidates", action="store_true", help="use the built-in CFB shortlist")
    ap.add_argument("--current", action="store_true",
                    help="vet the handles already in TARGET_X_ACCOUNTS, and report what each covers")
    ap.add_argument("--json", metavar="PATH", help="also write raw results to a JSON file")
    args = ap.parse_args(argv)

    handles = args.handles or (
        list(TARGET_X_ACCOUNTS) if args.current else CANDIDATES if args.candidates else []
    )
    if not handles:
        ap.error("give handles, or pass --current / --candidates")

    print(f"Vetting {len(handles)} handle(s). This sends nothing — it only reads X.\n")
    results = vet(handles)
    keep = report(results, auditing=args.current)

    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nRaw results written to {args.json}")
    return 0 if keep else 1


if __name__ == "__main__":
    sys.exit(main())
