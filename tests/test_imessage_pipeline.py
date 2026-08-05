"""Regression coverage for Ivy's bounded, nonblocking iMessage pipeline."""

from __future__ import annotations

import queue
import sqlite3
import threading
import time

import pytest

import main
from ivy_core.imessage_state import InboxStateStore, InboundMessage


@pytest.fixture
def isolated_pipeline(tmp_path, monkeypatch):
    stop_event = threading.Event()
    state = InboxStateStore(tmp_path / "inbox.db")
    monkeypatch.setattr(main, "_IMESSAGE_STOP_EVENT", stop_event)
    monkeypatch.setattr(main, "_IMESSAGE_STATE", state)
    monkeypatch.setattr(main, "_IMESSAGE_INBOX_QUEUE", queue.Queue(maxsize=20))
    monkeypatch.setattr(main, "_IMESSAGE_SLOW_QUEUE", queue.Queue(maxsize=20))
    monkeypatch.setattr(main, "_IMESSAGE_LATEST_BY_SENDER", {})
    monkeypatch.setattr(main, "IMESSAGE_DEBOUNCE_SECONDS", 0.02)
    monkeypatch.setattr(main, "IMESSAGE_QUEUE_PUT_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(main, "_is_authorized_sender", lambda _sender: True)
    yield state, stop_event
    stop_event.set()


def _message(message_id: int, text: str, sender: str = "authorized") -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        text=text,
        sender=sender,
        collected_monotonic=time.monotonic(),
    )


def test_state_store_persists_no_message_or_sender_content(tmp_path):
    state = InboxStateStore(tmp_path / "inbox.db")
    assert state.reserve(7, "conversation_read_only") is True
    assert state.reserve(7, "conversation_read_only") is False

    with sqlite3.connect(state.path) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(inbound_messages)")
        }
        raw = state.path.read_bytes()

    assert "text" not in columns
    assert "sender" not in columns
    assert b"private message" not in raw
    assert b"+15555550123" not in raw
    assert (state.path.stat().st_mode & 0o777) == 0o600
    assert (state.path.parent.stat().st_mode & 0o777) == 0o700


def test_state_store_rejects_symlink_database(tmp_path):
    target = tmp_path / "target.db"
    target.touch()
    linked = tmp_path / "linked.db"
    linked.symlink_to(target)

    with pytest.raises(RuntimeError, match="must not be a symlink"):
        InboxStateStore(linked).get_cursor()


def test_restart_requeues_only_never_started_rows(tmp_path):
    state = InboxStateStore(tmp_path / "inbox.db")
    state.reserve(1)
    state.reserve(2)
    assert state.mark_processing([2], "conversation_action") is True

    assert state.recover_after_restart() == [1]
    assert state.recent_counts() == {"completion_unknown": 1, "queued": 1}


def test_batch_fetch_is_bounded_and_rowid_ordered(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE message "
            "(ROWID INTEGER PRIMARY KEY, text TEXT, is_from_me INTEGER, handle_id INTEGER)"
        )
        conn.execute("CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT)")
        conn.execute("INSERT INTO handle(ROWID, id) VALUES(1, 'authorized')")
        for rowid in (5, 2, 9, 1, 7):
            conn.execute(
                "INSERT INTO message(ROWID, text, is_from_me, handle_id) VALUES(?, ?, 0, 1)",
                (rowid, f"message-{rowid}"),
            )

    main.close_chat_db()
    monkeypatch.setattr(main, "CHAT_DB_PATH", str(db_path))
    rows = main.safe_fetch_new_messages(0, limit=3)
    main.close_chat_db()

    assert rows is not None
    assert [row[0] for row in rows] == [1, 2, 5]


@pytest.mark.parametrize("limit", [0, 101, "not-a-number"])
def test_batch_fetch_rejects_unbounded_limits(limit):
    with pytest.raises(ValueError):
        main.safe_fetch_new_messages(0, limit=limit)


def test_full_inbound_queue_does_not_advance_cursor(isolated_pipeline, monkeypatch):
    state, _stop = isolated_pipeline
    full_queue: queue.Queue[InboundMessage] = queue.Queue(maxsize=1)
    full_queue.put(_message(999, "already queued"))
    monkeypatch.setattr(main, "_IMESSAGE_INBOX_QUEUE", full_queue)
    state.initialize_cursor(0)

    cursor = main._collect_imessage_rows([(1, "new", "authorized")], 0)

    assert cursor == 0
    assert state.get_cursor() == 0
    assert state.recent_counts() == {}


def test_recovery_db_failure_keeps_queued_rows_for_retry(
    isolated_pipeline, monkeypatch
):
    state, _stop = isolated_pipeline
    state.reserve(1)
    state.reserve(2)
    responses = iter(
        [
            None,
            [(1, "first", "authorized"), (2, "second", "authorized")],
        ]
    )
    monkeypatch.setattr(main, "safe_fetch_messages_by_ids", lambda _ids: next(responses))

    remaining = main._enqueue_recovered_messages([1, 2])

    assert remaining == [1, 2]
    assert state.recent_counts() == {"queued": 2}
    assert main._IMESSAGE_INBOX_QUEUE.empty()

    remaining = main._enqueue_recovered_messages(remaining)

    assert remaining == []
    assert [main._IMESSAGE_INBOX_QUEUE.get_nowait().message_id for _ in range(2)] == [1, 2]


