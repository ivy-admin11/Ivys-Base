"""Ivy's unprompted texts, and the budget that keeps them readable.

The value of this channel is entirely in Henry still reading it in a month.
Two failure modes destroy that, and both have precedent on this project: a
false alarm (the watchdog that would have paged weekly about a healthy job),
and a flood (no cap, so the real one arrives among five that did not matter).

These tests pin the budget rather than the checks' wording, because the budget
is the part that decides whether the channel survives.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from ivy_core import proactive as pro
from ivy_core.proactive import CRITICAL, NORMAL, SUGGESTION, Finding


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(pro, "STATE_PATH", tmp_path / "proactive_state.json")
    return tmp_path / "proactive_state.json"


class Sender:
    """Records what would have been texted."""

    def __init__(self, succeed=True):
        self.sent, self.succeed = [], succeed

    def __call__(self, body):
        self.sent.append(body)
        return self.succeed


def finding(key="thing", severity=NORMAL, title="Something", detail="Because.", fix=""):
    return Finding(key=key, severity=severity, title=title, detail=detail, fix=fix)


def at(hour, day="2026-09-08"):
    """A local-time-aware moment on a Tuesday, unless told otherwise."""
    return datetime.fromisoformat(f"{day}T{hour:02d}:00:00").astimezone()


def only(monkeypatch, findings):
    monkeypatch.setattr(pro, "collect", lambda now=None: findings)


class TestTheDailyCap:
    def test_one_ordinary_finding_gets_through(self, monkeypatch):
        only(monkeypatch, [finding()])
        s = Sender()
        assert len(pro.run_once(s, at(12))) == 1
        assert len(s.sent) == 1

    def test_a_second_ordinary_finding_waits(self, monkeypatch):
        """Two things wrong on one day is one text, not two."""
        only(monkeypatch, [finding(key="a"), finding(key="b")])
        s = Sender()
        pro.run_once(s, at(12))
        assert len(s.sent) == 1, "the cap is what keeps this channel readable"

    def test_tomorrow_the_budget_resets(self, monkeypatch):
        only(monkeypatch, [finding(key="a")])
        s = Sender()
        pro.run_once(s, at(12))
        only(monkeypatch, [finding(key="b")])
        pro.run_once(s, at(12, "2026-09-09"))
        assert len(s.sent) == 2


class TestCriticalsBypassEverything:
    def test_a_critical_ignores_the_daily_cap(self, monkeypatch):
        """The cap is only safe because genuinely broken things escape it."""
        only(monkeypatch, [finding(key="a")])
        s = Sender()
        pro.run_once(s, at(12))
        only(monkeypatch, [finding(key="down", severity=CRITICAL)])
        pro.run_once(s, at(12))
        assert len(s.sent) == 2

    def test_a_critical_ignores_quiet_hours(self, monkeypatch):
        only(monkeypatch, [finding(key="down", severity=CRITICAL)])
        s = Sender()
        assert pro.run_once(s, at(3))
        assert len(s.sent) == 1

    def test_many_criticals_all_send(self, monkeypatch):
        only(monkeypatch, [finding(key=f"c{i}", severity=CRITICAL) for i in range(4)])
        s = Sender()
        pro.run_once(s, at(12))
        assert len(s.sent) == 4


class TestQuietHours:
    @pytest.mark.parametrize("hour", [22, 23, 0, 3, 6])
    def test_nothing_ordinary_overnight(self, monkeypatch, hour):
        only(monkeypatch, [finding()])
        s = Sender()
        pro.run_once(s, at(hour))
        assert s.sent == []

    @pytest.mark.parametrize("hour", [7, 12, 21])
    def test_daytime_is_fine(self, monkeypatch, hour):
        only(monkeypatch, [finding()])
        s = Sender()
        pro.run_once(s, at(hour))
        assert len(s.sent) == 1


class TestRepeatSuppression:
    def test_the_same_finding_does_not_repeat_within_a_day(self, monkeypatch):
        only(monkeypatch, [finding(key="same")])
        s = Sender()
        pro.run_once(s, at(9))
        pro.run_once(s, at(11))
        pro.run_once(s, at(15))
        assert len(s.sent) == 1, "a standing problem must not nag hourly"

    def test_it_does_repeat_the_next_day(self, monkeypatch):
        only(monkeypatch, [finding(key="same")])
        s = Sender()
        pro.run_once(s, at(12))
        pro.run_once(s, at(12, "2026-09-09"))
        assert len(s.sent) == 2, "an unfixed problem should resurface"

    def test_suppression_applies_to_criticals_too(self, monkeypatch):
        """Bypassing the cap is not licence to repeat every five minutes."""
        only(monkeypatch, [finding(key="down", severity=CRITICAL)])
        s = Sender()
        pro.run_once(s, at(12))
        pro.run_once(s, at(13))
        assert len(s.sent) == 1


class TestFailedSends:
    def test_a_failed_send_is_not_recorded(self, monkeypatch):
        """Otherwise the finding is suppressed for a day having never arrived."""
        only(monkeypatch, [finding(key="a")])
        failing = Sender(succeed=False)
        assert pro.run_once(failing, at(12)) == []

        working = Sender()
        pro.run_once(working, at(13))
        assert len(working.sent) == 1, "it must be retried, not swallowed"

    def test_a_raising_sender_does_not_crash_the_poller(self, monkeypatch):
        only(monkeypatch, [finding()])

        def boom(_):
            raise RuntimeError("AppleScript timed out")
        assert pro.run_once(boom, at(12)) == []


class TestSuggestionsAreNeverTexted:
    def test_a_suggestion_does_not_alert(self, monkeypatch):
        only(monkeypatch, [finding(key="idea", severity=SUGGESTION)])
        s = Sender()
        pro.run_once(s, at(12))
        assert s.sent == [], "suggestions wait for the weekly digest"

    def test_a_suggestion_does_not_consume_the_daily_budget(self, monkeypatch):
        only(monkeypatch, [finding(key="idea", severity=SUGGESTION), finding(key="real")])
        s = Sender()
        pro.run_once(s, at(12))
        assert len(s.sent) == 1
        assert "Something" in s.sent[0]


class TestChecksAreRobust:
    def test_a_failing_check_does_not_silence_the_others(self, monkeypatch):
        """One broken check must not take the whole channel down."""
        def boom():
            raise RuntimeError("database is locked")
        monkeypatch.setattr(pro, "_CHECKS", [boom, lambda: [finding(key="ok")]])
        assert [f.key for f in pro.collect()] == ["ok"]

    def test_criticals_are_ordered_first(self, monkeypatch):
        monkeypatch.setattr(pro, "_CHECKS", [
            lambda: [finding(key="n", severity=NORMAL)],
            lambda: [finding(key="c", severity=CRITICAL)],
        ])
        assert [f.key for f in pro.collect()] == ["c", "n"]

    def test_no_check_duplicates_the_watchdog(self):
        """agent_watchdog owns stale-agent alerts. One fault, one text."""
        names = [f.__name__ for f in pro._CHECKS]
        assert not [n for n in names if "stale" in n or "agent" in n]

    def test_every_registered_check_runs_without_error(self):
        """Against the real repo, not fixtures — a check that throws is useless."""
        for fn in pro._CHECKS:
            fn()


class TestTheDigest:
    def test_it_goes_out_sunday_evening(self, monkeypatch):
        monkeypatch.setattr(pro, "pending_suggestions", lambda now=None: [])
        s = Sender()
        assert pro.maybe_send_digest(s, at(18, "2026-09-06"))  # a Sunday
        assert "week in review" in s.sent[0]

    def test_not_on_a_tuesday(self, monkeypatch):
        monkeypatch.setattr(pro, "pending_suggestions", lambda now=None: [])
        s = Sender()
        assert not pro.maybe_send_digest(s, at(18, "2026-09-08"))
        assert s.sent == []

    def test_only_once_a_week(self, monkeypatch):
        monkeypatch.setattr(pro, "pending_suggestions", lambda now=None: [])
        s = Sender()
        pro.maybe_send_digest(s, at(18, "2026-09-06"))
        pro.maybe_send_digest(s, at(20, "2026-09-06"))
        assert len(s.sent) == 1

    def test_a_failed_digest_is_retried_not_skipped(self, monkeypatch):
        monkeypatch.setattr(pro, "pending_suggestions", lambda now=None: [])
        failing = Sender(succeed=False)
        pro.maybe_send_digest(failing, at(18, "2026-09-06"))
        working = Sender()
        assert pro.maybe_send_digest(working, at(19, "2026-09-06"))

    def test_it_carries_suggestions(self, monkeypatch):
        monkeypatch.setattr(pro, "pending_suggestions",
                            lambda now=None: [finding(key="i", severity=SUGGESTION,
                                                      title="Roster is thin", fix="vet handles")])
        s = Sender()
        pro.maybe_send_digest(s, at(18, "2026-09-06"))
        assert "Roster is thin" in s.sent[0]
        assert "vet handles" in s.sent[0]

    def test_a_quiet_week_still_says_so(self, monkeypatch):
        monkeypatch.setattr(pro, "pending_suggestions", lambda now=None: [])
        s = Sender()
        pro.maybe_send_digest(s, at(18, "2026-09-06"))
        assert "Nothing outstanding" in s.sent[0]


class TestRendering:
    def test_a_finding_reads_as_a_text_message(self):
        body = finding(title="⚠️ 3 picks missing", detail="They never reached the sheet.",
                       fix="run the repair script").render()
        assert body.splitlines()[0] == "⚠️ 3 picks missing"
        assert "Fix: run the repair script" in body

    def test_no_fix_line_when_there_is_no_fix(self):
        assert "Fix:" not in finding(fix="").render()
