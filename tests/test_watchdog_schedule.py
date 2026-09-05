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
}


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
