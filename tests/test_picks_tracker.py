"""Tests for the pick database and the records it reports.

picks_tracker is the last untested module in the grading pipeline. It owns the
SQLite store behind every "how are we doing" number, so a fault here does not
break a run -- it reports a record that is wrong, which is the failure mode
this pipeline keeps producing.

Every test runs against a temporary database. Nothing touches data/picks.db,
and the two Google Sheets calls save_picks makes are stubbed out.
"""
from __future__ import annotations

import sqlite3

import pytest

from ivy_core import picks_tracker as pt


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """A fresh database per test, and no network."""
    monkeypatch.setattr(pt, "PICKS_DB", tmp_path / "picks.db")
    monkeypatch.setattr(pt, "log_picks_to_sheet", lambda *a, **k: None)
    monkeypatch.setattr(pt, "auto_sync_to_export_sheet", lambda *a, **k: None)
    monkeypatch.setattr(pt, "update_result_in_sheet", lambda *a, **k: None)
    return tmp_path / "picks.db"


def pick(**over):
    base = {
        "sport": "NFL",
        "matchup": "Chiefs @ Bills",
        "side": "Chiefs -2.5",
        "odds": -110,
        "handicapper": "@alice",
        "confidence": "high",
        "game_day": "2026-09-06",
        "start_time": "16:25",
        "reasoning": "because",
    }
    base.update(over)
    return base


def rows(db, table="picks"):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        conn.close()


def pick_ids(db):
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute("SELECT id FROM picks ORDER BY id")]
    finally:
        conn.close()


