#!/usr/bin/env python3
"""Show — or actually send — exactly what a PDF-first Sharp Picks report looks like.

Sharp Picks went PDF-first on 2026-09-09: a short covering text, the board in
an attached PDF, and the full board as text whenever chat.db cannot confirm the
attachment landed. The scheduled job only exercises that path when picks clear
the quality bar, which at the moment they rarely do, so this exists to see the
message without waiting for a qualifying slate.

It renders through the real functions — format_picks_summary, build_footer,
split_imessage_content, deliver_report — so the preview cannot drift from what
the job actually sends. Nothing here re-runs the sweep, calls a model, writes to
picks.db, or stamps the duplicate-suppression fingerprint.

    python3 scripts/preview_pdf_first.py              # print it
    python3 scripts/preview_pdf_first.py --send       # text it to Henry, PDF and all
    python3 scripts/preview_pdf_first.py --date 2026-09-01
"""

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: F401,E402  — loads .env before anything reads it
from ivy_core import text_delivery  # noqa: E402
from ivy_core.text_delivery import (  # noqa: E402
    BUBBLE_MAX_CHARS,
    build_footer,
    split_imessage_content,
)
from proactive_agents import sports_bettor as sb  # noqa: E402

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "picks.db")


def load_picks(report_date=None):
    """Rebuild pick dicts from the database in the shape the formatters expect.

    Placeholder rows ("A vs B") are skipped: eight of them sit in the live
    database and they would make the preview look broken for a reason that has
    nothing to do with delivery.
    """
    con = sqlite3.connect(DB)
    if not report_date:
        row = con.execute(
            "SELECT report_date FROM picks WHERE matchup != 'A vs B' "
            "GROUP BY report_date ORDER BY report_date DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None, []
        report_date = row[0]

    rows = con.execute(
        "SELECT sport, matchup, side, odds, handicapper, game_day, start_time, sharp_count "
        "FROM picks WHERE report_date = ? AND matchup != 'A vs B' ORDER BY id",
        (report_date,),
    ).fetchall()

    def _odds(value):
        """American odds render as integers. The column is REAL, so a straight
        read gives "-108.0" — which is not a price anyone writes."""
        if value in (None, ""):
            return ""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value)
        return f"{number:+.0f}" if number == int(number) else f"{number:+g}"

    picks = []
    for sport, matchup, side, odds, handicapper, game_day, start, sharps in rows:
        handles = [h.strip() for h in (handicapper or "").split(",") if h.strip()]
        count = int(sharps or len(handles) or 1)
        picks.append({
            "sport": sport, "matchup": matchup, "side": side,
            "odds": _odds(odds), "handicappers": handles,
            "consensus_count": count, "is_consensus": count >= 2,
            "game_day": game_day or "", "start": start, "enrichment": {},
        })
    return report_date, picks


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--send", action="store_true",
                    help="Actually deliver it to Henry (real iMessage, real PDF attachment)")
    ap.add_argument("--date", help="report_date to rebuild (default: most recent with real picks)")
    args = ap.parse_args()

    report_date, picks = load_picks(args.date)
    if not picks:
        print("No picks in the database to preview"
              + (f" for {args.date}" if args.date else "") + ".")
        return 1

    print(f"Rebuilt {len(picks)} pick(s) from {report_date}.\n")

    cover = sb.format_picks_summary(picks)
    full_body, detail = sb.format_picks_digest(picks)
    report_id = "SP-PREVIEW"

    footer = build_footer(("MORE", "WHY <n>", "PDF"), report_id=report_id)
    bubbles = split_imessage_content(cover, max_chars=BUBBLE_MAX_CHARS)
    bubbles[-1] = f"{bubbles[-1]}\n\n{footer}"

    print("=" * 62)
    print("WHAT ARRIVES — covering text, then the PDF as an attachment")
    print("=" * 62)
    for i, b in enumerate(bubbles, 1):
        print(f"\n--- bubble {i}/{len(bubbles)} ---")
        print(b)

    print("\n" + "=" * 62)
    print("IF chat.db CANNOT CONFIRM THE PDF — this follows automatically")
    print("=" * 62)
    print("\nThe PDF didn't confirm as delivered, so here's the board as text.\n")
    print(full_body)

    if not args.send:
        print("\n" + "-" * 62)
        print("Preview only. Re-run with --send to receive it on your phone.")
        return 0

    pdf_path = None
    try:
        pdf_path = sb.format_picks_pdf(picks)
        print(f"\nBuilt PDF: {pdf_path}")
    except Exception as exc:
        print(f"\nPDF generation failed ({exc}) — sending text-first instead.")

    result = text_delivery.deliver_report(
        sb.HENRY_PHONE,
        job_name="sharp_picks",
        body=cover if pdf_path else full_body,
        report_id=report_id,
        detail=detail,
        pdf_path=pdf_path,
        content_summary=f"PREVIEW — {len(picks)} pick(s) from {report_date}",
        commands=("MORE", "WHY <n>", "PDF"),
        attach_pdf=bool(pdf_path),
        fallback_body=full_body if pdf_path else None,
    )

    print(f"\ntext        : {result.status} ({result.bubbles_sent}/{result.bubbles_total} bubbles)")
    print(f"attachment  : {result.attachment_status}")
    print(f"text fallback sent: {result.fallback_sent}")
    print(f"reached Henry: {result.content_reached_henry}")
    if result.attachment_status == text_delivery.ATTACH_VERIFIED:
        print("\nThe PDF was confirmed in chat.db — this is the good path.")
    elif result.fallback_sent:
        print("\nThe attachment could not be confirmed, so the board went as text.\n"
              "Two messages instead of one is the intended trade: never a lost report.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
