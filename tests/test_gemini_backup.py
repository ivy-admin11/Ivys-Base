from datetime import datetime, timedelta, timezone

import pytest

import main
from proactive_agents import Familia_meal_planner, happy_hour_scout


class _FakeFunctionCall:
    def __init__(self, name, args):
        self.name = name
        self.args = args


class _FakePart:
    def __init__(self, text=None, function_call=None):
        self.text = text
        self.function_call = function_call


class _FakeContent:
    def __init__(self, parts):
        self.parts = parts


class _FakeCandidate:
    def __init__(self, parts):
        self.content = _FakeContent(parts)


class _FakeResponse:
    def __init__(self, parts):
        self.candidates = [_FakeCandidate(parts)]


def test_gemini_backup_text_response(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        main,
        "_gemini_generate_content",
        lambda **kwargs: _FakeResponse([_FakePart(text="gemini text")]),
    )

    assert main._gemini_backup_reply("hello") == "gemini text"


def test_gemini_backup_tool_call(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    calls = []
    responses = iter([
        _FakeResponse([
            _FakePart(function_call=_FakeFunctionCall("run_job", {"job_name": "picks"}))
        ]),
        _FakeResponse([_FakePart(text="tool follow-up complete")]),
    ])

    monkeypatch.setattr(main, "_gemini_generate_content", lambda **kwargs: next(responses))
    monkeypatch.setattr(
        main,
        "_execute_tool_call",
        lambda name, args: calls.append((name, args)) or "ok",
    )

    reply = main._gemini_backup_reply("run it")

    assert reply == "tool follow-up complete"
    assert calls == [("run_job", {"job_name": "picks"})]


def test_gemini_provider_failure_raises(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        main,
        "_gemini_generate_content",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("provider down")),
    )

    with pytest.raises(RuntimeError, match="provider down"):
        main._gemini_backup_reply("hello")


def test_query_llm_with_tools_falls_back_to_gemini(monkeypatch):
    monkeypatch.setattr(main, "execute_deepseek_call", lambda *_: "")
    monkeypatch.setattr(main, "_gemini_backup_reply", lambda *_: "backup reply")

    assert main.query_llm_with_tools("hello") == "backup reply"


def test_happy_hour_timestamp_is_timezone_aware(monkeypatch):
    monkeypatch.setattr(happy_hour_scout, "fetch_local_specials", lambda: {"venues": [], "specials": []})
    monkeypatch.setattr(happy_hour_scout, "format_happy_hour_pdf", lambda _: "/tmp/nope.pdf")

    result = happy_hour_scout.run(force=True, send=False)
    ts = datetime.fromisoformat(result["timestamp"])
    assert ts.tzinfo is not None
    assert ts.utcoffset() == timedelta(0)


def test_familia_generated_at_is_timezone_aware(monkeypatch):
    monkeypatch.setattr(
        Familia_meal_planner,
        "query_llm",
        lambda *_args, **_kwargs: '[{"recipe_name":"A","ingredients":[],"macros":{}}]',
    )

    result = Familia_meal_planner.generate_family_meal_plan()
    ts = datetime.fromisoformat(result["generated_at"])
    assert ts.tzinfo is not None
    assert ts.utcoffset() == timezone.utc.utcoffset(None)
