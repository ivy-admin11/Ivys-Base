"""Log rotation and outbox retention (B6).

Nothing in this project rotated or pruned anything: logs/ had grown to 8.7 MB
across 78 files, and ivy_core.outbox.cleanup_old_reports() implemented a
72-hour TTL that no caller ever invoked (63 of 70 reports were past it).
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import housekeeping as hk  # noqa: E402

from ivy_core import outbox  # noqa: E402


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

def test_rotation_preserves_the_inode_an_open_descriptor_is_writing_to(tmp_path):
    """launchd holds an fd on StandardOutPath. Renaming the file leaves it
    writing to the moved inode and the fresh file stays empty forever, so
    rotation must copy-then-truncate."""
    p = tmp_path / "x.log"
    p.write_text("A" * 200)
    fh = open(p, "a")                       # stand-in for launchd's descriptor
    try:
        inode_before = p.stat().st_ino
        hk.rotate(p, max_bytes=10, keep=3, apply=True)
        assert p.stat().st_ino == inode_before
        assert (tmp_path / "x.log.1").read_text() == "A" * 200
        assert p.stat().st_size == 0
        fh.write("B" * 50)                  # a live write after rotation
        fh.flush()
    finally:
        fh.close()
    assert p.stat().st_size == 50           # landed in the truncated file


def test_a_file_under_the_threshold_is_left_alone(tmp_path):
    p = tmp_path / "small.log"
    p.write_text("x" * 100)
    assert hk.rotate(p, max_bytes=10_000, keep=3, apply=True) == []
    assert p.read_text() == "x" * 100
    assert not (tmp_path / "small.log.1").exists()


def test_generations_shift_and_the_oldest_is_dropped(tmp_path):
    p = tmp_path / "y.log"
    p.write_text("new")
    for gen in (1, 2):
        p.with_suffix(f".log.{gen}").write_text(f"gen{gen}")
    hk.rotate(p, max_bytes=1, keep=2, apply=True)
    assert p.with_suffix(".log.1").read_text() == "new"
    assert p.with_suffix(".log.2").read_text() == "gen1"
    assert not p.with_suffix(".log.3").exists()   # beyond --keep


@pytest.mark.parametrize("name", ["executions.db", "picks.db", "gateway_monitor_state.json"])
def test_databases_and_state_files_are_never_rotated(tmp_path, name):
    """logs/ holds three databases alongside the logs. executions.db is the
    job-receipt source of truth; losing it would destroy execution history."""
    p = tmp_path / name
    p.write_bytes(b"z" * 10_000)
    assert hk.rotate(p, max_bytes=1, keep=3, apply=True) == []
    assert p.stat().st_size == 10_000


def test_dry_run_changes_nothing(tmp_path):
    p = tmp_path / "z.log"
    p.write_text("q" * 500)
    actions = hk.rotate(p, max_bytes=1, keep=3, apply=False)
    assert actions                      # it reports what it would do
    assert p.stat().st_size == 500      # but does not do it
    assert not (tmp_path / "z.log.1").exists()


# ---------------------------------------------------------------------------
# Outbox retention
# ---------------------------------------------------------------------------

def _report(dirpath, report_id, *, age_hours, status="sent"):
    ts = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    (dirpath / f"{report_id}.json").write_text(json.dumps({
        "report_id": report_id, "job_name": "sharp_picks", "status": status,
        "generated_at": ts.isoformat(),
    }))
    (dirpath / f"{report_id}.pdf").write_bytes(b"%PDF fake")


def test_cleanup_removes_reports_past_the_ttl(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path)
    _report(tmp_path, "SP-OLD", age_hours=100)
    _report(tmp_path, "SP-NEW", age_hours=1)
    assert outbox.cleanup_old_reports() == 1
    assert not (tmp_path / "SP-OLD.json").exists()
    assert not (tmp_path / "SP-OLD.pdf").exists()   # the PDF goes too
    assert (tmp_path / "SP-NEW.json").exists()


def test_pending_survives_the_normal_ttl_but_not_the_long_backstop(tmp_path, monkeypatch):
    """A pending report may still be resent, so it outlives the 72h TTL — but
    without any cap one stuck entry would sit on disk forever."""
    monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path)
    _report(tmp_path, "SP-PEND", age_hours=100, status="pending")
    assert outbox.cleanup_old_reports() == 0
    assert (tmp_path / "SP-PEND.json").exists()

    _report(tmp_path, "SP-ANCIENT", age_hours=24 * (outbox.PENDING_TTL_DAYS + 1), status="pending")
    assert outbox.cleanup_old_reports() == 1
    assert not (tmp_path / "SP-ANCIENT.json").exists()


def test_save_report_self_prunes(tmp_path, monkeypatch):
    """The actual B6 bug: cleanup_old_reports() existed but nothing called it."""
    monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path)
    _report(tmp_path, "SP-STALE", age_hours=100)
    outbox.save_report("SP-FRESH", None, "sharp_picks", "+15555550100", "summary")
    assert not (tmp_path / "SP-STALE.json").exists()
    assert (tmp_path / "SP-FRESH.json").exists()


def test_a_cleanup_failure_never_breaks_a_delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path)
    monkeypatch.setattr(outbox, "cleanup_old_reports", lambda: (_ for _ in ()).throw(OSError("boom")))
    outbox.save_report("SP-OK", None, "sharp_picks", "+15555550100", "summary")
    assert (tmp_path / "SP-OK.json").exists()


def test_the_newest_report_per_job_is_kept_however_old(tmp_path, monkeypatch):
    """Happy Hour runs weekly and the meal planner on Sundays, so their only
    report is past the 72h TTL for most of the week. Enforcing the TTL alone
    deleted it and left MORE / WHY / PDF with nothing to answer from."""
    monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path)

    for rid, age in (("HH-OLD", 24 * 30), ("HH-NEWEST", 24 * 6)):
        ts = datetime.now(timezone.utc) - timedelta(hours=age)
        (tmp_path / f"{rid}.json").write_text(json.dumps({
            "report_id": rid, "job_name": "happy_hour", "status": "sent",
            "generated_at": ts.isoformat(),
        }))

    removed = outbox.cleanup_old_reports()

    assert removed == 1
    assert not (tmp_path / "HH-OLD.json").exists()
    # 6 days old, far past the 72h TTL, but it is the job's newest.
    assert (tmp_path / "HH-NEWEST.json").exists()
    assert outbox.find_newest("happy_hour") == "HH-NEWEST"


def test_retention_is_per_job_not_global(tmp_path, monkeypatch):
    """One busy job must not keep another job's newest report alive, and vice
    versa — each job answers its own MORE / PDF."""
    monkeypatch.setattr(outbox, "OUTBOX_DIR", tmp_path)
    for rid, job in (("SP-1", "sharp_picks"), ("HH-1", "happy_hour"), ("MP-1", "familia_meal_planner")):
        ts = datetime.now(timezone.utc) - timedelta(days=20)
        (tmp_path / f"{rid}.json").write_text(json.dumps({
            "report_id": rid, "job_name": job, "status": "sent",
            "generated_at": ts.isoformat(),
        }))
    assert outbox.cleanup_old_reports() == 0
    for job, rid in (("sharp_picks", "SP-1"), ("happy_hour", "HH-1"), ("familia_meal_planner", "MP-1")):
        assert outbox.find_newest(job) == rid


# ---------------------------------------------------------------------------
# Interpreter identity (B3)
# ---------------------------------------------------------------------------

def test_ready_reports_a_repointed_interpreter(monkeypatch):
    """The gateway's Full Disk Access is granted per interpreter binary, and
    .venv/bin/python resolves through uv's floating minor-version symlink. A
    `uv python upgrade` would swap the binary, silently revoke FDA, and make
    chat.db reads fail with no stated cause. Surface it as its own check."""
    import main

    monkeypatch.setattr(main, "TCC_GRANTED_INTERPRETER", "/nonexistent/python3.12")
    assert main._interpreter_matches_tcc_grant() is False

    monkeypatch.setattr(main, "TCC_GRANTED_INTERPRETER", sys.executable)
    assert main._interpreter_matches_tcc_grant() is True

    # Empty disables the check, so this is portable to another machine.
    monkeypatch.setattr(main, "TCC_GRANTED_INTERPRETER", "")
    assert main._interpreter_matches_tcc_grant() is True
