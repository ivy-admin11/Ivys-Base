"""Gateway monitor: what it alerts on, and what it deliberately does not.

The monitor is the thing that texts Henry at 3am, so the debounce behaviour
matters as much as the detection. No HTTP request and no iMessage is ever
made here — check_gateway and send_imessage are both stubbed.
"""

import json

import pytest

import scripts.monitor_gateway as mon


@pytest.fixture
def run_monitor(monkeypatch, tmp_path):
    """Drive one monitor run with a scripted sequence of gateway verdicts."""

    def _run(statuses, prior_state=None):
        monkeypatch.setattr(mon, "STATE_PATH", str(tmp_path / "state.json"))
        monkeypatch.setattr(mon, "DOWN_RECHECK_DELAY_SECONDS", 0)
        if prior_state is not None:
            (tmp_path / "state.json").write_text(json.dumps(prior_state))
        remaining = list(statuses)
        monkeypatch.setattr(mon, "check_gateway", lambda: remaining.pop(0))
        alerts = []
        monkeypatch.setattr(mon, "send_imessage", lambda phone, text: alerts.append(text) or True)
        assert mon.main() == 0
        return alerts, json.loads((tmp_path / "state.json").read_text())

    return _run


UP = ("up", "/health and /ready both passing")
DOWN = ("down", "/health unreachable")
DEGRADED = ("degraded", "imessage_poller_healthy")


def test_first_run_establishes_a_baseline_without_alerting(run_monitor):
    alerts, state = run_monitor([UP], prior_state=None)
    assert alerts == []
    assert state["status"] == "up"


def test_a_single_failed_probe_is_rechecked_and_not_alerted(run_monitor):
    """The gateway exits on purpose when chat.db access is revoked and launchd
    relaunches it seconds later. That restart is not an outage."""
    alerts, state = run_monitor([DOWN, UP], prior_state={"status": "up"})
    assert alerts == []
    assert state["status"] == "up"


def test_a_sustained_outage_alerts_once(run_monitor):
    alerts, state = run_monitor([DOWN, DOWN], prior_state={"status": "up"})
    assert len(alerts) == 1
    assert "DOWN" in alerts[0]
    assert state["status"] == "down"


def test_degraded_needs_two_sightings_then_recovers(run_monitor):
    """Right after the Mac wakes, the poller heartbeat is briefly stale and
    /ready reports it unhealthy for exactly one cycle."""
    alerts, state = run_monitor([DEGRADED], prior_state={"status": "up"})
    assert alerts == []
    assert state["status"] == "up"
    assert state["degraded_streak"] == 1

    alerts, state = run_monitor([DEGRADED], prior_state=state)
    assert len(alerts) == 1
    assert "NOT READY" in alerts[0]
    assert "imessage_poller_healthy" in alerts[0]
    assert state["status"] == "degraded"

    alerts, state = run_monitor([UP], prior_state=state)
    assert len(alerts) == 1
    assert "back UP" in alerts[0]
    assert state["degraded_streak"] == 0


def test_a_one_cycle_degraded_blip_never_alerts(run_monitor):
    alerts, state = run_monitor([DEGRADED], prior_state={"status": "up"})
    assert alerts == []
    alerts, state = run_monitor([UP], prior_state=state)
    assert alerts == [], "a blip that resolved should never produce a text"
    assert state["degraded_streak"] == 0


def test_failed_checks_are_named_in_the_alert():
    body = {"detail": {"ready": False, "checks": {
        "chat_db_readable": True,
        "imessage_poller_healthy": False,
        "receipts_db_writable": False,
    }}}
    assert mon._failed_checks(body) == ["imessage_poller_healthy", "receipts_db_writable"]
    assert mon._failed_checks({"checks": {"a": True}}) == []
    assert mon._failed_checks(None) == []


def test_a_failed_alert_send_does_not_update_the_alert_timestamp(monkeypatch, tmp_path):
    """If the text didn't go out, the hourly re-alert must still fire."""
    monkeypatch.setattr(mon, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(mon, "DOWN_RECHECK_DELAY_SECONDS", 0)
    (tmp_path / "state.json").write_text(json.dumps({"status": "up"}))
    monkeypatch.setattr(mon, "check_gateway", lambda: DOWN)
    monkeypatch.setattr(mon, "send_imessage", lambda phone, text: False)

    assert mon.main() == 0
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["status"] == "down"
    assert "last_alert_ts" not in state