def test_coalescing_preserves_order_and_combines_status_intent():
    unit = main.coalesce_imessage_messages(
        [
            _message(3, "tell me all your skills"),
            _message(1, "ivy status"),
            _message(2, "health check"),
        ]
    )

    assert unit.message_ids == (1, 2, 3)
    assert unit.category == "operations"
    assert unit.text.splitlines() == [
        "ivy status",
        "health check",
        "tell me all your skills",
    ]


def test_operations_unit_bypasses_every_provider_and_mutating_tool(
    isolated_pipeline, monkeypatch
):
    state, _stop = isolated_pipeline
    state.reserve(1)
    unit = main.coalesce_imessage_messages([_message(1, "ivy status")])
    main._IMESSAGE_LATEST_BY_SENDER[unit.sender] = 1

    forbidden = []

    def fail(*_args, **_kwargs):
        forbidden.append("called")
        raise AssertionError("operations command reached a slow or mutating handler")

    monkeypatch.setattr(main, "execute_deepseek_call", fail)
    monkeypatch.setattr(main, "execute_openai_call", fail)
    monkeypatch.setattr(main, "_gemini_backup_reply", fail)
    monkeypatch.setattr(main, "add_apple_reminder", fail)
    monkeypatch.setattr(main, "fetch_apple_reminders", fail)
    monkeypatch.setattr(main, "check_apple_calendar", fail)
    monkeypatch.setattr(main, "_operations_reply", lambda _unit: "status response")
    monkeypatch.setattr(main, "run_local_applescript_send", lambda *_args: "SUCCESS")

    main._process_imessage_unit(unit)

    assert forbidden == []
    assert state.recent_counts() == {"completed": 1}


def test_newer_read_only_request_suppresses_obsolete_reply(
    isolated_pipeline, monkeypatch
):
    state, _stop = isolated_pipeline
    state.reserve(1)
    unit = main.coalesce_imessage_messages([_message(1, "how are you?")])
    main._IMESSAGE_LATEST_BY_SENDER[unit.sender] = 2
    sends = []
    monkeypatch.setattr(
        main,
        "run_local_applescript_send",
        lambda *_args: sends.append("sent") or "SUCCESS",
    )

    main._process_imessage_unit(unit)

    assert sends == []
    assert state.recent_counts() == {"superseded": 1}


def test_job_command_is_never_cancelled_as_superseded(isolated_pipeline, monkeypatch):
    state, _stop = isolated_pipeline
    state.reserve(1)
    unit = main.coalesce_imessage_messages([_message(1, "run sharp picks")])
    main._IMESSAGE_LATEST_BY_SENDER[unit.sender] = 2
    sends = []
    monkeypatch.setattr(main, "handle_job_command", lambda *_args: "dispatched")
    monkeypatch.setattr(
        main,
        "run_local_applescript_send",
        lambda _sender, body: sends.append(body) or "SUCCESS",
    )

    main._process_imessage_unit(unit)

    assert sends == ["dispatched"]
    assert state.recent_counts() == {"completed": 1}


def test_reply_after_mutating_tool_is_not_suppressed(
    isolated_pipeline, monkeypatch
):
    state, _stop = isolated_pipeline
    state.reserve(1)
    unit = main.coalesce_imessage_messages([_message(1, "please put milk on my list")])
    main._IMESSAGE_LATEST_BY_SENDER[unit.sender] = 1
    sends = []

    def mutate_then_receive_newer(**_kwargs):
        main._IMESSAGE_LATEST_BY_SENDER[unit.sender] = 2
        return "reminder committed"

    handlers = dict(main.TOOL_HANDLERS)
    handlers["add_apple_reminder"] = mutate_then_receive_newer
    monkeypatch.setattr(main, "TOOL_HANDLERS", handlers)
    monkeypatch.setattr(
        main,
        "_conversation_reply",
        lambda _text: main._execute_tool_call(
            "add_apple_reminder", {"title": "milk", "list_name": "Household"}
        ),
    )
    monkeypatch.setattr(
        main,
        "run_local_applescript_send",
        lambda _sender, body: sends.append(body) or "SUCCESS",
    )
    monkeypatch.setattr(main, "IMESSAGE_SLOW_ACK_SECONDS", 30.0)

    main._process_imessage_unit(unit)

    assert sends == ["reminder committed"]
    assert state.recent_counts() == {"completed": 1}


