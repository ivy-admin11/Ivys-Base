"""Attachment delivery verification, conversation memory, non-blocking
health, and gateway-monitor debounce. Nothing here touches Messages.app,
chat.db, the network, or a real subprocess."""

import os
import sqlite3
import threading
import time
from unittest.mock import patch

import pytest

from ivy_core import attachment_verify, messaging


# ---------------------------------------------------------------------------
# attachment_verify
# ---------------------------------------------------------------------------

def test_classify_delivered_failed_pending():
    assert attachment_verify.classify({"error": 0, "is_sent": 1, "transfer_state": 5}) == "delivered"
    assert attachment_verify.classify({"error": 25, "is_sent": 1, "transfer_state": 5}) == "failed"
    assert attachment_verify.classify({"error": 0, "is_sent": 0, "transfer_state": 5}) == "pending"
    assert attachment_verify.classify({"error": 0, "is_sent": 1, "transfer_state": 0}) == "pending"


def test_apple_epoch_round_trip():
    now = time.time()
    assert abs(attachment_verify.from_apple_ns(attachment_verify.to_apple_ns(now)) - now) < 1e-3


def test_screen_lock_detection_parses_ioreg_plist():
    unlocked = "<key>IOConsoleUsers</key><array><dict><key>kCGSSessionOnConsoleKey</key><true/></dict></array>"
    locked = ("<key>IOConsoleUsers</key><array><dict><key>CGSSessionScreenIsLocked</key>\n\t\t\t<true/>"
              "<key>kCGSSessionOnConsoleKey</key><true/></dict></array>")

    class R:
        def __init__(self, out):
            self.stdout = out

    with patch.object(attachment_verify.subprocess, "run", return_value=R(unlocked)):
        assert attachment_verify.screen_is_locked() is False
    with patch.object(attachment_verify.subprocess, "run", return_value=R(locked)):
        assert attachment_verify.screen_is_locked() is True
    with patch.object(attachment_verify.subprocess, "run", return_value=R("")):
        assert attachment_verify.screen_is_locked() is None


