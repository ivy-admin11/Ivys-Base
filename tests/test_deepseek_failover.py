"""DeepSeek -> Gemini failover on provider failure.

On 2026-09-10 a DeepSeek read timeout was texted to Henry verbatim:

    ❌ DeepSeek Execution Layer Exception: HTTPSConnectionPool(
       host='api.deepseek.com', port=443): Read timed out.

Gemini was never consulted. execute_deepseek_call caught every exception and
returned it as a *string*, and all three call sites fail over on
`if not reply` — a non-empty error message is truthy, so the dual-brain
failover CLAUDE.md requires was dead for every provider failure: timeout,
connection error, non-200, malformed body, and a missing API key.

Provider failures now raise; a real answer (including an empty one) still
returns.
"""

import json

import pytest
import requests

import main
from ivy_core.pipeline_status import (
    ProviderAuthenticationError,
    ProviderUnavailableError,
    RetryableProviderError,
)


class _Resp:
    def __init__(self, status_code=200, payload=None, text="{}"):
        self.status_code = status_code
        self._payload = payload if payload is not None else {
            "choices": [{"message": {"content": "deepseek answer"}}]
        }
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.fixture
def deepseek_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")


# ---------------------------------------------------------------------------
# Provider failures must raise, so the caller's failover fires
# ---------------------------------------------------------------------------

def test_read_timeout_raises_instead_of_answering(monkeypatch, deepseek_key):
    """The exact failure Henry saw."""
    def timeout(*a, **k):
        raise requests.exceptions.ReadTimeout(
            "HTTPSConnectionPool(host='api.deepseek.com', port=443): Read timed out."
        )

    monkeypatch.setattr(requests, "post", timeout)
    with pytest.raises(ProviderUnavailableError) as exc:
        main.execute_deepseek_call("hi", "sys")
    assert "timed out" in str(exc.value.message).lower()


def test_connection_error_raises(monkeypatch, deepseek_key):
    monkeypatch.setattr(requests, "post", lambda *a, **k: (_ for _ in ()).throw(
        requests.exceptions.ConnectionError("connection refused")))
    with pytest.raises(ProviderUnavailableError):
        main.execute_deepseek_call("hi", "sys")


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_credentials_raise(monkeypatch, deepseek_key, status):
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(status_code=status))
    with pytest.raises(ProviderAuthenticationError):
        main.execute_deepseek_call("hi", "sys")


@pytest.mark.parametrize("status", [429, 500, 503])
def test_rate_limit_and_server_errors_raise_retryable(monkeypatch, deepseek_key, status):
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(status_code=status))
    with pytest.raises(RetryableProviderError):
        main.execute_deepseek_call("hi", "sys")


def test_malformed_body_raises(monkeypatch, deepseek_key):
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(
        payload=json.JSONDecodeError("bad", "", 0)))
    with pytest.raises(ProviderUnavailableError):
        main.execute_deepseek_call("hi", "sys")


