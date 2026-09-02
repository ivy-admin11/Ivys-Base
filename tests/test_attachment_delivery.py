"""Attachment delivery: chat.db verification, the method chain, and the
gateway lookup endpoint job subprocesses depend on.

Nothing here touches Messages.app, the real chat.db, or the network —
conftest pins CHAT_DB_PATH and IVY_GATEWAY_URL at dead ends."""

import os
import sqlite3
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
        INSERT INTO handle VALUES (1, '+12147334061'), (2, '+18179138648');
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
        since_ts=now - 60, filename="picks.pdf", handle="(214) 733-4061", db_path=str(db)
    )
    assert len(rows) == 1
    assert rows[0]["handle"] == "+12147334061"
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
        last_error_category = None

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
        last_error_category = "timeout"

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
# the gateway endpoint job subprocesses fall back to
# ---------------------------------------------------------------------------

def test_attachments_endpoint_requires_key_and_reports_unreadable_db(monkeypatch):
    import main
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    headers = {"X-API-Key": os.environ["ADMIN_SECRET"]}
    assert client.get("/imessage/attachments", params={"since": 0}).status_code == 401

    def denied(**kwargs):
        raise sqlite3.DatabaseError("authorization denied")

    monkeypatch.setattr(main.attachment_verify, "fetch_outgoing_attachments", denied)
    resp = client.get("/imessage/attachments", params={"since": 0}, headers=headers)
    assert resp.status_code == 503
    # The error text must not leak the sqlite message or the db path.
    assert "authorization denied" not in resp.text

    monkeypatch.setattr(
        main.attachment_verify, "fetch_outgoing_attachments",
        lambda **k: [{"transfer_name": "a.pdf", "state": "delivered"}],
    )
    resp = client.get("/imessage/attachments", params={"since": 0, "filename": "a.pdf"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["count"] == 1
