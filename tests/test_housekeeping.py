"""Log rotation and outbox retention.

Nothing rotated logs, and `ivy_core.outbox.cleanup_old_reports()` implemented a
TTL that no caller ever invoked, so retention was never actually enforced.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import housekeeping as hk  # noqa: E402

from ivy_core import outbox  # noqa: E402


# --------------------------------------------------------------------------
# Rotation
# --------------------------------------------------------------------------

def test_rotation_preserves_the_inode_an_open_descriptor_writes_to(tmp_path):
    """launchd holds an fd on StandardOutPath. A rename leaves it writing to
    the moved inode while the new file stays permanently empty, so rotation
    must copy-then-truncate."""
    p = tmp_path / "x.log"
    p.write_text("A" * 200)
    fh = open(p, "a")                        # stand-in for launchd's descriptor
    try:
        inode_before = p.stat().st_ino
        hk.rotate(p, max_bytes=10, keep=3, apply=True)
        assert p.stat().st_ino == inode_before
        assert (tmp_path / "x.log.1").read_text() == "A" * 200
        assert p.stat().st_size == 0
        fh.write("B" * 50)
        fh.flush()
    finally:
        fh.close()
    assert p.stat().st_size == 50            # the live write landed


def test_file_under_threshold_is_untouched(tmp_path):
    p = tmp_path / "small.log"
    p.write_text("x" * 100)
    assert hk.rotate(p, max_bytes=10_000, keep=3, apply=True) == []
    assert not (tmp_path / "small.log.1").exists()


def test_generations_shift_and_the_oldest_is_dropped(tmp_path):
    p = tmp_path / "y.log"
    p.write_text("new")
    for gen in (1, 2):
        p.with_suffix(f".log.{gen}").write_text(f"gen{gen}")
    hk.rotate(p, max_bytes=1, keep=2, apply=True)
    assert p.with_suffix(".log.1").read_text() == "new"
    assert p.with_suffix(".log.2").read_text() == "gen1"
    assert not p.with_suffix(".log.3").exists()


@pytest.mark.parametrize("name", sorted(hk.PROTECTED_NAMES))
def test_state_files_in_logs_are_never_rotated(tmp_path, name):
    """logs/ holds the execution-receipt DB, the poller's cursor state and the
    monitor state alongside the logs. Losing executions.db would destroy the
    record of what actually ran."""
    p = tmp_path / name
    p.write_bytes(b"z" * 10_000)
    assert hk.rotate(p, max_bytes=1, keep=3, apply=True) == []
    assert p.stat().st_size == 10_000


def test_dry_run_changes_nothing(tmp_path):
    p = tmp_path / "z.log"
    p.write_text("q" * 500)
    assert hk.rotate(p, max_bytes=1, keep=3, apply=False)   # reports intent
    assert p.stat().st_size == 500                          # but does nothing
    assert not (tmp_path / "z.log.1").exists()


# --------------------------------------------------------------------------
# Outbox retention
# --------------------------------------------------------------------------

def _report(dirpath, report_id, *, age_hours, job="sharp_picks", status="sent"):
    ts = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    (dirpath / f"{report_id}.json").write_text(json.dumps({
        "report_id": report_id, "job_name": job, "status": status,
        "generated_at": ts.isoformat(),
    }))
    (dirpath / f"{report_id}.pdf").write_bytes(b"%PDF fake")


def test_cleanup_removes_reports_past_the_ttl(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path)
    _report(tmp_path, "SP-OLD", age_hours=100)
    _report(tmp_path, "SP-NEW", age_hours=1)
    assert outbox.cleanup_old_reports() == 1
    assert not (tmp_path / "SP-OLD.json").exists()
    assert not (tmp_path / "SP-OLD.pdf").exists()
    assert (tmp_path / "SP-NEW.json").exists()


def test_newest_report_per_job_is_kept_however_old(tmp_path, monkeypatch):
    """Happy Hour and the meal planner run weekly, so their only report is past
    the 72h TTL for four days out of seven. Enforcing the TTL alone left
    MORE / WHY / PDF with nothing to resolve against."""
    monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path)
    _report(tmp_path, "HH-OLD", age_hours=24 * 30, job="happy_hour")
    _report(tmp_path, "HH-NEWEST", age_hours=24 * 6, job="happy_hour")
    assert outbox.cleanup_old_reports() == 1
    assert not (tmp_path / "HH-OLD.json").exists()
    # 6 days old — far past the 72h TTL, but it is the job's newest.
    assert (tmp_path / "HH-NEWEST.json").exists()


def test_retention_is_per_job_not_global(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path)
    for rid, job in (("SP-1", "sharp_picks"), ("HH-1", "happy_hour"), ("MP-1", "familia_meal_planner")):
        _report(tmp_path, rid, age_hours=24 * 20, job=job)
    # Each job's only report is its newest, so none may be removed —
    # one busy job must not keep another job's report alive, or vice versa.
    assert outbox.cleanup_old_reports() == 0
    for rid in ("SP-1", "HH-1", "MP-1"):
        assert (tmp_path / f"{rid}.json").exists()


def test_pending_survives_the_ttl_but_not_the_long_backstop(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path)
    _report(tmp_path, "SP-PEND", age_hours=100, status="pending")
    assert outbox.cleanup_old_reports() == 0
    _report(tmp_path, "SP-ANCIENT", age_hours=24 * (outbox.PENDING_TTL_DAYS + 1), status="pending")
    assert outbox.cleanup_old_reports() == 1
    assert not (tmp_path / "SP-ANCIENT.json").exists()