class TestSavePicks:
    def test_saves_a_batch(self, isolated_db):
        pt.save_picks([pick(), pick(side="Bills +2.5")], "2026-09-05")
        assert len(rows(isolated_db)) == 2

    def test_every_pick_gets_a_pending_result_row(self, isolated_db):
        """The stats queries count pending via this row; without it a pick
        vanishes from every total."""
        pt.save_picks([pick()], "2026-09-05")
        results = rows(isolated_db, "results")
        assert len(results) == 1
        assert results[0][2] is None  # result column starts NULL

    def test_one_bad_pick_does_not_discard_the_batch(self, isolated_db):
        """Regression: the IntegrityError escaped before conn.commit().

        sport/matchup/side are NOT NULL and picks are parsed from free-form
        posts, so an incomplete one is routine. Losing the whole night's board
        to a single bad parse is not.
        """
        pt.save_picks([pick(), pick(side=None), pick(matchup="A @ B")], "2026-09-05")
        saved = rows(isolated_db)
        assert len(saved) == 2, "good picks in the batch must survive"

    def test_a_fully_bad_batch_is_survivable(self, isolated_db):
        pt.save_picks([pick(sport=None), pick(side=None)], "2026-09-05")
        assert rows(isolated_db) == []

    def test_result_rows_stay_aligned_after_a_skip(self, isolated_db):
        """A skipped pick must not leave an orphan or a misnumbered result."""
        pt.save_picks([pick(), pick(side=None), pick(side="Bills +2.5")], "2026-09-05")
        conn = sqlite3.connect(isolated_db)
        try:
            orphans = conn.execute(
                "SELECT COUNT(*) FROM results r "
                "LEFT JOIN picks p ON p.id = r.pick_id WHERE p.id IS NULL"
            ).fetchone()[0]
            unmatched = conn.execute(
                "SELECT COUNT(*) FROM picks p "
                "LEFT JOIN results r ON p.id = r.pick_id WHERE r.pick_id IS NULL"
            ).fetchone()[0]
        finally:
            conn.close()
        assert orphans == 0 and unmatched == 0

    def test_empty_batch_is_a_no_op(self, isolated_db):
        pt.save_picks([], "2026-09-05")
        assert rows(isolated_db) == []

    def test_accepts_merged_field_names(self, isolated_db):
        """Merged picks use start/handicappers; raw picks use start_time/handicapper."""
        pt.save_picks(
            [{"sport": "NFL", "matchup": "A @ B", "side": "A -1",
              "start": "19:00", "handicappers": ["@a", "@b"]}],
            "2026-09-05",
        )
        conn = sqlite3.connect(isolated_db)
        try:
            handicapper, start_time, sharps = conn.execute(
                "SELECT handicapper, start_time, sharp_count FROM picks"
            ).fetchone()
        finally:
            conn.close()
        assert start_time == "19:00"
        assert sharps == 2
        assert "@a" in handicapper and "@b" in handicapper

    def test_sharp_count_for_a_single_handicapper(self, isolated_db):
        pt.save_picks([pick(handicapper="@alice")], "2026-09-05")
        conn = sqlite3.connect(isolated_db)
        try:
            assert conn.execute("SELECT sharp_count FROM picks").fetchone()[0] == 1
        finally:
            conn.close()

    def test_unattributed_pick_counts_no_sharps(self, isolated_db):
        pt.save_picks([pick(handicapper=None)], "2026-09-05")
        conn = sqlite3.connect(isolated_db)
        try:
            assert conn.execute("SELECT sharp_count FROM picks").fetchone()[0] == 0
        finally:
            conn.close()

    def test_a_failing_sheet_does_not_lose_the_picks(self, isolated_db, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("Sheets is down")
        monkeypatch.setattr(pt, "log_picks_to_sheet", boom)
        monkeypatch.setattr(pt, "auto_sync_to_export_sheet", boom)
        pt.save_picks([pick()], "2026-09-05")
        assert len(rows(isolated_db)) == 1


class TestUpdateResult:
    def test_records_a_grade(self, isolated_db):
        pt.save_picks([pick()], "2026-09-05")
        pid = pick_ids(isolated_db)[0]
        pt.update_pick_result(pid, "W", "31-17")
        conn = sqlite3.connect(isolated_db)
        try:
            result, score, resolved = conn.execute(
                "SELECT result, final_score, resolved_at FROM results WHERE pick_id = ?", (pid,)
            ).fetchone()
        finally:
            conn.close()
        assert (result, score) == ("W", "31-17")
        assert resolved is not None

    def test_regrading_overwrites_rather_than_duplicates(self, isolated_db):
        pt.save_picks([pick()], "2026-09-05")
        pid = pick_ids(isolated_db)[0]
        pt.update_pick_result(pid, "W")
        pt.update_pick_result(pid, "L")
        assert len(rows(isolated_db, "results")) == 1
        assert pt.get_stats_overall()["losses"] == 1

    def test_unknown_pick_id_is_survivable(self, isolated_db):
        pt.save_picks([pick()], "2026-09-05")
        pt.update_pick_result(9999, "W")  # must not raise
        assert pt.get_stats_overall()["pending"] == 1

    def test_a_failing_sheet_does_not_lose_the_grade(self, isolated_db, monkeypatch):
        pt.save_picks([pick()], "2026-09-05")
        pid = pick_ids(isolated_db)[0]

        def boom(*a, **k):
            raise RuntimeError("Sheets is down")
        monkeypatch.setattr(pt, "update_result_in_sheet", boom)
        pt.update_pick_result(pid, "W")
        assert pt.get_stats_overall()["wins"] == 1


class TestOverallStats:
    def test_counts_and_hit_rate(self, isolated_db):
        pt.save_picks([pick() for _ in range(5)], "2026-09-05")
        ids = pick_ids(isolated_db)
        for pid, res in zip(ids, ["W", "W", "W", "L", "P"]):
            pt.update_pick_result(pid, res)
        s = pt.get_stats_overall()
        assert (s["wins"], s["losses"], s["pushes"], s["pending"]) == (3, 1, 1, 0)
        assert s["hit_rate"] == pytest.approx(75.0)
        assert s["total"] == 5

    def test_pushes_are_excluded_from_hit_rate(self, isolated_db):
        pt.save_picks([pick(), pick()], "2026-09-05")
        ids = pick_ids(isolated_db)
        pt.update_pick_result(ids[0], "W")
        pt.update_pick_result(ids[1], "P")
        assert pt.get_stats_overall()["hit_rate"] == pytest.approx(100.0)

    def test_ungraded_picks_are_pending_not_losses(self, isolated_db):
        pt.save_picks([pick(), pick()], "2026-09-05")
        s = pt.get_stats_overall()
        assert s["pending"] == 2 and s["losses"] == 0
        assert s["hit_rate"] == 0

    def test_empty_database_reports_zeroes(self, isolated_db):
        s = pt.get_stats_overall()
        assert s["total"] == 0 and s["hit_rate"] == 0


class TestHandicapperStats:
    def test_each_backer_of_a_consensus_pick_gets_credit(self, isolated_db):
        """Regression: a consensus pick was filed under a composite name.

        save_picks joins backers into one string, so grouping on that column
        produced a phantom "@alice, @bob" handicapper and credited neither
        real handle -- which quietly distorts any read of who is producing.
        """
        pt.save_picks(
            [{"sport": "NFL", "matchup": "A @ B", "side": "A -1",
              "handicappers": ["@alice", "@bob"]}],
            "2026-09-05",
        )
        pt.update_pick_result(pick_ids(isolated_db)[0], "W")
        stats = pt.get_stats_by_handicapper()

        assert "@alice" in stats and "@bob" in stats
        assert not any("," in name for name in stats), "composite handicapper leaked"
        assert stats["@alice"]["wins"] == 1
        assert stats["@bob"]["wins"] == 1

    def test_solo_and_consensus_picks_accumulate_together(self, isolated_db):
        pt.save_picks([
            {"sport": "NFL", "matchup": "A @ B", "side": "A -1", "handicapper": "@alice"},
            {"sport": "NFL", "matchup": "C @ D", "side": "C -2",
             "handicappers": ["@alice", "@bob"]},
        ], "2026-09-05")
        ids = pick_ids(isolated_db)
        pt.update_pick_result(ids[0], "W")
        pt.update_pick_result(ids[1], "L")

        stats = pt.get_stats_by_handicapper()
        assert stats["@alice"]["total"] == 2
        assert stats["@alice"]["wins"] == 1 and stats["@alice"]["losses"] == 1
        assert stats["@alice"]["hit_rate"] == pytest.approx(50.0)
        assert stats["@bob"]["total"] == 1 and stats["@bob"]["losses"] == 1
        assert stats["@bob"]["hit_rate"] == 0

    def test_unattributed_picks_are_labelled_not_dropped(self, isolated_db):
        pt.save_picks([pick(handicapper=None)], "2026-09-05")
        stats = pt.get_stats_by_handicapper()
        assert "unattributed" in stats
        assert None not in stats

    def test_ordered_by_wins(self, isolated_db):
        pt.save_picks([
            {"sport": "NFL", "matchup": "A @ B", "side": "A", "handicapper": "@few"},
            {"sport": "NFL", "matchup": "C @ D", "side": "C", "handicapper": "@many"},
            {"sport": "NFL", "matchup": "E @ F", "side": "E", "handicapper": "@many"},
        ], "2026-09-05")
        for pid in pick_ids(isolated_db):
            pt.update_pick_result(pid, "W")
        assert list(pt.get_stats_by_handicapper())[0] == "@many"


class TestSplitHandicappers:
    @pytest.mark.parametrize(
        "stored,expected",
        [
            ("@alice", ["@alice"]),
            ("@alice, @bob", ["@alice", "@bob"]),
            ("@alice,@bob", ["@alice", "@bob"]),
            ("@a, @b, @c", ["@a", "@b", "@c"]),
            (None, ["unattributed"]),
            ("", ["unattributed"]),
            (", ,", ["unattributed"]),
        ],
    )
    def test_splits(self, stored, expected):
        assert pt._split_handicappers(stored) == expected


class TestSportStats:
    def test_grouped_by_sport(self, isolated_db):
        pt.save_picks([pick(sport="NFL"), pick(sport="NFL"), pick(sport="MLB")], "2026-09-05")
        for pid in pick_ids(isolated_db):
            pt.update_pick_result(pid, "W")
        stats = pt.get_stats_by_sport()
        assert stats["NFL"]["wins"] == 2
        assert stats["MLB"]["wins"] == 1


class TestPdfSummary:
    def test_renders_without_data(self, isolated_db):
        out = pt.format_stats_for_pdf()
        assert "Sharp Picks Record" in out

    def test_reports_the_record(self, isolated_db):
        pt.save_picks([pick(), pick()], "2026-09-05")
        ids = pick_ids(isolated_db)
        pt.update_pick_result(ids[0], "W")
        pt.update_pick_result(ids[1], "L")
        out = pt.format_stats_for_pdf()
        assert "1W-1L" in out
        assert "50.0%" in out

    def test_names_individual_handicappers(self, isolated_db):
        pt.save_picks(
            [{"sport": "NFL", "matchup": "A @ B", "side": "A -1",
              "handicappers": ["@alice", "@bob"]}],
            "2026-09-05",
        )
        pt.update_pick_result(pick_ids(isolated_db)[0], "W")
        out = pt.format_stats_for_pdf()
        assert "@alice" in out and "@bob" in out
        assert "@alice, @bob" not in out
