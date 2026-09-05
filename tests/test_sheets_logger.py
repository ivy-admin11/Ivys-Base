"""Tests for the write path to the Sheets dashboard.

sheets_logger had no tests. It is the module that records picks and writes
grades back, and like result_updater it fails quietly: a wrong column does not
raise, it just produces a dashboard that stays empty while everything looks
like it ran. Three functions had each written the column layout down
separately, and all three disagreed. Those disagreements are pinned here.

Nothing in this file talks to Google. A recording double stands in for the
API so the row and range arguments can be asserted directly.
"""
from __future__ import annotations

import pytest

from ivy_core import sheets_logger as sl


# --------------------------------------------------------------------------
# A recording stand-in for the Sheets API.
# --------------------------------------------------------------------------
class FakeValues:
    def __init__(self, store):
        self.store = store

    def append(self, *, spreadsheetId, range, valueInputOption, body):
        self.store["appends"].append({"range": range, "values": body["values"]})
        return self

    def update(self, *, spreadsheetId, range, valueInputOption, body):
        self.store["updates"].append({"range": range, "values": body["values"]})
        return self

    def get(self, *, spreadsheetId, range):
        self.store["gets"].append(range)
        return self

    def execute(self):
        return {"values": self.store["sheet"]}


class FakeService:
    def __init__(self, sheet_rows=None):
        self.store = {
            "appends": [], "updates": [], "gets": [],
            "sheet": sheet_rows if sheet_rows is not None else [],
        }

    def spreadsheets(self):
        return self

    def values(self):
        return FakeValues(self.store)


@pytest.fixture
def service(monkeypatch):
    def _make(sheet_rows=None):
        svc = FakeService(sheet_rows)
        monkeypatch.setattr(sl, "_get_sheets_service", lambda: svc)
        return svc
    return _make


HEADER = list(sl.COLUMNS)


def sheet_row(sport="NFL", matchup="Chiefs @ Bills", side="Chiefs -2.5",
              odds="-110", handicapper="@someone", confidence="high",
              game_day="2026-09-06", start_time="16:25", report_date="2026-09-05",
              result="", final_score=""):
    return [sport, matchup, side, odds, handicapper, confidence,
            game_day, start_time, report_date, result, final_score]


# --------------------------------------------------------------------------
# The column map itself.
# --------------------------------------------------------------------------
class TestColumnLayout:
    def test_matches_the_header_sync_writes(self):
        """scripts/sync_picks_to_sheet.py creates the sheet; it is the truth."""
        assert HEADER == [
            "Sport", "Matchup", "Side", "Odds", "Handicapper", "Confidence",
            "GameDay", "StartTime", "ReportDate", "Result", "FinalScore",
        ]

    def test_result_is_column_J_not_K(self):
        """Regression: the grade was written to K while the summary read J."""
        assert sl.column_letter("Result") == "J"
        assert sl.column_letter("FinalScore") == "K"

    def test_last_column_letter_tracks_the_layout(self):
        assert sl.LAST_COLUMN_LETTER == "K"


# --------------------------------------------------------------------------
# Appending picks.
# --------------------------------------------------------------------------
class TestLogPicks:
    def test_row_is_built_in_header_order(self, service):
        """Regression: rows were appended shifted one column right.

        ReportDate was written first, pushing Sport into B and Matchup into C,
        so update_result_in_sheet -- which reads Matchup from B -- could never
        find a pick that this function had appended.
        """
        svc = service()
        sl.log_picks_to_sheet([{
            "sport": "NFL", "matchup": "Chiefs @ Bills", "side": "Chiefs -2.5",
            "odds": -110, "handicapper": "@someone", "confidence": "high",
            "game_day": "2026-09-06", "start_time": "16:25",
        }], "2026-09-05")

        row = svc.store["appends"][0]["values"][0]
        assert row[sl.COL["Sport"]] == "NFL"
        assert row[sl.COL["Matchup"]] == "Chiefs @ Bills"
        assert row[sl.COL["Side"]] == "Chiefs -2.5"
        assert row[sl.COL["ReportDate"]] == "2026-09-05"

    def test_row_length_matches_the_header(self, service):
        svc = service()
        sl.log_picks_to_sheet([{"sport": "NFL"}], "2026-09-05")
        assert len(svc.store["appends"][0]["values"][0]) == len(HEADER)

    def test_result_columns_start_empty(self, service):
        svc = service()
        sl.log_picks_to_sheet([{"sport": "NFL"}], "2026-09-05")
        row = svc.store["appends"][0]["values"][0]
        assert row[sl.COL["Result"]] == ""
        assert row[sl.COL["FinalScore"]] == ""

    def test_missing_fields_become_empty_not_missing(self, service):
        """A sparse pick must still produce a full-width row."""
        svc = service()
        sl.log_picks_to_sheet([{"matchup": "A @ B"}], "2026-09-05")
        row = svc.store["appends"][0]["values"][0]
        assert len(row) == len(HEADER)
        assert row[sl.COL["Matchup"]] == "A @ B"
        assert row[sl.COL["Sport"]] == ""

    def test_odds_are_stringified(self, service):
        svc = service()
        sl.log_picks_to_sheet([{"odds": -110}], "2026-09-05")
        assert svc.store["appends"][0]["values"][0][sl.COL["Odds"]] == "-110"

    def test_every_pick_becomes_a_row(self, service):
        svc = service()
        sl.log_picks_to_sheet([{"sport": "NFL"}, {"sport": "MLB"}], "2026-09-05")
        assert len(svc.store["appends"][0]["values"]) == 2

    def test_no_auth_is_survivable(self, monkeypatch):
        monkeypatch.setattr(sl, "_get_sheets_service", lambda: None)
        sl.log_picks_to_sheet([{"sport": "NFL"}], "2026-09-05")  # must not raise


