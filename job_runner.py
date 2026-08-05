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
import re
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config import JOB_MAX_RUNTIME_SECONDS
from ivy_core import receipts

logger = logging.getLogger("ivy.jobs")

PROJECT_ROOT = Path(__file__).resolve().parent
# Detached workers must use the same reviewed interpreter as the dispatcher.
# Production deploys use an immutable SHA-addressed venv, so hard-coding the
# repository's developer `.venv` would otherwise run jobs outside the release
# environment (or fail if that developer venv is absent).
VENV_PYTHON = Path(sys.executable).resolve()

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)((?:api[_-]?key|access[_-]?token|token|secret|password)\s*[=:]\s*)[^&\s]+"
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_URL_QUERY = re.compile(r"(https?://[^?\s]+)\?[^\s]+")


def _sanitize_failure_detail(value: object, *, limit: int = 500) -> str:
    text = str(value)
    text = _SECRET_ASSIGNMENT.sub(r"\1[redacted]", text)
    text = _BEARER_TOKEN.sub("Bearer [redacted]", text)
    text = _URL_QUERY.sub(r"\1?[redacted]", text)
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    return text[:limit]


def _record_terminal_safely(
    execution_id: str,
    status: str,
    detail: str,
    **metadata: object,
) -> bool:
    try:
        return receipts.record_finish(execution_id, status, detail, **metadata)
    except Exception as exc:
        logger.error(
            "Could not finalize execution %s as %s (%s)",
            execution_id,
            status,
            type(exc).__name__,
        )
        return False


class JobStatus(Enum):
    """Job execution status."""
    SUCCESS = "success"
    ALREADY_RUNNING = "already_running"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


