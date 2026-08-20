"""Focused tests for the bounded, argv-only AppleScript runtime."""

from __future__ import annotations

import ast
import logging
import math
import subprocess
import threading
import time
from pathlib import Path

import pytest
from filelock import Timeout as FileLockTimeout

from utils import applescript
from utils.applescript import AppleScriptRunner


TRICKY_VALUE = (
    'quote " then newline\nemoji 🧪 and backslash \\ '
    'tell application "Finder"\ndo shell script "not executed"\nend tell'
)
PRIVATE_DETAIL = "raw-secret-contact-and-local-path-" + "/Users" + "/private/report.pdf"


def _successful_process(command: list[str], stdout: str = "SUCCESS\n") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


@pytest.mark.parametrize(
    ("method_name", "method_args", "script", "argv"),
    [
        (
            "fetch_calendar_events",
            (TRICKY_VALUE,),
            applescript.CALENDAR_EVENTS_ARGV_SCRIPT,
            [TRICKY_VALUE],
        ),
        (
            "fetch_reminders",
            (TRICKY_VALUE,),
            applescript.FETCH_REMINDERS_ARGV_SCRIPT,
            [TRICKY_VALUE],
        ),
        (
            "add_reminder",
            (TRICKY_VALUE, f"title: {TRICKY_VALUE}"),
            applescript.ADD_REMINDER_ARGV_SCRIPT,
            [TRICKY_VALUE, f"title: {TRICKY_VALUE}"],
        ),
        (
            "send_imessage_argv",
            (TRICKY_VALUE, f"body: {TRICKY_VALUE}"),
            applescript.SEND_TEXT_ARGV_SCRIPT,
            [TRICKY_VALUE, f"body: {TRICKY_VALUE}"],
        ),
        (
            "send_imessage",
            (TRICKY_VALUE, f"compat body: {TRICKY_VALUE}"),
            applescript.SEND_TEXT_ARGV_SCRIPT,
            [TRICKY_VALUE, f"compat body: {TRICKY_VALUE}"],
        ),
        (
            "send_imessage_file_argv",
            (TRICKY_VALUE, f"/tmp/{TRICKY_VALUE}"),
            applescript.SEND_FILE_ARGV_SCRIPT,
            [TRICKY_VALUE, f"/tmp/{TRICKY_VALUE}"],
        ),
    ],
)
def test_helpers_use_exact_fixed_argv_command(
    monkeypatch,
    method_name: str,
    method_args: tuple[str, ...],
    script: str,
    argv: list[str],
) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return _successful_process(command)

    monkeypatch.setattr(applescript.subprocess, "run", fake_run)
    runner = AppleScriptRunner(timeout=7.5)

    assert getattr(runner, method_name)(*method_args) == "SUCCESS"
    assert calls == [
        (
            [applescript.OSASCRIPT_EXECUTABLE, "-e", script, *argv],
            {
                "capture_output": True,
                "text": True,
                "timeout": 7.5,
                "check": False,
                "shell": False,
            },
        )
    ]
    assert TRICKY_VALUE not in script
    assert runner.last_error_category is None
    assert math.isfinite(runner.last_duration_seconds)
    assert runner.last_duration_seconds >= 0


def test_timeout_is_bounded_sanitized_and_recorded(monkeypatch, caplog) -> None:
    def raise_timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(
            command,
            kwargs["timeout"],
            output=PRIVATE_DETAIL,
            stderr=PRIVATE_DETAIL,
        )

    monkeypatch.setattr(applescript.subprocess, "run", raise_timeout)
    caplog.set_level(logging.ERROR, logger="ivy.applescript")
    runner = AppleScriptRunner(timeout=0.25)

    result = runner.send_imessage_argv("recipient", "message")

    assert result == "ERROR: AppleScript execution timed out."
    assert runner.last_error_category == applescript.ERROR_CATEGORY_TIMEOUT
    assert runner.last_duration_seconds >= 0
    assert PRIVATE_DETAIL not in result
    assert PRIVATE_DETAIL not in caplog.text


def test_nonzero_exit_does_not_leak_stdout_or_stderr(monkeypatch, caplog) -> None:
    def fail(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            17,
            stdout=f"ERROR: {PRIVATE_DETAIL}",
            stderr=PRIVATE_DETAIL,
        )

    monkeypatch.setattr(applescript.subprocess, "run", fail)
    caplog.set_level(logging.WARNING, logger="ivy.applescript")
    runner = AppleScriptRunner()

    result = runner.fetch_reminders("Household")

    assert result == "ERROR: AppleScript failed."
    assert runner.last_error_category == applescript.ERROR_CATEGORY_NONZERO_EXIT
    assert PRIVATE_DETAIL not in result
    assert PRIVATE_DETAIL not in caplog.text


def test_handled_applescript_error_is_sanitized(monkeypatch, caplog) -> None:
    def script_error(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"ERROR: {PRIVATE_DETAIL}",
            stderr=PRIVATE_DETAIL,
        )

    monkeypatch.setattr(applescript.subprocess, "run", script_error)
    caplog.set_level(logging.WARNING, logger="ivy.applescript")
    runner = AppleScriptRunner()

    result = runner.add_reminder("Household", "title")

    assert result == "ERROR: AppleScript failed."
    assert runner.last_error_category == applescript.ERROR_CATEGORY_APPLESCRIPT
    assert PRIVATE_DETAIL not in result
    assert PRIVATE_DETAIL not in caplog.text


