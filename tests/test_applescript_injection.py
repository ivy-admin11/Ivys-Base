"""AppleScript injection guards for the Apple Reminders tools.

Before 2026-09-02 ``fetch_apple_reminders`` and ``add_apple_reminder`` built
their AppleScript with f-strings, so a list name or reminder title containing a
double quote closed the string literal early and everything after it became
executable script text. Both arguments come from LLM tool calls, which are
derived from inbound iMessage text, so the payload was attacker-supplied.

Both now pass untrusted content as process argv (see :mod:`utils.applescript`),
which no crafted input can escape: the script source is a fixed constant.
"""

import subprocess
import sys

import pytest

import main
from utils import applescript
from utils.applescript import REMINDERS_ADD_ARGV_SCRIPT, REMINDERS_FETCH_ARGV_SCRIPT

# Closes the string literal, ends the tell block, and opens a new one against a
# different app — exactly what the old f-string code would have executed.
# Deliberately avoids the words the auto-categoriser rewrites (meal/food/dinner/
# recipe/taco/house/chore/clean/task) so the payload reaches the script intact.
INJECTION_LIST = (
    'Errands"\n'
    '        end tell\n'
    '        tell application "Finder"\n'
    '            return "PWNED'
)
INJECTION_TITLE = (
    'Milk (1 gal)" }\n'
    '            make new reminder with properties {name:"PWNED'
)


@pytest.fixture
def captured_argv(monkeypatch):
    """Capture the exact argv osascript would have been invoked with."""
    calls = []

    class _Result:
        returncode = 0
        stdout = "SUCCESS"
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _only_call(captured):
    assert len(captured) == 1
    return captured[0]


def _assert_argv(cmd, constant, expected_args):
    """The whole safety contract in one place.

    Asserts the exact argv vector, not just membership: order matters because
    the scripts read `item 1 of argv` / `item 2 of argv`, and a transposition
    would silently swap a reminder's title with its list name.
    """
    assert cmd[0] == "osascript" and cmd[1] == "-e"
    script_source = cmd[2]
    # Source is the fixed module constant, byte for byte — never built per call.
    # This is what catches a revert to f-strings even if utils/ is left intact.
    assert script_source == constant
    # "--" must terminate option parsing, or an argument beginning with "-"
    # becomes an osascript option instead of data (see run_argv).
    assert cmd[3] == "--", "argv must be terminated with -- before user data"
    assert cmd[4:] == expected_args
    for payload in expected_args:
        assert payload not in script_source
    assert "PWNED" not in script_source


def test_add_reminder_never_interpolates_title_into_script_source(captured_argv):
    main.add_apple_reminder(INJECTION_TITLE, list_name="Household")
    cmd, _ = _only_call(captured_argv)
    _assert_argv(cmd, REMINDERS_ADD_ARGV_SCRIPT, ["Household", INJECTION_TITLE])


def test_add_reminder_never_interpolates_list_name_into_script_source(captured_argv):
    main.add_apple_reminder("Milk", list_name=INJECTION_LIST)
    cmd, _ = _only_call(captured_argv)
    # The payload is also clamped away by the allowlist — belt and braces.
    _assert_argv(cmd, REMINDERS_ADD_ARGV_SCRIPT, ["Household", "Milk"])


def test_fetch_reminders_never_interpolates_list_name_into_script_source(captured_argv):
    main.fetch_apple_reminders(INJECTION_LIST)
    cmd, _ = _only_call(captured_argv)
    _assert_argv(cmd, REMINDERS_FETCH_ARGV_SCRIPT, ["Household"])


def test_dash_leading_value_cannot_become_a_second_osascript_option(captured_argv):
    """The bug an adversarial review caught after the first fix.

    argv passing alone only moved the injection: with no "--", a list name of
    "-e" is read as an option and the title becomes a SECOND script fragment.
    An AppleScript `property` initialiser runs at load time, before the run
    handler, so `do shell script` executes even though argv is then empty.
    """
    payload = 'property pwn : (do shell script "echo owned")'
    main.add_apple_reminder(payload, list_name="-e")
    cmd, _ = _only_call(captured_argv)

    assert cmd[3] == "--", "no -- means -e is parsed as an option"
    assert cmd.count("-e") == 1, "a second -e would concatenate a new script"
    # Everything after -- is inert data, and the bogus list was clamped away.
    assert cmd[4:] == ["Household", payload]


def test_reminder_list_is_clamped_to_the_allowlist(captured_argv):
    """Both Gemini paths clamped list_name; the DeepSeek path — the primary
    brain — did not, so an inbound text could name any list and the add script
    would create it. Enforced in the handler now, so every provider shares it."""
    main.add_apple_reminder("Milk", list_name="Someone Else's Private List")
    cmd, _ = _only_call(captured_argv)
    assert cmd[4] == "Household"


def test_meal_plan_routing_still_works(captured_argv):
    """The allowlist must not break the existing keyword auto-categoriser."""
    main.add_apple_reminder("Flank steak (1.5 lbs)", list_name="dinner")
    cmd, _ = _only_call(captured_argv)
    assert cmd[4] == "Meal Plan"


