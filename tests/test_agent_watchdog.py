"""The watchdog that would have caught six days of silence.

On 2026-09-05 Happy Hour had not run since 30 August and the meal planner
since the same day. Nothing errored — launchd simply stopped starting them —
so there was no log line, no failed report, and no alert. The only symptom
was an absence. These tests pin the behaviour that turns an absence into a
message.
"""

from datetime import datetime, timedelta, timezone

# Happy Hour runs Weekday 0 (Sundays), so six days of silence is the
# schedule working normally -- that misreading is what produced a false
# 'Happy Hour has stopped running' finding. Nine days is the first span
# that means a run was genuinely missed.

import pytest

from ivy_core import agent_watchdog as wd


@pytest.fixture
def clock():
    return datetime(2026, 9, 5, 17, 0, tzinfo=timezone.utc)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "STATE_PATH", tmp_path / "watchdog_state.json")
    monkeypatch.setattr(wd, "LOG_FILES", {})          # outbox is the only source
    return tmp_path


def _seen(monkeypatch, mapping):
    monkeypatch.setattr(wd, "last_activity", lambda job: mapping.get(job))


class TestStaleness:
    def test_a_job_silent_past_its_cadence_is_flagged(self, monkeypatch, clock):
        _seen(monkeypatch, {"happy_hour": clock - timedelta(days=9)})
        stale = wd.stale_agents(clock)
        assert [s["job"] for s in stale] == ["happy_hour"]
        assert stale[0]["silent_hours"] == pytest.approx(216.0)

    def test_a_job_that_ran_recently_is_not_flagged(self, monkeypatch, clock):
        _seen(monkeypatch, {"sharp_picks": clock - timedelta(hours=6)})
        assert wd.stale_agents(clock) == []

    def test_a_quiet_slate_is_not_mistaken_for_a_dead_job(self, monkeypatch, clock):
        """Sharp Picks runs three times a day and often reports nothing
        bettable. Twelve hours of no *qualifying* picks must not alarm."""
        _seen(monkeypatch, {"sharp_picks": clock - timedelta(hours=12)})
        assert wd.stale_agents(clock) == []

    def test_the_daily_meal_planner_tolerates_one_missed_run_not_a_week(
        self, monkeypatch, clock
    ):
        """It ran weekly until 2026-09-09 and carried a 10-day leash to match.
        Daily now, so the leash came down with it: one skipped morning is
        forgivable, a second means nobody is cooking from it."""
        _seen(monkeypatch, {"familia_meal_planner": clock - timedelta(hours=30)})
        assert wd.stale_agents(clock) == [], "one missed run must not cry wolf"
        _seen(monkeypatch, {"familia_meal_planner": clock - timedelta(hours=60)})
        assert [s["job"] for s in wd.stale_agents(clock)] == ["familia_meal_planner"]

    def test_a_job_with_no_history_is_not_reported(self, monkeypatch, clock):
        """Never-run and never-installed are indistinguishable from here;
        claiming a job 'stopped' when it never started would be a lie."""
        _seen(monkeypatch, {})
        assert wd.stale_agents(clock) == []

    def test_the_worst_offender_is_listed_first(self, monkeypatch, clock):
        _seen(monkeypatch, {
            "happy_hour": clock - timedelta(days=9),
            "familia_meal_planner": clock - timedelta(days=20),
        })
        assert [s["job"] for s in wd.stale_agents(clock)][0] == "familia_meal_planner"


class TestAlerting:
    def test_the_alert_names_the_job_and_the_fix(self, monkeypatch, isolated, clock):
        _seen(monkeypatch, {"happy_hour": clock - timedelta(days=9)})
        sent = []
        wd.check_once(lambda body: sent.append(body) or True, clock)
        assert len(sent) == 1
        assert "Happy Hour Scout" in sent[0]
        assert "9 days" in sent[0]
        assert "launchctl list | grep com.ivy" in sent[0]

    def test_it_does_not_nag_on_every_poll(self, monkeypatch, isolated, clock):
        _seen(monkeypatch, {"happy_hour": clock - timedelta(days=9)})
        sent = []

        def send(body):
            sent.append(body)
            return True

        wd.check_once(send, clock)
        wd.check_once(send, clock + timedelta(hours=1))
        wd.check_once(send, clock + timedelta(hours=6))
        assert len(sent) == 1, "a job that stays down must not alert hourly"

    def test_it_alerts_again_the_next_day(self, monkeypatch, isolated, clock):
        _seen(monkeypatch, {"happy_hour": clock - timedelta(days=9)})
        sent = []

        def send(body):
            sent.append(body)
            return True

        wd.check_once(send, clock)
        wd.check_once(send, clock + timedelta(hours=25))
        assert len(sent) == 2, "a still-dead job should reappear daily"

    def test_a_failed_send_is_not_recorded_as_alerted(self, monkeypatch, isolated, clock):
        """Otherwise an undeliverable alert silences itself for a day — the
        exact failure mode this whole watchdog exists to prevent."""
        _seen(monkeypatch, {"happy_hour": clock - timedelta(days=9)})
        assert wd.check_once(lambda body: False, clock) == []
        sent = []
        assert len(wd.check_once(lambda b: sent.append(b) or True, clock)) == 1

    def test_nothing_is_sent_when_everything_is_healthy(self, monkeypatch, isolated, clock):
        _seen(monkeypatch, {"sharp_picks": clock - timedelta(hours=2)})
        sent = []
        assert wd.check_once(lambda b: sent.append(b) or True, clock) == []
        assert sent == []


def test_last_activity_prefers_the_most_recent_evidence(tmp_path, monkeypatch):
    """A job that ran but produced no report still writes a log, and that
    counts as alive — otherwise a quiet week reads as a dead agent."""
    monkeypatch.setattr(wd, "PROJECT_ROOT", tmp_path)
    log = tmp_path / "logs" / "happy_hour_scheduled.log"
    log.parent.mkdir(parents=True)
    log.write_text("ran, found nothing")
    monkeypatch.setattr(wd, "LOG_FILES", {"happy_hour": "logs/happy_hour_scheduled.log"})
    monkeypatch.setattr(wd, "_outbox_latest", lambda job: None)

    seen = wd.last_activity("happy_hour")
    assert seen is not None
    assert (datetime.now(timezone.utc) - seen).total_seconds() < 60
