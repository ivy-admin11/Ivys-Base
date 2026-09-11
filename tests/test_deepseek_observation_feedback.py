"""DeepSeek's observe -> reason round trip after a tool call.

Until 2026-09-10 the primary brain returned the raw tool output as Ivy's
reply — "what's on my list?" came back as "Milk, Eggs" — while the Gemini
backup fed the observation back to the model and answered in a sentence. The
fallback gave better answers than the primary for every tool-using question.

The primary now runs the same one-round ReAct loop. The invariant that
matters most: once a tool has run, NOTHING on the follow-up may raise, because
the callers fail over to Gemini on an exception and Gemini would run the
tool again.
"""

import json

import pytest
import requests

import main
from ivy_core.pipeline_status import ProviderUnavailableError


def _tool_call_response(*calls):
    return {"choices": [{"message": {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}
            for cid, name, args in calls
        ],
    }}]}


def _text_response(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.fixture
def two_round(monkeypatch):
    """Mock requests.post: first call returns a tool call, second returns text.
    Records every payload so the follow-up shape can be asserted."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    class _Posted(list):
        timeouts: list = []

    posted = _Posted()
    responses: list = []
    timeouts = posted.timeouts = []

    def fake_post(url, json=None, headers=None, timeout=None):
        posted.append(json)
        timeouts.append(timeout)
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setitem(main.TOOL_HANDLERS, "fetch_apple_reminders",
                        lambda list_name="Household": "Milk, Eggs")
    return posted, responses


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------

def test_tool_result_is_fed_back_and_the_model_writes_the_answer(two_round):
    posted, responses = two_round
    responses += [
        _Resp(payload=_tool_call_response(("c1", "fetch_apple_reminders", {}))),
        _Resp(payload=_text_response("You have milk and eggs on your Household list.")),
    ]
    out = main.execute_deepseek_call("what's on my list?", "sys")
    assert out == "You have milk and eggs on your Household list."
    assert out != "Milk, Eggs"                     # no longer the raw output
    assert len(posted) == 2


def test_follow_up_payload_has_the_openai_tool_transcript_shape(two_round):
    posted, responses = two_round
    responses += [
        _Resp(payload=_tool_call_response(("c1", "fetch_apple_reminders", {}))),
        _Resp(payload=_text_response("ok")),
    ]
    main.execute_deepseek_call("what's on my list?", "sys")

    first, follow = posted
    msgs = follow["messages"]
    # Same conversation prefix, then the assistant's tool-call turn, then the
    # tool result keyed by the call id the model issued.
    assert msgs[: len(first["messages"])] == first["messages"]
    assert msgs[-2]["role"] == "assistant" and msgs[-2]["tool_calls"][0]["id"] == "c1"
    assert msgs[-1] == {"role": "tool", "tool_call_id": "c1", "content": "Milk, Eggs"}
    # Same model and schema, but forced to answer in prose — one round only.
    assert follow["model"] == first["model"]
    assert follow["tools"] == first["tools"]
    assert follow["tool_choice"] == "none"


def test_every_tool_call_runs_and_is_fed_back_not_just_the_first(two_round, monkeypatch):
    posted, responses = two_round
    ran: list = []
    monkeypatch.setitem(main.TOOL_HANDLERS, "fetch_apple_reminders",
                        lambda list_name="Household": ran.append("reminders") or "Milk")
    monkeypatch.setitem(main.TOOL_HANDLERS, "check_apple_calendar",
                        lambda timeframe="today": ran.append("calendar") or "Dentist 3pm")
    responses += [
        _Resp(payload=_tool_call_response(
            ("c1", "fetch_apple_reminders", {}),
            ("c2", "check_apple_calendar", {"timeframe": "today"}))),
        _Resp(payload=_text_response("Milk to buy, dentist at 3.")),
    ]
    out = main.execute_deepseek_call("what's today look like?", "sys")
    assert ran == ["reminders", "calendar"]
    tool_turns = [m for m in posted[1]["messages"] if m["role"] == "tool"]
    assert [t["tool_call_id"] for t in tool_turns] == ["c1", "c2"]
    assert out == "Milk to buy, dentist at 3."


# ---------------------------------------------------------------------------
# The invariant: a tool that already ran must never be run again
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("failure", [
    requests.exceptions.ReadTimeout("Read timed out."),
    requests.exceptions.ConnectionError("refused"),
    _Resp(status_code=503, payload={}),
    _Resp(status_code=200, payload=json.JSONDecodeError("bad", "", 0)),
    _Resp(status_code=200, payload=_text_response("")),       # empty answer
    _Resp(status_code=200, payload=_text_response(None)),     # null answer
], ids=["timeout", "connection", "http503", "bad-json", "empty", "null"])
def test_follow_up_failure_degrades_to_raw_output_and_never_raises(two_round, monkeypatch, failure):
    posted, responses = two_round
    runs: list = []
    monkeypatch.setitem(main.TOOL_HANDLERS, "add_apple_reminder",
                        lambda title, list_name="Household": runs.append(title) or f"Added {title}")
    responses += [
        _Resp(payload=_tool_call_response(("c1", "add_apple_reminder", {"title": "Milk"}))),
        failure,
    ]
    # Must NOT raise: an exception here fails over to Gemini, which would add
    # the reminder a second time.
    out = main.execute_deepseek_call("remind me to buy milk", "sys")
    assert out == "Added Milk"
    assert runs == ["Milk"]                       # executed exactly once


def test_missing_tool_call_id_fails_over_before_anything_runs(two_round, monkeypatch):
    """The follow-up transcript is keyed by the id, so without it the round
    trip is unbuildable — knowable before any tool runs, so it fails over."""
    posted, responses = two_round
    runs: list = []
    monkeypatch.setitem(main.TOOL_HANDLERS, "fetch_apple_reminders",
                        lambda list_name="Household": runs.append(1) or "Milk")
    responses += [_Resp(payload={"choices": [{"message": {
        "content": None,
        "tool_calls": [{"function": {"name": "fetch_apple_reminders", "arguments": "{}"}}],
    }}]})]
    with pytest.raises(ProviderUnavailableError):
        main.execute_deepseek_call("list?", "sys")
    assert runs == []


# ---------------------------------------------------------------------------
# Provider-response defects still fail over (nothing has run yet)
# ---------------------------------------------------------------------------

def test_hallucinated_tool_still_fails_over_before_anything_runs(two_round):
    posted, responses = two_round
    responses += [_Resp(payload=_tool_call_response(("c1", "check_the_weather", {})))]
    with pytest.raises(ProviderUnavailableError):
        main.execute_deepseek_call("hi", "sys")
    assert len(posted) == 1


def test_malformed_tool_call_shape_fails_over(two_round):
    posted, responses = two_round
    responses += [_Resp(payload={"choices": [{"message": {
        "content": None, "tool_calls": [{"id": "c1", "function": {}}]}}]})]
    with pytest.raises(ProviderUnavailableError):
        main.execute_deepseek_call("hi", "sys")


def test_non_object_tool_arguments_fail_over(two_round):
    posted, responses = two_round
    responses += [_Resp(payload={"choices": [{"message": {
        "content": None,
        "tool_calls": [{"id": "c1", "function": {"name": "fetch_apple_reminders", "arguments": "[1,2]"}}]}}]})]
    with pytest.raises(ProviderUnavailableError):
        main.execute_deepseek_call("hi", "sys")


def test_plain_reply_with_null_content_is_empty_not_an_attribute_error(two_round):
    """The API may send content: null. That is an empty answer (so the caller
    fails over), not a crash."""
    posted, responses = two_round
    responses += [_Resp(payload=_text_response(None))]
    assert main.execute_deepseek_call("hi", "sys") == ""


# ---------------------------------------------------------------------------
# The hole a review found: validate EVERYTHING before executing ANYTHING
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_second_call", [
    {"id": "c2", "function": {"name": "check_the_weather", "arguments": "{}"}},   # hallucinated
    {"id": "c2", "function": {"name": "fetch_apple_reminders", "arguments": "{nope"}},  # bad json
    {"id": "c2", "function": {"name": "fetch_apple_reminders", "arguments": "[1]"}},   # not an object
    {"function": {"name": "fetch_apple_reminders", "arguments": "{}"}},             # no id
    "not even a dict",                                                               # malformed entry
    {"id": "c2", "function": "not a dict"},                                          # malformed function
], ids=["hallucinated", "bad-json", "not-object", "no-id", "entry-not-dict", "function-not-dict"])
def test_a_defective_second_call_means_the_first_never_runs(two_round, monkeypatch, bad_second_call):
    """[valid add_apple_reminder, defective] used to run the reminder and THEN
    raise — and the callers' failover would have Gemini add it again."""
    posted, responses = two_round
    runs: list = []
    monkeypatch.setitem(main.TOOL_HANDLERS, "add_apple_reminder",
                        lambda title, list_name="Household": runs.append(title) or f"Added {title}")
    responses += [_Resp(payload={"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "c1", "function": {"name": "add_apple_reminder", "arguments": json.dumps({"title": "Milk"})}},
        bad_second_call,
    ]}}]})]
    with pytest.raises(ProviderUnavailableError):
        main.execute_deepseek_call("remind me to buy milk and check the weather", "sys")
    assert runs == []                              # NOTHING executed
    assert len(posted) == 1                        # no follow-up attempted


