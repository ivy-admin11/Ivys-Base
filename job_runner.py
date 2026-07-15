"""
Job registry and executor for Ivy — maps natural language requests to launchd
agents (scheduled jobs with a real launchd target) or direct Python
entrypoints (ad-hoc jobs run as a detached subprocess, independent of
whether the calling process — a short-lived `ivy run ...` invocation, or a
request handler inside the long-lived gateway — is still alive when the job
finishes).
"""

import logging
import os
import subprocess
import time
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ivy_core import receipts

logger = logging.getLogger("ivy.jobs")

PROJECT_ROOT = Path(__file__).resolve().parent
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"


class JobStatus(Enum):
    """Job execution status."""
    SUCCESS = "success"
    ALREADY_RUNNING = "already_running"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class Job:
    """Represents a runnable job with metadata."""

    def __init__(
        self,
        name: str,
        display_name: str,
        aliases: List[str],
        description: str,
        executor: str,  # "launchctl", "entrypoint", or "shell"
        target: Optional[str] = None,      # launchd label (for "launchctl")
        entrypoint: Optional[str] = None,   # "module.path:function" (for "entrypoint")
        schedule: Optional[str] = None,
        available: bool = True,
        unavailable_reason: Optional[str] = None,
    ):
        self.name = name
        self.display_name = display_name
        self.aliases = aliases
        self.description = description
        self.executor = executor
        self.target = target
        self.entrypoint = entrypoint
        self.schedule = schedule
        self.available = available
        self.unavailable_reason = unavailable_reason

    def __repr__(self):
        return f"<Job {self.name}: {self.description}>"


# Job registry — map natural language to executables.
#
# The gateway itself is deliberately NOT registered here: a job request must
# never be able to restart or kill the process handling that request.
JOB_REGISTRY = [
    Job(
        name="sharp_picks",
        display_name="Sharp Picks",
        aliases=["picks", "sharppicks", "sharp picks", "daily picks", "sports picks",
                  "sports bettor", "sports_bettor", "my sports picks", "run picks",
                  "send me sharp picks"],
        description="Run daily sports picks job — analyzes matchups and sends picks",
        executor="entrypoint",
        target="com.ivy.sharppicks",  # scheduled cadence — still installed via launchd
        entrypoint="proactive_agents.sports_bettor:run",  # ad-hoc requests bypass launchd entirely
        schedule="Every 30 min (4 CST windows daily)",
    ),
    Job(
        name="happy_hour",
        display_name="Happy Hour Scout",
        aliases=["happy hour", "hh scout", "happy_hour_scout", "scout"],
        description="Find happy hours near you — searches venues and deals",
        executor="entrypoint",
        target="com.ivy.happy_hour_scout",
        entrypoint="proactive_agents.happy_hour_scout:run",
        # NOTE: the actual installed plist's StartCalendarInterval sets
        # Weekday=1, which is Monday in launchd's convention (0/7=Sunday),
        # not Sunday as this description previously (incorrectly) claimed.
        # Preserved as-is pending confirmation of which day was intended.
        schedule="Mondays 12pm CST",
    ),
    Job(
        name="bravo_scout",
        display_name="Bravo Scout",
        aliases=["bravo", "bravoscout", "reality scout"],
        description="Monitor Bravo reality TV schedules and episodes",
        executor="launchctl",
        target="com.ivy.bravoscout",
        available=False,
        unavailable_reason=(
            "proactive_agents/bravo_scout.py does not exist in this repo — no "
            "implementation has ever been committed to main (only uncommitted "
            "copies survive in abandoned .claude/worktrees/ directories)."
        ),
    ),
    Job(
        name="familia_meal_planner",
        display_name="Familia Meal Planner",
        aliases=[
            "planner", "weekly planner", "meal planner", "meals", "meal plan",
            "familia meal planner", "familia_meal_planner",
            "household meal plan", "household/meal plan",
        ],
        description=(
            "Generate a Venezuelan-American-Asian fusion weekly meal plan and "
            "text it to the household"
        ),
        executor="entrypoint",
        target="com.ivy.familia_meal_planner",  # new scheduled label — see deploy/launchd/
        entrypoint="proactive_agents.Familia_meal_planner:run",
        schedule="Sundays 8am CST",
    ),
    Job(
        name="brain",
        display_name="Brain (Grok xAI)",
        aliases=["brain", "grok", "xai"],
        description="Brain agent — uses Grok for knowledge queries",
        executor="launchctl",
        target="com.ivy.brain",
    ),
]


