"""Tests for the capabilities alert Henry gets by text.

trigger_capabilities_alert.py had no tests. It is a script whose only job is to
put an accurate message in front of Henry, so the failures that matter are the
quiet ones: a tool that disappears from the message, a send that never happened
being reported as fine, or a formatting crash that kills the whole alert.

Four live bugs were found while writing these and are pinned below:

* an ``unavailable`` tool with ``reason: None`` raised TypeError and killed the
  entire alert -- compute_tool_statuses() emits ``reason: None`` routinely;
* any tool whose status was not one of ready/unavailable/disabled was dropped
  from the body while still counting in the "N/M tools ready" denominator;
* every reason was suffixed with "..." whether or not it was truncated;
* the timestamp was hardcoded "CST" regardless of the machine's timezone;
* ``--dry-run`` was declared with ``default=True``, which made the flag dead and
  let ``--dry-run --send`` send a real iMessage.

Nothing here touches the network, the filesystem, or Messages.app: every send
path is monkeypatched and every write goes to tmp_path.
"""
from __future__ import annotations

import os
import time

import pytest

import trigger_capabilities_alert as alert


def tool(name: str, status: str = "ready", reason=None) -> dict:
    """One entry shaped like main.compute_tool_statuses() returns."""
    return {"tool_name": name, "description": f"{name} desc", "status": status, "reason": reason}


@pytest.fixture(autouse=True)
def no_real_sends(monkeypatch):
    """Hard stop: an unpatched send in any test raises instead of texting Henry."""
    def explode(*args, **kwargs):  # pragma: no cover - only runs if a test forgets
        raise AssertionError("test attempted a real iMessage send")

    monkeypatch.setattr(alert, "send_imessage", explode)


@pytest.fixture
def statuses(monkeypatch):
    """Install a fake compute_tool_statuses() and return its setter."""
    def use(rows):
        monkeypatch.setattr(alert, "compute_tool_statuses", lambda: list(rows))
        return rows

    return use


@pytest.fixture
def utc_clock():
    """Run the body with the process in UTC, then put the machine's TZ back."""
    original = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


class TestFormatCapabilitiesAlert:
    def test_ready_tools_are_listed_and_counted(self, statuses):
        statuses([tool("imessage_send"), tool("check_apple_calendar")])
        text = alert.format_capabilities_alert()
        assert "✅ READY (2):" in text
        assert "  • imessage_send" in text
        assert "  • check_apple_calendar" in text
        assert "📊 2/2 tools ready" in text

    def test_unavailable_tools_carry_their_reason(self, statuses):
        statuses([tool("odds", status="unavailable", reason="Missing ODDS_API_KEY")])
        text = alert.format_capabilities_alert()
        assert "❌ UNAVAILABLE (1):" in text
        assert "Missing ODDS_API_KEY" in text
        assert "📊 0/1 tools ready" in text

    def test_disabled_tools_are_listed(self, statuses):
        statuses([tool("voice", status="disabled", reason="Feature flag disabled")])
        text = alert.format_capabilities_alert()
        assert "⊘ DISABLED (1):" in text
        assert "  • voice" in text

    def test_sections_are_omitted_when_empty(self, statuses):
        statuses([tool("only_ready")])
        text = alert.format_capabilities_alert()
        assert "UNAVAILABLE" not in text
        assert "DISABLED" not in text
        assert "OTHER" not in text

    def test_empty_toolkit_does_not_crash(self, statuses):
        statuses([])
        assert "📊 0/0 tools ready" in alert.format_capabilities_alert()

    # --- pinned bugs -----------------------------------------------------

    def test_null_reason_does_not_kill_the_whole_alert(self, statuses):
        """Regression: reason=None raised TypeError, so nothing was ever sent.

        compute_tool_statuses() sets reason=None for anything with nothing to
        explain, so None is a legal value in this schema -- the formatter used
        to slice it unconditionally.
        """
        statuses([tool("broken", status="unavailable", reason=None)])
        text = alert.format_capabilities_alert()
        assert "  • broken (Unknown)" in text

    def test_missing_reason_key_does_not_kill_the_whole_alert(self, statuses):
        statuses([{"tool_name": "broken", "status": "unavailable"}])
        assert "  • broken (Unknown)" in alert.format_capabilities_alert()

    def test_unknown_status_is_reported_not_silently_dropped(self, statuses):
        """Regression: the denominator counted it, the body never named it.

        Henry saw "1/2 tools ready" with no way to tell which tool was the
        missing one or why -- the exact shape of a failure that looks fine.
        """
        statuses([
            tool("fine"),
            tool("deepseek", status="degraded", reason="Provider returned HTTP 401"),
        ])
        text = alert.format_capabilities_alert()
        assert "deepseek" in text
        assert "degraded" in text
        assert "📊 1/2 tools ready" in text

    def test_short_reason_is_not_marked_as_truncated(self, statuses):
        """Regression: every reason got a "..." suffix, truncated or not."""
        statuses([tool("odds", status="unavailable", reason="no key")])
        text = alert.format_capabilities_alert()
        assert "  • odds (no key)" in text
        assert "no key..." not in text

    def test_long_reason_is_truncated_and_marked(self, statuses):
        long_reason = "Missing ODDS_API_KEY environment variable; Missing XAI_API_KEY too"
        statuses([tool("odds", status="unavailable", reason=long_reason)])
        text = alert.format_capabilities_alert()
        assert long_reason not in text
        assert "..." in text

    def test_timestamp_uses_the_real_timezone_not_a_hardcoded_label(self, statuses, utc_clock):
        """Regression: the label was the literal string "CST" on every host."""
        statuses([tool("fine")])
        text = alert.format_capabilities_alert()
        assert "UTC" in text
        assert "CST" not in text


