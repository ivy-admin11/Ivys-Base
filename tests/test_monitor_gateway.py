"""Tests for the monitor that is supposed to notice when the gateway dies.

monitor_gateway.py had no tests at all. It is the last line of defence: when
it is wrong an outage is not merely unfixed, it is *unreported* -- the failure
mode that produced a month of silence (2026-07-19 to 2026-08-21) and the
three-day one where /health kept answering 200 while the iMessage poller was
dead (2026-08-24). Two live bugs were found while writing these and are
pinned below.

Nothing here touches the network, Messages.app, or the real
logs/gateway_monitor_state.json.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import monitor_gateway as mg  # noqa: E402

# The request never got an answer -- connection refused, timeout, DNS. This is
# the only failure the prober is allowed to retry.
TRANSPORT_FAILURE = object()
# A response whose body is not JSON at all (an nginx error page, say).
NO_JSON_BODY = object()

READY_OK = (200, {"checks": {"imessage_poller": True, "chat_db": True}})


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is NO_JSON_BODY:
            raise ValueError("Expecting value")
        return self._body


class FakeGateway:
    """Stands in for requests.get.

    Each endpoint holds a queue of answers and the last one repeats, so a
    steady state needs a single entry while a flaky one can be scripted probe
    by probe. An entry is a status code, a (code, body) pair, or
    TRANSPORT_FAILURE.
    """

    def __init__(self):
        self.health = [200]
        self.ready = [READY_OK]
        self.calls = []  # (url, headers, timeout)

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers, timeout))
        queue = self.health if url == mg.GATEWAY_HEALTH_URL else self.ready
        spec = queue.pop(0) if len(queue) > 1 else queue[0]
        if spec is TRANSPORT_FAILURE:
            raise requests.exceptions.ConnectionError("connection refused")
        code, body = spec if isinstance(spec, tuple) else (spec, {})
        return FakeResponse(code, body)

    def hits(self, url):
        return [c[0] for c in self.calls].count(url)

    # -- whole-gateway states -------------------------------------------
    def be_up(self):
        self.health, self.ready = [200], [READY_OK]

    def be_down(self):
        self.health, self.ready = [TRANSPORT_FAILURE], [READY_OK]

    def be_degraded(self, failing="imessage_poller"):
        self.health = [200]
        self.ready = [(503, {"detail": {"checks": {failing: False, "chat_db": True}}})]


class FakeSender:
    """send_imessage, which returns False when the text did not go out."""

    def __init__(self):
        self.sent = []
        self.ok = True

    def __call__(self, phone, text):
        self.sent.append((phone, text))
        return self.ok

    @property
    def texts(self):
        return [t for _, t in self.sent]


@pytest.fixture
def gw(tmp_path, monkeypatch):
    fake = FakeGateway()
    monkeypatch.setattr(mg.requests, "get", fake.get)
    # A directory that does not exist yet, so every run exercises save_state's
    # mkdir as well -- and so no test can reach the real state file.
    monkeypatch.setattr(mg, "STATE_PATH", str(tmp_path / "state" / "gateway_monitor_state.json"))
    monkeypatch.setattr(mg, "HEALTH_CHECK_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(mg, "DOWN_RECHECK_DELAY_SECONDS", 0)
    return fake


@pytest.fixture
def sms(monkeypatch):
    sender = FakeSender()
    monkeypatch.setattr(mg, "send_imessage", sender)
    return sender


def read_state() -> dict:
    return json.loads(Path(mg.STATE_PATH).read_text())


def write_state(**fields) -> None:
    p = Path(mg.STATE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(fields))


def recent() -> float:
    """An alert sent a minute ago -- inside the re-alert interval."""
    return time.time() - 60


def stale() -> float:
    """An alert sent longer ago than the re-alert interval."""
    return time.time() - mg.REALERT_INTERVAL_SECONDS - 60


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

class TestClassification:
    def test_both_endpoints_passing_is_up(self, gw):
        assert mg.check_gateway()[0] == "up"

    def test_health_passing_but_ready_failing_is_degraded_not_up(self, gw):
        """The 2026-08-24 outage: the process was alive and /health said 200
        while the poller was dead, so watching liveness alone stayed silent
        for three days."""
        gw.be_degraded()
        status, reason = mg.check_gateway()
        assert status == "degraded"
        assert "imessage_poller" in reason

    def test_degraded_reason_names_only_the_failing_checks(self, gw):
        gw.ready = [(503, {"detail": {"checks": {"chat_db": False, "poller": False, "db": True}}})]
        assert mg.check_gateway() == ("degraded", "chat_db, poller")

    def test_checks_at_the_top_level_are_read_too(self, gw):
        # A 200 body carries checks at the top level; a 503 nests them under
        # FastAPI's "detail". Both shapes must be understood.
        gw.ready = [(503, {"checks": {"chat_db": False}})]
        assert mg.check_gateway() == ("degraded", "chat_db")

    @pytest.mark.parametrize("body", [{}, None, NO_JSON_BODY, {"detail": "nope"}, {"checks": "nope"}])
    def test_a_ready_failure_without_a_usable_body_still_reports_degraded(self, gw, body):
        gw.ready = [(503, body)]
        status, reason = mg.check_gateway()
        assert status == "degraded"
        assert "503" in reason

    def test_ready_unreachable_while_health_passes_is_degraded_not_down(self, gw):
        gw.ready = [TRANSPORT_FAILURE]
        assert mg.check_gateway() == ("degraded", "/ready unreachable while /health passes")

    def test_health_unreachable_is_down(self, gw):
        gw.be_down()
        assert mg.check_gateway() == ("down", "/health unreachable")

    @pytest.mark.parametrize("code", [500, 503, 401, 404])
    def test_health_answering_anything_but_200_is_down(self, gw, code):
        gw.health = [code]
        status, reason = mg.check_gateway()
        assert status == "down"
        assert str(code) in reason

    def test_ready_is_not_probed_when_health_already_failed(self, gw):
        gw.be_down()
        mg.check_gateway()
        assert gw.hits(mg.GATEWAY_READY_URL) == 0

    def test_the_boolean_shim_treats_degraded_as_not_up(self, gw):
        gw.be_up()
        assert mg.check_gateway_up() is True
        gw.be_degraded()
        assert mg.check_gateway_up() is False
        gw.be_down()
        assert mg.check_gateway_up() is False


# ---------------------------------------------------------------------------
# Retries -- transport failures only
# ---------------------------------------------------------------------------

class TestRetries:
    def test_a_transport_failure_is_retried_the_configured_number_of_times(self, gw):
        gw.be_down()
        mg.check_gateway()
        assert gw.hits(mg.GATEWAY_HEALTH_URL) == mg.HEALTH_CHECK_ATTEMPTS

    def test_one_flaky_request_does_not_report_the_gateway_down(self, gw):
        """Regression (2026-08-22): a single dropped connection texted Henry
        'DOWN' while the gateway process never stopped."""
        gw.health = [TRANSPORT_FAILURE, 200]
        assert mg.check_gateway()[0] == "up"

    def test_a_recovery_on_the_last_attempt_still_counts(self, gw):
        gw.health = [TRANSPORT_FAILURE] * (mg.HEALTH_CHECK_ATTEMPTS - 1) + [200]
        assert mg.check_gateway()[0] == "up"

    @pytest.mark.parametrize("code", [503, 500, 401])
    def test_an_http_answer_is_definitive_and_is_never_retried(self, gw, code):
        """A status code is the server talking. Retrying it would triple the
        load on an already-struggling gateway and delay the alert."""
        gw.health = [code]
        mg.check_gateway()
        assert gw.hits(mg.GATEWAY_HEALTH_URL) == 1

    def test_ready_503_is_not_retried_either(self, gw):
        gw.be_degraded()
        mg.check_gateway()
        assert gw.hits(mg.GATEWAY_READY_URL) == 1

    def test_ready_transport_failures_are_retried(self, gw):
        gw.ready = [TRANSPORT_FAILURE]
        mg.check_gateway()
        assert gw.hits(mg.GATEWAY_READY_URL) == mg.HEALTH_CHECK_ATTEMPTS

    def test_every_probe_carries_the_admin_key_and_a_timeout(self, gw):
        """Without the key /ready answers 401 and the monitor would read a
        healthy gateway as degraded forever."""
        mg.check_gateway()
        assert gw.calls
        for _url, headers, timeout in gw.calls:
            assert headers["X-API-Key"] == mg.ADMIN_SECRET
            assert timeout == mg.REQUEST_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# State: first run, persistence, corruption
# ---------------------------------------------------------------------------

class TestState:
    def test_the_first_run_only_establishes_a_baseline(self, gw, sms):
        gw.be_down()
        assert mg.main() == 0
        assert sms.sent == []
        assert read_state()["status"] == "down"

    def test_the_second_run_can_then_alert(self, gw, sms):
        gw.be_down()
        mg.main()
        gw.be_up()
        mg.main()
        assert any("back UP" in t for t in sms.texts)

    def test_state_round_trips_through_the_file(self, gw, sms):
        gw.be_up()
        mg.main()
        state = read_state()
        assert state["status"] == "up"
        assert state["degraded_streak"] == 0
        assert "reason" in state

    def test_a_corrupt_state_file_is_not_fatal(self, gw, sms):
        p = Path(mg.STATE_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"status": "do')  # a write interrupted mid-flush
        gw.be_down()
        assert mg.main() == 0
        assert read_state()["status"] == "down"

    def test_state_json_that_is_not_an_object_is_not_fatal(self, gw, sms):
        """BUG (fixed): load_state() promised a dict but handed back whatever
        JSON it found. A file holding `null` or `[]` -- a truncated write, a
        hand-edit, an older format -- made main() die with AttributeError on
        state.get(). A crashed monitor is a silent monitor, which is the exact
        failure it exists to prevent. Verified: reverting the isinstance guard
        makes this test fail with AttributeError."""
        for raw in ["null", "[]", '"up"', "3"]:
            Path(mg.STATE_PATH).parent.mkdir(parents=True, exist_ok=True)
            Path(mg.STATE_PATH).write_text(raw)
            gw.be_down()
            assert mg.main() == 0, raw
            assert read_state()["status"] == "down"

    def test_load_state_always_returns_a_dict(self, gw):
        assert mg.load_state() == {}  # no file at all
        Path(mg.STATE_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(mg.STATE_PATH).write_text("[1, 2]")
        assert mg.load_state() == {}

    def test_save_state_creates_the_directory(self, gw, sms):
        assert not Path(mg.STATE_PATH).parent.exists()
        mg.main()
        assert Path(mg.STATE_PATH).exists()

    def test_unrelated_state_keys_survive_a_run(self, gw, sms):
        write_state(status="up", last_alert_ts=recent(), note="kept")
        mg.main()
        assert read_state()["note"] == "kept"


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

class TestTransitions:
    def test_up_to_down_texts_henry(self, gw, sms):
        write_state(status="up", last_alert_ts=recent())
        gw.be_down()
        mg.main()
        assert len(sms.sent) == 1
        phone, text = sms.sent[0]
        assert phone == mg.HENRY_PHONE
        assert "DOWN" in text

    def test_down_to_up_reports_the_recovery(self, gw, sms):
        write_state(status="down", last_alert_ts=recent())
        gw.be_up()
        mg.main()
        assert "back UP" in sms.texts[0]

    def test_a_steady_gateway_is_silent(self, gw, sms):
        write_state(status="up", last_alert_ts=recent())
        gw.be_up()
        mg.main()
        mg.main()
        assert sms.sent == []

    def test_a_down_reading_is_rechecked_before_alerting(self, gw, sms):
        """The gateway deliberately exits and lets launchd relaunch it when
        chat.db access is lost; that gap is not an outage worth a text."""
        write_state(status="up", last_alert_ts=recent())
        gw.health = [TRANSPORT_FAILURE] * mg.HEALTH_CHECK_ATTEMPTS + [200]
        mg.main()
        assert sms.sent == []
        assert read_state()["status"] == "up"

    def test_a_down_that_survives_the_recheck_alerts(self, gw, sms):
        write_state(status="up", last_alert_ts=recent())
        gw.be_down()
        mg.main()
        assert "DOWN" in sms.texts[0]
        assert read_state()["status"] == "down"


# ---------------------------------------------------------------------------
# The degraded debounce
# ---------------------------------------------------------------------------

class TestDegradedDebounce:
    def test_a_first_degraded_sighting_is_not_alerted(self, gw, sms):
        write_state(status="up", last_alert_ts=recent())
        gw.be_degraded()
        mg.main()
        assert sms.sent == []

    def test_the_recorded_status_stays_up_while_awaiting_confirmation(self, gw, sms):
        """So the confirmed alert still reads as an up->degraded transition
        rather than a no-change."""
        write_state(status="up", last_alert_ts=recent())
        gw.be_degraded()
        mg.main()
        state = read_state()
        assert state["status"] == "up"
        assert state["degraded_streak"] == 1

    def test_a_second_consecutive_sighting_alerts(self, gw, sms):
        write_state(status="up", last_alert_ts=recent())
        gw.be_degraded()
        for _ in range(mg.DEGRADED_CONSECUTIVE_RUNS_BEFORE_ALERT):
            mg.main()
        assert len(sms.sent) == 1
        assert "NOT READY" in sms.texts[0]
        assert "imessage_poller" in sms.texts[0]
        assert read_state()["status"] == "degraded"

    def test_a_single_degraded_blip_never_alerts(self, gw, sms):
        """Right after wake-from-sleep the poller heartbeat is stale for one
        cycle and /ready reports it unhealthy."""
        write_state(status="up", last_alert_ts=recent())
        gw.be_degraded()
        mg.main()
        gw.be_up()
        mg.main()
        assert sms.sent == []
        assert read_state()["degraded_streak"] == 0

    def test_the_streak_resets_so_two_blips_apart_do_not_add_up(self, gw, sms):
        write_state(status="up", last_alert_ts=recent())
        for _ in range(3):
            gw.be_degraded()
            mg.main()
            gw.be_up()
            mg.main()
        assert sms.sent == []

    def test_down_is_never_debounced(self, gw, sms):
        """Degraded is ambiguous; a dead process is not."""
        write_state(status="up", last_alert_ts=recent())
        gw.be_down()
        mg.main()
        assert len(sms.sent) == 1


# ---------------------------------------------------------------------------
# Re-alerting while still broken
# ---------------------------------------------------------------------------

class TestRealert:
    def test_still_down_inside_the_interval_stays_quiet(self, gw, sms):
        write_state(status="down", last_alert_ts=recent())
        gw.be_down()
        mg.main()
        assert sms.sent == []

    def test_still_down_past_the_interval_alerts_again(self, gw, sms):
        write_state(status="down", last_alert_ts=stale())
        gw.be_down()
        mg.main()
        assert "STILL DOWN" in sms.texts[0]

    def test_still_degraded_past_the_interval_alerts_again(self, gw, sms):
        write_state(status="degraded", last_alert_ts=stale(), degraded_streak=2)
        gw.be_degraded()
        mg.main()
        assert "STILL DEGRADED" in sms.texts[0]

    def test_a_realert_restarts_the_clock(self, gw, sms):
        write_state(status="down", last_alert_ts=stale())
        gw.be_down()
        mg.main()
        assert read_state()["last_alert_ts"] > recent()
        mg.main()
        assert len(sms.sent) == 1  # the second run is inside the interval again

    def test_a_healthy_gateway_never_realerts(self, gw, sms):
        write_state(status="up", last_alert_ts=stale())
        gw.be_up()
        mg.main()
        assert sms.sent == []


# ---------------------------------------------------------------------------
# When the alert itself fails to send
# ---------------------------------------------------------------------------

class TestAlertSendFailure:
    def test_a_failed_alert_does_not_stamp_the_realert_clock(self, gw, sms):
        write_state(status="up", last_alert_ts=stale())
        gw.be_down()
        sms.ok = False
        mg.main()
        assert read_state().get("last_alert_ts", 0.0) < recent()

    def test_a_failed_recovery_alert_is_retried_on_the_next_run(self, gw, sms):
        """BUG (fixed): the status was advanced whether or not the text went
        out, so a dropped alert was recorded as if it had been delivered. The
        'back UP' text was the worst case -- the re-alert path only covers
        non-up statuses, so once the status read 'up' the recovery notice was
        gone for good and Henry was never told the gateway came back.
        Verified: reverting the alert_delivered guard makes this test fail
        (the second run sends nothing)."""
        write_state(status="down", last_alert_ts=recent())
        gw.be_up()
        sms.ok = False
        mg.main()
        assert "back UP" in sms.texts[0]
        assert read_state()["status"] == "down", "a lost alert must not be recorded as delivered"

        sms.ok = True
        mg.main()
        assert len(sms.sent) == 2
        assert "back UP" in sms.texts[1]
        assert read_state()["status"] == "up"

    def test_a_failed_down_alert_is_retried_on_the_next_run(self, gw, sms):
        """Same bug from the other direction. Because last_alert_ts was left
        pointing at some *earlier* successful alert, the re-alert path stayed
        quiet too -- an outage could go unreported for the best part of an
        hour after the first text was dropped."""
        write_state(status="up", last_alert_ts=recent())
        gw.be_down()
        sms.ok = False
        mg.main()
        assert "DOWN" in sms.texts[0]
        assert read_state()["status"] == "up"

        sms.ok = True
        mg.main()
        assert len(sms.sent) == 2
        assert "DOWN" in sms.texts[1]
        assert read_state()["status"] == "down"

    def test_a_failed_degraded_alert_is_retried_after_confirmation(self, gw, sms):
        write_state(status="up", last_alert_ts=recent())
        gw.be_degraded()
        sms.ok = False
        for _ in range(mg.DEGRADED_CONSECUTIVE_RUNS_BEFORE_ALERT):
            mg.main()
        assert len(sms.sent) == 1
        assert read_state()["status"] == "up"

        sms.ok = True
        mg.main()
        assert len(sms.sent) == 2
        assert "NOT READY" in sms.texts[1]
        assert read_state()["status"] == "degraded"

    def test_a_delivered_alert_does_advance_the_status(self, gw, sms):
        write_state(status="up", last_alert_ts=recent())
        gw.be_down()
        mg.main()
        assert read_state()["status"] == "down"
        mg.main()
        assert len(sms.sent) == 1  # no duplicate for the same transition

    def test_a_send_that_returns_a_falsy_non_bool_counts_as_failure(self, gw, sms, monkeypatch):
        monkeypatch.setattr(mg, "send_imessage", lambda phone, text: None)
        write_state(status="down", last_alert_ts=recent())
        gw.be_up()
        mg.main()
        assert read_state()["status"] == "down"

    def test_main_returns_zero_even_when_the_alert_fails(self, gw, sms):
        write_state(status="up", last_alert_ts=recent())
        gw.be_down()
        sms.ok = False
        assert mg.main() == 0
