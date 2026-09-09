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

# isolated_db stubs auto_sync_to_export_sheet so save_picks stays offline.
# Hold the real one from before that patch, or these tests exercise the stub.
REAL_AUTO_SYNC = pt.auto_sync_to_export_sheet


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


class TestScriptLoadsEnvironment:
    """Regression: the first real run reported the API key missing.

    It was in .env the whole time. config.py owns the load_dotenv call, and
    nothing under ivy_core triggers it -- so a script that imports only
    ivy_core modules runs with an empty environment and silently grades
    nothing. Any entry point that reads configuration has to import config.
    """

    def test_the_repair_script_imports_config(self):
        import ast

        tree = ast.parse((REPO / "scripts" / "repair_dashboard.py").read_text())
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "config" in imported, (
            "repair_dashboard.py must import config, which is what loads .env; "
            "without it the script runs with no API key and grades nothing"
        )

    def test_importing_config_populates_the_environment(self, tmp_path, monkeypatch):
        """config.load_dotenv is the mechanism, so prove it actually loads."""
        import importlib
        import os

        env = tmp_path / ".env"
        env.write_text("IVY_TEST_SENTINEL=loaded\n")
        monkeypatch.delenv("IVY_TEST_SENTINEL", raising=False)

        from dotenv import load_dotenv
        load_dotenv(dotenv_path=env, override=False)
        assert os.getenv("IVY_TEST_SENTINEL") == "loaded"
        importlib.invalidate_caches()


class TestScriptsAreRunnableStandalone:
    """Regression: sync_picks_to_sheet.py died with "No module named 'ivy_core'".

    Run as `python scripts/x.py`, sys.path[0] is scripts/, so repo modules are
    not importable. The script had grown an ivy_core import without the
    matching path setup, and only failed at the point of use -- partway
    through a rebuild, after reporting progress.
    """

    @pytest.mark.parametrize(
        "script", sorted(p.name for p in (REPO / "scripts").glob("*.py"))
    )
    def test_a_script_importing_repo_modules_puts_the_root_on_the_path(self, script):
        import ast

        source = (REPO / "scripts" / script).read_text()
        tree = ast.parse(source)

        repo_modules = {"ivy_core", "config", "proactive_agents", "picks_formatter"}
        imports_repo = any(
            (isinstance(n, ast.ImportFrom) and n.module and n.module.split(".")[0] in repo_modules)
            or (isinstance(n, ast.Import) and any(a.name.split(".")[0] in repo_modules for a in n.names))
            for n in ast.walk(tree)
        )
        if not imports_repo:
            pytest.skip(f"{script} imports no repo modules")

        assert "sys.path.insert" in source or "sys.path.append" in source, (
            f"{script} imports repo modules but never puts the repo root on "
            f"sys.path; it will fail with ModuleNotFoundError when run directly"
        )

    def test_the_sync_script_compiles_and_resolves_its_imports(self):
        """Catch an ImportError at test time rather than mid-rebuild."""
        out = subprocess.run(
            [sys.executable, "-c",
             "import importlib.util, sys, pathlib;"
             "p = pathlib.Path('scripts/sync_picks_to_sheet.py');"
             "spec = importlib.util.spec_from_file_location('sync_check', p);"
             "m = importlib.util.module_from_spec(spec);"
             "spec.loader.exec_module(m);"
             "print('COLUMNS', len(m.COLUMNS))"],
            cwd=REPO, capture_output=True, text=True, timeout=120,
        )
        assert out.returncode == 0, f"module-level import failed:\n{out.stderr}"
        assert "COLUMNS 12" in out.stdout


class _Exec:
    """Every Sheets call ends in .execute(); this carries its payload."""

    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeSheetsAPI:
    """The chained Sheets client, recording what gets appended."""

    def __init__(self, rows, tab="Sharp Picks", gid=1305096861):
        self.rows = [list(r) for r in rows]
        self.appended = []
        self.tab, self.gid = tab, gid

    # service.spreadsheets()
    def spreadsheets(self):
        return self

    # .get(spreadsheetId=...) -> metadata
    def get(self, *, spreadsheetId, range=None, **kw):
        if range is None:
            return _Exec({"sheets": [{"properties": {"sheetId": self.gid,
                                                     "title": self.tab}}]})
        return _Exec({"values": self.rows})

    # .values()
    def values(self):
        return self

    def append(self, *, spreadsheetId, range, valueInputOption, body):
        self.appended.append(body["values"])
        self.rows.extend(body["values"])
        return _Exec({})

    def update(self, *, spreadsheetId, range, valueInputOption, body):
        return _Exec({})