class JobRunner:
    """Executes jobs via launchctl or a direct Python entrypoint."""

    def __init__(self):
        self.registry = {job.name: job for job in JOB_REGISTRY}
        self.running_jobs = {}  # track running job process IDs

    def find_job(self, query: str) -> Optional[Job]:
        """
        Find job by name or alias (case-insensitive).
        Returns the first matching job or None.
        """
        query_lower = query.lower().strip()

        # Exact name match
        if query_lower in self.registry:
            return self.registry[query_lower]

        # Alias match
        for job in self.registry.values():
            if query_lower in [alias.lower() for alias in job.aliases]:
                return job

        # Fuzzy match — check if query is substring of name/aliases
        for job in self.registry.values():
            if (query_lower in job.name.lower() or
                any(query_lower in alias.lower() for alias in job.aliases)):
                return job

        return None

    def run_job(
        self,
        job_name: str,
        *,
        force: bool = False,
        send: bool = True,
        requester: Optional[str] = None,
    ) -> Tuple[JobStatus, str]:
        """
        Execute a job by name and return status + message. Never fabricates
        success — a missing plist, a nonexistent entrypoint module, or a
        nonzero launchctl exit code all come back as an explicit failure.
        """
        execution_id = receipts.record_start(job_name, requester=requester)

        job = self.find_job(job_name)
        if not job:
            available_names = ", ".join(j.display_name for j in self.registry.values() if j.available)
            status, message = JobStatus.NOT_FOUND, f"Job '{job_name}' not found. Available jobs: {available_names}"
        elif not job.available:
            status, message = JobStatus.UNAVAILABLE, f"{job.display_name} is unavailable: {job.unavailable_reason}"
        else:
            try:
                if job.executor == "entrypoint":
                    status, message = self._run_entrypoint_job(job, force=force, send=send, requester=requester)
                elif job.executor == "launchctl":
                    status, message = self._run_launchctl_job(job)
                elif job.executor == "shell":
                    status, message = self._run_shell_job(job)
                else:
                    status, message = JobStatus.ERROR, f"Unknown executor type: {job.executor}"
            except Exception as e:
                logger.error(f"Error running job {job.name}: {e}")
                status, message = JobStatus.ERROR, f"Error running {job.display_name}: {str(e)}"

        # Records the dispatch outcome, not necessarily the detached
        # subprocess's eventual completion (entrypoint jobs run
        # independently after being launched — see _run_entrypoint_job).
        receipts.record_finish(execution_id, status.value, message)
        return status, message

    def _run_launchctl_job(self, job: Job) -> Tuple[JobStatus, str]:
        """Run a launchd agent, verifying every launchctl call's actual exit
        status — a completed subprocess.run() is not proof launchctl
        succeeded."""
        uid = os.getuid()
        plist_path = os.path.expanduser(f"~/Library/LaunchAgents/{job.target}.plist")

        if not os.path.exists(plist_path):
            return JobStatus.UNAVAILABLE, (
                f"{job.display_name}: launchd plist missing at expected path {plist_path}"
            )

        list_result = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=5
        )

        if job.target in list_result.stdout:
            # Already loaded — trigger it now instead of waiting for the schedule.
            result = subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{uid}/{job.target}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                logger.warning("kickstart failed for %s: %s", job.target, detail)
                return JobStatus.ERROR, f"Could not trigger {job.display_name}: {detail or 'unknown launchctl error'}"
            return JobStatus.SUCCESS, f"✓ {job.display_name} triggered. {job.description}"

        # Not loaded — bootstrap it into the user's GUI domain.
        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", plist_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            logger.warning("bootstrap failed for %s: %s", job.target, detail)
            return JobStatus.ERROR, f"Could not load {job.display_name}: {detail or 'unknown launchctl error'}"
        return JobStatus.SUCCESS, f"✓ {job.display_name} loaded and started. {job.description}"

    def _run_entrypoint_job(
        self,
        job: Job,
        *,
        force: bool = False,
        send: bool = True,
        requester: Optional[str] = None,
    ) -> Tuple[JobStatus, str]:
        """Run a job as a detached `python -m <module> --force --send`
        subprocess — no launchd involved, and no launchd job needs to be
        preloaded. Deliberately a subprocess, not an in-process thread: an
        in-process daemon thread would be killed the instant a short-lived
        `ivy run ...` CLI invocation exits, well before a multi-minute sweep
        (sports odds, X handicappers, PDF generation, iMessage delivery)
        actually finishes. A detached subprocess survives independently of
        whichever process requested it.
        """
        if not VENV_PYTHON.exists():
            return JobStatus.ERROR, (
                f"Could not run {job.display_name}: project venv python not found at {VENV_PYTHON}"
            )

        module_name = job.entrypoint.split(":", 1)[0]
        args = [str(VENV_PYTHON), "-m", module_name]
        if force:
            args.append("--force")
        if send:
            args.append("--send")

        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{job.name}_adhoc_{int(time.time())}.log"

        env = dict(os.environ)
        env["PYTHONPATH"] = str(PROJECT_ROOT)

        logger.info(
            "Ad-hoc %s requested by %s (force=%s, send=%s) -> %s",
            job.name, requester or "unknown", force, send, " ".join(args),
        )
        try:
            with open(log_path, "wb") as log_file:
                subprocess.Popen(
                    args,
                    cwd=str(PROJECT_ROOT),
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except Exception as exc:
            return JobStatus.ERROR, f"Could not start {job.display_name}: {exc}"

        return JobStatus.SUCCESS, f"✓ {job.display_name} started (log: {log_path}). {job.description}"

    def _run_shell_job(self, job: Job) -> Tuple[JobStatus, str]:
        """Run a shell script."""
        try:
            subprocess.Popen(
                [job.target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            return JobStatus.SUCCESS, f"✓ {job.display_name} started. {job.description}"
        except Exception as e:
            return JobStatus.ERROR, f"Could not run {job.display_name}: {str(e)}"

    def list_jobs(self) -> List[Dict[str, object]]:
        """Return all available jobs with metadata, including unavailable ones
        (with a reason) — never silently omitted."""
        return [
            {
                "name": job.name,
                "display_name": job.display_name,
                "description": job.description,
                "aliases": ", ".join(job.aliases),
                "schedule": job.schedule or "On-demand",
                "available": job.available,
                "unavailable_reason": job.unavailable_reason,
            }
            for job in self.registry.values()
        ]


# Global job runner instance
job_runner = JobRunner()