class TestSendAlert:
    def test_confirmed_send_returns_true_with_the_right_recipient(self, monkeypatch):
        calls = []
        monkeypatch.setattr(alert, "send_imessage", lambda p, m: calls.append((p, m)) or True)
        assert alert.send_alert("hello") is True
        assert calls == [(alert.HENRY_PHONE, "hello")]

    def test_refused_send_returns_false(self, monkeypatch):
        """send_imessage returns False when chat.db never confirmed the send."""
        monkeypatch.setattr(alert, "send_imessage", lambda p, m: False)
        assert alert.send_alert("hello") is False

    def test_raising_send_returns_false_and_is_logged(self, monkeypatch, caplog):
        def boom(phone, text):
            raise RuntimeError("osascript died")

        monkeypatch.setattr(alert, "send_imessage", boom)
        with caplog.at_level("ERROR", logger="ivy.capabilities_alert"):
            assert alert.send_alert("hello") is False
        assert "osascript died" in caplog.text

    def test_unimportable_sender_returns_false(self, monkeypatch):
        monkeypatch.setattr(alert, "send_imessage", None)
        assert alert.send_alert("hello") is False


class TestMain:
    def test_dry_run_sends_nothing(self, statuses, monkeypatch):
        statuses([tool("fine")])
        sends = []
        monkeypatch.setattr(alert, "send_imessage", lambda p, m: sends.append(m) or True)
        assert alert.main(dry_run=True) == 0
        assert sends == []

    def test_successful_send_exits_zero(self, statuses, monkeypatch):
        statuses([tool("fine")])
        sends = []
        monkeypatch.setattr(alert, "send_imessage", lambda p, m: sends.append(m) or True)
        assert alert.main(dry_run=False) == 0
        assert len(sends) == 1

    def test_failed_send_exits_nonzero(self, statuses, monkeypatch):
        statuses([tool("fine")])
        monkeypatch.setattr(alert, "send_imessage", lambda p, m: False)
        assert alert.main(dry_run=False) == 1

    def test_exactly_one_message_per_run(self, statuses, monkeypatch):
        """No retry loop: a run must never text Henry the same summary twice."""
        statuses([tool("fine")])
        sends = []
        monkeypatch.setattr(alert, "send_imessage", lambda p, m: sends.append(m) or True)
        alert.main(dry_run=False)
        assert len(sends) == 1

    def test_formatting_failure_exits_nonzero_without_sending(self, monkeypatch):
        def boom():
            raise ValueError("registry unreadable")

        monkeypatch.setattr(alert, "compute_tool_statuses", boom)
        # send_imessage is the exploding autouse stub: reaching it fails the test.
        assert alert.main(dry_run=False) == 1

    def test_writes_no_files(self, statuses, monkeypatch, tmp_path):
        """The alert is text-only; it must not leave state on disk."""
        statuses([tool("fine")])
        monkeypatch.setattr(alert, "send_imessage", lambda p, m: True)
        monkeypatch.chdir(tmp_path)
        assert alert.main(dry_run=False) == 0
        assert list(tmp_path.iterdir()) == []


class TestResolveDryRun:
    def parse(self, argv):
        return alert.build_parser().parse_args(argv)

    def test_no_flags_is_a_dry_run(self):
        assert alert.resolve_dry_run(self.parse([])) is True

    def test_send_actually_sends(self):
        assert alert.resolve_dry_run(self.parse(["--send"])) is False

    def test_dry_run_flag_is_a_dry_run(self):
        assert alert.resolve_dry_run(self.parse(["--dry-run"])) is True

    def test_explicit_dry_run_beats_send(self):
        """Regression: --dry-run was declared default=True, so args.dry_run was
        constant and ignored -- `--dry-run --send` sent a real iMessage."""
        assert alert.resolve_dry_run(self.parse(["--dry-run", "--send"])) is True
