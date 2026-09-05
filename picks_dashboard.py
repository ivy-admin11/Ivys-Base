"""Dashboard-style PDF for the Sharp Picks report.

Replaces the plain data table with the layout Henry asked for on 2026-09-05:
a navy masthead, a row of stat tiles, a signal-status banner explaining the
consensus result, and a two-column board of pick cards.

Kept separate from ``picks_formatter.PicksReportFormatter`` on purpose — that
one is shared with Happy Hour and the meal planner, and this layout is specific
to picks (market badges, consensus counts, handicapper attribution).

Every card is attributed to the handicapper who posted it. The old report said
"X Sharp Picks" on every line, which named the platform rather than the source.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

NAVY = colors.HexColor("#1B355E")
NAVY_DEEP = colors.HexColor("#16294A")
GOLD = colors.HexColor("#C9A227")
INK = colors.HexColor("#1A1A1A")
BLUE = colors.HexColor("#1B4F9C")
MUTED = colors.HexColor("#6B7280")
RULE = colors.HexColor("#DCE0E6")
CANVAS = colors.HexColor("#F4F6F9")
BANNER = colors.HexColor("#FBF3DA")
BADGE_BG = colors.HexColor("#E8EEF7")

PAGE = landscape(letter)
MARGIN = 0.35 * inch
CONTENT_W = PAGE[0] - 2 * MARGIN


def _s(style_name, **kw):
    base = dict(name=style_name, fontName="Helvetica", fontSize=8,
                leading=10, textColor=INK)
    base.update(kw)
    return ParagraphStyle(**base)


TITLE = _s("t", fontName="Helvetica-Bold", fontSize=21, leading=24, textColor=colors.white)
DATELINE = _s("d", fontName="Helvetica-Bold", fontSize=8, leading=11,
              textColor=colors.white, alignment=TA_RIGHT)
DATELINE_SUB = _s("ds", fontSize=7.5, leading=10,
                  textColor=colors.HexColor("#B9C6DA"), alignment=TA_RIGHT)
STAT_VALUE = _s("sv", fontName="Helvetica-Bold", fontSize=19, leading=21, textColor=NAVY)
STAT_VALUE_SM = _s("svs", fontName="Helvetica-Bold", fontSize=15, leading=19, textColor=NAVY)
STAT_LABEL = _s("sl", fontSize=6.5, leading=8, textColor=MUTED)
BANNER_TXT = _s("bn", fontSize=7.5, leading=10, textColor=colors.HexColor("#5B4A15"))
BANNER_R = _s("bnr", fontSize=6.5, leading=9, textColor=colors.HexColor("#8A7431"), alignment=TA_RIGHT)
SECTION = _s("sec", fontName="Helvetica-Bold", fontSize=10, leading=12, textColor=NAVY)
CARD_NUM = _s("cn", fontName="Helvetica-Bold", fontSize=6.5, leading=8, textColor=GOLD)
CARD_BADGE = _s("cb", fontName="Helvetica-Bold", fontSize=6, leading=8, textColor=NAVY)
CARD_MATCHUP = _s("cm", fontName="Helvetica-Bold", fontSize=7.5, leading=9, textColor=INK)
CARD_PICK = _s("cp", fontName="Helvetica-Bold", fontSize=11, leading=13, textColor=BLUE)
CARD_META = _s("cme", fontSize=6.5, leading=8.5, textColor=MUTED)
CARD_ANALYSIS = _s("ca", fontSize=6.8, leading=9, textColor=colors.HexColor("#44506A"))
FOOT_L = _s("fl", fontSize=6.5, leading=9, textColor=MUTED)
FOOT_R = _s("fr", fontSize=6.5, leading=9, textColor=MUTED, alignment=TA_RIGHT)

_PERIOD_RE = re.compile(r"\b([12](?:h|q)|1st\s+half|2nd\s+half|first\s+half|second\s+half)\b", re.I)


def market_badge(side: str) -> str:
    """The label on a card: SPREAD / TOTAL / TEAM TOTAL / ML / PROP, with any
    period qualifier in front ("1H SPREAD")."""
    s = (side or "").lower()
    period = ""
    m = _PERIOD_RE.search(s)
    if m:
        token = m.group(1).lower().replace(" ", "")
        period = {"firsthalf": "1H", "1sthalf": "1H",
                  "secondhalf": "2H", "2ndhalf": "2H"}.get(token, token.upper()) + " "

    if "tt " in s or "team total" in s:
        kind = "TEAM TOTAL"
    elif re.search(r"\b(hr|k|so|rbi|tb|pts|reb|ast|td)s?\b", s) or any(
            w in s for w in ("strikeout", "yard", "reception", "touchdown", "rebound",
                             "assist", "point", "home run", "hit")):
        kind = "PROP"
    elif "over" in s or "under" in s or "total" in s:
        kind = "TOTAL"
    elif "ml" in s.split() or "moneyline" in s:
        kind = "MONEYLINE"
    elif re.search(r"[+-]\d", s):
        kind = "SPREAD"
    else:
        kind = "PICK"
    return (period + kind).strip()


def _odds_label(pick: Dict[str, Any]) -> str:
    """The price, without repeating the team already named in the pick.

    _price_for_side returns the market's own half ("Tennessee Volunteers -48.5
    (-110)"), which on a card that already says "Tennessee -50" reads as a
    stutter. The number is kept exactly as the book has it — including when it
    differs from the number the sharp posted, which is real information.
    """
    odds = str(pick.get("odds") or "").strip()
    if not odds:
        return "No line"
    # Totals lead with a word that IS the bet ("Over 50.5"), so leave them be.
    if re.match(r"^(over|under)\b", odds, re.IGNORECASE):
        return odds
    # Otherwise the leading words are the team name, already on the card.
    stripped = re.sub(r"^(?:[A-Za-z.''-]+\s+)+(?=[+-]?\d)", "", odds).strip()
    return stripped or odds


def _attribution(pick: Dict[str, Any]) -> str:
    """The handicapper who posted it — never the platform."""
    handles = [h for h in (pick.get("handicappers") or []) if h]
    if not handles:
        return "unattributed"
    if len(handles) <= 2:
        return ", ".join(f"@{h}" for h in handles)
    return f"@{handles[0]} +{len(handles) - 1} more"


def _card(index: int, pick: Dict[str, Any]) -> Table:
    badge_text = market_badge(pick.get("side", ""))
    # Width follows the text: a nested table with colWidths=[None] stretches to
    # fill the cell, which painted the badge across the whole card.
    badge_w = stringWidth(badge_text, "Helvetica-Bold", CARD_BADGE.fontSize) + 12
    badge = Table([[Paragraph(escape(badge_text), CARD_BADGE)]],
                  colWidths=[badge_w], style=TableStyle([
                      ("BACKGROUND", (0, 0), (-1, -1), BADGE_BG),
                      ("LEFTPADDING", (0, 0), (-1, -1), 5),
                      ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                      ("TOPPADDING", (0, 0), (-1, -1), 2),
                      ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                  ]), hAlign="LEFT")

    head = Table([[Paragraph(f"#{index:02d}", CARD_NUM), badge]],
                 colWidths=[0.32 * inch, badge_w], style=TableStyle([
                     ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                     ("LEFTPADDING", (0, 0), (-1, -1), 0),
                     ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                     ("TOPPADDING", (0, 0), (-1, -1), 0),
                     ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                 ]), hAlign="LEFT")

    meta_bits = []
    when = pick.get("start_label") or pick.get("start") or ""
    if when:
        meta_bits.append(escape(str(when)))
    meta_bits.append("Odds " + escape(_odds_label(pick)))
    meta_bits.append(escape(_attribution(pick)))
    count = int(pick.get("consensus_count") or 0)
    if count >= 2:
        meta_bits.append(f"<b>{count} sharps</b>")

    rows = [
        [head],
        [Paragraph(escape(str(pick.get("matchup") or "TBD")), CARD_MATCHUP)],
        [Paragraph(escape(str(pick.get("side") or "")), CARD_PICK)],
        [Paragraph(" &#8226; ".join(meta_bits), CARD_META)],
    ]
    # Pre-escaped by the caller (it carries <br/> between the take and the
    # line/injury notes), so it is passed through as markup, not re-escaped.
    analysis = str(pick.get("analysis") or "").strip()
    if analysis:
        rows.append([Paragraph(analysis, CARD_ANALYSIS)])
    style = [
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 0.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0.5),
        ("TOPPADDING", (0, 0), (0, 0), 5),
        ("BOTTOMPADDING", (0, -1), (0, -1), 5),
        ("BOX", (0, 0), (-1, -1), 0.5, RULE),
    ]
    if count >= 2:
        style.append(("LINEBEFORE", (0, 0), (0, -1), 2.5, GOLD))
    return Table(rows, colWidths=[None], style=TableStyle(style))


def _stat_tile(value: str, label: str, small: bool = False) -> Table:
    return Table(
        [[Paragraph(escape(value), STAT_VALUE_SM if small else STAT_VALUE)],
         [Paragraph(escape(label.upper()), STAT_LABEL)]],
        colWidths=[None],
        style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.white),
            ("LEFTPADDING", (0, 0), (-1, -1), 9),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (0, 0), 4),
            ("BOTTOMPADDING", (0, 0), (0, 0), 0),
            ("TOPPADDING", (0, 1), (0, 1), 0),
            ("BOTTOMPADDING", (0, 1), (0, 1), 4),
            ("LINEBEFORE", (0, 0), (0, -1), 2.5, GOLD),
        ]),
    )


def build_dashboard(
    pdf_path: str,
    picks: Sequence[Dict[str, Any]],
    *,
    generated_at: Optional[datetime] = None,
    signal_note: str = "",
    confidence_label: str = "",
) -> str:
    """Render the picks dashboard to ``pdf_path`` and return it."""
    now = generated_at or datetime.now()
    picks = list(picks)
    handles = {h for p in picks for h in (p.get("handicappers") or [])}
    consensus = [p for p in picks if int(p.get("consensus_count") or 0) >= 2]
    sports = [p.get("sport") for p in picks if p.get("sport")]
    sport_label = sports[0] if sports and len(set(sports)) == 1 else (
        "MULTI" if sports else "—")

    doc = SimpleDocTemplate(
        pdf_path, pagesize=PAGE,
        leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=MARGIN + 22,
        title="Ivy's Sharp Picks", author="Ivy",
    )

    masthead = Table(
        [[Paragraph("Ivy's Sharp Picks", TITLE),
          Paragraph(f"{now:%a} &#8226; {now:%b %d, %Y}<br/>"
                    f"<font size=7.5 color='#B9C6DA'>{escape(sport_label)} &#8226; DAILY CARD</font>",
                    DATELINE)]],
        colWidths=[CONTENT_W * 0.62, CONTENT_W * 0.38],
        style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), NAVY),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (0, 0), 16),
            ("RIGHTPADDING", (-1, 0), (-1, 0), 16),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]),
    )

    tile_w = (CONTENT_W - 3 * 6) / 4.0
    tiles = Table(
        [[_stat_tile(str(len(picks)), "Total picks"),
          _stat_tile(str(len(consensus)), "Consensus plays"),
          _stat_tile(str(len(handles)), "Handicapper" if len(handles) == 1 else "Handicappers"),
          _stat_tile(sport_label, "Sport", small=len(sport_label) > 5)]],
        colWidths=[tile_w] * 4, hAlign="LEFT",
        style=TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), NAVY),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("LINEBELOW", (0, 0), (-1, -1), 2, GOLD),
        ]),
    )

    story: List[Any] = [masthead, tiles, Spacer(1, 6)]

    if signal_note:
        story.append(Table(
            [[Paragraph(f"<b>SIGNAL STATUS</b>  {escape(signal_note)}", BANNER_TXT),
              Paragraph(escape(confidence_label), BANNER_R)]],
            colWidths=[CONTENT_W * 0.76, CONTENT_W * 0.24],
            style=TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), BANNER),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#E4D5A0")),
            ]),
        ))
        story.append(Spacer(1, 7))

    story.append(Paragraph("TODAY'S PICK BOARD", SECTION))
    story.append(Spacer(1, 4))

    if not picks:
        story.append(Paragraph("No picks on the board.", CARD_META))
    else:
        cards = [_card(i, p) for i, p in enumerate(picks, 1)]
        half = (len(cards) + 1) // 2
        left, right = cards[:half], cards[half:]
        right += [""] * (len(left) - len(right))
        col_w = (CONTENT_W - 10) / 2.0
        grid = Table(
            [[l, r] for l, r in zip(left, right)],
            colWidths=[col_w, col_w],
            style=TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (0, -1), 10),
                ("RIGHTPADDING", (1, 0), (1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]),
        )
        story.append(grid)

    source = ", ".join(f"@{h}" for h in sorted(handles)) if handles else "X"
    footer_left = (f"Generated {now:%Y-%m-%d %H:%M}  \u2022  Source: {source}  "
                   "\u2022  For entertainment purposes only.")

    def _bg(canvas, _doc):
        canvas.saveState()
        canvas.setFillColor(CANVAS)
        canvas.rect(0, 0, PAGE[0], PAGE[1], stroke=0, fill=1)

        y = MARGIN + 12
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(MARGIN, y + 9, PAGE[0] - MARGIN, y + 9)
        canvas.setFont("Helvetica", 6.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, y, footer_left)
        canvas.drawRightString(PAGE[0] - MARGIN, y, "IVY \u2022 SHARP PICKS")
        canvas.restoreState()

    doc.build(story, onFirstPage=_bg, onLaterPages=_bg)
    return pdf_path
