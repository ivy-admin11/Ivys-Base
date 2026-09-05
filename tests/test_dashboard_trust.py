"""The dashboard must claim only what is actually known.

Two things made the old dashboard misleading, and neither was a crash:

  - It reported 85 picks as "pending", implying results were still coming.
    Their games were long past and outside the scores window, so no result
    would ever arrive. "Pending" and "ungradeable" are different claims.
  - It reported a hit rate off three decided picks. 2W-1L is not a 66.7%
    track record, and a percentage printed at one decimal place says
    otherwise.

These tests pin both, plus the repair script that moves existing rows into
the honest state.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from ivy_core import picks_tracker as pt
from ivy_core.pick_stats import MIN_DECIDED_FOR_RATE, UNVERIFIABLE, format_hit_rate, summarize

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "PICKS_DB", tmp_path / "picks.db")
    monkeypatch.setattr(pt, "log_picks_to_sheet", lambda *a, **k: None)
    monkeypatch.setattr(pt, "auto_sync_to_export_sheet", lambda *a, **k: None)
    monkeypatch.setattr(pt, "update_result_in_sheet", lambda *a, **k: None)
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


class TestHitRateThreshold:
    def test_a_small_sample_yields_no_rate(self):
        """Regression: 2W-1L was reported as a 66.7% hit rate."""
        assert summarize(wins=2, losses=1)["hit_rate"] is None

    def test_the_raw_record_is_always_available(self):
        s = summarize(wins=2, losses=1)
        assert (s["wins"], s["losses"], s["decided"]) == (2, 1, 3)

    def test_a_rate_appears_once_the_sample_is_big_enough(self):
        s = summarize(wins=15, losses=5)
        assert s["decided"] == MIN_DECIDED_FOR_RATE
        assert s["hit_rate"] == pytest.approx(75.0)

    def test_no_data_is_none_not_zero_percent(self):
        """0% is a claim about performance; None is the absence of one."""
        assert summarize()["hit_rate"] is None

    def test_wording_says_why_there_is_no_rate(self):
        assert "3 of 20" in format_hit_rate(summarize(wins=2, losses=1))

    def test_wording_carries_the_sample_size(self):
        assert "20 decided" in format_hit_rate(summarize(wins=15, losses=5))

    def test_pushes_never_count_toward_the_sample(self):
        assert summarize(wins=10, losses=10, pushes=50)["decided"] == 20


class TestUnverifiableState:
    def test_unverifiable_is_not_pending(self, isolated_db):
        pt.save_picks([pick(), pick(side="Bills +2.5")], "2026-09-05")
        pt.update_pick_result(ids(isolated_db)[0], UNVERIFIABLE)
        s = pt.get_stats_overall()
        assert s["unverifiable"] == 1
        assert s["pending"] == 1, "an ungradeable pick must not inflate pending"

    def test_unverifiable_is_not_a_loss(self, isolated_db):
        pt.save_picks([pick()], "2026-09-05")
        pt.update_pick_result(ids(isolated_db)[0], UNVERIFIABLE)
        s = pt.get_stats_overall()
        assert s["losses"] == 0 and s["decided"] == 0

    def test_unverifiable_still_counts_in_the_total(self, isolated_db):
        """The picks were made. Hiding them would flatter the record."""
        pt.save_picks([pick()], "2026-09-05")
        pt.update_pick_result(ids(isolated_db)[0], UNVERIFIABLE)
        assert pt.get_stats_overall()["total"] == 1

    def test_it_does_not_move_a_hit_rate(self, isolated_db):
        picks = [pick() for _ in range(21)]
        pt.save_picks(picks, "2026-09-05")
        all_ids = ids(isolated_db)
        for pid in all_ids[:15]:
            pt.update_pick_result(pid, "W")
        for pid in all_ids[15:20]:
            pt.update_pick_result(pid, "L")
        before = pt.get_stats_overall()["hit_rate"]
        pt.update_pick_result(all_ids[20], UNVERIFIABLE)
        assert pt.get_stats_overall()["hit_rate"] == pytest.approx(before)

    def test_the_summary_distinguishes_the_two(self, isolated_db):
        pt.save_picks([pick(), pick(side="Bills +2.5")], "2026-09-05")
        pt.update_pick_result(ids(isolated_db)[0], UNVERIFIABLE)
        out = pt.format_stats_for_pdf()
        assert "awaiting results" in out
        assert "ungradeable" in out

    def test_the_summary_never_prints_a_small_sample_rate(self, isolated_db):
        pt.save_picks([pick(), pick(side="B")], "2026-09-05")
        pt.update_pick_result(ids(isolated_db)[0], "W")
        out = pt.format_stats_for_pdf()
        assert "100.0%" not in out
        assert "no rate yet" in out

    def test_by_handicapper_reports_it_too(self, isolated_db):
        pt.save_picks([pick()], "2026-09-05")
        pt.update_pick_result(ids(isolated_db)[0], UNVERIFIABLE)
        assert pt.get_stats_by_handicapper()["@alice"]["unverifiable"] == 1

    def test_by_sport_reports_it_too(self, isolated_db):
        pt.save_picks([pick(sport="NFL")], "2026-09-05")
        pt.update_pick_result(ids(isolated_db)[0], UNVERIFIABLE)
        assert pt.get_stats_by_sport()["NFL"]["unverifiable"] == 1


class TestRepairScript:
    """The script that moves existing rows into the honest state."""

    def _seed(self, db):
        pt.save_picks([
            pick(game_day="2026-07-19", side="old one"),
            pick(game_day="2026-07-20", side="old two"),
            pick(game_day="2026-09-05", side="recent"),
            pick(game_day=None, side="no game_day"),
        ], "2026-09-05")
        return ids(db)

    def _seed_undated(self, db):
        """A row with no usable date at all.

        save_picks cannot produce one -- report_date is NOT NULL and always
        supplies a fallback -- so the undated branch is a safety net for rows
        written by other means, and has to be built directly to be exercised.
        """
        conn = sqlite3.connect(db)
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO picks (sport, matchup, side, report_date, game_day) "
                "VALUES ('NFL', 'A @ B', 'undated', '', NULL)"
            )
            cur.execute("INSERT INTO results (pick_id) VALUES (?)", (cur.lastrowid,))
            conn.commit()
        finally:
            conn.close()

    def test_stale_picks_are_separated_from_recent_ones(self, isolated_db):
        from scripts.repair_dashboard import find_unverifiable
        self._seed(isolated_db)
        conn = sqlite3.connect(isolated_db)
        try:
            stale, recent, undated = find_unverifiable(conn, today="2026-09-05")
        finally:
            conn.close()
        assert [r[5] for r in stale] == ["old one", "old two"]
        # "no game_day" falls back to its report_date, which is recent.
        assert [r[5] for r in recent] == ["recent", "no game_day"]
        assert undated == []

    def test_an_undated_pick_is_left_alone(self, isolated_db):
        """No date means no evidence it is too old. Leave it pending."""
        from scripts.repair_dashboard import find_unverifiable
        self._seed(isolated_db)
        self._seed_undated(isolated_db)
        conn = sqlite3.connect(isolated_db)
        try:
            stale, _, undated = find_unverifiable(conn, today="2026-09-05")
        finally:
            conn.close()
        assert [r[5] for r in undated] == ["undated"]
        assert not any(r[5] == "undated" for r in stale)

    def test_a_missing_game_day_falls_back_to_the_report_date(self, isolated_db):
        """The fallback is what keeps a dateless game from being written off."""
        from scripts.repair_dashboard import find_unverifiable
        self._seed(isolated_db)
        conn = sqlite3.connect(isolated_db)
        try:
            stale, recent, _ = find_unverifiable(conn, today="2026-09-05")
        finally:
            conn.close()
        assert "no game_day" in [r[5] for r in recent]
        assert "no game_day" not in [r[5] for r in stale]

    def test_dry_run_writes_nothing(self, isolated_db):
        from scripts.repair_dashboard import find_unverifiable, mark_unverifiable
        self._seed(isolated_db)
        conn = sqlite3.connect(isolated_db)
        try:
            stale, _, _ = find_unverifiable(conn, today="2026-09-05")
            mark_unverifiable(conn, stale, apply=False)
        finally:
            conn.close()
        assert pt.get_stats_overall()["unverifiable"] == 0

    def test_apply_marks_only_the_stale_ones(self, isolated_db):
        from scripts.repair_dashboard import find_unverifiable, mark_unverifiable
        self._seed(isolated_db)
        conn = sqlite3.connect(isolated_db)
        try:
            stale, _, _ = find_unverifiable(conn, today="2026-09-05")
            mark_unverifiable(conn, stale, apply=True)
        finally:
            conn.close()
        s = pt.get_stats_overall()
        assert s["unverifiable"] == 2
        assert s["pending"] == 2

    def test_it_never_overwrites_a_real_grade(self, isolated_db):
        """A pick already graded W keeps its W, however old it is."""
        from scripts.repair_dashboard import find_unverifiable, mark_unverifiable
        all_ids = self._seed(isolated_db)
        pt.update_pick_result(all_ids[0], "W")
        conn = sqlite3.connect(isolated_db)
        try:
            stale, _, _ = find_unverifiable(conn, today="2026-09-05")
            mark_unverifiable(conn, stale, apply=True)
        finally:
            conn.close()
        s = pt.get_stats_overall()
        assert s["wins"] == 1
        assert s["unverifiable"] == 1

    def test_running_it_twice_changes_nothing_further(self, isolated_db):
        from scripts.repair_dashboard import find_unverifiable, mark_unverifiable
        self._seed(isolated_db)
        for _ in range(2):
            conn = sqlite3.connect(isolated_db)
            try:
                stale, _, _ = find_unverifiable(conn, today="2026-09-05")
                mark_unverifiable(conn, stale, apply=True)
            finally:
                conn.close()
        assert pt.get_stats_overall()["unverifiable"] == 2

    @pytest.mark.parametrize("game_day,report_date,expected", [
        ("2026-09-06", "2026-09-05", "2026-09-06"),
        (None, "2026-09-05", "2026-09-05"),
        ("", "2026-09-05", "2026-09-05"),
        ("garbage", "2026-09-05", "2026-09-05"),
        ("2026-09-06T19:00:00", "2026-09-05", "2026-09-06"),
        (None, None, None),
    ])
    def test_date_resolution(self, game_day, report_date, expected):
        from scripts.repair_dashboard import pick_date
        assert pick_date(game_day, report_date) == expected

    def test_the_script_runs_dry_without_network(self):
        """End to end, with both network stages off."""
        out = subprocess.run(
            [sys.executable, "scripts/repair_dashboard.py", "--no-regrade", "--no-sheet"],
            cwd=REPO, capture_output=True, text=True, timeout=120,
        )
        assert out.returncode == 0, out.stderr
        assert "DRY-RUN" in out.stdout
