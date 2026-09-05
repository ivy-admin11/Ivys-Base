"""
Picks Formatter — Professional PDF generation for sports picks, happy hour, meals, etc.

Converts pick data into polished, branded PDF reports matching the format shown
in the 48-Hour Betting Report image. Reusable across sports_bettor, happy_hour_scout,
familia_meal_planner, and other agents.

Effort: ~200 lines of reportlab code. Dependencies already in requirements.txt.
"""

import os
import tempfile
from datetime import datetime
from typing import Any, List, Dict, Optional
from xml.sax.saxutils import escape as _xml_escape

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, white
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
)
from reportlab.lib.enums import TA_CENTER


#: Left/right page margin used by every report (and the basis for the usable
#: text width that column widths are fitted to).
MARGIN = 0.75 * inch


def _esc(value: Any) -> str:
    """Make caller-supplied text safe for a reportlab ``Paragraph``.

    ``Paragraph`` parses its text as mini-HTML, so raw ``&``/``<``/``>`` in a
    venue name, team name or X handle either raises ``ValueError`` ("unclosed
    tags") or silently swallows everything that looks like a tag — a real
    "Bar & Grill <Wings>" lost its "<Wings>" before this existed. ``None`` is
    also normalised to an empty string so a missing odds field does not print
    the literal word "None".
    """
    if value is None:
        return ""
    return _xml_escape(str(value))