def test_slow_conversation_does_not_block_later_status_response(
    isolated_pipeline, monkeypatch
):
    state, stop_event = isolated_pipeline
    for message_id in (1, 2):
        state.reserve(message_id)

    provider_started = threading.Event()
    provider_release = threading.Event()
    status_sent = threading.Event()
    sent_bodies = []

    def slow_provider(_text):
        provider_started.set()
        assert provider_release.wait(2)
        return "old conversation response"

    def fake_send(_sender, body):
        sent_bodies.append(body)
        if body == "fresh status response":
            status_sent.set()
        return "SUCCESS"

    monkeypatch.setattr(main, "_conversation_reply", slow_provider)
    monkeypatch.setattr(main, "_operations_reply", lambda _unit: "fresh status response")
    monkeypatch.setattr(main, "run_local_applescript_send", fake_send)
    monkeypatch.setattr(main, "IMESSAGE_SLOW_ACK_SECONDS", 30.0)

    dispatcher = threading.Thread(target=main._imessage_dispatcher_worker, daemon=True)
    processor = threading.Thread(target=main._imessage_slow_worker, daemon=True)
    dispatcher.start()
    processor.start()

    first = _message(1, "please explain the week")
    main._IMESSAGE_LATEST_BY_SENDER[first.sender] = 1
    main._IMESSAGE_INBOX_QUEUE.put(first)
    assert provider_started.wait(1)

    second = _message(2, "ivy status")
    main._IMESSAGE_LATEST_BY_SENDER[second.sender] = 2
    main._IMESSAGE_INBOX_QUEUE.put(second)

    assert status_sent.wait(1), "status response waited behind the slow provider"
    provider_release.set()
    stop_event.set()
    dispatcher.join(2)
    processor.join(2)

    assert sent_bodies == ["fresh status response"]
    assert state.recent_counts() == {"completed": 1, "superseded": 1}


def test_slow_conversation_does_not_block_job_dispatch(
    isolated_pipeline, monkeypatch
):
    state, stop_event = isolated_pipeline
    for message_id in (1, 2):
        state.reserve(message_id)

    provider_started = threading.Event()
    provider_release = threading.Event()
    job_sent = threading.Event()
    job_calls = []

    def slow_provider(_text):
        provider_started.set()
        assert provider_release.wait(2)
        return "conversation result"

    monkeypatch.setattr(main, "_conversation_reply", slow_provider)
    monkeypatch.setattr(
        main,
        "handle_job_command",
        lambda *_args: job_calls.append("sharp_picks") or "job dispatched",
    )

    def fake_send(_sender, body):
        if body == "job dispatched":
            job_sent.set()
        return "SUCCESS"

    monkeypatch.setattr(main, "run_local_applescript_send", fake_send)
    monkeypatch.setattr(main, "IMESSAGE_SLOW_ACK_SECONDS", 30.0)

    dispatcher = threading.Thread(target=main._imessage_dispatcher_worker, daemon=True)
    processor = threading.Thread(target=main._imessage_slow_worker, daemon=True)
    dispatcher.start()
    processor.start()

    main._IMESSAGE_INBOX_QUEUE.put(_message(1, "explain the week"))
    assert provider_started.wait(1)
    main._IMESSAGE_INBOX_QUEUE.put(_message(2, "run sharp picks"))

    assert job_sent.wait(1), "job dispatch waited behind the slow provider"
    provider_release.set()
    stop_event.set()
    dispatcher.join(2)
    processor.join(2)

    assert job_calls == ["sharp_picks"]


def test_rapid_status_burst_collapses_and_job_dispatches_once(
    isolated_pipeline, monkeypatch
):
    state, stop_event = isolated_pipeline
    messages = [
        _message(1, "ivy status"),
        _message(2, "health check"),
        _message(3, "tell me all your skills"),
        _message(4, "what is turned on"),
        _message(5, "system health"),
        _message(6, "capabilities"),
        _message(7, "run sharp picks"),
    ]
    for message in messages:
        state.reserve(message.message_id)

    sent_bodies = []
    job_calls = []
    sends_complete = threading.Event()

    monkeypatch.setattr(main, "_operations_reply", lambda _unit: "one consolidated status")

    def fake_job(*_args):
        job_calls.append("sharp_picks")
        return "job dispatched"

    def fake_send(_sender, body):
        sent_bodies.append(body)
        if len(sent_bodies) == 2:
            sends_complete.set()
        return "SUCCESS"

    monkeypatch.setattr(main, "handle_job_command", fake_job)
    monkeypatch.setattr(main, "run_local_applescript_send", fake_send)

    dispatcher = threading.Thread(target=main._imessage_dispatcher_worker, daemon=True)
    processor = threading.Thread(target=main._imessage_slow_worker, daemon=True)
    dispatcher.start()
    processor.start()

    for message in messages:
        if main._category_can_be_superseded(main.classify_imessage_text(message.text)):
            main._IMESSAGE_LATEST_BY_SENDER[message.sender] = message.message_id
        main._IMESSAGE_INBOX_QUEUE.put(message)

    assert sends_complete.wait(2)
    stop_event.set()
    dispatcher.join(2)
    processor.join(2)

    assert sent_bodies == ["one consolidated status", "job dispatched"]
    assert job_calls == ["sharp_picks"]
    assert state.recent_counts() == {"completed": 7}
