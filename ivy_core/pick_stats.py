"""Shared vocabulary for pick records.

picks_tracker imports sheets_logger, so anything both modules need lives
here instead of in either of them. One definition of what a record is
means the database summary and the spreadsheet summary cannot disagree --
and they already did once, over which column held the grade.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Optional


# A pick's result is W, L, P, or NULL while it waits for its game. "U" is a
# fourth state: the game happened but no grade can ever be established -- the
# scores endpoint only serves the last couple of days, so a pick that went
# ungraded past that window is not pending, it is unknowable. Reporting those
# as "pending" is the specific way this dashboard was misleading: it implied
# 85 results were still coming when none were.
UNVERIFIABLE = "U"

# Below this many decided picks (W+L), a percentage is noise dressed up as a
# record -- 2W-1L is not a 66.7% hit rate. The raw W-L-P is always shown; the
# percentage is withheld until it means something.
MIN_DECIDED_FOR_RATE = 20


def summarize(wins=0, losses=0, pushes=0, pending=0, unverifiable=0, total=None) -> Dict:
    """Build a stats dict. One definition, so the three queries cannot drift."""
    wins, losses = wins or 0, losses or 0
    pushes, pending = pushes or 0, pending or 0
    unverifiable = unverifiable or 0
    decided = wins + losses
    return {
        "total": total if total is not None else wins + losses + pushes + pending + unverifiable,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "pending": pending,
        "unverifiable": unverifiable,
        "decided": decided,
        # None, not 0: "no basis for a number" and "graded 0%" are different
        # claims, and a caller that prints 0.0% for an empty record is lying.
        "hit_rate": (wins / decided) * 100 if decided >= MIN_DECIDED_FOR_RATE else None,
    }


def format_hit_rate(stats: Dict) -> str:
    """Render a hit rate, or say plainly why there isn't one."""
    if stats["hit_rate"] is None:
        return f"no rate yet — {stats['decided']} of {MIN_DECIDED_FOR_RATE} decided"
    return f"{stats['hit_rate']:.1f}% of {stats['decided']} decided"


def resolve_pick_date(game_day, report_date) -> Optional[str]:
    """The date a pick's game was played, as YYYY-MM-DD, or None.

    game_day is written by an LLM parsing free-form posts, so it is often not
    a date at all: 79 of 81 ungraded picks hold the literal string "today".
    Each falls back to its report_date, which is always a real date.

    This lived in two places with two behaviours. The backfill's copy did
    `game_day or report_date`, and "today" is truthy — so it took the garbage,
    failed to parse it, and skipped the pick instead of falling back. Every
    one of those 79 picks was silently unreachable. One definition now.
    """
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
