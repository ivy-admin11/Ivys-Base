"""FastAPI endpoints via TestClient. Uses TestClient WITHOUT the `with`
context manager, so the lifespan (which starts the iMessage poller thread)
never runs — no test here touches the real chat.db.
"""

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)
HEADERS = {"X-API-Key": os.environ["ADMIN_SECRET"]}


@pytest.fixture
def unavailable_bravo(monkeypatch):
    """Bravo Scout is runnable now; these tests need a registered job that
    is NOT runnable. Mark it unavailable for the test and guard Popen so a
    regression can never launch the real agent (which texts the household)."""
    job = main.job_runner.find_job("bravo")
    monkeypatch.setattr(job, "available", False)
    monkeypatch.setattr(job, "unavailable_reason", "proactive_agents/bravo_scout.py does not exist (test fixture)")
    with patch("job_runner.subprocess.Popen") as mock_popen:
        yield job
    mock_popen.assert_not_called()


def test_poller_thread_not_started_by_bare_testclient():
    """Sanity check on the test setup itself: bare TestClient (no `with`)
    must not trigger the lifespan/poller thread."""
    import threading

    thread_names = [t.name for t in threading.enumerate()]
    assert not any("imessage" in name.lower() for name in thread_names)


def test_health_requires_auth():
    resp = client.get("/health")
    assert resp.status_code == 401


def test_health_ok_with_key():
    resp = client.get("/health", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_capabilities_lists_bravo_scout_as_unavailable_with_reason(unavailable_bravo):
    resp = client.get("/capabilities", headers=HEADERS)
    assert resp.status_code == 200
    jobs = resp.json()["jobs"]
    bravo = next(j for j in jobs if j["name"] == "bravo_scout")
    assert bravo["available"] is False
    assert bravo["unavailable_reason"]


def test_ready_endpoint_returns_checks_dict():
    resp = client.get("/ready", headers=HEADERS)
    assert resp.status_code in (200, 503)
    body = resp.json() if resp.status_code == 200 else resp.json()["detail"]
    assert "checks" in body
    assert "chat_db_readable" in body["checks"]


def test_version_endpoint_reports_pid_and_git_sha():
    resp = client.get("/version", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["pid"] == os.getpid()
    assert "git_sha" in body


def test_jobs_endpoint_never_lists_gateway_as_a_job():
    resp = client.get("/jobs", headers=HEADERS)
    assert resp.status_code == 200
    names = [j["name"] for j in resp.json()["jobs"]]
    assert "gateway" not in names


def test_executions_endpoint_reflects_a_real_run_job_call(unavailable_bravo):
    run_resp = client.post("/run-job", params={"job_name": "bravo"}, headers=HEADERS)
    assert run_resp.status_code == 200

    exec_resp = client.get("/executions", params={"job_name": "bravo_scout"}, headers=HEADERS)
    assert exec_resp.status_code == 200
    executions = exec_resp.json()["executions"]
    assert executions
    assert executions[0]["status"] == "unavailable"


def test_execution_not_found_returns_404():
    resp = client.get("/executions/does-not-exist-xyz", headers=HEADERS)
    assert resp.status_code == 404


def test_run_job_response_never_claims_success_for_unavailable_job(unavailable_bravo):
    resp = client.post("/run-job", params={"job_name": "bravo"}, headers=HEADERS)
    body = resp.json()
    assert "unavailable" in body["result"].lower()
    assert "✅" not in body["result"]


# ---------------------------------------------------------------------------
# Low disk: a warning, never a readiness failure
# ---------------------------------------------------------------------------

def _ready_body(resp):
    """/ready answers 503 with the payload under "detail" when a hard check
    fails, and 200 with it at the top level otherwise. Under TestClient the
    poller is not running, so 503 is the normal case here."""
    return resp.json() if resp.status_code == 200 else resp.json()["detail"]


def test_low_disk_is_reported_as_a_warning(monkeypatch):
    """SQLite does not degrade gracefully when a volume fills — it raises
    "database or disk is full" and the write is lost. In this codebase that
    means a report delivered with no receipt, which is the one ambiguity
    everything else here exists to prevent. Ivy has to see it coming."""
    monkeypatch.setattr(main, "_disk_free_gb", lambda *a, **k: 1.2)
    body = _ready_body(client.get("/ready", headers=HEADERS))
    assert any("low_disk_space" in w for w in body.get("warnings", []))
    assert "1.2 GB free" in " ".join(body["warnings"])


def test_low_disk_is_never_a_readiness_check(monkeypatch):
    """Free space is the host's business. Ivy is still serving, and pulling a
    working gateway out of rotation over the machine's storage would be the
    same overreaction as failing readiness for a dead failover. It belongs in
    warnings, and must never appear among the checks that gate readiness."""
    monkeypatch.setattr(main, "_disk_free_gb", lambda *a, **k: 0.1)
    body = _ready_body(client.get("/ready", headers=HEADERS))
    assert not any("disk_space" in name for name in body["checks"]), (
        "low disk must not gate readiness"
    )
    assert any("low_disk_space" in w for w in body.get("warnings", []))


def test_ample_disk_produces_no_warning(monkeypatch):
    monkeypatch.setattr(main, "_disk_free_gb", lambda *a, **k: 500.0)
    body = _ready_body(client.get("/ready", headers=HEADERS))
    assert not any("low_disk" in w for w in body.get("warnings", []))


def test_an_unreadable_volume_is_not_reported_as_full(monkeypatch):
    """None means "could not measure", which is not the same claim as "nearly
    out of space" — inventing the alarming reading would be its own bug."""
    monkeypatch.setattr(main, "_disk_free_gb", lambda *a, **k: None)
    body = _ready_body(client.get("/ready", headers=HEADERS))
    assert not any("low_disk" in w for w in body.get("warnings", []))