class TestAutoSyncDoesNotDuplicate:
    """Regression: every save re-appended the entire pick history.

    auto_sync_to_export_sheet selects all picks and appends them, and runs
    after every save_picks. Its comment claimed it added only new picks; there
    was no filter. A sheet freshly rebuilt to 88 rows would reach 176 on the
    next picks job and 264 on the one after, every row duplicated. It runs
    unattended, so nothing would have reported it.
    """

    @pytest.fixture
    def api(self, monkeypatch):
        from ivy_core.sheets_logger import COLUMNS
        fake = FakeSheetsAPI([list(COLUMNS)])
        monkeypatch.setattr("ivy_core.sheets_logger._get_sheets_service", lambda: fake)
        return fake

    def _sync(self):
        return REAL_AUTO_SYNC()

    def test_first_sync_writes_every_pick(self, isolated_db, api):
        pt.save_picks([pick(side="A -1"), pick(side="B +1")], "2026-09-05")
        assert self._sync() is True
        assert sum(len(b) for b in api.appended) == 2

    def test_a_second_sync_appends_nothing(self, isolated_db, api):
        pt.save_picks([pick(side="A -1"), pick(side="B +1")], "2026-09-05")
        self._sync()
        api.appended.clear()
        assert self._sync() is True
        appended = sum(len(b) for b in api.appended)
        assert appended == 0, (
            f"second sync re-appended {appended} row(s) already on the sheet — "
            "this is the duplication bug"
        )

    def test_only_the_new_pick_is_appended(self, isolated_db, api):
        from ivy_core.sheets_logger import COL
        pt.save_picks([pick(side="A -1")], "2026-09-05")
        self._sync()
        pt.save_picks([pick(side="B +1")], "2026-09-05")
        api.appended.clear()
        self._sync()

        appended = [r for batch in api.appended for r in batch]
        assert len(appended) == 1, f"expected only the new pick, got {appended}"
        assert appended[0][COL["Side"]] == "B +1"

    def test_the_sheet_does_not_grow_when_nothing_is_added(self, isolated_db, api):
        pt.save_picks([pick(side=f"S{i}") for i in range(5)], "2026-09-05")
        self._sync()
        size = len(api.rows)
        for _ in range(3):
            self._sync()
        assert len(api.rows) == size, "repeated syncs must not grow the sheet"

    def test_matching_ignores_case_and_padding(self, isolated_db, api):
        """A row already on the sheet must not be re-added over whitespace."""
        from ivy_core.sheets_logger import COL
        pt.save_picks([pick(side="A -1")], "2026-09-05")
        self._sync()
        api.rows[1][COL["Side"]] = "  a -1  "
        api.appended.clear()
        self._sync()
        assert sum(len(b) for b in api.appended) == 0


class TestTheClosingSummaryIsTrue:
    """The repair script reported "88 pick(s) marked unverifiable" on a run
    that marked exactly one.

    `marked` held the write-off count, then step 6 reassigned it to
    mark_all_synced()'s row count — 88 — and the closing line printed that.
    A summary that overstates by 88x is the same class of fault as everything
    else this pipeline has produced: confidently wrong, nothing raised.
    """

    def test_the_write_off_count_is_not_shadowed(self):
        import re

        src = (REPO / "scripts" / "repair_dashboard.py").read_text()
        body = src[src.index("def main("):]
        assigns = re.findall(r"^\s*marked\s*=", body, re.M)
        assert len(assigns) == 1, (
            f"`marked` is assigned {len(assigns)} times in main(); a second "
            "assignment is what made the closing line report the wrong number"
        )

    def test_mark_all_synced_uses_its_own_name(self):
        src = (REPO / "scripts" / "repair_dashboard.py").read_text()
        assert "confirmed = mark_all_synced()" in src

    def test_the_backfill_runs_before_the_write_off(self):
        """Writing a pick off as ungradeable and then trying to grade it is
        backwards; the write-off should only cover what nothing reached."""
        src = (REPO / "scripts" / "repair_dashboard.py").read_text()
        body = src[src.index("def main("):]
        assert body.index("Backfill from historical scoreboards") < body.index(
            "Ungraded picks still with no source"
        )


