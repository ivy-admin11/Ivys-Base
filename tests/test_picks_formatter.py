"""Tests for the PDF report builder every proactive agent renders through.

picks_formatter had zero tests while being the last step of two live delivery
paths (happy_hour_scout, Familia_meal_planner). It feeds text that came from
the outside world -- venue names, recipe names, model-written reasoning --
straight into reportlab ``Paragraph`` objects, which parse their input as
mini-HTML. That makes "silently wrong PDF" and "the nightly job dies at the
last step" the two realistic failure modes, and both are pinned below.

Every PDF here is written into pytest's ``tmp_path``; nothing touches the repo
or the user's folders.
"""
from __future__ import annotations

import pytest
from pypdf import PdfReader

from picks_formatter import MARGIN, PicksReportFormatter

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import Table


SPORTS_FIELDS = ("sport", "matchup", "side", "odds", "reasoning")


def pick(**overrides) -> dict:
    """One row of a report, with the five keys the default column set reads."""
    row = {
        "sport": "MLB",
        "matchup": "Tampa Bay Rays @ Kansas City Royals",
        "side": "Tampa Bay Rays ML",
        "odds": "-132",
        "reasoning": "Free pick on the moneyline.",
    }
    row.update(overrides)
    return row


def formatter(**kwargs) -> PicksReportFormatter:
    kwargs.setdefault("title", "Ivy 48-Hour Betting Report")
    kwargs.setdefault("subtitle", "Sharp X Picks vs. Live Vegas Odds")
    return PicksReportFormatter(**kwargs)


def build(tmp_path, name="report.pdf", **kwargs) -> str:
    """Render a report into tmp_path and hand back the path."""
    kwargs.setdefault("summary", "Executive summary of the card.")
    kwargs.setdefault("consensus_picks", [pick()])
    kwargs.setdefault("other_picks", [])
    fmt = kwargs.pop("formatter", None) or formatter()
    return fmt.generate_pdf(str(tmp_path / name), **kwargs)


def text_of(path: str) -> str:
    """All extractable text in the PDF, newlines flattened.

    reportlab lays each table cell out as its own text run, so extraction is
    full of line breaks that have nothing to do with the source string.
    """
    reader = PdfReader(path)
    return " ".join(page.extract_text() for page in reader.pages).replace("\n", " ")


def page_count(path: str) -> int:
    return len(PdfReader(path).pages)


def tables_in(story) -> list:
    return [flowable for flowable in story if isinstance(flowable, Table)]


@pytest.fixture
def captured_story(monkeypatch):
    """Capture the flowable list handed to ``doc.build``.

    Some properties (a table's laid-out width, which rows repeat) are far
    easier to assert on the document structure than to reverse out of drawn
    PDF operators.
    """
    import picks_formatter

    captured: list = []
    real = picks_formatter.SimpleDocTemplate

    class Spy(real):
        def build(self, story, *args, **kwargs):
            captured.extend(story)
            return super().build(story, *args, **kwargs)

    monkeypatch.setattr(picks_formatter, "SimpleDocTemplate", Spy)
    return captured


