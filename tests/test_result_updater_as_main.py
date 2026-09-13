"""The scheduled grader runs as `python -m ivy_core.result_updater`. Test it
that way.

For as long as the ESPN fallback existed, every test of it imported the
module. The launchd job does not import it -- it executes it, top to bottom,
and the `if __name__ == "__main__"` guard sat ABOVE `def backfill_pick`. So
the guard ran auto_update_results() before that def existed, every pick's
fallback raised NameError, the except logged it at DEBUG, and the job wrote
"falling back to ESPN" then "0 updated" one millisecond later, four times a
day, from the day the fallback was written. 35 picks pending on 2026-09-13
with a working scoreboard three lines of code away.
"""

import ast
import logging
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
MODULE = REPO / "ivy_core" / "result_updater.py"


def test_the_main_guard_is_the_last_statement_in_the_module():
    """Nothing may be defined below the guard: under -m it never executes."""
    tree = ast.parse(MODULE.read_text())
    last = tree.body[-1]
    assert isinstance(last, ast.If) and ast.unparse(last.test) == "__name__ == '__main__'", (
        f"last top-level statement is {type(last).__name__}, not the __main__ guard"
    )


def test_every_function_auto_update_results_calls_is_defined_above_the_guard():
    tree = ast.parse(MODULE.read_text())
    guard_line = tree.body[-1].lineno
    defined = {n.name: n.lineno for n in tree.body if isinstance(n, ast.FunctionDef)}
    auto = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == "auto_update_results")
    called = {
        node.func.id for node in ast.walk(auto)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    late = {name: defined[name] for name in called if name in defined and defined[name] > guard_line}
    assert not late, f"defined after the __main__ guard, invisible under -m: {late}"


def test_running_as_main_reaches_the_espn_fallback(tmp_path):
    """Execute the module exactly as launchd does. With one pending pick and
    the Odds API returning nothing, the ESPN fetch must at least be
    ATTEMPTED -- the NameError never got that far."""
    import sqlite3
    db = tmp_path / "picks.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE picks (id INTEGER PRIMARY KEY, sport TEXT, matchup TEXT, side TEXT,
            odds REAL, handicapper TEXT, confidence TEXT, game_day TEXT, start_time TEXT,
            reasoning TEXT, report_date TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sharp_count INTEGER);
        CREATE TABLE results (pick_id INTEGER, result TEXT, final_score TEXT,
            updated_at TIMESTAMP, sheet_synced INTEGER DEFAULT 0);
        INSERT INTO picks (sport, matchup, side, report_date, sharp_count)
            VALUES ('NCAAF', 'Ohio State Buckeyes @ Texas Longhorns', 'Ohio State Buckeyes +1.5', '2026-09-12', 1);
        INSERT INTO results (pick_id) VALUES (1);
    """)
    con.commit()
    con.close()

    # A sitecustomize that redirects the DB and stubs both score sources, so
    # the subprocess touches no network and no real file. It records whether
    # backfill_pick's fetch was reached.
    marker = tmp_path / "espn_was_called"
    real_db = REPO / "data" / "picks.db"
    real_mtime = real_db.stat().st_mtime if real_db.exists() else None

    # Under -m the module is executed as a fresh `__main__` namespace, so
    # patching attributes on the imported `ivy_core.result_updater` object
    # does not reach it -- its own PICKS_DB is re-evaluated from __file__.
    # Redirect at the sqlite3 level instead, which every namespace shares,
    # and stub the two network sources on their shared modules.
    (tmp_path / "sitecustomize.py").write_text(f'''
import sys, pathlib, sqlite3
sys.path.insert(0, {str(REPO)!r})
_real_connect = sqlite3.connect
def _connect(path, *a, **k):
    if str(path).endswith("picks.db"):
        path = {str(db)!r}
    return _real_connect(path, *a, **k)
sqlite3.connect = _connect
import requests
requests.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no network in this test"))
import ivy_core.historical_scores as hs
def _fetch(sport, date):
    pathlib.Path({str(marker)!r}).write_text(f"{{sport}} {{date}}")
    return []
hs.fetch_completed_games = _fetch
import ivy_core.picks_tracker as pt
pt.auto_sync_to_export_sheet = lambda: None
''')
    env = {"PYTHONPATH": f"{tmp_path}:{REPO}", "ALLOW_INSECURE_ADMIN_SECRET": "true",
           "ADMIN_SECRET": "x", "HENRY_PHONE": "+1", "XAI_API_KEY": "t",
           "ODDS_API_KEY": "", "ENABLE_IMESSAGE_POLLER": "false",
           "PATH": "/usr/bin:/bin"}
    proc = subprocess.run(
        [sys.executable, "-m", "ivy_core.result_updater"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=60,
    )
    out = proc.stdout + proc.stderr
    assert "NameError" not in out, out[-800:]
    assert marker.exists(), (
        "ran as __main__ and the ESPN fetch was never reached -- "
        "something is defined below the guard again\n" + out[-800:]
    )
    assert marker.read_text() == "NCAAF 2026-09-12", (
        "graded the wrong pick -- did the subprocess read the real database?"
    )
    if real_mtime is not None:
        assert real_db.stat().st_mtime == real_mtime, "the live picks.db was touched by a test"


def test_a_failing_fallback_is_loud(monkeypatch, tmp_path, caplog):
    """DEBUG hid 35 failures a run. It has to be WARNING, and a fallback that
    fails for every pick has to say so at ERROR."""
    import sqlite3
    from ivy_core import result_updater as ru
    db = tmp_path / "picks.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE picks (id INTEGER PRIMARY KEY, sport TEXT, matchup TEXT, side TEXT,
            odds REAL, handicapper TEXT, confidence TEXT, game_day TEXT, start_time TEXT,
            reasoning TEXT, report_date TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sharp_count INTEGER);
        CREATE TABLE results (pick_id INTEGER, result TEXT, final_score TEXT,
            updated_at TIMESTAMP, sheet_synced INTEGER DEFAULT 0);
        INSERT INTO picks (sport, matchup, side, report_date, sharp_count) VALUES ('NFL','A @ B','A ML','2026-09-10',1);
        INSERT INTO results (pick_id) VALUES (1);
    """)
    con.commit()
    con.close()
    monkeypatch.setattr(ru, "PICKS_DB", db)
    monkeypatch.setattr(ru, "get_completed_games", lambda *a, **k: [])

    def boom(pick, cache):
        raise NameError("name 'backfill_pick' is not defined")
    monkeypatch.setattr(ru, "backfill_pick", boom)

    with caplog.at_level(logging.DEBUG, logger="ivy.result_updater"):
        ru.auto_update_results()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "ESPN grading failed" in r.getMessage()]
    assert warnings, "the per-pick failure is still at DEBUG"
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "every pick" in r.getMessage()]
    assert errors, "a fallback that fails for every pick must be an ERROR"
