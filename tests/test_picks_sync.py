"""ivy_core.picks_sync: canonical snapshot + summary orchestration.

Every test mocks ivy_core.sheets_logger entirely — none of these ever make
a real network call.
"""

from ivy_core import picks_sync
from ivy_core import picks_tracker as pt


def _pick(**overrides):
    base = {
        "sport": "NFL", "matchup": "A @ B", "side": "A -3", "odds": "-110",
        "handicappers": ["sharp1"], "confidence": "high", "game_day": "today",
        "start": "2026-08-01T20:00:00Z",
    }
    base.update(overrides)
    return base


def test_missing_configuration_is_not_reported_as_success(monkeypatch):
    monkeypatch.setattr(
        picks_sync.sheets_logger, "write_snapshot",
        lambda header, rows, tab_name=None: {"status": "not_configured", "reason": "no spreadsheet id"},
    )
    result = picks_sync.sync_canonical_snapshot()
    assert result["status"] == "not_configured"
    assert result["status"] != "success"


def test_never_reports_success_after_an_exception(monkeypatch):
    def boom(header, rows, tab_name=None):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(picks_sync.sheets_logger, "write_snapshot", boom)
    result = picks_sync.sync_canonical_snapshot()
    assert result["status"] == "error"


def test_two_identical_syncs_produce_the_same_snapshot_content_no_append(monkeypatch):
    pt.save_picks([_pick()], report_date="2026-07-21")

    calls = []
    monkeypatch.setattr(
        picks_sync.sheets_logger, "write_snapshot",
        lambda header, rows, tab_name=None: calls.append(("snapshot", header, rows)) or {"status": "success"},
    )
    monkeypatch.setattr(
        picks_sync.sheets_logger, "write_summary",
        lambda header, rows, tab_name=None: calls.append(("summary", header, rows)) or {"status": "success"},
    )

    result_1 = picks_sync.sync_canonical_snapshot()
    result_2 = picks_sync.sync_canonical_snapshot()

    assert result_1["status"] == "success"
    assert result_1["canonical_row_count"] == result_2["canonical_row_count"] == 1

    snapshot_calls = [c for c in calls if c[0] == "snapshot"]
    assert len(snapshot_calls) == 2
    assert snapshot_calls[0][1:] == snapshot_calls[1][1:], "unchanged data must produce identical snapshot content"
    # write_snapshot/write_summary are the only entry points used — no
    # separate append call exists anywhere in this orchestration path.


def test_summary_status_skipped_when_snapshot_write_fails(monkeypatch):
    monkeypatch.setattr(
        picks_sync.sheets_logger, "write_snapshot",
        lambda header, rows, tab_name=None: {"status": "error", "reason": "sheets_write_failed"},
    )
    summary_calls = []
    monkeypatch.setattr(
        picks_sync.sheets_logger, "write_summary",
        lambda header, rows, tab_name=None: summary_calls.append(1) or {"status": "success"},
    )

    result = picks_sync.sync_canonical_snapshot()

    assert result["status"] == "error"
    assert result["summary_status"] == "skipped"
    assert not summary_calls, "summary must not be written when the snapshot write failed"