class TestBasicRendering:
    def test_produces_a_readable_pdf_at_the_requested_path(self, tmp_path):
        path = build(tmp_path)
        assert path == str(tmp_path / "report.pdf")
        assert (tmp_path / "report.pdf").exists()
        assert page_count(path) == 1

    def test_supplied_text_actually_lands_in_the_pdf(self, tmp_path):
        # A PDF that builds but drops its content is the failure this module
        # is most able to hide, so assert on extracted text, not just bytes.
        path = build(
            tmp_path,
            summary="Two sharps are on the Rays.",
            consensus_picks=[pick(matchup="Rays @ Royals", odds="-132")],
            other_picks=[pick(matchup="Mets @ Blue Jays", odds="-102")],
        )
        body = text_of(path)
        for expected in (
            "Ivy 48-Hour Betting Report",
            "Sharp X Picks vs. Live Vegas Odds",
            "Two sharps are on the Rays.",
            "Rays @ Royals",
            "Mets @ Blue Jays",
            "-102",
        ):
            assert expected in body

    def test_consensus_section_is_rendered_before_the_other_section(self, tmp_path):
        # The whole point of the report is that the high-confidence plays are
        # what the reader sees first.
        path = build(
            tmp_path,
            consensus_picks=[pick(matchup="CONSENSUSMATCHUP")],
            other_picks=[pick(matchup="OTHERMATCHUP")],
        )
        body = text_of(path)
        assert body.index("CONSENSUSMATCHUP") < body.index("OTHERMATCHUP")

    def test_empty_report_still_builds(self, tmp_path):
        # An agent that found nothing today must not crash on delivery; the
        # section headings simply drop out.
        path = build(tmp_path, summary="No qualifying plays today.", consensus_picks=[], other_picks=[])
        body = text_of(path)
        assert "No qualifying plays today." in body
        assert "High-Likelihood" not in body
        assert page_count(path) == 1

    def test_short_report_fits_on_one_page(self, tmp_path):
        # Ten picks is the happy_hour_scout cap; that report is meant to be a
        # single page and a regression to two would be silent.
        path = build(tmp_path, consensus_picks=[pick() for _ in range(6)], other_picks=[pick() for _ in range(4)])
        assert page_count(path) == 1

    def test_unknown_color_scheme_falls_back_to_sports(self, tmp_path):
        # A typo'd scheme name must not KeyError at delivery time.
        fmt = formatter(color_scheme="not-a-scheme")
        assert fmt.theme is fmt.colors["sports"]
        assert build(tmp_path, formatter=fmt)


