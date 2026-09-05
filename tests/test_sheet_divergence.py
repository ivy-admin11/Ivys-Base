"""The database and the sheet must not disagree in silence.

On 5 September four picks graded correctly while every Sheets call returned
404. The grades landed in the database, the dashboard showed none of them, and
the only trace was a log line that scrolled away. update_result_in_sheet
returned None whether it had written a row or not, so no caller could tell.

Divergence is now recorded per pick (results.sheet_synced), queryable, and
reported in the summary.
"""
from __future__ import annotations

import sqlite3

import pytest

from ivy_core import picks_tracker as pt
from ivy_core import sheets_logger as sl


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "PICKS_DB", tmp_path / "picks.db")
    monkeypatch.setattr(pt, "log_picks_to_sheet", lambda *a, **k: True)
    monkeypatch.setattr(pt, "auto_sync_to_export_sheet", lambda *a, **k: True)
    return tmp_path / "picks.db"


def pick(**over):
    base = {"sport": "NFL", "matchup": "Chiefs @ Bills", "side": "Chiefs -2.5",
            "handicapper": "@alice", "game_day": "2026-09-06"}
    base.update(over)
    return base


def ids(db):
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute("SELECT id FROM picks ORDER BY id")]
    finally:
        conn.close()


def sheet_write(result):
    """A stand-in for update_result_in_sheet with a fixed outcome."""
    def _f(*a, **k):
        if isinstance(result, Exception):
            raise result
        return result
    return _f


class TestTheWriteReportsItself:
    """update_result_in_sheet used to return None on every path."""

    def test_no_auth_is_false_not_none(self, monkeypatch):
        monkeypatch.setattr(sl, "_get_sheets_service", lambda: None)
        assert sl.update_result_in_sheet("A @ B", "A -1", "W") is False

    def test_a_pick_absent_from_the_sheet_is_false(self, monkeypatch):
        from tests.test_sheets_logger import FakeService, HEADER
        svc = FakeService([HEADER])
        monkeypatch.setattr(sl, "_get_sheets_service", lambda: svc)
        assert sl.update_result_in_sheet("Nobody @ Nowhere", "X", "W") is False

    def test_a_successful_write_is_true(self, monkeypatch):
        from tests.test_sheets_logger import FakeService, HEADER, sheet_row
        svc = FakeService([HEADER, sheet_row()])
        monkeypatch.setattr(sl, "_get_sheets_service", lambda: svc)
        assert sl.update_result_in_sheet("Chiefs @ Bills", "Chiefs -2.5", "W") is True

    def test_log_picks_reports_too(self, monkeypatch):
        monkeypatch.setattr(sl, "_get_sheets_service", lambda: None)
        assert sl.log_picks_to_sheet([{}], "2026-09-05") is False


class TestDivergenceIsRecorded:
    def test_a_failed_sheet_write_is_remembered(self, isolated_db, monkeypatch):
        """The exact 5 September failure: graded in the DB, absent from the sheet."""
        pt.save_picks([pick()], "2026-09-05")
        monkeypatch.setattr(pt, "update_result_in_sheet", sheet_write(False))
        pt.update_pick_result(ids(isolated_db)[0], "W")

        assert pt.get_stats_overall()["wins"] == 1, "the grade is in the database"
        missing = pt.unsynced_grades()
        assert len(missing) == 1
        assert missing[0]["result"] == "W"

    def test_a_successful_write_records_no_divergence(self, isolated_db, monkeypatch):
        pt.save_picks([pick()], "2026-09-05")
        monkeypatch.setattr(pt, "update_result_in_sheet", sheet_write(True))
        pt.update_pick_result(ids(isolated_db)[0], "W")
        assert pt.unsynced_grades() == []

    def test_an_exception_counts_as_divergence(self, isolated_db, monkeypatch):
        pt.save_picks([pick()], "2026-09-05")
        monkeypatch.setattr(pt, "update_result_in_sheet",
                            sheet_write(RuntimeError("404")))
        pt.update_pick_result(ids(isolated_db)[0], "W")
        assert len(pt.unsynced_grades()) == 1

    def test_unverifiable_picks_are_not_divergence(self, isolated_db, monkeypatch):
        """Writing off an ungradeable pick is a local decision, not a sheet write."""
        pt.save_picks([pick()], "2026-09-05")
        monkeypatch.setattr(pt, "update_result_in_sheet", sheet_write(False))
        pt.update_pick_result(ids(isolated_db)[0], pt.UNVERIFIABLE)
        assert pt.unsynced_grades() == []

    def test_ungraded_picks_are_not_divergence(self, isolated_db):
        pt.save_picks([pick()], "2026-09-05")
        assert pt.unsynced_grades() == []

    def test_the_summary_says_the_dashboard_is_behind(self, isolated_db, monkeypatch):
        pt.save_picks([pick()], "2026-09-05")
        monkeypatch.setattr(pt, "update_result_in_sheet", sheet_write(False))
        pt.update_pick_result(ids(isolated_db)[0], "W")
        out = pt.format_stats_for_pdf()
        assert "missing from the sheet" in out
        assert "understating" in out

    def test_the_summary_is_quiet_when_they_agree(self, isolated_db, monkeypatch):
        pt.save_picks([pick()], "2026-09-05")
        monkeypatch.setattr(pt, "update_result_in_sheet", sheet_write(True))
        pt.update_pick_result(ids(isolated_db)[0], "W")
        assert "missing from the sheet" not in pt.format_stats_for_pdf()