def test_applescript_calls_carry_a_timeout(captured_argv):
    """A bare subprocess.run with no timeout on the poller thread is how a
    hung Apple app wedges all inbound iMessage handling."""
    main.add_apple_reminder("Milk")
    _, kwargs = _only_call(captured_argv)
    assert kwargs["timeout"] == applescript.DEFAULT_TIMEOUT_S


@pytest.mark.parametrize(
    "script", [REMINDERS_FETCH_ARGV_SCRIPT, REMINDERS_ADD_ARGV_SCRIPT],
    ids=["fetch", "add"],
)
def test_reminders_scripts_are_argv_based_with_no_format_placeholders(script):
    assert script.lstrip().startswith("on run argv")
    # The only braces may be the two AppleScript record literals; anything else
    # would mean someone reintroduced an interpolated placeholder.
    residue = script.replace("{name:listNameValue}", "").replace("{name:titleValue}", "")
    assert "{" not in residue and "%s" not in residue


@pytest.mark.skipif(sys.platform != "darwin", reason="osacompile only exists on macOS")
@pytest.mark.parametrize(
    "script", [REMINDERS_FETCH_ARGV_SCRIPT, REMINDERS_ADD_ARGV_SCRIPT],
    ids=["fetch", "add"],
)
def test_reminders_scripts_compile(script, tmp_path):
    """Compile-only (no Reminders access, no Automation consent needed).

    Guards the `list <variable>` form these use in place of `list "<literal>"`.
    """
    src = tmp_path / "s.applescript"
    src.write_text(script)
    res = subprocess.run(
        ["osacompile", "-o", str(tmp_path / "out.scpt"), str(src)],
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 0, res.stderr


def test_fetch_reminders_reports_failure_instead_of_claiming_empty(monkeypatch):
    """A failed read used to be indistinguishable from an empty list."""
    monkeypatch.setattr(
        main._GATEWAY_APPLESCRIPT, "fetch_reminders_argv",
        lambda list_name: 'ERROR: Reminders got an error: Can\'t get list "Nope".',
    )
    out = main.fetch_apple_reminders("Nope")
    assert "No active reminders found" not in out
    assert "Couldn't read" in out


def test_fetch_reminders_still_reports_a_genuinely_empty_list(monkeypatch):
    monkeypatch.setattr(
        main._GATEWAY_APPLESCRIPT, "fetch_reminders_argv", lambda list_name: ""
    )
    assert main.fetch_apple_reminders("Household") == "No active reminders found."


def test_add_reminder_requires_an_exact_success_receipt(monkeypatch):
    """The old substring test would call this a success: the error message
    quotes a title that happens to contain the word SUCCESS."""
    monkeypatch.setattr(
        main._GATEWAY_APPLESCRIPT, "add_reminder_argv",
        lambda list_name, title: 'ERROR: Can\'t make reminder "SUCCESS party"',
    )
    out = main.add_apple_reminder("SUCCESS party")
    assert out.startswith("❌")


def test_add_reminder_reports_success_on_exact_receipt(monkeypatch):
    monkeypatch.setattr(
        main._GATEWAY_APPLESCRIPT, "add_reminder_argv",
        lambda list_name, title: "SUCCESS",
    )
    out = main.add_apple_reminder("Milk (1 gal)")
    assert out.startswith("✅") and "Milk (1 gal)" in out


def test_calendar_read_cannot_hang_forever(monkeypatch):
    """check_apple_calendar had a bare subprocess.run() with no timeout; it
    runs on the poller thread, so a wedged osascript wedged inbound iMessage."""
    monkeypatch.setattr(
        main._GATEWAY_APPLESCRIPT, "run",
        lambda script: "ERROR: AppleScript execution timed out.",
    )
    out = main.check_apple_calendar("today")
    assert "AppleScript Database Error" in out


def test_deepseek_tool_dispatch_passes_the_inbound_message(monkeypatch):
    """Regression: committed code called `_execute_tool_call(..., inbound_text=text)`
    inside `execute_deepseek_call`, whose parameter is `text_content`. Every
    DeepSeek tool call therefore raised NameError and came back as
    "❌ DeepSeek Execution Layer Exception: name 'text' is not defined" —
    silently disabling tool use on the PRIMARY brain.
    """
    class Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"tool_calls": [
                {"function": {"name": "fetch_apple_reminders", "arguments": "{}"}}]}}]}

    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp())
    monkeypatch.setattr(main, "DEEPSEEK_API_KEY", "fake", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake")
    monkeypatch.setattr(
        main._GATEWAY_APPLESCRIPT, "fetch_reminders_argv", lambda list_name: "Milk, Eggs"
    )

    seen = {}
    real = main._execute_tool_call

    def spy(tool_name, tool_args, inbound_text=""):
        seen["inbound_text"] = inbound_text
        return real(tool_name, tool_args, inbound_text=inbound_text)

    monkeypatch.setattr(main, "_execute_tool_call", spy)

    out = main.execute_deepseek_call("what's on my list?", "sys")
    assert "not defined" not in out
    assert out == "Milk, Eggs"
    # The inbound message must reach the re-run guard, not an undefined name.
    assert seen["inbound_text"] == "what's on my list?"
