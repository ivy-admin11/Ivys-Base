"""Per-sender conversation memory: what the provider chain is told, what it
forgets, and that a follow-up is never mistaken for a job command.

No provider is ever contacted — every test stubs the network boundary.
"""

import time

import pytest

import main


@pytest.fixture(autouse=True)
def clean_memory():
    main._CONVERSATIONS.clear()
    yield
    main._CONVERSATIONS.clear()


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------

def test_history_is_per_sender_and_ordered_oldest_first():
    main.remember_turn("+1", "first question", "first answer")
    main.remember_turn("+1", "second question", "second answer")
    main.remember_turn("+2", "unrelated", "reply")

    assert [t["content"] for t in main.conversation_history("+1")] == [
        "first question", "first answer", "second question", "second answer",
    ]
    assert [t["role"] for t in main.conversation_history("+1")] == [
        "user", "assistant", "user", "assistant",
    ]
    assert [t["content"] for t in main.conversation_history("+2")] == ["unrelated", "reply"]


def test_history_is_capped_to_the_most_recent_turns(monkeypatch):
    monkeypatch.setattr(main, "CONVERSATION_MAX_MESSAGES", 4)
    for i in range(5):
        main.remember_turn("+1", f"q{i}", f"a{i}")
    assert [t["content"] for t in main.conversation_history("+1")] == ["q3", "a3", "q4", "a4"]


def test_history_expires_and_the_sender_is_dropped(monkeypatch):
    main.remember_turn("+1", "stale", "old reply")
    assert main.conversation_history("+1")
    monkeypatch.setattr(main, "CONVERSATION_TTL_SECONDS", 0)
    time.sleep(0.01)
    assert main.conversation_history("+1") == []
    assert "+1" not in main._CONVERSATIONS, "expired sender should not leak memory"


def test_unanswered_turn_is_still_remembered():
    main.remember_turn("+1", "are you there?", None)
    assert main.conversation_history("+1") == [{"role": "user", "content": "are you there?"}]


def test_forget_conversation_clears_one_sender_only():
    main.remember_turn("+1", "a", "b")
    main.remember_turn("+2", "c", "d")
    main.forget_conversation("+1")
    assert main.conversation_history("+1") == []
    assert main.conversation_history("+2")


def test_unknown_sender_has_empty_history():
    assert main.conversation_history("+nobody") == []


# ---------------------------------------------------------------------------
# what the providers actually receive
# ---------------------------------------------------------------------------

def test_provider_messages_puts_history_between_system_and_current():
    history = [
        {"role": "user", "content": "same day ooni dough?"},
        {"role": "assistant", "content": "65% hydration. Want a full recipe?"},
    ]
    msgs = main._provider_messages("SYS", "Yes, I want the full recipe", history)
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[0]["content"] == "SYS"
    assert msgs[1]["content"] == "same day ooni dough?"
    assert msgs[-1]["content"] == "Yes, I want the full recipe"


def test_provider_messages_drops_malformed_turns():
    history = [
        {"role": "user", "content": "keep me"},
        {"role": "system", "content": "not a conversation turn"},
        {"role": "assistant", "content": ""},
        {"role": "assistant"},
    ]
    msgs = main._provider_messages("SYS", "now", history)
    assert [m["content"] for m in msgs] == ["SYS", "keep me", "now"]


def test_provider_messages_with_no_history_is_just_system_and_user():
    assert main._provider_messages("SYS", "hi", None) == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "hi"},
    ]


def test_deepseek_payload_carries_history(monkeypatch):
    captured = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "Here is the recipe."}}]}

    monkeypatch.setattr(
        main.requests, "post",
        lambda url, json=None, headers=None, timeout=None: (
            captured.update(payload=json) or Resp()
        ),
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    history = [{"role": "assistant", "content": "Want a full recipe?"}]
    reply = main.execute_deepseek_call("Yes", "SYS", history=history)

    assert reply == "Here is the recipe."
    assert [m["role"] for m in captured["payload"]["messages"]] == ["system", "assistant", "user"]


def test_gemini_backup_folds_history_into_the_prompt(monkeypatch):
    seen = {}

    def fake_generate(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        raise RuntimeError("stop after capturing the prompt")

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main, "_gemini_generate_content", fake_generate, raising=False)
    monkeypatch.setattr(main, "ENABLE_PROMPT_CACHING", False, raising=False)

    history = [{"role": "assistant", "content": "Want a full recipe?"}]
    with pytest.raises(Exception):
        main._gemini_backup_reply("Yes", history=history)

    blob = repr(seen)
    assert "Want a full recipe?" in blob
    assert "Current message: Yes" in blob


def test_format_history_labels_each_speaker():
    text = main.format_history_for_prompt(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    )
    assert text.splitlines()[1:] == ["User: hi", "Ivy: hello"]


# ---------------------------------------------------------------------------
# the reply chain records what it said
# ---------------------------------------------------------------------------

def test_conversation_reply_supplies_history_and_records_the_exchange(monkeypatch):
    main.remember_turn("+1", "same day ooni dough?", "Want a full recipe?")
    seen = {}

    def fake_deepseek(text, instruction, history=None):
        seen["history"] = history
        return "Here is the dough recipe."

    monkeypatch.setattr(main, "execute_deepseek_call", fake_deepseek)

    reply = main._conversation_reply("Yes, I want the full recipe", "+1")

    assert reply == "Here is the dough recipe."
    assert [t["content"] for t in seen["history"]] == [
        "same day ooni dough?", "Want a full recipe?",
    ]
    # The new exchange is now itself part of the sender's history.
    assert [t["content"] for t in main.conversation_history("+1")][-2:] == [
        "Yes, I want the full recipe", "Here is the dough recipe.",
    ]


def test_conversation_reply_without_a_sender_keeps_no_memory(monkeypatch):
    monkeypatch.setattr(
        main, "execute_deepseek_call",
        lambda text, instruction, history=None: "answer",
    )
    assert main._conversation_reply("hello") == "answer"
    assert main._CONVERSATIONS == {}


def test_total_provider_outage_still_records_the_question(monkeypatch):
    def dead(*a, **k):
        raise RuntimeError("provider down")

    for name in ("execute_deepseek_call", "execute_openai_call", "_gemini_backup_reply"):
        monkeypatch.setattr(main, name, dead)

    reply = main._conversation_reply("did you get my message?", "+1")

    assert "temporarily unavailable" in reply
    assert [t["content"] for t in main.conversation_history("+1")] == ["did you get my message?"]


# ---------------------------------------------------------------------------
# the bug this exists to prevent
# ---------------------------------------------------------------------------

def test_a_bare_follow_up_is_not_classified_as_a_job_command():
    """"Yes, I want the full recipe" must reach the conversation lane. It once
    dispatched the Familia Meal Planner instead of answering."""
    for text in ("Yes, I want the full recipe", "yes", "sure", "send it"):
        assert main._resolve_job_command(text) is None
        assert main.classify_imessage_text(text).startswith("conversation")


def test_an_explicit_job_request_still_dispatches():
    assert main._resolve_job_command("run sharp picks") == "sharp_picks"
    assert main.classify_imessage_text("run sharp picks") == "job"