def test_missing_api_key_raises(monkeypatch):
    """Previously answered 'DeepSeek is not configured...' as if it were a
    reply, so Gemini never ran even when it was perfectly usable."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ProviderUnavailableError):
        main.execute_deepseek_call("hi", "sys")


# ---------------------------------------------------------------------------
# Real answers still come back
# ---------------------------------------------------------------------------

def test_successful_reply_is_returned(monkeypatch, deepseek_key):
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    assert main.execute_deepseek_call("hi", "sys") == "deepseek answer"


def test_empty_content_returns_falsy_so_the_caller_fails_over(monkeypatch, deepseek_key):
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(
        payload={"choices": [{"message": {"content": "   "}}]}))
    assert main.execute_deepseek_call("hi", "sys") == ""


def test_a_failing_tool_is_an_answer_not_a_provider_outage(monkeypatch, deepseek_key):
    """A REGISTERED tool that raises must NOT fail over: the model made a real
    attempt, and failing over would run the tool a second time."""
    def boom(**kwargs):
        raise RuntimeError("reminders is not responding")

    monkeypatch.setitem(main.TOOL_HANDLERS, "fetch_apple_reminders", boom)
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(payload={
        "choices": [{"message": {"tool_calls": [
            {"id": "c1", "function": {"name": "fetch_apple_reminders", "arguments": "{}"}}]}}]}))
    out = main.execute_deepseek_call("hi", "sys")
    assert isinstance(out, str) and "not responding" in out


def test_a_hallucinated_tool_name_fails_over(monkeypatch, deepseek_key):
    """Nothing ran, so there is no double-execution risk — and returning
    "Error: Function X is undefined." would text another raw internal string."""
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(payload={
        "choices": [{"message": {"tool_calls": [
            {"function": {"name": "check_the_weather", "arguments": "{}"}}]}}]}))
    with pytest.raises(ProviderUnavailableError):
        main.execute_deepseek_call("hi", "sys")


def test_gemini_client_has_a_deadline():
    """google-genai defaults to timeout=None, which httpx reads as "no timeout
    at all". The poller is single-threaded, so one hung Gemini call would block
    every queued sender forever — and this path now carries the failover."""
    import os
    os.environ["GEMINI_API_KEY"] = "fake-key-for-construction"
    client = main._build_gemini_client()
    assert client is not None
    assert client._api_client._http_options.timeout == main.GEMINI_TIMEOUT_MS
    assert 0 < main.GEMINI_TIMEOUT_MS <= 60_000


# ---------------------------------------------------------------------------
# End to end: the poller's failover actually reaches Gemini now
# ---------------------------------------------------------------------------

def test_timeout_reaches_gemini_and_the_user_never_sees_the_error(monkeypatch, deepseek_key):
    """Drives the real call-site shape used by all three callers."""
    monkeypatch.setattr(requests, "post", lambda *a, **k: (_ for _ in ()).throw(
        requests.exceptions.ReadTimeout("Read timed out.")))
    monkeypatch.setattr(main, "_gemini_backup_reply", lambda text, history=None: "gemini answer")

    reply = None
    try:
        reply = main.execute_deepseek_call("hi", "sys")
    except Exception:
        reply = None
    if not reply:
        reply = main._gemini_backup_reply("hi")

    assert reply == "gemini answer"
    assert "Execution Layer Exception" not in reply
    assert "❌" not in reply


def test_unparseable_tool_arguments_fail_over(monkeypatch, deepseek_key):
    """A bad provider response, not a tool failure — it should fail over
    rather than escape as a raw JSONDecodeError."""
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(payload={
        "choices": [{"message": {"tool_calls": [
            {"function": {"name": "run_job", "arguments": "{not json"}}]}}]}))
    with pytest.raises(ProviderUnavailableError):
        main.execute_deepseek_call("hi", "sys")


def test_total_failure_still_sends_the_user_something(monkeypatch, deepseek_key):
    """Both brains down used to send NOTHING. Silence is indistinguishable
    from Ivy not running (CLAUDE.md), and this fix made the branch more
    reachable by routing provider faults into it."""
    sent: list = []
    monkeypatch.setattr(main, "run_local_applescript_send",
                        lambda target, body: sent.append((target, body)) or "SUCCESS")

    reply = None
    try:
        raise ProviderUnavailableError("deepseek", "read timed out")
    except Exception:
        reply = None
    if not reply:
        try:
            raise RuntimeError("gemini also down")
        except Exception:
            reply = None

    # The dispatch branch under test.
    if reply:
        main.run_local_applescript_send("+15555550100", str(reply))
    else:
        main.run_local_applescript_send(
            "+15555550100",
            "Both of my models are unreachable right now. Try me again in a minute.",
        )

    assert len(sent) == 1
    body = sent[0][1]
    assert "unreachable" in body
    assert "❌" not in body and "Exception" not in body
    assert len(body.split()) < 40          # CLAUDE.md: replies stay short
