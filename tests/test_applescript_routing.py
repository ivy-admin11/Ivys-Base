"""Tests proving Apple integration functions route through the shared
utils.applescript.AppleScriptRunner instead of calling subprocess directly.

Covers:
- all four functions use the shared utility (applescript_runner)
- quotes and backslashes are escaped safely when embedded in script source
- timeouts and runner errors degrade gracefully (no exceptions raised)
- iMessage content is passed through argv rather than source interpolation
"""

from unittest.mock import patch

import main


@patch.object(main.applescript_runner, "run")
def test_check_apple_calendar_uses_safe_runner(mock_run):
    mock_run.return_value = ""
    result = main.check_apple_calendar("today")

    mock_run.assert_called_once()
    assert result == "Your Hilla calendar has no upcoming events listed."


@patch.object(main.applescript_runner, "run")
def test_fetch_reminders_escapes_list_name(mock_run):
    mock_run.return_value = "Buy milk"

    result = main.fetch_apple_reminders('Household" & do shell script "bad')

    script = mock_run.call_args.args[0]
    assert '\\"' in script
    assert result == "Buy milk"


@patch.object(main.applescript_runner, "run")
def test_fetch_reminders_empty_list_is_distinct_from_error(mock_run):
    mock_run.return_value = ""

    result = main.fetch_apple_reminders()

    assert result == "No active reminders found."


@patch.object(main.applescript_runner, "run")
def test_add_reminder_escapes_title(mock_run):
    mock_run.return_value = "SUCCESS"

    result = main.add_apple_reminder('Milk "and eggs"', "Household")

    script = mock_run.call_args.args[0]
    assert 'Milk \\"and eggs\\"' in script
    assert result.startswith("✅ Added")


@patch.object(main.applescript_runner, "run")
def test_add_reminder_preserves_auto_categorization(mock_run):
    mock_run.return_value = "SUCCESS"

    result = main.add_apple_reminder("Buy tacos", "taco night")

    script = mock_run.call_args.args[0]
    assert 'list "Meal Plan"' in script
    assert "Meal Plan" in result


@patch.object(main.applescript_runner, "run")
def test_add_reminder_requires_exact_success(mock_run):
    mock_run.return_value = "SUCCESS but with extra text"

    result = main.add_apple_reminder("Milk", "Household")

    assert result.startswith("❌ Reminders Integration Error:")


@patch.object(main.applescript_runner, "send_imessage_argv")
def test_local_imessage_uses_argv_safe_runner(mock_send):
    mock_send.return_value = "SUCCESS"

    result = main.run_local_applescript_send(
        "+14695550123",
        'Message with "quotes" and \\ backslashes',
    )

    mock_send.assert_called_once_with(
        "+14695550123",
        'Message with "quotes" and \\ backslashes',
    )
    assert result == "SUCCESS"


@patch.object(main.applescript_runner, "run")
def test_reminder_runner_error_is_returned_cleanly(mock_run):
    mock_run.return_value = "ERROR: AppleScript execution timed out."

    result = main.fetch_apple_reminders()

    assert result == (
        "❌ Reminders Integration Error: "
        "ERROR: AppleScript execution timed out."
    )


@patch.object(main.applescript_runner, "run")
def test_add_reminder_runner_error_degrades_gracefully(mock_run):
    mock_run.return_value = "ERROR: AppleScript execution failed."

    result = main.add_apple_reminder("Milk", "Household")

    assert result == (
        "❌ Reminders Integration Error: "
        "ERROR: AppleScript execution failed."
    )
