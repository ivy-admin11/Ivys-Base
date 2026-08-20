"""Hermetic tests for detached-job lifecycle receipts and worker finalization."""

from __future__ import annotations

import sqlite3
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import job_runner as jr
from ivy_core import job_worker, receipts
from job_runner import Job, JobRunner, JobStatus


def _fake_entrypoint_job() -> Job:
    return Job(
        name="fake_lifecycle",
        display_name="Fake Lifecycle",
        aliases=[],
        description="lifecycle test job",
        executor="entrypoint",
        entrypoint="tests.fake_agent:run",
    )


def _configure_fake_dispatch(tmp_path, monkeypatch):
    fake_python = tmp_path / "python"
    fake_python.write_text("#!/bin/sh\n")
    fake_python.chmod(0o755)
    monkeypatch.setattr(jr, "VENV_PYTHON", fake_python)
    monkeypatch.setattr(jr, "PROJECT_ROOT", tmp_path)
    runner = JobRunner()
    job = _fake_entrypoint_job()
    runner.registry[job.name] = job
    return runner, job, fake_python


def test_additive_migration_marks_legacy_dispatch_success_unknown(tmp_path, monkeypatch):
    legacy_db = tmp_path / "legacy.db"
    with sqlite3.connect(legacy_db) as conn:
        conn.execute(
            """
            CREATE TABLE executions (
                execution_id TEXT PRIMARY KEY,
                job_name TEXT NOT NULL,
                requester TEXT,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                detail TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-1",
                "sharp_picks",
                "pytest",
                "success",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:01+00:00",
                "spawned",
            ),
        )
        conn.execute(
            "INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-started",
                "happy_hour",
                "pytest",
                "started",
                "2026-01-01T00:00:00+00:00",
                None,
                "worker detached",
            ),
        )
    monkeypatch.setattr(receipts, "DB_PATH", legacy_db)

    record = receipts.get_execution("legacy-1", reconcile=False)

    assert record["status"] == "completion_unknown"
    assert record["outcome"] == "legacy_dispatch_success"
    assert record["delivery_status"] == "unknown"
    abandoned = receipts.get_execution("legacy-started", reconcile=False)
    assert abandoned["status"] == "completion_unknown"
    assert abandoned["finished_at"] is not None
    with sqlite3.connect(legacy_db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == receipts.SCHEMA_VERSION


def test_terminal_transition_is_compare_and_set_and_first_result_wins():
    execution_id = receipts.record_start("fake", requester="pytest", executor="entrypoint")
    assert receipts.record_spawned(execution_id, pid=12345, log_path="/tmp/fake.log")

    assert receipts.record_finish(
        execution_id,
        "completed",
        "done",
        outcome="success",
        exit_code=0,
        result={"status": "success"},
        delivery_status="not_attempted",
    )
    assert not receipts.record_finish(
        execution_id,
        "failed",
        "late failure",
        outcome="error",
        exit_code=1,
    )

    record = receipts.get_execution(execution_id, reconcile=False)
    assert record["status"] == "completed"
    assert record["outcome"] == "success"
    assert record["finished_at"] is not None
    assert record["result"] == {"status": "success"}


def test_single_active_job_guard_is_atomic_across_threads():
    # Initialize schema before racing the inserts; the assertion concerns the
    # partial unique index, not concurrent migration setup.
    receipts.list_recent(reconcile=False)

    def start_once():
        try:
            return ("started", receipts.record_start("same_job", requester="pytest"))
        except receipts.ExecutionAlreadyActive as exc:
            return ("active", exc.execution_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: start_once(), range(2)))

    assert sorted(kind for kind, _ in results) == ["active", "started"]
    assert results[0][1] == results[1][1]
    active = receipts.list_recent(job_name="same_job", reconcile=False)
    assert len(active) == 1
    assert active[0]["status"] == "queued"


def test_receipts_database_and_parent_are_private():
    receipts.list_recent(reconcile=False)

    assert (receipts.DB_PATH.stat().st_mode & 0o777) == 0o600
    assert (receipts.DB_PATH.parent.stat().st_mode & 0o777) == 0o700


def test_stale_dead_worker_becomes_completion_unknown(monkeypatch):
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(receipts, "_utcnow", lambda: old)
    execution_id = receipts.record_start("stale_job", executor="entrypoint")
    receipts.record_spawned(execution_id, pid=424242)

    reconciled = receipts.reconcile_stale(
        max_age_seconds=60,
        now=old + timedelta(minutes=10),
        pid_checker=lambda _pid: False,
    )

    assert reconciled == [execution_id]
    record = receipts.get_execution(execution_id, reconcile=False)
    assert record["status"] == "completion_unknown"
    assert record["outcome"] == "worker_lost"
    assert record["delivery_status"] == "unknown"


def test_stale_dry_run_preserves_not_requested_delivery(monkeypatch):
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(receipts, "_utcnow", lambda: old)
    execution_id = receipts.record_start(
        "stale_dry_run",
        executor="entrypoint",
        delivery_status="not_requested",
    )
    receipts.record_spawned(execution_id, pid=424242)

    receipts.reconcile_stale(
        max_age_seconds=60,
        now=old + timedelta(minutes=10),
        pid_checker=lambda _pid: False,
    )

    record = receipts.get_execution(execution_id, reconcile=False)
    assert record["status"] == "completion_unknown"
    assert record["delivery_status"] == "not_requested"


def test_successful_popen_remains_dispatched_until_child_claims_execution(
    tmp_path, monkeypatch
):
    runner, job, fake_python = _configure_fake_dispatch(tmp_path, monkeypatch)
    process = MagicMock(pid=43210)

    with patch("job_runner.subprocess.Popen", return_value=process) as popen:
        dispatch = runner.run_job_detailed(
            job.name,
            force=True,
            send=True,
            requester="pytest",
        )

    assert dispatch.status == JobStatus.SUCCESS
    assert dispatch.execution_id
    assert dispatch.lifecycle_status == "dispatched"
    record = receipts.get_execution(dispatch.execution_id, reconcile=False)
    assert record["status"] == "dispatched"
    assert record["finished_at"] is None
    assert record["worker_started_at"] is None
    assert record["heartbeat_at"] is None
    assert record["pid"] == 43210
    assert dispatch.execution_id in record["log_path"]
    assert (Path(record["log_path"]).stat().st_mode & 0o777) == 0o600

    argv = popen.call_args.args[0]
    assert argv[:3] == [str(fake_python), "-m", "ivy_core.job_worker"]
    assert argv[argv.index("--execution-id") + 1] == dispatch.execution_id
    assert argv[argv.index("--entrypoint") + 1] == job.entrypoint
    assert "--force" in argv
    assert "--send" in argv
    assert argv[argv.index("--timeout-seconds") + 1] == "3600"
    assert popen.call_args.kwargs["start_new_session"] is True


def test_spawn_metadata_failure_does_not_turn_successful_popen_terminal(
    tmp_path, monkeypatch
):
    runner, job, _ = _configure_fake_dispatch(tmp_path, monkeypatch)

    with (
        patch("job_runner.subprocess.Popen", return_value=MagicMock(pid=43210)),
        patch(
            "job_runner.receipts.record_spawned",
            side_effect=sqlite3.OperationalError("database temporarily locked"),
        ),
    ):
        dispatch = runner.run_job_detailed(job.name, send=True)

    assert dispatch.status == JobStatus.SUCCESS
    record = receipts.get_execution(dispatch.execution_id, reconcile=False)
    assert record["status"] == "queued"
    assert record["finished_at"] is None


def test_popen_failure_is_terminal_and_failure_detail_is_sanitized(tmp_path, monkeypatch):
    runner, job, _ = _configure_fake_dispatch(tmp_path, monkeypatch)

    with patch(
        "job_runner.subprocess.Popen",
        side_effect=OSError("token=do-not-store-this"),
    ):
        dispatch = runner.run_job_detailed(job.name, send=True)

    assert dispatch.status == JobStatus.ERROR
    record = receipts.get_execution(dispatch.execution_id, reconcile=False)
    assert record["status"] == "dispatch_failed"
    assert record["finished_at"] is not None
    assert "do-not-store-this" not in (record["detail"] or "")
    assert record["outcome"] == "spawn_exception:OSError"


def test_launchctl_failure_receipt_sanitizes_subprocess_detail(monkeypatch):
    runner = JobRunner()
    job = Job(
        name="fake_launchctl",
        display_name="Fake Launchctl",
        aliases=[],
        description="test",
        executor="launchctl",
        target="com.ivy.fake",
    )
    runner.registry[job.name] = job

    def launchctl_result(command, **_kwargs):
        result = MagicMock()
        if command[:2] == ["launchctl", "list"]:
            result.stdout = "com.ivy.fake\n"
            result.returncode = 0
            result.stderr = ""
        else:
            result.stdout = ""
            result.returncode = 1
            result.stderr = "token=must-not-persist"
        return result

    with (
        patch("job_runner.os.path.exists", return_value=True),
        patch("job_runner.subprocess.run", side_effect=launchctl_result),
    ):
        dispatch = runner.run_job_detailed(job.name, send=False)

    assert dispatch.status == JobStatus.ERROR
    assert "must-not-persist" not in dispatch.message
    record = receipts.get_execution(dispatch.execution_id, reconcile=False)
    assert record["status"] == "dispatch_failed"
    assert "must-not-persist" not in record["detail"]
    assert record["delivery_status"] == "not_requested"


def test_fast_worker_terminal_result_is_not_overwritten_by_parent_spawn_update(
    tmp_path, monkeypatch
):
    runner, job, _ = _configure_fake_dispatch(tmp_path, monkeypatch)

    def finish_before_popen_returns(argv, **_kwargs):
        execution_id = argv[argv.index("--execution-id") + 1]
        receipts.record_finish(
            execution_id,
            "completed",
            "fast result",
            outcome="success",
            exit_code=0,
            delivery_status="not_attempted",
        )
        return MagicMock(pid=54321)

    with patch("job_runner.subprocess.Popen", side_effect=finish_before_popen_returns):
        dispatch = runner.run_job_detailed(job.name, send=False)

    record = receipts.get_execution(dispatch.execution_id, reconcile=False)
    assert record["status"] == "completed"
    assert record["outcome"] == "success"
    assert record["pid"] == 54321


def test_worker_forwards_correlation_and_records_domain_and_delivery(monkeypatch):
    execution_id = receipts.record_start(
        "fake_worker",
        requester="requester-1",
        executor="entrypoint",
    )
    observed = {}

    def fake_run(**kwargs):
        observed.update(kwargs)
        return {
            "status": "success",
            "result_type": "picks",
            "sent": True,
            "report_id": "SP-TEST-1",
            "api_key": "must-not-persist",  # pragma: allowlist secret
        }

    monkeypatch.setattr(job_worker, "_resolve_entrypoint", lambda _value: fake_run)
    exit_code, _ = job_worker.run_execution(
        execution_id,
        entrypoint="fake.module:run",
        force=True,
        send=True,
        heartbeat_interval=0,
    )

    assert exit_code == 0
    assert observed == {
        "force": True,
        "send": True,
        "requester": "requester-1",
        "request_id": execution_id,
    }
    record = receipts.get_execution(execution_id, reconcile=False)
    assert record["status"] == "completed"
    assert record["outcome"] == "success"
    assert record["delivery_status"] == "submitted_unverified"
    assert record["report_ids"] == ["SP-TEST-1"]
    assert "api_key" not in record["result"]
    assert record["result"]["status"] == "success"


@pytest.mark.parametrize(
    "agent_result, expected_status, expected_outcome, expected_delivery",
    [
        (
            {"status": "no_qualifying_picks", "sent": False},
            "completed",
            "no_qualifying_picks",
            "not_attempted",
        ),
        (
            {"status": "success", "result_type": "duplicate", "sent": False},
            "completed",
            "duplicate",
            "not_attempted",
        ),
        (
            {"result_type": "skipped", "reason": "locked"},
            "skipped",
            "skipped",
            "not_attempted",
        ),
        (
            {"status": "auth_failure", "sent": False},
            "failed",
            "auth_failure",
            "not_attempted",
        ),
        (
            {"status": "success", "alert_sent": False},
            "completed",
            "success",
            "unknown",
        ),
    ],
)
def test_worker_separates_lifecycle_from_domain_outcome(
    monkeypatch,
    agent_result,
    expected_status,
    expected_outcome,
    expected_delivery,
):
    execution_id = receipts.record_start("normalized", executor="entrypoint")
    monkeypatch.setattr(
        job_worker,
        "_resolve_entrypoint",
        lambda _value: lambda **_kwargs: agent_result,
    )

    job_worker.run_execution(
        execution_id,
        entrypoint="fake.module:run",
        force=False,
        send=True,
        heartbeat_interval=0,
    )

    record = receipts.get_execution(execution_id, reconcile=False)
    assert record["status"] == expected_status
    assert record["outcome"] == expected_outcome
    assert record["delivery_status"] == expected_delivery


def test_worker_does_not_claim_success_for_unstructured_result(monkeypatch):
    execution_id = receipts.record_start("unstructured", executor="entrypoint")
    monkeypatch.setattr(
        job_worker,
        "_resolve_entrypoint",
        lambda _value: lambda **_kwargs: None,
    )

    exit_code, _ = job_worker.run_execution(
        execution_id,
        entrypoint="fake.module:run",
        force=False,
        send=True,
        heartbeat_interval=0,
    )

    assert exit_code == 0
    record = receipts.get_execution(execution_id, reconcile=False)
    assert record["status"] == "completed"
    assert record["outcome"] == "unstructured_result"
    assert record["delivery_status"] == "unknown"


def test_worker_rejects_execution_id_owned_by_different_pid(monkeypatch):
    execution_id = receipts.record_start("owned", executor="entrypoint")
    receipts.record_spawned(execution_id, pid=111)
    called = False

    def should_not_run(**_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(job_worker.os, "getpid", lambda: 222)
    monkeypatch.setattr(job_worker, "_resolve_entrypoint", lambda _value: should_not_run)

    exit_code, result = job_worker.run_execution(
        execution_id,
        entrypoint="fake.module:run",
        force=False,
        send=True,
        heartbeat_interval=0,
    )

    assert exit_code == 2
    assert result is None
    assert called is False
    record = receipts.get_execution(execution_id, reconcile=False)
    assert record["status"] == "dispatched"
    assert record["pid"] == 111


def test_worker_cli_creates_receipt_for_scheduled_invocation(monkeypatch, capsys):
    observed = {}

    def fake_run_execution(execution_id, **kwargs):
        observed.update({"execution_id": execution_id, **kwargs})
        receipts.record_finish(
            execution_id,
            "completed",
            "test completion",
            outcome="test",
            exit_code=0,
            delivery_status="not_attempted",
        )
        return 0, {"status": "test"}

    monkeypatch.setattr(job_worker, "run_execution", fake_run_execution)

    exit_code = job_worker.main([
        "--job-name",
        "scheduled_test",
        "--entrypoint",
        "tests.fake_agent:run",
        "--force",
        "--send",
    ])

    assert exit_code == 0
    record = receipts.get_execution(observed["execution_id"], reconcile=False)
    assert record["job_name"] == "scheduled_test"
    assert record["requester"] == "scheduled"
    assert record["status"] == "completed"
    assert observed["entrypoint"] == "tests.fake_agent:run"
    assert observed["force"] is True
    assert observed["send"] is True
    assert observed["max_runtime_seconds"] == 3600.0
    assert '"status": "test"' in capsys.readouterr().out


def test_worker_cli_reports_existing_scheduled_execution(monkeypatch, capsys):
    execution_id = receipts.record_start("scheduled_active", executor="entrypoint")
    run_execution = MagicMock()
    monkeypatch.setattr(job_worker, "run_execution", run_execution)

    exit_code = job_worker.main([
        "--job-name",
        "scheduled_active",
        "--entrypoint",
        "tests.fake_agent:run",
    ])

    assert exit_code == 0
    run_execution.assert_not_called()
    output = capsys.readouterr().out
    assert '"status": "already_running"' in output
    assert execution_id in output


def test_worker_exception_receipt_does_not_store_exception_message(monkeypatch):
    execution_id = receipts.record_start("explodes", executor="entrypoint")

    def explode(**_kwargs):
        raise RuntimeError("api_key=top-secret-value")

    monkeypatch.setattr(job_worker, "_resolve_entrypoint", lambda _value: explode)
    exit_code, _ = job_worker.run_execution(
        execution_id,
        entrypoint="fake.module:run",
        force=False,
        send=True,
        heartbeat_interval=0,
    )

    assert exit_code == 1
    record = receipts.get_execution(execution_id, reconcile=False)
    assert record["status"] == "failed"
    assert record["outcome"] == "exception:RuntimeError"
    assert "top-secret-value" not in record["detail"]
    assert "top-secret-value" not in record["result_json"]
    assert record["delivery_status"] == "unknown"


def test_stale_reconciliation_does_not_overwrite_racing_fresh_heartbeat(
    monkeypatch,
):
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = {"now": old}
    monkeypatch.setattr(receipts, "_utcnow", lambda: clock["now"])
    execution_id = receipts.record_start("heartbeat_race", executor="entrypoint")
    receipts.record_spawned(execution_id, pid=424242)
    assert receipts.record_running(execution_id, pid=424242)

    def refresh_before_pid_result(_pid):
        clock["now"] = old + timedelta(minutes=9)
        assert receipts.record_heartbeat(execution_id)
        return False

    reconciled = receipts.reconcile_stale(
        max_age_seconds=60,
        now=old + timedelta(minutes=10),
        pid_checker=refresh_before_pid_result,
    )

    assert reconciled == []
    record = receipts.get_execution(execution_id, reconcile=False)
    assert record["status"] == "running"
    assert record["heartbeat_at"] == (old + timedelta(minutes=9)).isoformat()


def test_timeout_watchdog_records_terminal_timeout_before_hard_exit():
    execution_id = receipts.record_start("hung_job", executor="entrypoint")
    assert receipts.record_running(execution_id, pid=12345)
    exits = []

    job_worker._timeout_watchdog(
        execution_id,
        threading.Event(),
        0.001,
        True,
        exits.append,
    )

    assert exits == [124]
    record = receipts.get_execution(execution_id, reconcile=False)
    assert record["status"] == "timed_out"
    assert record["outcome"] == "runtime_timeout"
    assert record["exit_code"] == 124
    assert record["delivery_status"] == "unknown"


def test_timeout_watchdog_does_not_kill_already_completed_execution():
    execution_id = receipts.record_start("fast_job", executor="entrypoint")
    assert receipts.record_finish(
        execution_id,
        "completed",
        "done",
        outcome="success",
        exit_code=0,
        delivery_status="not_attempted",
    )
    exits = []

    job_worker._timeout_watchdog(
        execution_id,
        threading.Event(),
        0.001,
        False,
        exits.append,
    )

    assert exits == []
    assert receipts.get_execution(execution_id, reconcile=False)["status"] == "completed"


def test_worker_cli_rejects_unbounded_runtime():
    with pytest.raises(SystemExit):
        job_worker.build_parser().parse_args([
            "--job-name",
            "bad_timeout",
            "--entrypoint",
            "tests.fake_agent:run",
            "--timeout-seconds",
            "0",
        ])


def test_result_sanitizer_redacts_broad_credential_key_names():
    cleaned = job_worker.sanitize_result({
        "Authorization": "Bearer do-not-store",
        "session_cookie": "cookie-value",
        "oauthCredential": "credential-value",
        "private-key": "key-value",
        "safe": "visible",
    })

    assert cleaned == {
        "Authorization": "[redacted]",
        "session_cookie": "[redacted]",
        "oauthCredential": "[redacted]",
        "private-key": "[redacted]",
        "safe": "visible",
    }


def test_receipt_summary_drops_recipient_and_arbitrary_agent_content():
    summary = job_worker.summarize_result_for_receipt({
        "status": "success",
        "message": "private generated report body",
        "recipients_status": {"Private Person": True},
        "deliveries": [{
            "recipient": "Private Person",
            "channel": "imessage_text",
            "status": "submitted_unverified",
            "report_id": "REPORT-1",
        }],
    })

    assert summary == {
        "status": "success",
        "deliveries": [{
            "channel": "imessage_text",
            "status": "submitted_unverified",
            "report_id": "REPORT-1",
        }],
    }


def test_real_subprocess_claims_and_finalizes_dispatch_receipt(tmp_path):
    module_dir = tmp_path / "agent_module"
    module_dir.mkdir()
    (module_dir / "fake_cross_process_agent.py").write_text(
        "def run(**kwargs):\n"
        "    return {\n"
        "        'status': 'success',\n"
        "        'result_type': 'cross_process',\n"
        "        'delivery_status': 'not_requested',\n"
        "    }\n",
        encoding="utf-8",
    )
    execution_id = receipts.record_start(
        "cross_process",
        requester="pytest",
        executor="entrypoint",
        delivery_status="not_requested",
    )
    receipts.record_spawned(execution_id, log_path=str(tmp_path / "worker.log"))
    assert receipts.get_execution(execution_id, reconcile=False)["status"] == "dispatched"

    env = dict(os.environ)
    env["IVY_RECEIPTS_DB"] = str(receipts.DB_PATH)
    env["PYTHONPATH"] = os.pathsep.join((str(module_dir), str(Path(__file__).parents[1])))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ivy_core.job_worker",
            "--execution-id",
            execution_id,
            "--job-name",
            "cross_process",
            "--entrypoint",
            "fake_cross_process_agent:run",
            "--timeout-seconds",
            "30",
        ],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    record = receipts.get_execution(execution_id, reconcile=False)
    assert record["status"] == "completed"
    assert record["worker_started_at"]
    assert record["heartbeat_at"]
    assert isinstance(record["pid"], int) and record["pid"] > 0
    assert record["outcome"] == "cross_process"
    assert record["delivery_status"] == "not_requested"
