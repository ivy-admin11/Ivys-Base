"""job_runner.py: alias resolution, launchctl return-code handling, receipts.

Every test here mocks subprocess — none of them ever invoke a real
launchctl command against a live scheduled job.
"""

import os
from unittest.mock import MagicMock, patch

from job_runner import Job, JobRunner, JobStatus
from job_runner import job_runner as global_job_runner


def test_meals_alias_resolves_to_familia_not_phantom_weekly_planner():
    job = global_job_runner.find_job("meals")
    assert job is not None
    assert job.name == "familia_meal_planner"
    assert job.entrypoint == "proactive_agents.Familia_meal_planner:run"


def test_household_meal_plan_alias_resolves():
    job = global_job_runner.find_job("household/meal plan")
    assert job is not None
    assert job.name == "familia_meal_planner"


def test_wrong_job_alias_does_not_silently_match_another_job():
    assert global_job_runner.find_job("brain") is not None
    assert global_job_runner.find_job("brain").name == "brain"
    # "sports" alone shouldn't resolve to brain or happy_hour
    picks_job = global_job_runner.find_job("sports picks")
    assert picks_job.name == "sharp_picks"


def test_bravo_scout_marked_unavailable_with_reason():
    job = global_job_runner.find_job("bravo")
    assert job.available is False
    assert "does not exist" in job.unavailable_reason


def test_gateway_is_not_registered_as_a_job():
    assert global_job_runner.find_job("gateway") is None


def test_run_job_unavailable_never_touches_subprocess():
    with patch("job_runner.subprocess.run") as mock_run, \
         patch("job_runner.subprocess.Popen") as mock_popen:
        status, message = global_job_runner.run_job("bravo")
    assert status == JobStatus.UNAVAILABLE
    mock_run.assert_not_called()
    mock_popen.assert_not_called()


def test_run_job_not_found_returns_available_jobs_list():
    status, message = global_job_runner.run_job("definitely_not_a_real_job_xyz")
    assert status == JobStatus.NOT_FOUND
    assert "Sharp Picks" in message


def test_run_job_records_a_receipt_regardless_of_outcome():
    """Regression test: unavailable/not-found attempts used to return
    before receipts.record_start() was ever called, so they never showed
    up in execution history at all."""
    from ivy_core import receipts

    global_job_runner.run_job("bravo", requester="pytest")
    recent = receipts.list_recent(limit=5, job_name="bravo_scout")
    assert recent, "bravo_scout attempt was not recorded"
    assert recent[0]["status"] == "unavailable"
    assert recent[0]["requester"] == "pytest"
    assert recent[0]["finished_at"] is not None


def test_launchctl_missing_plist_returns_unavailable_not_error():
    runner = JobRunner()
    fake_job = Job(
        name="fake", display_name="Fake", aliases=[], description="test",
        executor="launchctl", target="com.ivy.definitely_does_not_exist_xyz",
    )
    status, message = runner._run_launchctl_job(fake_job)
    assert status == JobStatus.UNAVAILABLE


def test_launchctl_nonzero_returncode_is_error_not_success():
    """The core CP4 fix: a completed subprocess.run() is not proof
    launchctl succeeded — only returncode == 0 is."""
    runner = JobRunner()
    job = Job(
        name="fake", display_name="Fake", aliases=[], description="test",
        executor="launchctl", target="com.ivy.brain",
    )

    def side_effect(cmd, **kwargs):
        result = MagicMock()
        if cmd[:2] == ["launchctl", "list"]:
            result.stdout, result.returncode = "com.ivy.brain\n", 0
        elif cmd[:2] == ["launchctl", "kickstart"]:
            result.returncode, result.stderr, result.stdout = 1, "boom", ""
        return result

    with patch("job_runner.os.path.exists", return_value=True), \
         patch("job_runner.subprocess.run", side_effect=side_effect):
        status, message = runner._run_launchctl_job(job)
    assert status == JobStatus.ERROR


def test_launchctl_success_path_returns_success():
    runner = JobRunner()
    job = Job(
        name="fake", display_name="Fake", aliases=[], description="test",
        executor="launchctl", target="com.ivy.brain",
    )

    def side_effect(cmd, **kwargs):
        result = MagicMock()
        result.stdout, result.returncode, result.stderr = "com.ivy.brain\n", 0, ""
        return result

    with patch("job_runner.os.path.exists", return_value=True), \
         patch("job_runner.subprocess.run", side_effect=side_effect):
        status, message = runner._run_launchctl_job(job)
    assert status == JobStatus.SUCCESS


def test_launchctl_uses_dynamic_uid_not_hardcoded_501():
    runner = JobRunner()
    job = Job(
        name="fake", display_name="Fake", aliases=[], description="test",
        executor="launchctl", target="com.ivy.brain",
    )
    calls = []

    def side_effect(cmd, **kwargs):
        calls.append(cmd)
        result = MagicMock()
        result.stdout, result.returncode, result.stderr = "com.ivy.brain\n", 0, ""
        return result

    with patch("job_runner.os.path.exists", return_value=True), \
         patch("job_runner.subprocess.run", side_effect=side_effect):
        runner._run_launchctl_job(job)
    kickstart_call = next(c for c in calls if c[:2] == ["launchctl", "kickstart"])
    assert kickstart_call[-1] == f"gui/{os.getuid()}/com.ivy.brain"


def test_entrypoint_job_missing_venv_python_returns_error(tmp_path, monkeypatch):
    import job_runner as jr

    monkeypatch.setattr(jr, "VENV_PYTHON", tmp_path / "does_not_exist" / "python")
    runner = JobRunner()
    job = Job(
        name="fake", display_name="Fake", aliases=[], description="test",
        executor="entrypoint", entrypoint="proactive_agents.Familia_meal_planner:run",
    )
    with patch("job_runner.subprocess.Popen") as mock_popen:
        status, message = runner._run_entrypoint_job(job)
    assert status == JobStatus.ERROR
    mock_popen.assert_not_called()


def test_entrypoint_job_spawns_detached_subprocess_not_thread(tmp_path, monkeypatch):
    """Regression test: an earlier version of this method used an
    in-process daemon thread, which a short-lived `ivy run ...` CLI
    invocation would kill before a multi-minute job actually finished."""
    import job_runner as jr

    fake_python = tmp_path / "python"
    fake_python.write_text("#!/bin/sh\n")
    fake_python.chmod(0o755)
    monkeypatch.setattr(jr, "VENV_PYTHON", fake_python)
    monkeypatch.setattr(jr, "PROJECT_ROOT", tmp_path)

    runner = JobRunner()
    job = Job(
        name="fake", display_name="Fake", aliases=[], description="test",
        executor="entrypoint", entrypoint="proactive_agents.Familia_meal_planner:run",
    )
    with patch("job_runner.subprocess.Popen") as mock_popen:
        status, message = runner._run_entrypoint_job(job, force=True, send=True)

    assert status == JobStatus.SUCCESS
    mock_popen.assert_called_once()
    args, kwargs = mock_popen.call_args
    argv = args[0]
    assert str(fake_python) in argv
    assert "--force" in argv
    assert "--send" in argv
    assert kwargs.get("start_new_session") is True
