import sqlite3

import pytest

from ivy_core import picks_tracker


def test_save_picks_rolls_back_when_results_insert_fails(tmp_path, monkeypatch):
    db_path = tmp_path / "picks.db"
    monkeypatch.setattr(picks_tracker, "PICKS_DB", db_path)
    monkeypatch.setattr(picks_tracker, "log_picks_to_sheet", lambda *args, **kwargs: None)
    monkeypatch.setattr(picks_tracker, "auto_sync_to_export_sheet", lambda: None)

    real_connect = sqlite3.connect

    class FailingCursor:
        def __init__(self, cursor):
            self._cursor = cursor

        def execute(self, sql, params=()):
            if "INSERT INTO results" in sql:
                raise sqlite3.IntegrityError("forced failure")
            return self._cursor.execute(sql, params)

        @property
        def lastrowid(self):
            return self._cursor.lastrowid

    class FailingConnection:
        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            self._conn.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._conn.__exit__(exc_type, exc, tb)

        def cursor(self):
            return FailingCursor(self._conn.cursor())

        def __getattr__(self, name):
            return getattr(self._conn, name)

    def failing_connect(*args, **kwargs):
        return FailingConnection(real_connect(*args, **kwargs))

    monkeypatch.setattr(picks_tracker.sqlite3, "connect", failing_connect)

    with pytest.raises(sqlite3.IntegrityError):
        picks_tracker.save_picks(
            [
                {
                    "sport": "NBA",
                    "matchup": "A @ B",
                    "side": "A +2.5",
                    "odds": -110,
                }
            ],
            report_date="2026-07-27",
        )

    with real_connect(db_path) as conn:
        cursor = conn.cursor()
        pick_count = cursor.execute("SELECT COUNT(*) FROM picks").fetchone()[0]
        result_count = cursor.execute("SELECT COUNT(*) FROM results").fetchone()[0]

    assert pick_count == 0
    assert result_count == 0