class PicksReportFormatter:
    """Generate professional PDF reports for any pick data."""

    def __init__(self, title: str, subtitle: str, color_scheme: str = "sports"):
        """
        Initialize formatter.

        Args:
            title: Report title (e.g., "Ivy 48-Hour Betting Report")
            subtitle: Subtitle with context (e.g., "Sharp X Picks vs. Live Vegas Odds")
            color_scheme: "sports" (blue/gold), "happy_hour" (warm), "meals" (green)
        """
        self.title = title
        self.subtitle = subtitle
        self.color_scheme = color_scheme

        # Brand colors by type
        self.colors = {
            "sports": {
                "header": HexColor("#1a2d5f"),
                "accent": HexColor("#d4af37"),
                "table_header": HexColor("#2d4a8f"),
                "highlight": HexColor("#fff8dc"),
            },
            "happy_hour": {
                "header": HexColor("#8b4513"),
                "accent": HexColor("#ff6b35"),
                "table_header": HexColor("#a0522d"),
                "highlight": HexColor("#ffe4b5"),
            },
            "meals": {
                "header": HexColor("#2d5016"),
                "accent": HexColor("#ff9500"),
                "table_header": HexColor("#3d7c1e"),
                "highlight": HexColor("#e8f5e9"),
            },
        }

        self.theme = self.colors.get(color_scheme, self.colors["sports"])

    def generate_pdf(
        self,
        filename: str,
        summary: str,
        consensus_picks: List[Dict[str, str]],
        other_picks: List[Dict[str, str]],
        metadata: Optional[Dict[str, str]] = None,
        headers: Optional[List[str]] = None,
        col_widths: Optional[List[float]] = None,
        fields: Optional[List[str]] = None,
        consensus_heading: str = "🔥 High-Likelihood Consensus Plays",
        other_heading: str = "Other Sharp Picks",
    ) -> str:
        """
        Generate a professional PDF report.

        Args:
            filename: Output PDF path
            summary: Executive summary paragraph
            consensus_picks: List of dicts with keys: sport, matchup, side, odds, reasoning
                (or whatever keys `fields` names)
            other_picks: List of dicts with same structure
            metadata: Optional dict with pick_count (a complete phrase,
                rendered as-is), source, timestamp
            headers: Optional column header labels (defaults to the sports-report
                labels); pass domain-appropriate labels for non-sports callers.
                Must have the same length as `fields`.
            col_widths: Optional column widths in inches. The usable text width
                is 7.0in (letter minus the 0.75in margins); widths that sum to
                more than that are scaled down proportionally to fit rather than
                spilling past the right margin. Defaults to the sports-report widths.
            fields: Optional list of pick dict keys, in column order (defaults to
                the 5 sports-report keys). Lets a caller add/reorder columns —
                e.g. a "when" column for game date/time — as long as `headers`
                and `col_widths` are updated to match.
            consensus_heading: Section title above the first table (defaults to
                the sports-report heading; pass a domain-appropriate title for
                non-sports callers).
            other_heading: Section title above the second table (same default
                caveat as consensus_heading).

        Returns:
            Path to generated PDF
        """
        headers = headers or ["Sport", "Matchup", "Side", "Odds", "Reasoning"]
        col_widths = col_widths or [0.8, 2.0, 1.0, 0.9, 2.8]
        fields = fields or ["sport", "matchup", "side", "odds", "reasoning"]

        # Usable text width between the margins set on the doc below. Column
        # widths that sum to more than this used to be drawn anyway, spilling
        # the last column into (and past) the right margin.
        frame_width = letter[0] - 2 * MARGIN
        scale = min(1.0, frame_width / (sum(col_widths) * inch)) if sum(col_widths) > 0 else 1.0
        scaled_col_widths = [w * inch * scale for w in col_widths]

        cell_style = ParagraphStyle(
            "TableCell", parent=getSampleStyleSheet()["Normal"], fontSize=8, leading=10,
        )
        header_style = ParagraphStyle(
            "TableCellHeader", parent=getSampleStyleSheet()["Normal"],
            fontSize=9, leading=11, textColor=white, fontName="Helvetica-Bold",
        )

        def _row(cells: List[Any]) -> List[Paragraph]:
            return [Paragraph(_esc(c), cell_style) for c in cells]

        def _header_row() -> List[Paragraph]:
            return [Paragraph(_esc(h), header_style) for h in headers]
        doc = SimpleDocTemplate(
            filename,
            pagesize=letter,
            rightMargin=MARGIN,
            leftMargin=MARGIN,
            topMargin=0.5 * inch,
            bottomMargin=0.5 * inch,
        )

        story = []
        styles = getSampleStyleSheet()

        # ====== HEADER ======
        title_style = ParagraphStyle(
            "CustomTitle",
            parent=styles["Heading1"],
            fontSize=24,
            textColor=self.theme["header"],
            spaceAfter=6,
            alignment=TA_CENTER,
            fontName="Helvetica-Bold",
        )
        story.append(Paragraph(_esc(self.title), title_style))

        subtitle_style = ParagraphStyle(
            "CustomSubtitle",
            parent=styles["Normal"],
            fontSize=11,
            textColor=self.theme["header"],
            spaceAfter=12,
            alignment=TA_CENTER,
            fontName="Helvetica",
        )
        story.append(Paragraph(_esc(self.subtitle), subtitle_style))

        # Divider line
        divider_table = Table([[""]], colWidths=[frame_width])
        divider_table.setStyle(
            TableStyle([("LINEBELOW", (0, 0), (-1, -1), 2, self.theme["header"])])
        )
        story.append(divider_table)
        story.append(Spacer(1, 0.2 * inch))

        # ====== SUMMARY ======
        summary_style = ParagraphStyle(
            "Summary",
            parent=styles["Normal"],
            fontSize=10,
            leading=14,
            spaceAfter=0.15 * inch,
        )
        story.append(Paragraph(_esc(summary), summary_style))
        story.append(Spacer(1, 0.15 * inch))

        heading_style = ParagraphStyle(
            "TableHeading",
            parent=styles["Heading2"],
            fontSize=12,
            textColor=self.theme["accent"],
            spaceAfter=8,
            fontName="Helvetica-Bold",
        )

        # ====== CONSENSUS PICKS TABLE ======
        if consensus_picks:
            story.append(Paragraph(_esc(consensus_heading), heading_style))

            # Header row keeps the bold/white header style regardless of _row's default.
            consensus_table_data = [_header_row()]
            for pick in consensus_picks:
                consensus_table_data.append(_row([pick.get(f, "") for f in fields]))

            consensus_table = Table(
                consensus_table_data,
                colWidths=scaled_col_widths,
                # Repeat the header on every page the table spills onto, and let
                # an over-tall single row split instead of raising LayoutError.
                repeatRows=1,
                splitInRow=1,
            )
            consensus_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), self.theme["table_header"]),
                        ("TEXTCOLOR", (0, 0), (-1, 0), white),
                        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                        ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
                        ("BACKGROUND", (0, 1), (-1, -1), self.theme["highlight"]),
                        ("GRID", (0, 0), (-1, -1), 0.5, self.theme["header"]),
                        ("TOPPADDING", (0, 0), (-1, -1), 5),
                        ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                )
            )
            story.append(consensus_table)
            story.append(Spacer(1, 0.2 * inch))

        # ====== OTHER PICKS TABLE ======
        if other_picks:
            story.append(Paragraph(_esc(other_heading), heading_style))

            other_table_data = [_header_row()]
            for pick in other_picks:
                other_table_data.append(_row([pick.get(f, "") for f in fields]))

            other_table = Table(
                other_table_data,
                colWidths=scaled_col_widths,
                repeatRows=1,
                splitInRow=1,
            )
            other_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), self.theme["table_header"]),
                        ("TEXTCOLOR", (0, 0), (-1, 0), white),
                        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                        ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
                        ("GRID", (0, 0), (-1, -1), 0.5, self.theme["header"]),
                        ("TOPPADDING", (0, 0), (-1, -1), 5),
                        ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                )
            )
            story.append(other_table)
            story.append(Spacer(1, 0.2 * inch))

        # ====== FOOTER ======
        if metadata:
            def _meta(key: str, default: str) -> str:
                # ``or default`` and not ``.get(key, default)``: an explicit
                # None used to render the literal word "None" in the footer.
                return _esc(metadata.get(key) or default)

            footer_text = (
                f"Generated {_meta('timestamp', 'N/A')} • "
                # Rendered verbatim: every caller passes a full phrase
                # ("12 picks from 3 handicappers"), so appending a unit here
                # produced "... (2 consensus) pick(s)".
                f"{_meta('pick_count', 'no picks')} • "
                f"Source: {_meta('source', 'Ivy')} • "
                "For entertainment purposes only."
            )
        else:
            footer_text = f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} • For entertainment purposes only."

        footer_style = ParagraphStyle(
            "Footer",
            parent=styles["Normal"],
            fontSize=7,
            textColor=HexColor("#999999"),
            spaceAfter=0,
            alignment=TA_CENTER,
        )
        story.append(Paragraph(footer_text, footer_style))

        # Build PDF
        doc.build(story)
        return filename


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    # Example: Sports picks (like the image)
    formatter = PicksReportFormatter(
        title="Ivy 48-Hour Betting Report",
        subtitle="Sharp X Picks vs. Live Vegas Odds | Wednesday, July 01, 2026",
        color_scheme="sports",
    )

    consensus = [
        {
            "sport": "MLB",
            "matchup": "Tampa Bay Rays @ Kansas City Royals",
            "side": "Tampa Bay Rays ML",
            "odds": "-132",
            "reasoning": "Free pick on the moneyline.",
        }
    ]

    other = [
        {
            "sport": "MLB",
            "matchup": "Chicago White Sox @ Baltimore Orioles",
            "side": "Over 10.5",
            "odds": "+100",
            "reasoning": "Free pick on the total.",
        },
        {
            "sport": "MLB",
            "matchup": "New York Mets @ Toronto Blue Jays",
            "side": "Toronto Blue Jays ML",
            "odds": "-102",
            "reasoning": "Free pick on the moneyline.",
        },
    ]

    metadata = {
        "pick_count": "8 picks from 9 handicappers on X",
        "source": "Sharp X Picks",
        "timestamp": "2026-07-01 09:01",
    }

    # tempfile, not a fixed /tmp name: a predictable path in a world-writable
    # directory is the B108 finding, and this demo block is importable.
    _demo_dir = tempfile.mkdtemp(prefix="ivy_picks_demo_")
    pdf_path = formatter.generate_pdf(
        os.path.join(_demo_dir, "example_picks.pdf"),
        summary=(
            "Two sharps are locking in the Tampa Bay Rays ML (-132) against the Royals "
            "as our top consensus play for this 48-hour card. The full slate is packed with "
            "MLB action, featuring a mix of moneyline, totals, spreads, and high-value player props."
        ),
        consensus_picks=consensus,
        other_picks=other,
        metadata=metadata,
    )

    print(f"✅ PDF generated: {pdf_path}")