class TestTheRecordCountsBetsNotMentions:
    """The first successful backfill produced 14W-13L over "27 decided".

    It was 11W-10L over 21. "Over 9.5" on one Padres/Royals game was recorded
    four times and counted as four wins. Four handicappers agreeing is
    consensus — worth knowing, and not four independent outcomes. Counting it
    four times inflates the sample and moves the rate on a single game, which
    matters at a threshold: 51.9% and 52.4% sit either side of break-even at
    standard -110 juice.
    """

    def _seed(self, db, picks):
        pt.save_picks(picks, "2026-09-05")
        return ids(db)

    def test_the_same_bet_from_four_sources_counts_once(self, isolated_db):
        same = [pick(matchup="Jets @ Dolphins", side="Over 9.5", handicapper=f"@h{i}",
                     game_day="2026-09-05") for i in range(4)]
        for pid in self._seed(isolated_db, same):
            pt.update_pick_result(pid, "W")
        s = pt.get_stats_overall()
        assert s["wins"] == 1, "one game, one outcome"
        assert s["decided"] == 1

    def test_the_collapse_is_reported_not_hidden(self, isolated_db):
        same = [pick(matchup="Jets @ Dolphins", side="Over 9.5", handicapper=f"@h{i}",
                     game_day="2026-09-05") for i in range(4)]
        for pid in self._seed(isolated_db, same):
            pt.update_pick_result(pid, "W")
        s = pt.get_stats_overall()
        assert s["graded_picks"] == 4
        assert s["duplicate_picks"] == 3
        assert "same bet posted more than once" in pt.format_stats_for_pdf()

    def test_different_sides_of_one_game_stay_separate(self):
        """Over and Under on the same game are two bets, not a duplicate."""
        rows = [("Jets @ Dolphins", "Over 9.5", "W", "2026-09-05", "2026-09-05"),
                ("Jets @ Dolphins", "Under 9.5", "L", "2026-09-05", "2026-09-05")]
        assert len(pt._distinct_bets(rows)) == 2

    def test_the_same_side_on_different_days_stays_separate(self):
        rows = [("Jets @ Dolphins", "Over 9.5", "W", "2026-09-05", "2026-09-05"),
                ("Jets @ Dolphins", "Over 9.5", "L", "2026-09-06", "2026-09-06")]
        assert len(pt._distinct_bets(rows)) == 2

    def test_casing_and_spacing_do_not_defeat_the_collapse(self):
        rows = [("Jets @ Dolphins", "Over 9.5", "W", None, "2026-09-05"),
                ("jets @ dolphins", "  over 9.5 ", "W", None, "2026-09-05")]
        assert len(pt._distinct_bets(rows)) == 1

    def test_a_today_game_day_still_collapses(self):
        """resolve_pick_date has to run before the key is built, or the same
        bet lands under two different keys."""
        rows = [("Jets @ Dolphins", "Over 9.5", "W", "today", "2026-09-05"),
                ("Jets @ Dolphins", "Over 9.5", "W", None, "2026-09-05")]
        assert len(pt._distinct_bets(rows)) == 1

    def test_no_duplicates_reports_nothing_extra(self, isolated_db):
        distinct = [pick(matchup=f"A{i} @ B{i}", side="Over 9.5",
                         game_day="2026-09-05") for i in range(3)]
        for pid in self._seed(isolated_db, distinct):
            pt.update_pick_result(pid, "W")
        assert pt.get_stats_overall()["duplicate_picks"] == 0
        assert "same bet posted more than once" not in pt.format_stats_for_pdf()
