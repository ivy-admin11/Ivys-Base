"""Which Reminders list Ivy writes to, and saying so.

Henry asked for a recipe on "our reminders list". The allowlist held only
("Household", "Meal Plan"); his shared list is "Recipes / Grocery". The
unrecognised name was silently rewritten to "Household", the add script
created that list because it did not exist, and Ivy replied "✅ Added". The
item existed, in a brand-new list nobody opens.

Three separate faults, each individually harmless, and the reply said success
throughout — the pattern this codebase keeps producing.
"""
from __future__ import annotations

import pytest

import main


@pytest.fixture
def reminders(monkeypatch):
    """Stand in for the AppleScript runner, recording the list actually used."""
    calls = {"added": [], "enumerated": 0}

    class Runner:
        def __init__(self, result="SUCCESS", lists="Household, Recipes / Grocery"):
            self.result, self.lists = result, lists

        def add_reminder_argv(self, list_name, title):
            calls["added"].append((list_name, title))
            return self.result

        def fetch_reminders_argv(self, list_name):
            calls["added"].append((list_name, None))
            return "milk"

        def list_reminder_lists(self):
            calls["enumerated"] += 1
            return self.lists

    def _install(result="SUCCESS", lists="Household, Recipes / Grocery"):
        monkeypatch.setattr(main, "_GATEWAY_APPLESCRIPT", Runner(result, lists))
        return calls
    return _install


class TestTheSharedListIsReachable:
    def test_the_real_shared_list_is_allowed(self):
        """It was not, which is the whole bug."""
        assert "Recipes / Grocery" in main._ALLOWED_REMINDER_LISTS

    @pytest.mark.parametrize("asked", [
        "Recipes / Grocery", "recipes / grocery", "recipes/grocery",
        "Recipes/Grocery", "  recipes / grocery  ",
    ])
    def test_it_resolves_however_it_is_typed(self, asked):
        assert main._resolve_reminder_list(asked)[0] == "Recipes / Grocery"

    def test_a_recipe_request_lands_there(self, reminders):
        calls = reminders()
        main.add_apple_reminder("Chicken teriyaki broccoli bowls", "recipes")
        assert calls["added"][0][0] == "Recipes / Grocery"

    def test_the_allowlist_is_configurable(self, monkeypatch):
        """A hardcoded pair is what made a real list unreachable."""
        import importlib
        monkeypatch.setenv("IVY_REMINDER_LISTS", "Alpha,Beta Gamma")
        importlib.reload(main)
        try:
            assert main._ALLOWED_REMINDER_LISTS == ("Alpha", "Beta Gamma")
        finally:
            monkeypatch.delenv("IVY_REMINDER_LISTS")
            importlib.reload(main)


class TestNoSilentRedirect:
    def test_an_unknown_list_is_refused_not_rewritten(self, reminders):
        """The old code sent anything unrecognised to Household."""
        calls = reminders()
        out = main.add_apple_reminder("something", "Wine Cellar")
        assert calls["added"] == [], "nothing should have been written"
        assert "don't have a list called 'Wine Cellar'" in out

    def test_the_refusal_names_what_is_available(self, reminders):
        reminders()
        out = main.add_apple_reminder("x", "Wine Cellar")
        for name in main._ALLOWED_REMINDER_LISTS:
            assert name in out

    def test_a_fuzzy_match_is_disclosed(self, reminders):
        reminders()
        out = main.add_apple_reminder("x", "grocery")
        assert "Recipes / Grocery" in out
        assert "matched" in out

    def test_reads_refuse_an_unknown_list_too(self, reminders):
        calls = reminders()
        out = main.fetch_apple_reminders("Wine Cellar")
        assert calls["added"] == []
        assert "don't have a list called" in out

    def test_an_arbitrary_name_still_cannot_reach_reminders(self, reminders):
        """The allowlist is a security control: an inbound iMessage must not
        be able to name any list it likes."""
        calls = reminders()
        main.add_apple_reminder("x", "../../etc/passwd")
        assert calls["added"] == []


class TestMissingListIsReported:
    def test_a_missing_list_is_not_created(self, reminders):
        """The add script used to make one, so the write 'succeeded' into a
        brand-new empty list."""
        from utils.applescript import REMINDERS_ADD_ARGV_SCRIPT
        assert "make new list" not in REMINDERS_ADD_ARGV_SCRIPT

    def test_it_says_the_list_does_not_exist(self, reminders):
        reminders(result="ERROR: NO_SUCH_LIST")
        out = main.add_apple_reminder("x", "Household")
        assert "no 'Household' list" in out
        assert "didn't add anything" in out

    def test_it_reports_the_real_lists_on_that_mac(self, reminders):
        calls = reminders(result="ERROR: NO_SUCH_LIST",
                          lists="Recipes / Grocery, Shopping")
        out = main.add_apple_reminder("x", "Household")
        assert "Recipes / Grocery, Shopping" in out
        assert calls["enumerated"] == 1

    def test_enumeration_is_not_run_on_the_happy_path(self, reminders):
        """Enumerating lists is the slow call that caused the timeouts."""
        calls = reminders()
        main.add_apple_reminder("x", "Household")
        assert calls["enumerated"] == 0

    def test_an_unreadable_list_of_lists_is_still_honest(self, reminders):
        reminders(result="ERROR: NO_SUCH_LIST", lists="ERROR: timed out")
        out = main.add_apple_reminder("x", "Household")
        assert "couldn't read" in out


class TestTheReplySaysWhereItWent:
    def test_success_names_the_list_used(self, reminders):
        reminders()
        assert "'Recipes / Grocery'" in main.add_apple_reminder("teriyaki", "recipes")

    def test_a_generic_error_is_passed_through(self, reminders):
        reminders(result="ERROR: AppleScript execution timed out after 75s.")
        out = main.add_apple_reminder("x", "Household")
        assert "timed out" in out