class TestMarkupEscaping:
    """BUG: caller text was passed to Paragraph unescaped.

    reportlab parses Paragraph text as mini-HTML. Real inputs contain the
    characters that means something to it: "Bar & Grill", an X handle like
    "@sharp<x", a model writing "line moved 5 < 6". Before the fix these
    either raised ValueError (killing the whole nightly job at the last step)
    or silently deleted everything that looked like a tag.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            "Bar & Grill <Wings>",       # silently lost "<Wings>"
            "Joe's <b Sports",           # raised: "parse ended with 2 unclosed tags"
            "@sharp<x picks",            # raised: "parse ended with 1 unclosed tags"
            "Taco <i>Bell",              # raised: saw </para> instead of </i>
            "AT&T Stadium",
            "Half & Half <br/> Deal",
        ],
    )
    def test_markup_characters_in_a_cell_survive_verbatim(self, tmp_path, raw):
        path = build(tmp_path, consensus_picks=[pick(matchup=raw)])
        assert raw in text_of(path)

    def test_markup_characters_in_reasoning_survive(self, tmp_path):
        raw = "Total moved 5 < 6 & the closing number is > 7"
        path = build(tmp_path, consensus_picks=[pick(reasoning=raw)])
        assert raw in text_of(path)

    def test_markup_characters_in_title_subtitle_and_summary_survive(self, tmp_path):
        path = build(
            tmp_path,
            formatter=formatter(title="Ivy & Co <Daily>", subtitle="Sharps < Books & Us"),
            summary="Value & edge on the <under>.",
        )
        body = text_of(path)
        assert "Ivy & Co <Daily>" in body
        assert "Sharps < Books & Us" in body
        assert "Value & edge on the <under>." in body

    def test_markup_characters_in_headings_and_headers_survive(self, tmp_path):
        path = build(
            tmp_path,
            headers=["Sport", "Matchup & Time", "Side", "Odds", "Why"],
            consensus_heading="Locks & Leans <Top>",
            other_heading="Rest & Res <B>",
            other_picks=[pick()],
        )
        body = text_of(path)
        assert "Matchup & Time" in body
        assert "Locks & Leans <Top>" in body
        assert "Rest & Res <B>" in body

    def test_markup_characters_in_footer_metadata_survive(self, tmp_path):
        path = build(
            tmp_path,
            metadata={
                "timestamp": "2026-07-01 09:01",
                "pick_count": "8 picks & 3 leans",
                "source": "Sharp <X> Picks",
            },
        )
        body = text_of(path)
        assert "8 picks & 3 leans" in body
        assert "Sharp <X> Picks" in body


class TestMissingAndNullFields:
    def test_missing_keys_render_as_blank_not_a_crash(self, tmp_path):
        # happy_hour_scout builds rows out of .get() calls on scraped data.
        path = build(tmp_path, consensus_picks=[{"matchup": "Solo Field Only"}])
        assert "Solo Field Only" in text_of(path)

    def test_explicit_none_does_not_print_the_word_none(self, tmp_path):
        # BUG: str(None) put a literal "None" in the odds column whenever an
        # upstream API returned a null field.
        path = build(tmp_path, consensus_picks=[pick(side=None, odds=None)])
        body = text_of(path)
        assert "Tampa Bay Rays @ Kansas City Royals" in body
        assert "None" not in body

    def test_none_valued_metadata_falls_back_instead_of_printing_none(self, tmp_path):
        # BUG: .get(key, default) only defaults a *missing* key, so a caller
        # passing {"source": None} shipped "Source: None" to the reader.
        path = build(tmp_path, metadata={"timestamp": None, "pick_count": None, "source": None})
        body = text_of(path)
        assert "None" not in body
        assert "Source: Ivy" in body

    def test_non_string_cell_values_are_coerced(self, tmp_path):
        path = build(tmp_path, consensus_picks=[pick(odds=-132)])
        assert "-132" in text_of(path)

    def test_no_metadata_still_writes_a_dated_footer(self, tmp_path):
        path = build(tmp_path, metadata=None)
        assert "For entertainment purposes only." in text_of(path)


class TestLayoutWidth:
    """BUG: the default columns were wider than the page's text frame.

    Letter minus the 0.75in margins leaves 7.0in of usable width; the default
    column widths summed to 7.5in and reportlab drew them anyway, pushing the
    Reasoning column half an inch into the right margin (to within 0.25in of
    the paper edge, where most printers clip).
    """

    def test_default_tables_fit_inside_the_text_frame(self, tmp_path, captured_story):
        build(tmp_path, other_picks=[pick()])
        frame_width = letter[0] - 2 * MARGIN
        tables = tables_in(captured_story)
        assert tables, "expected the divider plus both pick tables"
        for table in tables:
            width, _height = table.wrap(frame_width, letter[1])
            assert width <= frame_width + 0.01

    def test_oversized_caller_widths_are_scaled_down_to_fit(self, tmp_path, captured_story):
        # A caller adding a "when" column should not silently produce a report
        # that bleeds off the right edge.
        build(
            tmp_path,
            headers=["Sport", "When", "Matchup", "Side", "Odds", "Reasoning"],
            fields=["sport", "when", "matchup", "side", "odds", "reasoning"],
            col_widths=[1.5, 1.5, 3.0, 2.0, 1.5, 4.0],
            consensus_picks=[pick(when="Sat 7:05pm")],
        )
        frame_width = letter[0] - 2 * MARGIN
        for table in tables_in(captured_story):
            width, _height = table.wrap(frame_width, letter[1])
            assert width <= frame_width + 0.01

    def test_narrow_caller_widths_are_left_alone(self, tmp_path, captured_story):
        build(tmp_path, col_widths=[0.5, 1.0, 0.5, 0.5, 1.0], consensus_picks=[pick()])
        pick_tables = [t for t in tables_in(captured_story) if len(t._cellvalues[0]) == 5]
        assert pick_tables
        width, _height = pick_tables[0].wrap(letter[0] - 2 * MARGIN, letter[1])
        assert width == pytest.approx(3.5 * inch)


class TestPagination:
    def test_header_row_repeats_on_every_page_of_a_long_table(self, tmp_path):
        # BUG: a table that spilled onto page 2 left the reader with unlabelled
        # columns -- odds and side are indistinguishable without the header.
        path = build(tmp_path, consensus_picks=[pick(matchup=f"Team {i} @ Team {i + 1}") for i in range(60)])
        reader = PdfReader(path)
        assert len(reader.pages) > 1
        pages_with_rows = [
            page for page in reader.pages if "Team " in page.extract_text()
        ]
        assert len(pages_with_rows) > 1
        for page in pages_with_rows:
            assert "Matchup" in page.extract_text()

    def test_a_pick_longer_than_a_page_does_not_kill_the_report(self, tmp_path):
        # BUG: LayoutError "Flowable ... too large on page". A single row taller
        # than the frame cannot be split by default, so one over-long
        # model-written reasoning string aborted the entire delivery.
        long_reasoning = "Sharp money is hammering this side. " * 120
        path = build(tmp_path, consensus_picks=[pick(reasoning=long_reasoning)])
        body = text_of(path)
        assert page_count(path) > 1
        assert long_reasoning.strip()[-40:] in " ".join(body.split())


class TestCallerShapes:
    def test_happy_hour_shape_renders(self, tmp_path):
        # Mirrors proactive_agents/happy_hour_scout.format_happy_hour_pdf,
        # including the "&" that half the venue names in Frisco contain.
        fmt = PicksReportFormatter(
            title="Happy Hour Scout Discovery",
            subtitle="Frisco/Dallas Happy Hour Specials",
            color_scheme="happy_hour",
        )
        specials = [
            {"sport": "Haywire", "matchup": "Upscale Casual", "side": "Active",
             "odds": "Special", "reasoning": "$7 margaritas 3-6pm"},
            {"sport": "Sfereco Bar & Grill", "matchup": "Italian", "side": "Active",
             "odds": "Special", "reasoning": "1/2 off wine <by the glass>"},
        ]
        path = build(
            tmp_path,
            name="hh.pdf",
            formatter=fmt,
            summary="Ivy discovered 2 venues with 2 active happy hour specials.",
            consensus_picks=specials,
            other_picks=[],
            headers=["Venue", "Category", "Status", "Type", "Special"],
            metadata={"timestamp": "2026-09-05 16:00", "pick_count": "2 specials across 2 venues",
                      "source": "Happy Hour Scout"},
        )
        body = text_of(path)
        assert "Sfereco Bar & Grill" in body
        assert "1/2 off wine <by the glass>" in body
        assert page_count(path) == 1

    def test_meal_planner_shape_renders(self, tmp_path):
        fmt = PicksReportFormatter(
            title="Familia Weekly Meal Plan",
            subtitle="Venezuelan-American-Asian Fusion",
            color_scheme="meals",
        )
        recipes = [
            {"sport": "Venezuelan", "matchup": "Arepas de Pabellón", "side": "45 min",
             "odds": "Difficulty: Medium", "reasoning": "Shred beef finely, serve deconstructed"},
            {"sport": "Asian", "matchup": "Salmon & Rice Bowls", "side": "30 min",
             "odds": "Difficulty: Easy", "reasoning": "Mild sauce on the side"},
        ]
        path = build(
            tmp_path,
            name="meals.pdf",
            formatter=fmt,
            summary="Weekly meal plan with 2 recipes for a family of three.",
            consensus_picks=recipes,
            other_picks=[],
            headers=["Cuisine", "Recipe", "Time", "Difficulty", "Toddler Notes"],
            consensus_heading="This Week's Meals",
        )
        body = text_of(path)
        assert "Salmon & Rice Bowls" in body
        assert "This Week's Meals" in body
        assert page_count(path) == 1

    def test_extra_column_via_fields_is_rendered_in_order(self, tmp_path):
        path = build(
            tmp_path,
            headers=["Sport", "When", "Matchup", "Side", "Odds", "Reasoning"],
            fields=["sport", "when", "matchup", "side", "odds", "reasoning"],
            col_widths=[0.6, 1.0, 1.8, 1.2, 0.7, 1.7],
            consensus_picks=[pick(when="Sat 7:05pm")],
        )
        body = text_of(path)
        assert "When" in body
        assert "Sat 7:05pm" in body