def test_missing_binary_is_sanitized_and_categorized(monkeypatch, caplog) -> None:
    def missing_binary(*_args, **_kwargs):
        raise FileNotFoundError(PRIVATE_DETAIL)

    monkeypatch.setattr(applescript.subprocess, "run", missing_binary)
    caplog.set_level(logging.ERROR, logger="ivy.applescript")
    runner = AppleScriptRunner()

    result = runner.fetch_calendar_events("Hilla")

    assert result == "ERROR: AppleScript execution failed."
    assert runner.last_error_category == applescript.ERROR_CATEGORY_EXECUTABLE_NOT_FOUND
    assert PRIVATE_DETAIL not in result
    assert PRIVATE_DETAIL not in caplog.text


def test_other_subprocess_error_is_sanitized_and_categorized(monkeypatch, caplog) -> None:
    def subprocess_error(*_args, **_kwargs):
        raise OSError(PRIVATE_DETAIL)

    monkeypatch.setattr(applescript.subprocess, "run", subprocess_error)
    caplog.set_level(logging.ERROR, logger="ivy.applescript")
    runner = AppleScriptRunner()

    result = runner.fetch_reminders("Household")

    assert result == "ERROR: AppleScript execution failed."
    assert runner.last_error_category == applescript.ERROR_CATEGORY_SUBPROCESS
    assert PRIVATE_DETAIL not in result
    assert PRIVATE_DETAIL not in caplog.text


def test_cross_process_lock_timeout_is_bounded_and_sanitized(monkeypatch, caplog) -> None:
    def busy(*_args, **_kwargs):
        raise FileLockTimeout(PRIVATE_DETAIL)

    monkeypatch.setattr(applescript._PROCESS_FILE_LOCK, "acquire", busy)
    caplog.set_level(logging.ERROR, logger="ivy.applescript")
    runner = AppleScriptRunner(timeout=0.25)

    result = runner.send_imessage_argv("recipient", "message")

    assert result == "ERROR: AppleScript execution failed."
    assert runner.last_error_category == applescript.ERROR_CATEGORY_BUSY
    assert PRIVATE_DETAIL not in result
    assert PRIVATE_DETAIL not in caplog.text


def test_success_clears_previous_error_telemetry(monkeypatch) -> None:
    outcomes = iter(
        [
            subprocess.CompletedProcess([], 1, stdout="", stderr=PRIVATE_DETAIL),
            subprocess.CompletedProcess([], 0, stdout="SUCCESS", stderr=""),
        ]
    )
    monkeypatch.setattr(applescript.subprocess, "run", lambda *_args, **_kwargs: next(outcomes))
    runner = AppleScriptRunner()

    assert runner.fetch_reminders("Household") == "ERROR: AppleScript failed."
    assert runner.last_error_category == applescript.ERROR_CATEGORY_NONZERO_EXIT

    assert runner.fetch_reminders("Household") == "SUCCESS"
    assert runner.last_error_category is None
    assert runner.last_telemetry.error_category is None


@pytest.mark.parametrize("timeout", [None, 0, -1, float("inf"), float("nan")])
def test_timeout_must_remain_finite_and_positive(timeout) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        AppleScriptRunner(timeout=timeout)

    runner = AppleScriptRunner()
    with pytest.raises(ValueError, match="finite positive"):
        runner.timeout = timeout


def test_calls_are_serialized_across_runner_instances(monkeypatch) -> None:
    first_call_entered = threading.Event()
    release_first_call = threading.Event()
    state_lock = threading.Lock()
    state = {"active": 0, "maximum_active": 0, "call_count": 0}

    def controlled_run(command, **_kwargs):
        with state_lock:
            state["active"] += 1
            state["call_count"] += 1
            state["maximum_active"] = max(state["maximum_active"], state["active"])
            call_number = state["call_count"]
        if call_number == 1:
            first_call_entered.set()
            assert release_first_call.wait(timeout=1)
        with state_lock:
            state["active"] -= 1
        return _successful_process(command)

    monkeypatch.setattr(applescript.subprocess, "run", controlled_run)
    first_runner = AppleScriptRunner()
    second_runner = AppleScriptRunner()
    results = []

    first = threading.Thread(
        target=lambda: results.append(first_runner.fetch_reminders("one"))
    )
    second = threading.Thread(
        target=lambda: results.append(second_runner.fetch_reminders("two"))
    )
    first.start()
    assert first_call_entered.wait(timeout=1)
    second.start()

    time.sleep(0.05)
    with state_lock:
        assert state["call_count"] == 1

    release_first_call.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert sorted(results) == ["SUCCESS", "SUCCESS"]
    assert state["call_count"] == 2
    assert state["maximum_active"] == 1


def test_production_modules_do_not_bypass_central_applescript_runner() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    sources = {
        path: (repo_root / path).read_text(encoding="utf-8")
        for path in (
            "main.py",
            "Hen_Lex.py",
            "ivy_core/messaging.py",
            "mcp-servers/imessage/server.py",
        )
    }

    for path, source in sources.items():
        for node in ast.walk(ast.parse(source, filename=path)):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            function = node.func
            if not (
                isinstance(function, ast.Attribute)
                and function.attr in {"run", "Popen"}
                and isinstance(function.value, ast.Name)
                and function.value.id == "subprocess"
            ):
                continue
            argv = node.args[0]
            if not isinstance(argv, (ast.List, ast.Tuple)) or not argv.elts:
                continue
            executable = argv.elts[0]
            assert not (
                isinstance(executable, ast.Constant)
                and executable.value in {"osascript", "/usr/bin/osascript"}
            ), path

    assert "_runner.send_imessage_argv" in sources["mcp-servers/imessage/server.py"]
