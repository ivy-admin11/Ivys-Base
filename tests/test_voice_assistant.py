"""Tests for voice session lifecycle, cleanup and cache accounting.

voice_assistant had no tests. It is a long-lived, in-process store: the module
instantiates a global VoiceSessionManager at import and main.py's /voice/*
endpoints mutate it for the life of the server. Nothing here fails loudly --
a session that leaks, or a stat that counts the wrong things, just quietly
misreports while the endpoints keep returning 200.

No Gemini client is constructed and no network is touched: the module-level
`genai` handle and the injected cache_manager are both replaced with fakes.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import voice_assistant as va


def stale(session: va.VoiceSession, seconds: int = 10_000) -> va.VoiceSession:
    """Age a session past its TTL without sleeping."""
    session.last_activity = datetime.now() - timedelta(seconds=seconds)
    return session


def manager(**kwargs) -> va.VoiceSessionManager:
    """A manager whose periodic cleanup will not fire on its own.

    Every public method calls _cleanup_expired_sessions() first, so with the
    real 300 s interval a test can never tell an intentional sweep from an
    incidental one. Tests that want a sweep call it explicitly.
    """
    kwargs.setdefault("cleanup_interval_seconds", 10_000)
    return va.VoiceSessionManager(**kwargs)


class TestSessionExpiry:
    def test_a_fresh_session_is_not_expired(self):
        assert va.VoiceSession("henry").is_expired() is False

    def test_expiry_is_measured_from_last_activity_not_creation(self):
        # A session in continuous use must not be swept out from under the user.
        s = va.VoiceSession("henry", session_ttl_seconds=60)
        s.created_at = datetime.now() - timedelta(hours=5)
        assert s.is_expired() is False

    def test_idle_past_the_ttl_is_expired(self):
        s = va.VoiceSession("henry", session_ttl_seconds=60)
        stale(s, 61)
        assert s.is_expired() is True

    def test_activity_resets_the_clock(self):
        s = va.VoiceSession("henry", session_ttl_seconds=60)
        stale(s, 61)
        s.add_message("user", "still here")
        assert s.is_expired() is False


class TestConversationHistory:
    def test_empty_history_has_empty_context(self):
        # The context is interpolated into a prompt; "None" or a traceback there
        # would be fed to the model as if it were conversation.
        assert va.VoiceSession("henry").get_context() == ""

    def test_context_is_role_prefixed_and_newline_joined(self):
        s = va.VoiceSession("henry")
        s.add_message("user", "who won")
        s.add_message("assistant", "the Jets")
        assert s.get_context() == "USER: who won\nASSISTANT: the Jets"

    def test_context_is_capped_at_the_last_few_messages(self):
        s = va.VoiceSession("henry")
        for i in range(20):
            s.add_message("user", f"m{i}")
        lines = s.get_context().splitlines()
        assert len(lines) == va.MAX_CONTEXT_MESSAGES
        assert lines[-1] == "USER: m19"

    def test_history_is_bounded(self):
        """REGRESSION. self.messages grew forever. get_context() only ever reads
        the newest few, and a session that keeps being used keeps refreshing
        last_activity -- so it never expires, the manager's cleanup never sweeps
        it, and its history is a permanent leak in a server that stays up for
        weeks.
        """
        s = va.VoiceSession("henry")
        for i in range(5_000):
            s.add_message("user", "x" * 200)
        assert len(s.messages) < 500, f"retained {len(s.messages)} of 5000 messages"
        assert len(s.messages) <= va.MAX_SESSION_MESSAGES

    def test_the_newest_messages_are_the_ones_kept(self):
        s = va.VoiceSession("henry")
        for i in range(va.MAX_SESSION_MESSAGES + 10):
            s.add_message("user", f"m{i}")
        contents = [m["content"] for m in s.messages]
        assert contents[-1] == f"m{va.MAX_SESSION_MESSAGES + 9}"
        assert "m0" not in contents

    def test_messages_carry_a_timestamp(self):
        s = va.VoiceSession("henry")
        s.add_message("user", "hi")
        datetime.fromisoformat(s.messages[0]["timestamp"])  # parses or raises


class TestSessionToDict:
    def test_hit_rate_on_an_untouched_session_is_zero_not_a_crash(self):
        # to_dict() is served straight out of a monitoring endpoint; a
        # ZeroDivisionError here is a 500 on a health surface.
        assert va.VoiceSession("henry").to_dict()["cache_hit_rate_percent"] == 0

    def test_hit_rate_is_hits_over_queries(self):
        s = va.VoiceSession("henry")
        s.total_queries = 8
        s.cache_hits = 2
        assert s.to_dict()["cache_hit_rate_percent"] == 25.0

    def test_reports_expiry_and_state(self):
        s = stale(va.VoiceSession("henry", session_ttl_seconds=1))
        s.state = va.SessionState.ERROR
        d = s.to_dict()
        assert d["is_expired"] is True
        assert d["state"] == "error"
        assert d["user_id"] == "henry"


class TestManagerLookup:
    def test_create_session_registers_it_under_its_own_id(self):
        m = manager()
        s = m.create_session("henry")
        assert m.sessions[s.session_id] is s
        assert m.get_session(s.session_id) is s

    def test_sessions_get_distinct_ids(self):
        m = manager()
        assert m.create_session("henry").session_id != m.create_session("henry").session_id

    def test_unknown_id_returns_none(self):
        assert manager().get_session("no-such-session") is None

    def test_expired_session_is_not_handed_back(self):
        m = manager()
        s = stale(m.create_session("henry"))
        assert m.get_session(s.session_id) is None

    def test_closed_session_is_not_handed_back(self):
        m = manager()
        s = m.create_session("henry")
        m.close_session(s.session_id)
        assert m.get_session(s.session_id) is None

    def test_get_user_session_reuses_the_live_one(self):
        m = manager()
        s = m.create_session("henry")
        assert m.get_user_session("henry") is s

    def test_get_user_session_does_not_cross_users(self):
        m = manager()
        m.create_session("henry")
        assert m.get_user_session("lexi").user_id == "lexi"

    def test_get_user_session_replaces_an_expired_one(self):
        m = manager()
        old = stale(m.create_session("henry"))
        new = m.get_user_session("henry")
        assert new is not old
        assert new.is_expired() is False

    def test_get_user_session_replaces_a_closed_one(self):
        m = manager()
        old = m.create_session("henry")
        m.close_session(old.session_id)
        assert m.get_user_session("henry") is not old


class TestClose:
    def test_closing_marks_the_state(self):
        m = manager()
        s = m.create_session("henry")
        assert m.close_session(s.session_id) is True
        assert s.state == va.SessionState.CLOSED

    def test_closing_an_unknown_id_is_false(self):
        # main.py turns this False into a 404, so it must not be True for an id
        # that was never issued.
        assert manager().close_session("no-such-session") is False


class TestCleanup:
    def test_cleanup_is_rate_limited(self):
        # Every read path calls it; sweeping the whole dict on each call would
        # be O(sessions) per request.
        m = manager()
        s = stale(m.create_session("henry"))
        m.get_stats()
        assert s.session_id in m.sessions

    def test_a_due_sweep_removes_expired_and_closed_sessions(self):
        m = manager()
        stale(m.create_session("henry"))
        closed = m.create_session("lexi")
        m.close_session(closed.session_id)
        live = m.create_session("sam")

        m.last_cleanup = 0  # make the sweep due
        m._cleanup_expired_sessions()

        assert set(m.sessions) == {live.session_id}

    def test_a_sweep_that_removes_nothing_still_rearms_the_timer(self):
        m = manager()
        m.create_session("henry")
        m.last_cleanup = 0
        m._cleanup_expired_sessions()
        assert m.last_cleanup > 0


class TestStats:
    def test_active_count_excludes_expired_sessions(self):
        """REGRESSION. active_sessions counted `state == ACTIVE` and nothing
        else. Cleanup is rate-limited, so an expired session sits in the dict
        for up to a cleanup interval -- and every other method in the class
        (get_session, get_user_session) already refuses to hand it back. The
        /voice/stats endpoint therefore advertised sessions as active that no
        caller could use.
        """
        m = manager()
        stale(m.create_session("henry"))
        live = m.create_session("lexi")

        stats = m.get_stats()
        assert m.get_session(live.session_id) is live
        assert stats["active_sessions"] == 1

    def test_active_count_excludes_closed_sessions(self):
        m = manager()
        closed = m.create_session("henry")
        m.close_session(closed.session_id)
        m.create_session("lexi")
        assert m.get_stats()["active_sessions"] == 1

    def test_global_hit_rate_on_an_idle_manager_is_zero_not_a_crash(self):
        assert manager().get_stats()["global_cache_hit_rate_percent"] == 0

    def test_global_hit_rate_pools_every_session(self):
        m = manager()
        a = m.create_session("henry")
        a.total_queries, a.cache_hits = 6, 3
        b = m.create_session("lexi")
        b.total_queries, b.cache_hits = 4, 1
        stats = m.get_stats()
        assert stats["total_queries_all_sessions"] == 10
        assert stats["total_cache_hits_all_sessions"] == 4
        assert stats["global_cache_hit_rate_percent"] == 40.0

    def test_ttl_is_reported_as_configured(self):
        assert manager(session_ttl_seconds=42).get_stats()["session_ttl_seconds"] == 42


class TestListSessions:
    def test_lists_everything_when_unfiltered(self):
        m = manager()
        m.create_session("henry")
        m.create_session("lexi")
        assert len(m.list_sessions()) == 2

    def test_filters_by_user(self):
        m = manager()
        m.create_session("henry")
        m.create_session("henry")
        m.create_session("lexi")
        listed = m.list_sessions(user_id="henry")
        assert len(listed) == 2
        assert {s["user_id"] for s in listed} == {"henry"}

    def test_unknown_user_lists_nothing(self):
        m = manager()
        m.create_session("henry")
        assert m.list_sessions(user_id="nobody") == []


class FakeCacheManager:
    """Stands in for PromptCacheManager; records what the prompt builder asks
    for so the enrichment can be inspected without a Gemini client."""

    def __init__(self):
        self.requests: list[dict] = []

    def create_cached_gemini_request(self, user_message, system_instruction, tool_declarations):
        self.requests.append({
            "user_message": user_message,
            "system_instruction": system_instruction,
            "tool_declarations": tool_declarations,
        })
        return ["cached-payload"]


class TestVoicePrompt:
    def test_delegates_to_the_cache_manager_when_one_is_wired_up(self):
        cache = FakeCacheManager()
        p = va.VoiceProcessor(cache_manager=cache)
        session = va.VoiceSession("henry")
        session.add_message("user", "who won last night")

        assert p.create_voice_prompt("and tonight?", session, "SYS", [{"name": "t"}]) == ["cached-payload"]
        sent = cache.requests[0]
        assert sent["user_message"] == "and tonight?"
        assert sent["tool_declarations"] == [{"name": "t"}]
        assert "SYS" in sent["system_instruction"]
        assert "USER: who won last night" in sent["system_instruction"]
        assert "under 50 words" in sent["system_instruction"]

    def test_does_not_record_the_users_turn(self):
        # main.py calls add_message("user", ...) itself, once, before the
        # provider fan-out; recording it here too would double the history.
        cache = FakeCacheManager()
        session = va.VoiceSession("henry")
        va.VoiceProcessor(cache_manager=cache).create_voice_prompt("hi", session, "SYS", [])
        assert session.messages == []

    def test_an_object_without_the_cache_method_is_ignored(self):
        """cache_manager is duck-typed in from main.py; an older or partial one
        must fall through to the plain builder rather than AttributeError."""
        p = va.VoiceProcessor(cache_manager=object())
        out = p.create_voice_prompt("hi", va.VoiceSession("henry"), "SYS", [])
        assert isinstance(out, list) and len(out) == 1
        assert out[0]["role"] == "user"

    def test_falls_back_to_a_plain_message_list(self):
        session = va.VoiceSession("henry")
        session.add_message("assistant", "earlier answer")

        out = va.VoiceProcessor().create_voice_prompt("hello", session, "SYS", [])
        assert len(out) == 1
        assert out[0]["role"] == "user"
        text = out[0]["parts"][0]["text"]
        assert "SYS" in text
        assert "ASSISTANT: earlier answer" in text
        assert text.endswith("User: hello")

    def test_the_fallback_no_longer_depends_on_an_sdk_being_installed(self):
        """This used to return None whenever `import google.generativeai`
        failed, and main.py handed that straight to generate_content(). The
        endpoint's except-Exception swallowed it, so the caller heard "I didn't
        understand that" and the log showed an opaque AttributeError rather than
        anything about a missing package. The previous test pinned that silent
        failure rather than fixing it.

        The builder only ever assembled dicts, which both Google SDKs accept,
        so it no longer imports one and there is no longer a path that returns
        None here.
        """
        import voice_assistant

        assert not hasattr(voice_assistant, "genai"), (
            "voice_assistant must not depend on an SDK to build a dict"
        )
        out = va.VoiceProcessor().create_voice_prompt("hi", va.VoiceSession("henry"), "SYS", [])
        assert out is not None
        assert out[0]["parts"][0]["text"].endswith("User: hi")


class TestQueryAccounting:
    def test_every_query_counts(self):
        s = va.VoiceSession("henry")
        p = va.VoiceProcessor()
        for _ in range(3):
            p.log_voice_query(s, "answer")
        assert s.total_queries == 3

    def test_only_a_nonzero_cached_token_count_is_a_hit(self):
        s = va.VoiceSession("henry")
        p = va.VoiceProcessor()
        p.log_voice_query(s, "a", cached_tokens=0)
        p.log_voice_query(s, "b", cached_tokens=512)
        assert (s.total_queries, s.cache_hits) == (2, 1)
        assert s.to_dict()["cache_hit_rate_percent"] == 50.0

    def test_a_short_reply_does_not_break_the_log_slice(self):
        # response_text[:50] on a reply shorter than 50 chars.
        s = va.VoiceSession("henry")
        va.VoiceProcessor().log_voice_query(s, "")
        assert s.total_queries == 1


def test_module_level_manager_is_configured_and_empty_at_import():
    # main.py imports this singleton directly; a stray session baked in at
    # import time would be shared across every user.
    assert isinstance(va.voice_session_manager, va.VoiceSessionManager)
    assert va.voice_session_manager.session_ttl == 900


@pytest.mark.parametrize("state", list(va.SessionState))
def test_session_states_serialize_as_plain_strings(state):
    # to_dict() emits state.value straight into a JSON response body.
    assert isinstance(state.value, str)