@dataclass(frozen=True)
class JobDispatchResult:
    """Detailed dispatch response for callers that need receipt correlation."""

    status: JobStatus
    message: str
    execution_id: Optional[str]
    lifecycle_status: str
    canonical_job_name: str


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
        max_runtime_seconds: int = JOB_MAX_RUNTIME_SECONDS,
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
        if not 30 <= int(max_runtime_seconds) <= 21600:
            raise ValueError("max_runtime_seconds must be between 30 and 21600")
        self.max_runtime_seconds = int(max_runtime_seconds)

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
        schedule="3x daily at 9am / 3pm / 9pm CT",
        max_runtime_seconds=3600,
    ),
    Job(
        name="happy_hour",
        display_name="Happy Hour Scout",
        aliases=["happy hour", "hh scout", "happy_hour_scout", "scout"],
        description="Find happy hours near you — searches venues and deals",
        executor="entrypoint",
        target="com.ivy.happy_hour_scout",
        entrypoint="proactive_agents.happy_hour_scout:run",
        # deploy/launchd/com.ivy.happy_hour_scout.plist.template sets
        # Weekday=0, which is Sunday in launchd's convention (0/7=Sunday).
        schedule="Sundays 12pm CST",
        max_runtime_seconds=1800,
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
        max_runtime_seconds=1800,
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
        """Backward-compatible two-value dispatch API.

        ``SUCCESS`` means the dispatch was accepted.  For an entrypoint job the
        durable receipt remains nonterminal until ``ivy_core.job_worker``
        records the agent's eventual outcome.
        """
        result = self.run_job_detailed(
            job_name,
            force=force,
            send=send,
            requester=requester,
        )
        return result.status, result.message

    def run_job_detailed(
        self,
        job_name: str,
        *,
        force: bool = False,
        send: bool = True,
        requester: Optional[str] = None,
    ) -> JobDispatchResult:
        """Dispatch a job and return its durable execution ID and lifecycle."""
        receipts.reconcile_stale()
        job = self.find_job(job_name)
        canonical_name = job.name if job else job_name.strip()
        executor = job.executor if job else None
        initial_delivery = "not_attempted" if send else "not_requested"

        try:
            execution_id = receipts.record_start(
                canonical_name,
                requester=requester,
                executor=executor,
                delivery_status=initial_delivery,
            )
        except receipts.ExecutionAlreadyActive as active:
            try:
                active_record = (
                    receipts.get_execution(active.execution_id, reconcile=False) or {}
                )
            except Exception:
                active_record = {}
            display_name = job.display_name if job else canonical_name
            return JobDispatchResult(
                status=JobStatus.ALREADY_RUNNING,
                message=(
                    f"{display_name} is already running "
                    f"(execution {active.execution_id})."
                ),
                execution_id=active.execution_id,
                lifecycle_status=str(active_record.get("status", "running")),
                canonical_job_name=canonical_name,
            )
        except Exception as exc:
            error_type = type(exc).__name__
            logger.error("Could not create execution receipt for %s (%s)", canonical_name, error_type)
            return JobDispatchResult(
                status=JobStatus.ERROR,
                message=f"Could not queue {canonical_name} ({error_type}).",
                execution_id=None,
                lifecycle_status="dispatch_failed",
                canonical_job_name=canonical_name,
            )

        if not job:
            available_names = ", ".join(
                candidate.display_name
                for candidate in self.registry.values()
                if candidate.available
            )
            status = JobStatus.NOT_FOUND
            message = f"Job '{job_name}' not found. Available jobs: {available_names}"
            lifecycle = "not_found"
            _record_terminal_safely(
                execution_id,
                lifecycle,
                message,
                outcome="not_found",
                delivery_status=initial_delivery,
            )
        elif not job.available:
            status = JobStatus.UNAVAILABLE
            message = f"{job.display_name} is unavailable: {job.unavailable_reason}"
            lifecycle = "unavailable"
            _record_terminal_safely(
                execution_id,
                lifecycle,
                message,
                outcome="unavailable",
                delivery_status=initial_delivery,
            )
        else:
            try:
                if job.executor == "entrypoint":
                    status, message = self._run_entrypoint_job(
                        job,
                        force=force,
                        send=send,
                        requester=requester,
                        execution_id=execution_id,
                    )
                    lifecycle = "dispatched" if status == JobStatus.SUCCESS else "dispatch_failed"
                elif job.executor == "launchctl":
                    status, message = self._run_launchctl_job(job)
                    lifecycle = (
                        "triggered_unobserved"
                        if status == JobStatus.SUCCESS
                        else "unavailable" if status == JobStatus.UNAVAILABLE
                        else "dispatch_failed"
                    )
                elif job.executor == "shell":
                    status, message = self._run_shell_job(job)
                    lifecycle = (
                        "triggered_unobserved"
                        if status == JobStatus.SUCCESS
                        else "dispatch_failed"
                    )
                else:
                    status = JobStatus.ERROR
                    message = f"Unknown executor type: {job.executor}"
                    lifecycle = "dispatch_failed"
            except Exception as exc:
                error_type = type(exc).__name__
                logger.error("Error dispatching %s (%s)", job.name, error_type)
                status = JobStatus.ERROR
                message = f"Could not start {job.display_name} ({error_type})."
                lifecycle = "dispatch_failed"

            if job.executor != "entrypoint" or status != JobStatus.SUCCESS:
                delivery = (
                    "unknown"
                    if lifecycle == "triggered_unobserved"
                    else initial_delivery
                )
                _record_terminal_safely(
                    execution_id,
                    lifecycle,
                    message,
                    outcome=lifecycle,
                    delivery_status=delivery,
                )

        try:
            current = receipts.get_execution(execution_id, reconcile=False) or {}
        except Exception as exc:
            logger.error(
                "Could not reload execution %s (%s)",
                execution_id,
                type(exc).__name__,
            )
            current = {}
        return JobDispatchResult(
            status=status,
            message=message,
            execution_id=execution_id,
            lifecycle_status=str(current.get("status", lifecycle)),
            canonical_job_name=canonical_name,
        )

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
                detail = _sanitize_failure_detail(
                    (result.stderr or result.stdout or "").strip()
                )
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
            detail = _sanitize_failure_detail(
                (result.stderr or result.stdout or "").strip()
            )
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
        execution_id: Optional[str] = None,
    ) -> Tuple[JobStatus, str]:
        """Run a job through the detached lifecycle-owning worker.

        The dispatcher records only spawn/PID/log metadata.  The worker marks
        the eventual completed/failed/skipped result.

        Historically this launched ``python -m <agent> --force --send``
        subprocess — no launchd involved, and no launchd job needs to be
        preloaded. Deliberately a subprocess, not an in-process thread: an
        in-process daemon thread would be killed the instant a short-lived
        `ivy run ...` CLI invocation exits, well before a multi-minute sweep
        (sports odds, X handicappers, PDF generation, iMessage delivery)
        actually finishes. A detached subprocess survives independently of
        whichever process requested it.
        """
        if not VENV_PYTHON.exists():
            if execution_id:
                _record_terminal_safely(
                    execution_id,
                    "dispatch_failed",
                    "The current Python interpreter was not found.",
                    outcome="venv_missing",
                    delivery_status="not_attempted" if send else "not_requested",
                )
            return JobStatus.ERROR, (
                f"Could not run {job.display_name}: current Python interpreter was not found"
            )

        if not job.entrypoint or ":" not in job.entrypoint:
            if execution_id:
                _record_terminal_safely(
                    execution_id,
                    "dispatch_failed",
                    "Registered entrypoint is invalid.",
                    outcome="invalid_entrypoint",
                    delivery_status="not_attempted" if send else "not_requested",
                )
            return JobStatus.ERROR, f"Could not run {job.display_name}: invalid entrypoint"

        owns_receipt = execution_id is None
        if execution_id is None:
            try:
                execution_id = receipts.record_start(
                    job.name,
                    requester=requester,
                    executor="entrypoint",
                    delivery_status="not_attempted" if send else "not_requested",
                )
            except receipts.ExecutionAlreadyActive:
                return JobStatus.ALREADY_RUNNING, f"{job.display_name} is already running."

        args = [
            str(VENV_PYTHON),
            "-m",
            "ivy_core.job_worker",
            "--execution-id",
            execution_id,
            "--job-name",
            job.name,
            "--entrypoint",
            job.entrypoint,
            "--timeout-seconds",
            str(job.max_runtime_seconds),
        ]
        if force:
            args.append("--force")
        if send:
            args.append("--send")

        log_dir = PROJECT_ROOT / "logs"
        log_path = log_dir / f"{job.name}_adhoc_{execution_id}.log"

        env = dict(os.environ)
        env["PYTHONPATH"] = str(PROJECT_ROOT)

        logger.info(
            "Ad-hoc job requested job=%s force=%s send=%s",
            job.name,
            force,
            send,
        )
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            with open(log_path, "xb") as log_file:
                log_path.chmod(0o600)
                process = subprocess.Popen(
                    args,
                    cwd=str(PROJECT_ROOT),
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except Exception as exc:
            error_type = type(exc).__name__
            _record_terminal_safely(
                execution_id,
                "dispatch_failed",
                f"{error_type}: detached worker could not be started.",
                outcome=f"spawn_exception:{error_type}",
                delivery_status="not_attempted" if send else "not_requested",
            )
            return JobStatus.ERROR, f"Could not start {job.display_name} ({error_type})."

        raw_pid = getattr(process, "pid", None)
        pid = raw_pid if isinstance(raw_pid, int) and raw_pid > 0 else None
        try:
            receipts.record_spawned(execution_id, pid=pid, log_path=str(log_path))
        except Exception as exc:
            # The process already exists.  A receipt metadata write failure
            # cannot truthfully turn a successful spawn into dispatch_failed;
            # the worker will make its own running/finalization attempt.
            logger.error(
                "Could not record spawned worker %s (%s)",
                execution_id,
                type(exc).__name__,
            )

        suffix = "" if owns_receipt else f" Execution: {execution_id}."
        return JobStatus.SUCCESS, (
            f"✓ {job.display_name} dispatched (log: {log_path.name}).{suffix} "
            f"{job.description}"
        )

    def _run_shell_job(self, job: Job) -> Tuple[JobStatus, str]:
        """Run a shell script."""
        try:
            subprocess.Popen(
                [job.target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            return JobStatus.SUCCESS, f"✓ {job.display_name} started. {job.description}"
        except Exception as exc:
            error_type = type(exc).__name__
            return JobStatus.ERROR, f"Could not run {job.display_name} ({error_type})."

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
                "max_runtime_seconds": job.max_runtime_seconds,
            }
            for job in self.registry.values()
        ]


# Global job runner instance
job_runner = JobRunner()
