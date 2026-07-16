"""
Picks Formatter — Professional PDF generation for sports picks, happy hour, meals, etc.

Converts pick data into polished, branded PDF reports matching the format shown
in the 48-Hour Betting Report image. Reusable across sports_bettor, happy_hour_scout,
familia_meal_planner, and other agents.

Effort: ~200 lines of reportlab code. Dependencies already in requirements.txt.
"""

from datetime import datetime
from typing import List, Dict, Optional

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, white
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
)
from reportlab.lib.enums import TA_CENTER


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
            metadata: Optional dict with pick_count, source, timestamp
            headers: Optional column header labels (defaults to the sports-report
                labels); pass domain-appropriate labels for non-sports callers.
                Must have the same length as `fields`.
            col_widths: Optional column widths in inches (must sum to <= 6.5in
                given the 0.75in margins); defaults to the sports-report widths.
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
        cell_style = ParagraphStyle(
            "TableCell", parent=getSampleStyleSheet()["Normal"], fontSize=8, leading=10,
        )
        header_style = ParagraphStyle(
            "TableCellHeader", parent=getSampleStyleSheet()["Normal"],
            fontSize=9, leading=11, textColor=white, fontName="Helvetica-Bold",
        )

        def _row(cells: List[str]) -> List[Paragraph]:
            return [Paragraph(str(c), cell_style) for c in cells]
        doc = SimpleDocTemplate(
            filename,
            pagesize=letter,
            rightMargin=0.75 * inch,
            leftMargin=0.75 * inch,
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
        story.append(Paragraph(self.title, title_style))

        subtitle_style = ParagraphStyle(
            "CustomSubtitle",
            parent=styles["Normal"],
            fontSize=11,
            textColor=self.theme["header"],
            spaceAfter=12,
            alignment=TA_CENTER,
            fontName="Helvetica",
        )
        story.append(Paragraph(self.subtitle, subtitle_style))

        # Divider line
        divider_table = Table([["" * 80]], colWidths=[7.5 * inch])
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
        story.append(Paragraph(summary, summary_style))
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
            story.append(Paragraph(consensus_heading, heading_style))

            consensus_table_data = [_row(headers)]
            for pick in consensus_picks:
                consensus_table_data.append(_row([pick.get(f, "") for f in fields]))
            # Header row keeps the bold/white header style regardless of _row's default.
            consensus_table_data[0] = [Paragraph(str(h), header_style) for h in headers]

            consensus_table = Table(
                consensus_table_data,
                colWidths=[w * inch for w in col_widths],
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
            story.append(Paragraph(other_heading, heading_style))

            other_table_data = [[Paragraph(str(h), header_style) for h in headers]]
            for pick in other_picks:
                other_table_data.append(_row([pick.get(f, "") for f in fields]))

            other_table = Table(
                other_table_data,
                colWidths=[w * inch for w in col_widths],
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
            footer_text = (
                f"Generated {metadata.get('timestamp', 'N/A')} • "
                f"{metadata.get('pick_count', '0')} pick(s) • "
                f"Source: {metadata.get('source', 'Ivy')} • "
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
        "pick_count": "8 pick(s) swept from 9 curated X handicappers",
        "source": "Sharp X Picks",
        "timestamp": "2026-07-01 09:01",
    }

    pdf_path = formatter.generate_pdf(
        "/tmp/example_picks.pdf",
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