class TestReconciliation:
    def test_resync_fixes_what_it_can(self, isolated_db, monkeypatch):
        pt.save_picks([pick(), pick(side="B +1")], "2026-09-05")
        monkeypatch.setattr(pt, "update_result_in_sheet", sheet_write(False))
        for pid in ids(isolated_db):
            pt.update_pick_result(pid, "W")
        assert len(pt.unsynced_grades()) == 2

        monkeypatch.setattr(pt, "update_result_in_sheet", sheet_write(True))
        outcome = pt.resync_grades()
        assert outcome == {"attempted": 2, "fixed": 2, "still_missing": 0}
        assert pt.unsynced_grades() == []

    def test_resync_reports_what_it_could_not_fix(self, isolated_db, monkeypatch):
        pt.save_picks([pick()], "2026-09-05")
        monkeypatch.setattr(pt, "update_result_in_sheet", sheet_write(False))
        pt.update_pick_result(ids(isolated_db)[0], "W")
        outcome = pt.resync_grades()
        assert outcome["still_missing"] == 1
        assert len(pt.unsynced_grades()) == 1, "an unfixed grade stays flagged"

    def test_resync_is_idempotent(self, isolated_db, monkeypatch):
        pt.save_picks([pick()], "2026-09-05")
        monkeypatch.setattr(pt, "update_result_in_sheet", sheet_write(False))
        pt.update_pick_result(ids(isolated_db)[0], "W")
        monkeypatch.setattr(pt, "update_result_in_sheet", sheet_write(True))
        pt.resync_grades()
        assert pt.resync_grades() == {"attempted": 0, "fixed": 0, "still_missing": 0}

    def test_a_full_rebuild_clears_the_flag(self, isolated_db, monkeypatch):
        """The rebuild rewrites every row, so it resolves all divergence."""
        pt.save_picks([pick()], "2026-09-05")
        monkeypatch.setattr(pt, "update_result_in_sheet", sheet_write(False))
        pt.update_pick_result(ids(isolated_db)[0], "W")
        assert pt.unsynced_grades()
        assert pt.mark_all_synced() == 1
        assert pt.unsynced_grades() == []


class TestMigration:
    def test_an_existing_database_without_the_column_is_upgraded(self, tmp_path, monkeypatch):
        """The column has to be added to databases that predate it."""
        db = tmp_path / "old.db"
        conn = sqlite3.connect(db)
        conn.execute("""CREATE TABLE picks (id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT NOT NULL, matchup TEXT NOT NULL, side TEXT NOT NULL,
            odds REAL, handicapper TEXT, confidence TEXT, game_day TEXT,
            start_time TEXT, reasoning TEXT, report_date TEXT NOT NULL,
            sharp_count INTEGER DEFAULT 1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("""CREATE TABLE results (id INTEGER PRIMARY KEY AUTOINCREMENT,
            pick_id INTEGER NOT NULL UNIQUE, result TEXT, final_score TEXT,
            resolved_at TIMESTAMP)""")
        conn.execute("INSERT INTO picks (sport,matchup,side,report_date) "
                     "VALUES ('NFL','A @ B','A -1','2026-09-05')")
        conn.execute("INSERT INTO results (pick_id, result) VALUES (1,'W')")
        conn.commit()
        conn.close()

        monkeypatch.setattr(pt, "PICKS_DB", db)
        assert len(pt.unsynced_grades()) == 1, "pre-existing grades start unconfirmed"

        conn = sqlite3.connect(db)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(results)")}
        finally:
            conn.close()
        assert "sheet_synced" in cols

    def test_migration_runs_only_once(self, isolated_db):
        pt._init_db()
        pt._init_db()
        conn = sqlite3.connect(isolated_db)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(results)")]
        finally:
            conn.close()
        assert cols.count("sheet_synced") == 1