def _fake_chat_db(path, rows):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, date INTEGER, is_from_me INTEGER,
            is_sent INTEGER, is_delivered INTEGER, error INTEGER, handle_id INTEGER);
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, transfer_name TEXT,
            transfer_state INTEGER, total_bytes INTEGER);
        CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
        INSERT INTO handle VALUES (1, '+15555550100'), (2, '+15555550101');
        """
    )
    for i, r in enumerate(rows, start=1):
        conn.execute(
            "INSERT INTO message VALUES (?,?,?,?,?,?,?)",
            (i, attachment_verify.to_apple_ns(r["ts"]), 1, r.get("is_sent", 1), 0, r.get("error", 0), r["handle"]),
        )
        conn.execute("INSERT INTO attachment VALUES (?,?,?,?)", (i, r["name"], r.get("transfer_state", 5), 1234))
        conn.execute("INSERT INTO message_attachment_join VALUES (?,?)", (i, i))
    conn.commit()
    conn.close()


def test_fetch_outgoing_attachments_filters_by_name_time_and_handle(tmp_path):
    db = tmp_path / "chat.db"
    now = time.time()
    _fake_chat_db(db, [
        {"ts": now - 3600, "name": "old.pdf", "handle": 1},
        {"ts": now - 5, "name": "picks.pdf", "handle": 1, "transfer_state": 5},
        {"ts": now - 4, "name": "picks.pdf", "handle": 2, "error": 25},
    ])
    rows = attachment_verify.fetch_outgoing_attachments(
        since_ts=now - 60, filename="picks.pdf", handle="(555) 555-0100", db_path=str(db)
    )
    assert len(rows) == 1
    assert rows[0]["handle"] == "+15555550100"
    assert rows[0]["state"] == "delivered"

    rows = attachment_verify.fetch_outgoing_attachments(since_ts=now - 60, db_path=str(db))
    assert [r["state"] for r in rows] == ["failed", "delivered"]

    assert attachment_verify.fetch_outgoing_attachments(since_ts=now - 60, filename="old.pdf", db_path=str(db)) == []


def test_wait_for_outcome_falls_back_to_gateway_when_local_read_denied(monkeypatch):
    def denied(**kwargs):
        raise sqlite3.DatabaseError("authorization denied")

    monkeypatch.setattr(attachment_verify, "fetch_outgoing_attachments", denied)

    class Resp:
        status_code = 200

        def json(self):
            return {"attachments": [{"error": 0, "is_sent": 1, "transfer_state": 5, "transfer_name": "x.pdf"}]}

    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Resp()

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    outcome, details = attachment_verify.wait_for_attachment_outcome("x.pdf", time.time(), timeout=1, interval=0.01)
    assert outcome == "delivered"
    assert details["source"] == "gateway"
    assert calls[0][0].endswith("/imessage/attachments")
    assert calls[0][1]["headers"]["X-API-Key"] == os.environ["ADMIN_SECRET"]


def test_wait_for_outcome_unknown_when_nothing_can_read_chat_db(monkeypatch):
    def denied(**kwargs):
        raise sqlite3.DatabaseError("authorization denied")

    monkeypatch.setattr(attachment_verify, "fetch_outgoing_attachments", denied)
    import requests

    def down(*a, **k):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(requests, "get", down)
    outcome, _ = attachment_verify.wait_for_attachment_outcome("x.pdf", time.time(), timeout=0.5, interval=0.01)
    assert outcome == "unknown"


def test_wait_for_outcome_missing_when_no_row_ever_appears(monkeypatch):
    monkeypatch.setattr(attachment_verify, "fetch_outgoing_attachments", lambda **k: [])
    outcome, _ = attachment_verify.wait_for_attachment_outcome("x.pdf", time.time(), timeout=0.05, interval=0.01)
    assert outcome == "missing"


# ---------------------------------------------------------------------------
# messaging.send_imessage_attachment — method chain driven by verification
# ---------------------------------------------------------------------------

@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "report.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    return str(p)


@pytest.fixture
def no_staging(monkeypatch, tmp_path):
    monkeypatch.setattr(messaging, "_IMSG_ATTACH_STAGE", str(tmp_path / "stage"))


def _script(outcomes):
    """Fake runner whose methods all return SUCCESS, recording the order."""
    calls = []

    class Runner:
        def send_imessage_file_argv(self, phone, path):
            calls.append("paste")
            return "SUCCESS"

        def send_imessage_file_scripting_argv(self, phone, path):
            calls.append("scripting")
            return "SUCCESS"

    return Runner(), calls


def test_scripting_is_tried_first_and_verified_delivery_stops_there(monkeypatch, pdf, no_staging):
    runner, calls = _script(None)
    monkeypatch.setattr(messaging, "_runner", runner)
    monkeypatch.setattr(attachment_verify, "screen_is_locked", lambda: False)
    monkeypatch.setattr(
        attachment_verify, "wait_for_attachment_outcome",
        lambda *a, **k: ("delivered", {"source": "local", "row": {}}),
    )
    receipt = messaging.send_imessage_attachment("+15555550100", pdf, report_id="R1")
    assert receipt.status == "verified_delivered"
    assert receipt.attempts == 1
    assert calls == ["scripting"]


def test_failed_scripting_falls_back_to_paste_then_fails(monkeypatch, pdf, no_staging):
    runner, calls = _script(None)
    monkeypatch.setattr(messaging, "_runner", runner)
    monkeypatch.setattr(attachment_verify, "screen_is_locked", lambda: False)
    monkeypatch.setattr(
        attachment_verify, "wait_for_attachment_outcome",
        lambda *a, **k: ("failed", {"source": "local", "row": {"error": 25, "transfer_state": 0}}),
    )
    sent_texts = []
    monkeypatch.setattr(messaging, "send_imessage", lambda p, t: sent_texts.append(t) or True)
    receipt = messaging.send_imessage_attachment("+15555550100", pdf, report_id="R2")
    assert receipt.status == "failed"
    assert not receipt
    assert calls == ["scripting", "paste"]
    assert receipt.attempts == 2
    assert "error=25" in receipt.error_detail
    assert sent_texts == []  # the caller, not messaging, decides on the text fallback


def test_locked_screen_never_falls_back_to_keystroke_paste(monkeypatch, pdf, no_staging):
    runner, calls = _script(None)
    monkeypatch.setattr(messaging, "_runner", runner)
    monkeypatch.setattr(attachment_verify, "screen_is_locked", lambda: True)
    monkeypatch.setattr(
        attachment_verify, "wait_for_attachment_outcome",
        lambda *a, **k: ("missing", {"source": "gateway"}),
    )
    receipt = messaging.send_imessage_attachment("+15555550100", pdf, report_id="R3")
    assert calls == ["scripting"]  # keystrokes would land on the lock screen
    assert receipt.status == "failed"


def test_scripting_script_addresses_participant_of_account():
    from utils.applescript import SEND_FILE_SCRIPTING_ARGV_SCRIPT

    assert "participant recipientValue of targetAccount" in SEND_FILE_SCRIPTING_ARGV_SCRIPT
    assert "buddy" not in SEND_FILE_SCRIPTING_ARGV_SCRIPT


def test_unverifiable_send_is_not_retried(monkeypatch, pdf, no_staging):
    runner, calls = _script(None)
    monkeypatch.setattr(messaging, "_runner", runner)
    monkeypatch.setattr(attachment_verify, "screen_is_locked", lambda: None)
    monkeypatch.setattr(
        attachment_verify, "wait_for_attachment_outcome",
        lambda *a, **k: ("unknown", {"source": "unavailable"}),
    )
    receipt = messaging.send_imessage_attachment("+15555550100", pdf, report_id="R4")
    assert receipt.status == "submitted_unverified"
    assert bool(receipt) is True
    assert calls == ["scripting"]


def test_applescript_error_moves_to_next_method(monkeypatch, pdf, no_staging):
    calls = []

    class Runner:
        def send_imessage_file_argv(self, phone, path):
            calls.append("paste")
            return "SUCCESS"

        def send_imessage_file_scripting_argv(self, phone, path):
            calls.append("scripting")
            return "ERROR: AppleScript execution timed out."

    monkeypatch.setattr(messaging, "_runner", Runner())
    monkeypatch.setattr(attachment_verify, "screen_is_locked", lambda: False)
    monkeypatch.setattr(
        attachment_verify, "wait_for_attachment_outcome",
        lambda *a, **k: ("delivered", {"source": "local", "row": {}}),
    )
    receipt = messaging.send_imessage_attachment("+15555550100", pdf, report_id="R5")
    assert calls == ["scripting", "paste"]
    assert receipt.status == "verified_delivered"
    assert receipt.attempts == 2


def test_missing_file_fails_without_running_any_method(monkeypatch, tmp_path):
    runner, calls = _script(None)
    monkeypatch.setattr(messaging, "_runner", runner)
    receipt = messaging.send_imessage_attachment("+15555550100", str(tmp_path / "nope.pdf"))
    assert receipt.status == "failed"
    assert receipt.error_code == "FILE_MISSING_OR_EMPTY"
    assert calls == []


# ---------------------------------------------------------------------------
# main.py: conversation memory, reply sending, health, poller heartbeat
# ---------------------------------------------------------------------------

import main  # noqa: E402


def test_conversation_history_is_per_sender_capped_and_expiring(monkeypatch):
    main._CONVERSATIONS.clear()
    monkeypatch.setattr(main, "CONVERSATION_MAX_MESSAGES", 4)
    for i in range(5):
        main.remember_turn("+1", f"q{i}", f"a{i}")
    main.remember_turn("+2", "other", "reply")

    hist = main.conversation_history("+1")
    assert [t["content"] for t in hist] == ["q3", "a3", "q4", "a4"]
    assert [t["role"] for t in hist] == ["user", "assistant", "user", "assistant"]
    assert [t["content"] for t in main.conversation_history("+2")] == ["other", "reply"]

    monkeypatch.setattr(main, "CONVERSATION_TTL_SECONDS", 0)
    time.sleep(0.01)
    assert main.conversation_history("+1") == []


def test_unanswered_turn_is_still_remembered():
    main._CONVERSATIONS.clear()
    main.remember_turn("+1", "hello?", None)
    assert main.conversation_history("+1") == [{"role": "user", "content": "hello?"}]


def test_deepseek_payload_includes_history_before_current_message(monkeypatch):
    captured = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "Here is the recipe."}}]}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return Resp()

    monkeypatch.setattr(main.requests, "post", fake_post)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    history = [
        {"role": "user", "content": "same day ooni dough?"},
        {"role": "assistant", "content": "65% hydration... Want a full recipe?"},
    ]
    reply = main.execute_deepseek_call("Yes, I want the full recipe", "SYS", history=history)
    assert reply == "Here is the recipe."
    msgs = captured["json"]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[-1]["content"] == "Yes, I want the full recipe"
    assert msgs[1]["content"] == "same day ooni dough?"


def test_format_history_for_prompt():
    text = main.format_history_for_prompt([
        {"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
    ])
    assert text.splitlines()[1:] == ["User: hi", "Ivy: hello"]


def test_reply_send_uses_argv_runner_and_reports_failure(monkeypatch, caplog):
    seen = []

    class Runner:
        def send_imessage_argv(self, target, body):
            seen.append((target, body))
            return "ERROR: boom"

    monkeypatch.setattr(main, "_GATEWAY_APPLESCRIPT", Runner())
    body = 'Use "00" flour and \\ backslashes'
    result = main.run_local_applescript_send("+15555550100", body)
    assert result == "ERROR: boom"
    assert seen == [("+15555550100", body)]  # passed verbatim, never escaped into source
    assert any("NOT sent" in r.getMessage() for r in caplog.records)


def test_health_never_runs_a_provider_probe_on_the_request_thread(monkeypatch):
    request_thread = threading.get_ident()
    probe_threads = []

    def fake_probe(*, force=False):
        probe_threads.append(threading.get_ident())
        return {}

    monkeypatch.setattr(main, "probe_providers", fake_probe)
    monkeypatch.setattr(main, "_PROVIDER_PROBE_THREAD", None)
    with main._PROVIDER_PROBE_LOCK:
        main._PROVIDER_PROBE_CACHE.clear()

    status = main.cached_provider_status()
    assert set(status) == {"deepseek", "gemini"}
    assert all(p["status"] in ("pending", "unconfigured") for p in status.values())
    for _ in range(50):
        if probe_threads:
            break
        time.sleep(0.01)
    assert probe_threads and probe_threads[0] != request_thread


def test_cached_provider_status_returns_fresh_cache_without_probing(monkeypatch):
    with main._PROVIDER_PROBE_LOCK:
        main._PROVIDER_PROBE_CACHE.clear()
        main._PROVIDER_PROBE_CACHE.update({
            "deepseek": {"authenticated": True}, "gemini": {"authenticated": False},
            "_ts": time.monotonic(),
        })
    monkeypatch.setattr(main, "probe_providers", lambda **k: pytest.fail("probe must not run"))
    assert main.cached_provider_status()["deepseek"]["authenticated"] is True
    with main._PROVIDER_PROBE_LOCK:
        main._PROVIDER_PROBE_CACHE.clear()


def test_poller_heartbeat_uses_monotonic_clock(monkeypatch):
    monkeypatch.setitem(main.POLLER_STATE, "enabled", True)
    monkeypatch.setitem(main.POLLER_STATE, "running", True)
    monkeypatch.setitem(main.POLLER_STATE, "last_success_ts", time.monotonic())
    monkeypatch.setitem(main.POLLER_STATE, "started_at", time.monotonic())
    assert main.poller_healthy() is True
    monkeypatch.setitem(main.POLLER_STATE, "last_success_ts", time.monotonic() - main.POLLER_STALE_AFTER_SECONDS - 1)
    assert main.poller_healthy() is False


def test_tcc_denial_detection():
    assert main._is_tcc_denial(sqlite3.DatabaseError("authorization denied"))
    assert not main._is_tcc_denial(sqlite3.OperationalError("database is locked"))


def test_safe_fetch_raises_instead_of_masquerading_as_no_message(monkeypatch):
    monkeypatch.setattr(main, "DB_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(main, "DB_RETRY_BACKOFF", 0.0)

    def bad_connect(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(main.sqlite3, "connect", bad_connect)
    with pytest.raises(sqlite3.OperationalError):
        main.safe_fetch_last_message(0)


def test_attachments_endpoint_requires_key_and_reports_unreadable_db(monkeypatch):
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    headers = {"X-API-Key": os.environ["ADMIN_SECRET"]}
    assert client.get("/imessage/attachments", params={"since": 0}).status_code == 401

    def denied(**kwargs):
        raise sqlite3.DatabaseError("authorization denied")

    monkeypatch.setattr(main.attachment_verify, "fetch_outgoing_attachments", denied)
    resp = client.get("/imessage/attachments", params={"since": 0}, headers=headers)
    assert resp.status_code == 503

    monkeypatch.setattr(
        main.attachment_verify, "fetch_outgoing_attachments",
        lambda **k: [{"transfer_name": "a.pdf", "state": "delivered"}],
    )
    resp = client.get("/imessage/attachments", params={"since": 0, "filename": "a.pdf"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["count"] == 1


# ---------------------------------------------------------------------------
# gateway monitor debounce
# ---------------------------------------------------------------------------

def _run_monitor(monkeypatch, tmp_path, statuses, prior_state):
    import json
    import scripts.monitor_gateway as mon

    monkeypatch.setattr(mon, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(mon, "DOWN_RECHECK_DELAY_SECONDS", 0)
    if prior_state is not None:
        (tmp_path / "state.json").write_text(json.dumps(prior_state))
    seq = iter(statuses)
    monkeypatch.setattr(mon, "check_gateway", lambda: next(seq))
    alerts = []
    monkeypatch.setattr(mon, "send_imessage", lambda phone, text: alerts.append(text) or True)
    mon.main()
    return alerts, json.loads((tmp_path / "state.json").read_text())


def test_monitor_rechecks_before_declaring_down(monkeypatch, tmp_path):
    alerts, state = _run_monitor(
        monkeypatch, tmp_path,
        [("down", "/health unreachable"), ("up", "/health and /ready both passing")],
        {"status": "up"},
    )
    assert alerts == []
    assert state["status"] == "up"


def test_monitor_alerts_when_still_down_after_recheck(monkeypatch, tmp_path):
    alerts, state = _run_monitor(
        monkeypatch, tmp_path,
        [("down", "/health unreachable"), ("down", "/health unreachable")],
        {"status": "up"},
    )
    assert len(alerts) == 1 and "DOWN" in alerts[0]
    # The alert must name the label that actually serves port 8000. It said
    # com.lexi.ivy for weeks after com.ivy.gateway took over (2026-09-02).
    assert "com.ivy.gateway" in alerts[0]
    assert state["status"] == "down"


def test_monitor_needs_two_degraded_sightings(monkeypatch, tmp_path):
    alerts, state = _run_monitor(
        monkeypatch, tmp_path, [("degraded", "imessage_poller_healthy")], {"status": "up"},
    )
    assert alerts == []
    assert state["status"] == "up" and state["degraded_streak"] == 1

    alerts, state = _run_monitor(
        monkeypatch, tmp_path, [("degraded", "imessage_poller_healthy")], state,
    )
    assert len(alerts) == 1 and "NOT READY" in alerts[0]
    assert state["status"] == "degraded"

    alerts, state = _run_monitor(
        monkeypatch, tmp_path, [("up", "/health and /ready both passing")], state,
    )
    assert len(alerts) == 1 and "back UP" in alerts[0]
    assert "com.ivy.gateway" in alerts[0]
    assert state["degraded_streak"] == 0
