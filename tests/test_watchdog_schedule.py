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
    "daily_brief_morning": "com.ivy.daily_brief_morning",
    "daily_brief_evening": "com.ivy.daily_brief_evening",
}

# No daemons are watched today. com.ivy.brain was, until it was retired --
# see deploy/launchd/retired/README.md. Kept as an empty set because the
# distinction is real: a KeepAlive daemon has no schedule to derive a
# threshold from, so if one is ever watched again it needs the separate
# treatment below rather than the gap check.
DAEMONS: set[str] = set()


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


def test_happy_hour_is_daily_and_watched_as_such() -> None:
    """Happy Hour ran Weekday=0 until 2026-09-09, and an audit read the
    six-day silence as "stopped since Sep 1" — the misdiagnosis this file was
    written to prevent. It is daily now, at Henry's request, so the pin flips:
    the schedule must carry no Weekday key, and the watchdog must not still be
    waiting a week before it says anything.

    The threshold and the plist have to move together in either direction.
    Whichever one is left behind, the watchdog is wrong: too loud on a weekly
    job, or silent through a dead daily one.
    """
    interval = _load("com.ivy.happy_hour_scout")["StartCalendarInterval"]
    assert "Weekday" not in interval, "a leftover Weekday makes it weekly again"
    assert interval["Hour"] == 12
    assert EXPECTED_SILENCE_H["happy_hour"] <= 48


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


def test_retired_jobs_are_not_watched() -> None:
    """A retired job must be removed from the table, not given a bigger number.

    com.ivy.brain was added here, then retired the same day. Its log still
    exists with a July timestamp, so leaving it watched would have alerted on
    the next gateway restart about a job that is supposed to be dead -- the
    false-alarm failure this watchdog exists to avoid.
    """
    retired = {"ivy_brain"}
    assert not (set(EXPECTED_SILENCE_H) & retired), (
        "a retired job is still in EXPECTED_SILENCE_H; it will alert forever"
    )
    from ivy_core.agent_watchdog import LOG_FILES
    assert not (set(LOG_FILES) & retired)


def test_a_missing_daemon_log_does_not_false_alarm(tmp_path, monkeypatch) -> None:
    """No log at all is 'no evidence', not 'stopped' -- the machine may simply
    not have that project checked out."""
    from ivy_core import agent_watchdog as wd

    monkeypatch.setitem(wd.LOG_FILES, "ivy_brain", str(tmp_path / "absent.log"))
    assert wd.last_activity("ivy_brain") is None
