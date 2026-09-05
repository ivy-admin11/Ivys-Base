"""The watchdog's silence thresholds must follow the launchd schedules.

An earlier audit reported Happy Hour as "stopped running" because its log had
been quiet since a Monday. It runs on Sundays. Nothing was broken; the cadence
had been inferred from log spacing instead of read from the plist.

These tests remove the inference. Every threshold is checked against the
schedule the job is actually installed with, so a plist change that nobody
mirrors into EXPECTED_SILENCE_H fails here rather than paging Henry weekly.
"""
from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from ivy_core.agent_watchdog import EXPECTED_SILENCE_H

TEMPLATES = Path(__file__).resolve().parents[1] / "deploy" / "launchd"

# Watchdog job key -> launchd label. Kept explicit: the two names differ for
# every job, and guessing the mapping is how they drift apart.
JOB_PLISTS = {
    "sharp_picks": "com.ivy.sharppicks",
    "happy_hour": "com.ivy.happy_hour_scout",
    "familia_meal_planner": "com.ivy.familia_meal_planner",
    "ivy_brain": "com.ivy.brain",
}

# com.ivy.brain has no StartCalendarInterval: it is a KeepAlive daemon that
# launchd restarts on any exit. Its threshold is a liveness heuristic, not a
# derived schedule, so the gap check does not apply to it.
DAEMONS = {"ivy_brain"}


def _load(label: str) -> dict:
    raw = (TEMPLATES / f"{label}.plist.template").read_bytes()
    return plistlib.loads(raw)


def longest_scheduled_gap_h(plist: dict) -> float:
    """Hours between consecutive fires, worst case, for a StartCalendarInterval."""
    interval = plist.get("StartCalendarInterval")
    if interval is None:
        raise ValueError("no StartCalendarInterval")
    entries = interval if isinstance(interval, list) else [interval]

    # Any Weekday key pins the job to one day a week.
    if any("Weekday" in e for e in entries):
        if len({e.get("Weekday") for e in entries}) > 1:
            raise ValueError("multi-day schedules not modelled")
        return 168.0

    # Otherwise it is daily: find the largest wrap-around gap between fires.
    minutes = sorted(e.get("Hour", 0) * 60 + e.get("Minute", 0) for e in entries)
    if len(minutes) == 1:
        return 24.0
    gaps = [b - a for a, b in zip(minutes, minutes[1:])]
    gaps.append(minutes[0] + 1440 - minutes[-1])
    return max(gaps) / 60.0


@pytest.mark.parametrize("job,label", sorted(JOB_PLISTS.items()))
def test_watchdog_thresholds_match_plists(job: str, label: str) -> None:
    if job in DAEMONS:
        pytest.skip(f"{job} is an always-on daemon, not a scheduled job")
    gap = longest_scheduled_gap_h(_load(label))
    limit = EXPECTED_SILENCE_H[job]

    assert limit > gap, (
        f"{job}: watchdog alerts after {limit}h but {label} can legitimately "
        f"stay quiet for {gap:g}h. This fires on a healthy job."
    )
    assert limit <= gap * 2, (
        f"{job}: watchdog waits {limit}h against a {gap:g}h schedule — it would "
        f"miss more than a full missed run before saying anything."
    )


def test_every_watched_job_has_a_plist() -> None:
    assert set(EXPECTED_SILENCE_H) == set(JOB_PLISTS), (
        "A job is watched but not mapped to a launchd label (or vice versa); "
        "its threshold is unverifiable."
    )


def test_happy_hour_is_weekly_not_daily() -> None:
    """Regression pin for the specific misdiagnosis."""
    assert _load("com.ivy.happy_hour_scout")["StartCalendarInterval"]["Weekday"] == 0
    assert EXPECTED_SILENCE_H["happy_hour"] > 168


@pytest.mark.parametrize(
    "template", sorted(p.name for p in TEMPLATES.glob("*.plist.template"))
)
def test_every_plist_template_is_valid_xml(template: str) -> None:
    """launchctl rejects a malformed plist outright.

    The meal-planner template carried an XML comment containing a double
    hyphen, which is not legal XML. It parsed fine on the machine where the
    job was already installed, so nothing surfaced -- until a reinstall would
    have silently killed the job.
    """
    plistlib.loads((TEMPLATES / template).read_bytes())


@pytest.mark.parametrize("job", sorted(DAEMONS))
def test_daemons_are_keepalive_with_no_schedule(job: str) -> None:
    """A daemon's threshold cannot be derived, so pin what it actually is.

    com.ivy.brain runs KeepAlive=true with no calendar interval -- launchd
    relaunches it on any exit, so it should never be quiet for long. It went
    silent on 2026-07-16 and nothing reported it for seven weeks, because the
    watchdog only knew about the three scheduled agents.
    """
    plist = _load(JOB_PLISTS[job])
    assert plist.get("KeepAlive") is True
    assert "StartCalendarInterval" not in plist
    assert EXPECTED_SILENCE_H[job] > 0


def test_a_daemon_log_outside_the_repo_still_resolves(tmp_path, monkeypatch) -> None:
    """brain's log lives in ~/ai-admin-api, not in this repo."""
    from datetime import datetime, timezone

    from ivy_core import agent_watchdog as wd

    log = tmp_path / "ivy_brain_output.log"
    log.write_text("alive\n")
    monkeypatch.setitem(wd.LOG_FILES, "ivy_brain", str(log))   # absolute
    seen = wd.last_activity("ivy_brain")
    assert seen is not None, "an absolute log path must be readable"
    assert (datetime.now(timezone.utc) - seen).total_seconds() < 120


def test_a_missing_daemon_log_does_not_false_alarm(tmp_path, monkeypatch) -> None:
    """No log at all is 'no evidence', not 'stopped' -- the machine may simply
    not have that project checked out."""
    from ivy_core import agent_watchdog as wd

    monkeypatch.setitem(wd.LOG_FILES, "ivy_brain", str(tmp_path / "absent.log"))
    assert wd.last_activity("ivy_brain") is None