def test_a_tool_that_raises_is_an_observation_not_a_failover(two_round, monkeypatch):
    """_execute_tool_call contains handler exceptions; the follow-up gets an
    "Error:" observation and can explain it. Nothing raises, nothing re-runs."""
    posted, responses = two_round
    runs: list = []

    def boom(list_name="Household"):
        runs.append(1)
        raise RuntimeError("Reminders is not responding")

    monkeypatch.setitem(main.TOOL_HANDLERS, "fetch_apple_reminders", boom)
    responses += [
        _Resp(payload=_tool_call_response(("c1", "fetch_apple_reminders", {}))),
        _Resp(payload=_text_response("I couldn't reach Reminders just now.")),
    ]
    out = main.execute_deepseek_call("what's on my list?", "sys")
    assert out == "I couldn't reach Reminders just now."
    assert runs == [1]
    tool_turn = [m for m in posted[1]["messages"] if m["role"] == "tool"][0]
    assert tool_turn["content"].startswith("Error:")


def test_empty_tool_output_plus_failed_follow_up_is_still_a_truthy_reply(two_round, monkeypatch):
    """A falsy reply makes every caller fail over to Gemini, which would run
    the tool again. Once anything has executed the reply must be truthy."""
    posted, responses = two_round
    runs: list = []
    monkeypatch.setitem(main.TOOL_HANDLERS, "fetch_apple_reminders",
                        lambda list_name="Household": runs.append(1) or "")
    responses += [
        _Resp(payload=_tool_call_response(("c1", "fetch_apple_reminders", {}))),
        _Resp(status_code=503, payload={}),
    ]
    out = main.execute_deepseek_call("list?", "sys")
    assert out                                     # truthy — no failover
    assert runs == [1]


