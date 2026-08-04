"""FastAPI endpoints via TestClient. Uses TestClient WITHOUT the `with`
context manager, so the lifespan (which starts the iMessage poller thread)
never runs — no test here touches the real chat.db.
"""

import os
from unittest.mock import Mock

from fastapi.testclient import TestClient

import main

client = TestClient(main.app)
HEADERS = {"X-API-Key": os.environ["ADMIN_SECRET"]}


def test_poller_thread_not_started_by_bare_testclient():
    """Sanity check on the test setup itself: bare TestClient (no `with`)
    must not trigger the lifespan/poller thread."""
    import threading

    thread_names = [t.name for t in threading.enumerate()]
    assert not any("imessage" in name.lower() for name in thread_names)


def test_health_requires_auth():
    resp = client.get("/health")
    assert resp.status_code == 401


def test_interactive_api_documentation_is_disabled():
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_health_ok_with_key():
    resp = client.get("/health", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_is_pure_liveness_and_never_probes_providers(monkeypatch):
    probe = Mock(side_effect=AssertionError("health must not perform network I/O"))
    monkeypatch.setattr(main, "probe_providers", probe)

    resp = client.get("/health", headers=HEADERS)

    assert resp.status_code == 200
    probe.assert_not_called()


def test_capabilities_lists_bravo_scout_as_unavailable_with_reason():
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


def test_ready_uses_cached_provider_state_without_network_io(monkeypatch):
    deepseek = Mock(side_effect=AssertionError("ready must not probe providers"))
    openai = Mock(side_effect=AssertionError("ready must not probe providers"))
    gemini = Mock(side_effect=AssertionError("ready must not probe providers"))
    monkeypatch.setattr(main, "_probe_deepseek", deepseek)
    monkeypatch.setattr(main, "_probe_openai", openai)
    monkeypatch.setattr(main, "_probe_gemini", gemini)
    monkeypatch.setattr(
        main,
        "_cached_provider_snapshot",
        lambda: {"deepseek": {"authenticated": True}},
    )
    monkeypatch.setattr(main, "get_imessage_runtime_snapshot", lambda: {"ready": True})
    monkeypatch.setattr(main.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(main.os, "access", lambda _path, _mode: True)
    monkeypatch.setattr(main._shutil, "which", lambda _name: "/usr/bin/osascript")
    monkeypatch.setattr(main.sys, "platform", "darwin")

    resp = client.get("/ready", headers=HEADERS)

    assert resp.status_code == 200
    deepseek.assert_not_called()
    openai.assert_not_called()
    gemini.assert_not_called()


def test_ready_reads_receipts_without_reconciling_or_mutating(monkeypatch):
    observed = []
    monkeypatch.setattr(
        main.receipts,
        "list_recent",
        lambda **kwargs: observed.append(kwargs) or [],
    )

    client.get("/ready", headers=HEADERS)

    assert observed == [{"limit": 1, "reconcile": False}]


def test_runtime_requires_auth_and_returns_privacy_safe_sections(monkeypatch):
    assert client.get("/runtime").status_code == 401
    monkeypatch.setattr(
        main,
        "get_imessage_runtime_snapshot",
        lambda: {"ready": True, "queue_depth": 0, "last_error_category": None},
    )
    monkeypatch.setattr(
        main,
        "_cached_provider_snapshot",
        lambda: {"deepseek": {"status": "ready", "authenticated": True}},
    )
    monkeypatch.setattr(main, "compute_tool_statuses", lambda: [{"tool_name": "status"}])

    resp = client.get("/runtime", headers=HEADERS)

    assert resp.status_code == 200
    assert set(resp.json()) == {"imessage", "providers", "tools"}
    assert "API_KEY" not in resp.text


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


def test_executions_endpoint_reflects_a_real_run_job_call():
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


def test_execution_get_endpoints_are_read_only(monkeypatch):
    list_calls = []
    get_calls = []
    monkeypatch.setattr(
        main.receipts,
        "list_recent",
        lambda **kwargs: list_calls.append(kwargs) or [],
    )
    monkeypatch.setattr(
        main.receipts,
        "get_execution",
        lambda execution_id, **kwargs: get_calls.append(
            {"execution_id": execution_id, **kwargs}
        ) or None,
    )

    assert client.get("/executions", headers=HEADERS).status_code == 200
    assert client.get("/executions/missing", headers=HEADERS).status_code == 404
    assert list_calls == [{"limit": 50, "job_name": None, "reconcile": False}]
    assert get_calls == [{"execution_id": "missing", "reconcile": False}]


def test_execution_endpoints_hide_requester_results_and_full_log_path():
    execution_id = main.receipts.record_start(
        "privacy_test",
        requester="private-sender@example.invalid",
        executor="entrypoint",
    )
    main.receipts.record_spawned(
        execution_id,
        pid=1234,
        log_path="/private/sensitive/path/privacy_test.log",
    )
    main.receipts.record_finish(
        execution_id,
        "completed",
        "completed",
        outcome="success",
        exit_code=0,
        result={"message": "private message body", "api_key": "secret-value"},
        delivery_status="submitted_unverified",
    )

    resp = client.get(f"/executions/{execution_id}", headers=HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["log_name"] == "privacy_test.log"
    assert "requester" not in body
    assert "result" not in body
    assert "/private/sensitive/path" not in resp.text
    assert "private-sender" not in resp.text
    assert "private message body" not in resp.text
    assert "secret-value" not in resp.text


def test_run_job_response_never_claims_success_for_unavailable_job():
    resp = client.post("/run-job", params={"job_name": "bravo"}, headers=HEADERS)
    body = resp.json()
    assert "unavailable" in body["result"].lower()
    assert "✅" not in body["result"]