# --------------------------------------------------------------------------
# Writing grades back.
# --------------------------------------------------------------------------
class TestUpdateResult:
    def test_grade_lands_in_the_result_column(self, service):
        """Regression: the grade went into K, the FinalScore column.

        get_sheet_summary reads grades from J, so every graded pick was
        counted as still pending and the hit rate never moved off zero.
        """
        svc = service([HEADER, sheet_row()])
        sl.update_result_in_sheet("Chiefs @ Bills", "Chiefs -2.5", "W")

        assert svc.store["updates"], "no write was attempted"
        rng = svc.store["updates"][0]["range"]
        assert "!J2" in rng, f"grade written to {rng}, expected column J"
        assert svc.store["updates"][0]["values"] == [["W"]]

    def test_notes_land_in_final_score(self, service):
        svc = service([HEADER, sheet_row()])
        sl.update_result_in_sheet("Chiefs @ Bills", "Chiefs -2.5", "W", notes="31-17")
        assert "!K2" in svc.store["updates"][1]["range"]
        assert svc.store["updates"][1]["values"] == [["31-17"]]

    def test_matches_the_right_row_among_many(self, service):
        svc = service([
            HEADER,
            sheet_row(matchup="A @ B", side="A -1"),
            sheet_row(matchup="Chiefs @ Bills", side="Chiefs -2.5"),
            sheet_row(matchup="C @ D", side="C -3"),
        ])
        sl.update_result_in_sheet("Chiefs @ Bills", "Chiefs -2.5", "L")
        assert "!J3" in svc.store["updates"][0]["range"]

    def test_matching_is_case_insensitive(self, service):
        svc = service([HEADER, sheet_row()])
        sl.update_result_in_sheet("CHIEFS @ BILLS", "chiefs -2.5", "W")
        assert svc.store["updates"]

    def test_same_matchup_different_side_is_not_a_match(self, service):
        """Both sides of one game sit in the sheet; grading must not cross them."""
        svc = service([HEADER, sheet_row(side="Chiefs -2.5")])
        sl.update_result_in_sheet("Chiefs @ Bills", "Bills +2.5", "W")
        assert svc.store["updates"] == []

    def test_unknown_pick_writes_nothing(self, service):
        svc = service([HEADER, sheet_row()])
        sl.update_result_in_sheet("Nobody @ Nowhere", "Nobody ML", "W")
        assert svc.store["updates"] == []

    def test_short_rows_do_not_crash(self, service):
        """Sheets omits trailing empties, so rows arrive ragged."""
        svc = service([HEADER, ["NFL"], ["NFL", "Chiefs @ Bills"], sheet_row()])
        sl.update_result_in_sheet("Chiefs @ Bills", "Chiefs -2.5", "P")
        assert "!J4" in svc.store["updates"][0]["range"]

    def test_header_row_is_never_graded(self, service):
        svc = service([HEADER])
        sl.update_result_in_sheet("Matchup", "Side", "W")
        assert svc.store["updates"] == []

    def test_no_auth_is_survivable(self, monkeypatch):
        monkeypatch.setattr(sl, "_get_sheets_service", lambda: None)
        sl.update_result_in_sheet("A @ B", "A -1", "W")  # must not raise


# --------------------------------------------------------------------------
# Reading the record back.
# --------------------------------------------------------------------------
class TestSummary:
    def test_counts_and_hit_rate(self, service):
        service([
            HEADER,
            sheet_row(result="W"), sheet_row(result="W"), sheet_row(result="W"),
            sheet_row(result="L"),
            sheet_row(result="P"),
            sheet_row(result=""),
        ])
        s = sl.get_sheet_summary()
        assert (s["wins"], s["losses"], s["pushes"], s["pending"]) == (3, 1, 1, 1)
        assert s["hit_rate"] == pytest.approx(75.0)
        assert s["total"] == 6

    def test_pushes_are_excluded_from_hit_rate(self, service):
        service([HEADER, sheet_row(result="W"), sheet_row(result="P")])
        assert sl.get_sheet_summary()["hit_rate"] == pytest.approx(100.0)

    def test_grades_written_by_update_are_the_ones_counted(self, service):
        """The end-to-end column agreement, in one assertion.

        This is the test that would have caught the original defect: a grade
        written by update_result_in_sheet must be read back by the summary.
        """
        rows = [HEADER, sheet_row()]
        svc = service(rows)
        sl.update_result_in_sheet("Chiefs @ Bills", "Chiefs -2.5", "W")

        col = ord(svc.store["updates"][0]["range"].split("!")[1][0]) - ord("A")
        rows[1][col] = "W"
        assert sl.get_sheet_summary()["wins"] == 1

    def test_no_rows_is_none_not_a_zero_record(self, service):
        service([HEADER])
        assert sl.get_sheet_summary() is None

    def test_empty_sheet_is_none(self, service):
        service([])
        assert sl.get_sheet_summary() is None

    def test_ragged_rows_count_as_pending(self, service):
        service([HEADER, ["NFL", "A @ B"], sheet_row(result="W")])
        s = sl.get_sheet_summary()
        assert s["wins"] == 1 and s["pending"] == 1

    def test_grades_are_case_and_space_tolerant(self, service):
        service([HEADER, sheet_row(result=" w "), sheet_row(result="l")])
        s = sl.get_sheet_summary()
        assert s["wins"] == 1 and s["losses"] == 1

    def test_no_auth_returns_none(self, monkeypatch):
        monkeypatch.setattr(sl, "_get_sheets_service", lambda: None)
        assert sl.get_sheet_summary() is None