def test_model_ignoring_tool_choice_none_does_not_start_a_loop(two_round, monkeypatch):
    posted, responses = two_round
    runs: list = []
    monkeypatch.setitem(main.TOOL_HANDLERS, "fetch_apple_reminders",
                        lambda list_name="Household": runs.append(1) or "Milk")
    responses += [
        _Resp(payload=_tool_call_response(("c1", "fetch_apple_reminders", {}))),
        _Resp(payload=_tool_call_response(("c2", "fetch_apple_reminders", {}))),   # asks again
    ]
    out = main.execute_deepseek_call("list?", "sys")
    assert out == "Milk"                           # raw output, not a re-run
    assert runs == [1]                             # second request never dispatched
    assert len(posted) == 2


def test_both_rounds_carry_a_timeout_and_the_follow_up_is_shorter(two_round):
    """A follow-up without a timeout would hold the single poller thread — and
    every sender behind it — indefinitely."""
    posted, responses = two_round
    responses += [
        _Resp(payload=_tool_call_response(("c1", "fetch_apple_reminders", {}))),
        _Resp(payload=_text_response("ok")),
    ]
    main.execute_deepseek_call("list?", "sys")
    first, follow = posted.timeouts
    assert first == main.EXTERNAL_API_TIMEOUT
    assert follow == main.DEEPSEEK_FOLLOW_UP_TIMEOUT
    assert 0 < follow <= first


def test_a_failing_rerun_guard_does_not_run_the_job_or_escape(monkeypatch):
    """The guard reads the outbox and can raise. It must neither run the job
    (failing open) nor escape (which reaches the callers' failover)."""
    ran: list = []
    monkeypatch.setitem(main.TOOL_HANDLERS, "run_job", lambda job_name: ran.append(job_name) or "started")
    monkeypatch.setattr(main, "_rerun_would_be_wrong",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("outbox unreadable")))
    out = main._execute_tool_call("run_job", {"job_name": "sharp_picks"}, inbound_text="run picks")
    assert isinstance(out, str) and "didn't start" in out
    assert ran == []
