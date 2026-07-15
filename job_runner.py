"""
Job registry and executor for Ivy — maps natural language requests to launchd agents and shell scripts.
"""

import subprocess
import logging
from typing import Dict, List, Optional, Tuple
from enum import Enum

logger = logging.getLogger("ivy.jobs")

class JobStatus(Enum):
    """Job execution status."""
    SUCCESS = "success"
    ALREADY_RUNNING = "already_running"
    NOT_FOUND = "not_found"
    ERROR = "error"

class Job:
    """Represents a runnable job with metadata."""

    def __init__(
        self,
        name: str,
        display_name: str,
        aliases: List[str],
        description: str,
        executor: str,  # "launchctl" or "shell"
        target: str,     # agent name or script path
        schedule: Optional[str] = None
    ):
        self.name = name
        self.display_name = display_name
        self.aliases = aliases
        self.description = description
        self.executor = executor
        self.target = target
        self.schedule = schedule

    def __repr__(self):
        return f"<Job {self.name}: {self.description}>"

# Job registry — map natural language to executables
JOB_REGISTRY = [
    Job(
        name="sharp_picks",
        display_name="Sharp Picks",
        aliases=["picks", "sharppicks", "sharp picks", "daily picks", "sports picks"],
        description="Run daily sports picks job — analyzes matchups and sends picks",
        executor="launchctl",
        target="com.ivy.sharppicks",
        schedule="Every 30 min (4 CST windows daily)"
    ),
    Job(
        name="happy_hour",
        display_name="Happy Hour Scout",
        aliases=["happy hour", "hh scout", "happy_hour_scout", "scout"],
        description="Find happy hours near you — searches venues and deals",
        executor="launchctl",
        target="com.ivy.happy_hour_scout",
        schedule="Sundays 12pm CST"
    ),
    Job(
        name="bravo_scout",
        display_name="Bravo Scout",
        aliases=["bravo", "bravoscout", "reality scout"],
        description="Monitor Bravo reality TV schedules and episodes",
        executor="launchctl",
        target="com.ivy.bravoscout"
    ),
    Job(
        name="weekly_planner",
        display_name="Weekly Planner",
        aliases=["planner", "weekly planner", "weeklyplanner", "meal planner", "meals", "meal plan"],
        description="Generate weekly meal plan and save to Google Drive",
        executor="launchctl",
        target="com.ivy.weeklyplanner"
    ),
    Job(
        name="brain",
        display_name="Brain (Grok xAI)",
        aliases=["brain", "grok", "xai"],
        description="Brain agent — uses Grok for knowledge queries",
        executor="launchctl",
        target="com.ivy.brain"
    ),
    Job(
        name="gateway",
        display_name="Gateway API",
        aliases=["gateway", "api", "server"],
        description="Ivy's local FastAPI gateway (currently running)",
        executor="launchctl",
        target="com.ivy.gateway",
        schedule="Always running"
    ),
]

class JobRunner:
    """Executes jobs via launchctl or shell scripts."""

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

    def run_job(self, job_name: str) -> Tuple[JobStatus, str]:
        """
        Execute a job by name and return status + message.
        """
        job = self.find_job(job_name)
        if not job:
            return JobStatus.NOT_FOUND, f"Job '{job_name}' not found. Available jobs: {', '.join(j.display_name for j in self.registry.values())}"

        try:
            if job.executor == "launchctl":
                return self._run_launchctl_job(job)
            elif job.executor == "shell":
                return self._run_shell_job(job)
            else:
                return JobStatus.ERROR, f"Unknown executor type: {job.executor}"
        except Exception as e:
            logger.error(f"Error running job {job.name}: {e}")
            return JobStatus.ERROR, f"Error running {job.display_name}: {str(e)}"

    def _run_launchctl_job(self, job: Job) -> Tuple[JobStatus, str]:
        """Run a launchd agent."""
        # Check if already running
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=5
        )

        if job.target in result.stdout:
            # Job is already loaded; trigger it with "kickstart" (for interval-based jobs)
            # or just reload if needed
            try:
                subprocess.run(
                    ["launchctl", "kickstart", "-k", f"gui/501/{job.target}"],
                    capture_output=True,
                    timeout=5
                )
                return JobStatus.SUCCESS, f"✓ {job.display_name} triggered. {job.description}"
            except Exception as e:
                # If kickstart fails, job might already be running
                return JobStatus.ALREADY_RUNNING, f"{job.display_name} is already running."
        else:
            # Job not loaded — load it
            try:
                subprocess.run(
                    ["launchctl", "load", f"-w", f"/Library/LaunchAgents/{job.target}.plist"],
                    capture_output=True,
                    timeout=5
                )
                return JobStatus.SUCCESS, f"✓ {job.display_name} loaded and started. {job.description}"
            except Exception as e:
                logger.warning(f"Could not load {job.target}: {e}")
                # Try kickstart as fallback
                try:
                    subprocess.run(
                        ["launchctl", "kickstart", "-k", f"gui/501/{job.target}"],
                        capture_output=True,
                        timeout=5
                    )
                    return JobStatus.SUCCESS, f"✓ {job.display_name} triggered. {job.description}"
                except Exception as e2:
                    return JobStatus.ERROR, f"Could not run {job.display_name}: {str(e2)}"

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

    def list_jobs(self) -> List[Dict[str, str]]:
        """Return all available jobs with metadata."""
        return [
            {
                "name": job.name,
                "display_name": job.display_name,
                "description": job.description,
                "aliases": ", ".join(job.aliases),
                "schedule": job.schedule or "On-demand"
            }
            for job in self.registry.values()
        ]


# Global job runner instance
job_runner = JobRunner()
