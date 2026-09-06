"""Notice when a scheduled agent stops firing, and say so.

Why this exists
---------------
On 2026-09-05 an audit found Happy Hour had not run since 30 August and the
meal planner since the same day — six days of silence from two of four agents.
Nothing was broken loudly enough to notice: launchd simply wasn't starting
them, so there were no errors, no logs, and no reports. The only symptom was
an absence, and an absence is exactly what nobody sees.

The check deliberately lives in the GATEWAY, not in its own launchd unit. A
watchdog scheduled by the same mechanism it is watching shares the failure it
is meant to catch; the gateway is a long-running KeepAlive process, so it is
still alive precisely when the scheduler is not.

Staleness is measured against each job's own cadence with generous headroom —
this should fire when a job has plainly stopped, never because a slate was
quiet or a gate declined to run.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

import ivy_core.outbox as _outbox

logger = logging.getLogger("ivy.watchdog")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = PROJECT_ROOT / "data" / "watchdog_state.json"

# Hours of silence before a job is considered stopped. Each is several times
# the job's OBSERVED cadence, so a skipped run or a quiet slate never trips it.
#   sharp_picks  three reports a day                → 3 missed cycles
#   happy_hour   daily                              → 2 missed days
#   meal_planner writes weekly, not daily: its log
#                carries one entry every 7 days from
#                19 Jul to 30 Aug without exception  → 10 days
# Measured from what the logs actually show, not from what the plists say —
# the meal planner's template reads as a daily 08:00 start but has produced a
# strictly weekly log for seven weeks, and the watchdog has to match reality
# or it cries wolf every Monday.
# Derived from deploy/launchd/*.plist.template, NOT from how often a job
# happens to appear in the logs. Reading cadence off the logs is what made an
# earlier audit call Happy Hour "stopped since Sep 1": it runs Weekday 0, so a
# Monday-to-Saturday silence is the schedule working, not a failure. Each value
# is the longest legitimate gap plus roughly one interval of slack.
# test_watchdog_thresholds_match_plists holds these honest.
EXPECTED_SILENCE_H: Dict[str, int] = {
    "sharp_picks": 20,           # daily at 09/15/21 -> 12h worst gap
    "happy_hour": 192,           # Sundays 12:00 -> 168h between runs
    "familia_meal_planner": 192, # Sundays 08:00 -> 168h between runs
    "daily_brief_morning": 30,   # daily 07:30 -> 24h between runs
    "daily_brief_evening": 30,   # daily 17:30 -> 24h between runs
}

# com.ivy.brain was briefly watched here. It is now RETIRED (2026-09-05, see
# deploy/launchd/retired/README.md), and watching a job that is meant to be
# dead produces exactly one thing: a weekly false alarm about its silence.
# Its log still sits in ~/ai-admin-api with a July mtime, so leaving it in
# this table would have paged Henry on the next gateway restart.
#
# The rule this leaves behind: this table is for jobs that are supposed to be
# running. Retiring a job means removing it from here, not lowering its
# threshold.

FRIENDLY_NAMES = {
    "sharp_picks": "Sharp Picks",
    "happy_hour": "Happy Hour Scout",
    "familia_meal_planner": "Familia Meal Planner",
    "daily_brief_morning": "Morning Brief",
    "daily_brief_evening": "Evening Brief",
}

# Log files are the second source of truth: a job that ran and produced no
# report still writes a log, and that counts as alive.
LOG_FILES = {
    "sharp_picks": "logs/sharppicks_scheduled.log",
    "happy_hour": "logs/happy_hour_scheduled.log",
    "familia_meal_planner": "logs/familia_meal_planner.log",
    "daily_brief_morning": "logs/daily_brief_morning.log",
    "daily_brief_evening": "logs/daily_brief_evening.log",
}

# Never re-alert about the same job more often than this.
REALERT_AFTER_H = 24


def _log_mtime(job: str) -> Optional[datetime]:
    rel = LOG_FILES.get(job)
    if not rel:
        return None
    # com.ivy.brain's implementation and logs live outside this repo, so a
    # log path may be absolute or ~-relative rather than repo-relative.
    expanded = Path(rel).expanduser()
    path = expanded if expanded.is_absolute() else PROJECT_ROOT / rel
    try:
        if path.exists():
            return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError as exc:
        logger.warning("Watchdog: cannot stat %s: %s", rel, exc)
    return None


def _outbox_latest(job: str) -> Optional[datetime]:
    try:
        reports = _outbox.list_reports(job)
    except Exception as exc:
        logger.warning("Watchdog: outbox unreadable for %s: %s", job, exc)
        return None
    for meta in reports:
        raw = meta.get("generated_at") or ""
        try:
            ts = datetime.fromisoformat(raw)
        except ValueError:
            continue
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return None


def last_activity(job: str) -> Optional[datetime]:
    """The most recent sign of life: a delivered report or a log write."""
    stamps = [t for t in (_outbox_latest(job), _log_mtime(job)) if t]
    return max(stamps) if stamps else None


def stale_agents(now: Optional[datetime] = None) -> List[dict]:
    """Jobs that have been silent for longer than their cadence allows."""
    now = now or datetime.now(timezone.utc)
    out = []
    for job, limit_h in EXPECTED_SILENCE_H.items():
        seen = last_activity(job)
        if seen is None:
            # Never any evidence at all — a job that has never run is not
            # something this check can distinguish from one never installed.
            continue
        silent_h = (now - seen).total_seconds() / 3600.0
        if silent_h > limit_h:
            out.append({
                "job": job,
                "name": FRIENDLY_NAMES.get(job, job),
                "last_seen": seen,
                "silent_hours": round(silent_h, 1),
                "limit_hours": limit_h,
            })
    return sorted(out, key=lambda d: -d["silent_hours"])


def format_alert(stale: List[dict]) -> str:
    """The text Henry receives. Names the fix, not just the symptom."""
    plural = "jobs have" if len(stale) > 1 else "job has"
    lines = [f"\U0001F6A8 A background {plural} stopped running."]
    for s in stale:
        days = s["silent_hours"] / 24.0
        when = "a day" if days < 1.5 else f"{days:.0f} days"
        lines.append(f"• {s['name']} — nothing for {when} (last: {s['last_seen']:%b %-d})")
    lines.append("")
    lines.append("Usually launchd dropped the agent. Check with:")
    lines.append("launchctl list | grep com.ivy")
    return "\n".join(lines)


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Watchdog: state unreadable (%s); treating as empty.", exc)
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(STATE_PATH.parent, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2))
    except OSError as exc:
        logger.warning("Watchdog: could not persist state: %s", exc)


def check_once(send: Callable[[str], bool], now: Optional[datetime] = None) -> List[dict]:
    """Alert about newly-stale jobs. Returns the jobs alerted on.

    Each job is reported at most once per REALERT_AFTER_H so a job that stays
    down nags daily rather than every poll — silence is the bug being fixed
    here, but a repeating alarm is how the fix gets muted.
    """
    now = now or datetime.now(timezone.utc)
    stale = stale_agents(now)
    if not stale:
        return []

    state = _load_state()
    due = []
    for s in stale:
        prev = state.get(s["job"], {}).get("alerted_at")
        if prev:
            try:
                last = datetime.fromisoformat(prev)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if now - last < timedelta(hours=REALERT_AFTER_H):
                    continue
            except ValueError:
                pass
        due.append(s)

    if not due:
        return []

    if not send(format_alert(due)):
        logger.error("Watchdog: could not deliver the stale-job alert.")
        return []

    for s in due:
        state[s["job"]] = {"alerted_at": now.isoformat(), "silent_hours": s["silent_hours"]}
    _save_state(state)
    logger.warning("Watchdog: alerted on %s", ", ".join(s["job"] for s in due))
    return due
