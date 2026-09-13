"""The gateway must be able to tell you it is running old code.

What happened: a fix to ivy_core/proactive.py landed on disk on 2026-09-12 and
the gateway kept texting the OLD wording on 09-12 and 09-13. main.py imports
proactive once at startup and holds it in memory; nothing anywhere compared the
running process to the disk. The same thing happened on 2026-09-03 to the
reply-command handler. Twice is a pattern, not an accident.

/version used to run `git rev-parse HEAD` at request time and present that as
"which commit is this gateway actually running". It was which commit is on
disk. It could not, by construction, detect the one condition it claimed to
report.
"""

import os
import time

import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)
HEADERS = {"X-API-Key": os.environ["ADMIN_SECRET"]}


@pytest.fixture
def fresh_process(monkeypatch):
    """Pretend this process started just now, so nothing on disk is newer."""
    from datetime import datetime
    monkeypatch.setattr(main, "PROCESS_STARTED_AT", datetime.now())
    time.sleep(0.05)


@pytest.fixture
def ancient_process(monkeypatch):
    """Pretend this process started before every file in the repo."""
    from datetime import datetime
    monkeypatch.setattr(main, "PROCESS_STARTED_AT", datetime(2000, 1, 1))


def test_fresh_process_reports_nothing_stale(fresh_process):
    assert main._source_changed_since_start() == []


def test_a_source_file_newer_than_the_process_is_detected(fresh_process, tmp_path, monkeypatch):
    """The exact shape of the proactive.py incident."""
    root = tmp_path
    (root / "ivy_core").mkdir()
    old = root / "main.py"
    old.write_text("# unchanged\n")
    os.utime(old, (0, 0))
    monkeypatch.setattr(main, "PROJECT_ROOT_DIR", str(root))
    time.sleep(0.05)
    changed = root / "ivy_core" / "proactive.py"
    changed.write_text("# the fix that never went live\n")
    assert main._source_changed_since_start() == ["ivy_core/proactive.py"]


def test_job_scripts_are_not_the_gateways_problem(fresh_process, tmp_path, monkeypatch):
    """proactive_agents/ and scripts/ start fresh each run; a change there
    needs no restart and must not page for one."""
    root = tmp_path
    (root / "proactive_agents").mkdir()
    (root / "scripts").mkdir()
    monkeypatch.setattr(main, "PROJECT_ROOT_DIR", str(root))
    time.sleep(0.05)
    (root / "proactive_agents" / "sports_bettor.py").write_text("#\n")
    (root / "scripts" / "repair_dashboard.py").write_text("#\n")
    assert main._source_changed_since_start() == []


def test_ready_carries_a_stale_code_warning(ancient_process):
    body = client.get("/ready", headers=HEADERS).json()
    if "detail" in body:          # 503 wraps the payload
        body = body["detail"]
    warnings = body.get("warnings") or []
    stale = [w for w in warnings if w.startswith("stale_code:")]
    assert stale, f"no stale_code warning in {warnings}"
    assert "launchctl kickstart" in stale[0]


def test_the_warning_string_is_stable_so_the_monitor_alerts_once(ancient_process):
    """gateway_monitor dedups on the exact string. A count or a filename in it
    would re-page on every subsequent commit."""
    body = client.get("/ready", headers=HEADERS).json()
    if "detail" in body:
        body = body["detail"]
    (w,) = [w for w in body.get("warnings") or [] if w.startswith("stale_code:")]
    import re
    assert not re.search(r"\b\d+ (source )?files?\b", w), w
    assert ".py" not in w, w


def test_ready_is_quiet_when_nothing_changed(fresh_process):
    body = client.get("/ready", headers=HEADERS).json()
    if "detail" in body:
        body = body["detail"]
    assert not any(w.startswith("stale_code:") for w in body.get("warnings") or [])


def test_version_distinguishes_loaded_from_on_disk(ancient_process):
    body = client.get("/version", headers=HEADERS).json()
    assert "loaded_git_sha" in body
    assert body["stale"] is True
    assert body["stale_source_files"], "files newer than the process must be listed here"
    assert body["restart"] == main.GATEWAY_RESTART_HINT


def test_version_is_honest_when_fresh(fresh_process):
    body = client.get("/version", headers=HEADERS).json()
    assert body["stale_source_files"] == []
    assert body["restart"] is None
