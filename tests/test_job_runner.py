"""job_runner.py: alias resolution, launchctl return-code handling, receipts.

Every test here mocks subprocess — none of them ever invoke a real
launchctl command against a live scheduled job.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from job_runner import Job, JobRunner, JobStatus
from job_runner import job_runner as global_job_runner


@pytest.fixture
def unavailable_bravo(monkeypatch):
    """Bravo Scout is a real, runnable job now (b7eb065). These tests need
    *some* registered-but-unavailable job, so mark it unavailable for the
    duration of the test — never let a test launch the real agent."""
    job = global_job_runner.find_job("bravo")
    monkeypatch.setattr(job, "available", False)
    monkeypatch.setattr(job, "unavailable_reason", "proactive_agents/bravo_scout.py does not exist (test fixture)")
    return job


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


def test_bravo_scout_is_a_registered_runnable_job():
    job = global_job_runner.find_job("bravo")
    assert job is not None
    assert job.name == "bravo_scout"
    assert job.entrypoint == "proactive_agents.bravo_scout:run"
    assert job.available is True, job.unavailable_reason


def test_unavailable_job_reports_reason(unavailable_bravo):
    job = global_job_runner.find_job("bravo")
    assert job.available is False
    assert "does not exist" in job.unavailable_reason


def test_gateway_is_not_registered_as_a_job():
    assert global_job_runner.find_job("gateway") is None


def test_run_job_unavailable_never_touches_subprocess(unavailable_bravo):
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


def test_run_job_records_a_receipt_regardless_of_outcome(unavailable_bravo):
    """Regression test: unavailable/not-found attempts used to return
    before receipts.record_start() was ever called, so they never showed
    up in execution history at all."""
    from ivy_core import receipts

    with patch("job_runner.subprocess.Popen") as mock_popen:
        global_job_runner.run_job("bravo", requester="pytest")
    mock_popen.assert_not_called()
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
        executor="launchctl", target="com.ivy.brain",  # real installed plist, for the os.path.exists check
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


# ---------------------------------------------------------------------------
# The registry must not advertise what it cannot run
# ---------------------------------------------------------------------------

def test_a_launchctl_job_is_not_offered_without_an_installed_plist():
    """/capabilities reported brain as available=True for a launchd label that
    exists in no domain, so the only possible outcome of dispatching it was a
    failed launchctl call. `available` is a hand-set flag, not a live probe, so
    nothing caught the drift when the plist was retired underneath it.

    Claiming a job is ready is the same class of error as claiming one ran.
    """
    import os
    from pathlib import Path

    from job_runner import JOB_REGISTRY

    agents = Path(os.path.expanduser("~/Library/LaunchAgents"))
    offenders = []
    for job in JOB_REGISTRY:
        if job.executor != "launchctl" or not job.available:
            continue
        if not (agents / f"{job.target}.plist").exists():
            offenders.append(f"{job.name} -> {job.target}")
    assert not offenders, (
        "these jobs advertise themselves but have no installed plist: "
        + "; ".join(offenders)
    )


def test_the_retired_brain_job_says_why_it_is_gone():
    """A bare "unknown job" would invite someone to wire it back up. The entry
    stays so the name resolves to the reason it was retired."""
    from job_runner import JOB_REGISTRY

    brain = next(j for j in JOB_REGISTRY if j.name == "brain")
    assert brain.available is False
    assert "2026-09-05" in (brain.unavailable_reason or "")
    assert "chat.db" in (brain.unavailable_reason or "")


def test_the_daily_agents_have_no_weekday_key():
    """launchd matches only the keys present in StartCalendarInterval, so a
    leftover Weekday silently turns a daily job back into a weekly one — and
    it looks like a dead agent rather than a schedule."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for label in ("com.ivy.happy_hour_scout", "com.ivy.familia_meal_planner"):
        text = (root / "deploy" / "launchd" / f"{label}.plist.template").read_text()
        assert "<key>Weekday</key>" not in text, f"{label} is still weekly"


def test_the_meal_planner_gate_is_shorter_than_its_schedule():
    """The gate is a double-fire guard, not the schedule. It was 48 hours while
    the job ran weekly, where the two were indistinguishable. On a daily
    schedule a 48-hour gate delivers every OTHER day, which reads as a broken
    agent rather than a cadence — so it must stay under 24."""
    from proactive_agents.Familia_meal_planner import MIN_HOURS_BETWEEN_RUNS

    assert MIN_HOURS_BETWEEN_RUNS <= 24
