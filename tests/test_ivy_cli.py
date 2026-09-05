"""Tests for the ivy command-line entry point.

ivy_cli.py is a top-level script, not a module -- everything runs at import
time -- so each test executes it with runpy under a stubbed job_runner and a
stubbed agent. Nothing here can reach the real registry, the real launchd
jobs, the real .env, or the network: `main` is never the real module, and
os.chdir is intercepted so the test process never leaves its own directory.

What is worth pinning is the dispatch: which words route to the job runner,
which fall through to the agent, and what each path exits with. `ivy run x`
exiting 0 on a failed job would make a broken picks run look successful.
"""
from __future__ import annotations

import os
import runpy
import sys
import types
from pathlib import Path

import pytest

IVY_CLI = Path(__file__).resolve().parent.parent / "ivy_cli.py"


class Status:
    """Stands in for job_runner.JobStatus -- the CLI only reads `.name`."""

    def __init__(self, name: str):
        self.name = name


def job(
    display_name: str = "Sharp Picks",
    description: str = "Run daily sports picks job",
    aliases: str = "picks, sharp picks",
    *,
    available: bool = True,
    unavailable_reason: str | None = None,
) -> dict:
    """One entry shaped exactly like JobRunner.list_jobs() returns."""
    return {
        "name": display_name.lower().replace(" ", "_"),
        "display_name": display_name,
        "description": description,
        "aliases": aliases,
        "schedule": "On-demand",
        "available": available,
        "unavailable_reason": unavailable_reason,
    }


class FakeJobRunner:
    def __init__(self, jobs: list[dict], result: tuple[Status, str]):
        self._jobs = jobs
        self._result = result
        self.run_calls: list[str] = []

    def list_jobs(self) -> list[dict]:
        return self._jobs

    def run_job(self, job_name: str):
        self.run_calls.append(job_name)
        return self._result


class FakeAgent:
    """Callable stand-in for main.query_llm_with_tools."""

    def __init__(self, reply, noise: str):
        self._reply = reply
        self._noise = noise
        self.prompts: list[str] = []

    def __call__(self, prompt: str):
        self.prompts.append(prompt)
        if self._noise:
            print(self._noise)
        return self._reply


class CliRun:
    def __init__(self, exit_code, out, runner, agent, chdirs):
        self.exit_code = exit_code
        self.out = out
        self.runner = runner
        self.agent = agent
        self.chdirs = chdirs


@pytest.fixture
def run_cli(monkeypatch, capsys):
    def _run(
        *argv: str,
        jobs: list[dict] | None = None,
        result: tuple[Status, str] = (Status("SUCCESS"), "Sharp Picks started"),
        reply="the answer",
        noise: str = "",
    ) -> CliRun:
        runner = FakeJobRunner(jobs if jobs is not None else [job()], result)
        job_module = types.ModuleType("job_runner")
        job_module.job_runner = runner

        agent = FakeAgent(reply, noise)
        main_module = types.ModuleType("main")
        main_module.query_llm_with_tools = agent

        monkeypatch.setitem(sys.modules, "job_runner", job_module)
        monkeypatch.setitem(sys.modules, "main", main_module)
        monkeypatch.setattr(sys, "argv", ["ivy", *argv])

        chdirs: list[str] = []
        monkeypatch.setattr(os, "chdir", chdirs.append)

        exit_code = None
        try:
            runpy.run_path(str(IVY_CLI), run_name="ivy_cli_under_test")
        except SystemExit as exc:
            exit_code = exc.code
        return CliRun(exit_code, capsys.readouterr().out, runner, agent, chdirs)

    return _run


def test_runs_from_the_project_root_so_local_imports_resolve(run_cli):
    # `import main` and .env loading both depend on the cwd, not on sys.path.
    run = run_cli("list")
    assert run.chdirs == [str(IVY_CLI.parent)]


class TestList:
    def test_prints_each_job_with_its_description_and_aliases(self, run_cli):
        run = run_cli(
            "list",
            jobs=[job("Sharp Picks", "Analyzes matchups", "picks, sports picks"),
                  job("Bravo Scout", "Reality morning brief", "bravo")],
        )
        assert "Sharp Picks" in run.out
        assert "Analyzes matchups" in run.out
        assert "picks, sports picks" in run.out
        assert "Bravo Scout" in run.out
        assert "Reality morning brief" in run.out

    def test_exits_zero(self, run_cli):
        assert run_cli("list").exit_code == 0

    def test_unavailable_jobs_are_shown_not_hidden(self, run_cli):
        run = run_cli(
            "list",
            jobs=[job("Meal Planner", available=False, unavailable_reason="launchd plist was never installed")],
        )
        assert "Meal Planner" in run.out
        assert "UNAVAILABLE" in run.out
        assert "launchd plist was never installed" in run.out

    def test_unavailable_without_a_recorded_reason_still_explains_itself(self, run_cli):
        run = run_cli("list", jobs=[job("Meal Planner", available=False, unavailable_reason=None)])
        assert "no reason recorded" in run.out

    def test_available_jobs_carry_no_warning(self, run_cli):
        assert "UNAVAILABLE" not in run_cli("list", jobs=[job("Sharp Picks")]).out

    def test_empty_registry_is_not_an_error(self, run_cli):
        run = run_cli("list", jobs=[])
        assert run.exit_code == 0

    def test_does_not_start_the_agent(self, run_cli):
        # Listing jobs must not spin up the dual-brain agent or its API calls.
        assert run_cli("list").agent.prompts == []


class TestRun:
    def test_dispatches_the_named_job(self, run_cli):
        assert run_cli("run", "sharp_picks").runner.run_calls == ["sharp_picks"]

    def test_multi_word_job_names_are_rejoined(self, run_cli):
        """`./ivy run happy hour` arrives as two argv entries.

        Taking only args[1] would look up "happy" and miss the job.
        """
        assert run_cli("run", "happy", "hour").runner.run_calls == ["happy hour"]

    def test_prints_the_runners_message(self, run_cli):
        run = run_cli("run", "picks", result=(Status("SUCCESS"), "Sharp Picks started in the background"))
        assert "Sharp Picks started in the background" in run.out

    def test_success_exits_zero(self, run_cli):
        assert run_cli("run", "picks", result=(Status("SUCCESS"), "ok")).exit_code == 0

    @pytest.mark.parametrize("status", ["NOT_FOUND", "UNAVAILABLE", "ERROR", "ALREADY_RUNNING"])
    def test_every_non_success_status_exits_one(self, run_cli, status):
        # A launchd wrapper or a shell caller reads the exit code, not the text.
        run = run_cli("run", "picks", result=(Status(status), "did not run"))
        assert run.exit_code == 1
        assert "did not run" in run.out

    def test_missing_job_name_is_a_usage_error_that_dispatches_nothing(self, run_cli):
        run = run_cli("run")
        assert run.runner.run_calls == []
        assert isinstance(run.exit_code, str) and "usage" in run.exit_code

    def test_does_not_start_the_agent(self, run_cli):
        assert run_cli("run", "picks").agent.prompts == []

    def test_run_as_the_first_word_always_means_job_dispatch(self, run_cli):
        """`./ivy run the numbers for me` is a job lookup, never a question.

        Documented quirk: the first word decides, so the whole remainder is
        taken as a job name and the runner reports it as not found.
        """
        run = run_cli("run", "the", "numbers", "for", "me", result=(Status("NOT_FOUND"), "not found"))
        assert run.runner.run_calls == ["the numbers for me"]
        assert run.agent.prompts == []
        assert run.exit_code == 1


class TestHelp:
    @pytest.mark.parametrize("flag", ["help", "-h", "--help"])
    def test_help_prints_usage_and_exits_zero(self, run_cli, flag):
        run = run_cli(flag)
        assert run.exit_code == 0
        assert "Ivy's terminal interface" in run.out
        assert run.agent.prompts == []

    def test_no_arguments_prints_usage_and_exits_zero(self, run_cli):
        run = run_cli()
        assert run.exit_code == 0
        assert "Ivy's terminal interface" in run.out
        assert run.agent.prompts == []

    def test_whitespace_only_argument_is_treated_as_no_prompt(self, run_cli):
        run = run_cli("   ")
        assert run.exit_code == 0
        assert run.agent.prompts == []


class TestQueryMode:
    def test_words_are_joined_into_one_prompt(self, run_cli):
        run = run_cli("what's", "on", "my", "calendar", "today?")
        assert run.agent.prompts == ["what's on my calendar today?"]

    def test_prints_the_answer(self, run_cli):
        assert "Two events today" in run_cli("calendar", reply="Two events today").out

    def test_the_agents_reasoning_chain_is_suppressed(self, run_cli):
        """Only the final answer belongs on stdout.

        The agent narrates tool calls as it goes; the CLI exists to hand back
        one clean line, and callers pipe this.
        """
        run = run_cli("calendar", reply="Two events today", noise="TOOL CALL: get_calendar(...)")
        assert "Two events today" in run.out
        assert "TOOL CALL" not in run.out

    def test_stdout_is_usable_again_after_the_agent_runs(self, run_cli):
        # The redirect must not outlive the call, or the answer goes to /dev/null.
        assert run_cli("calendar", reply="visible", noise="hidden").out.strip() == "visible"

    @pytest.mark.parametrize("reply", [None, ""])
    def test_an_empty_answer_says_so_rather_than_printing_nothing(self, run_cli, reply):
        assert "No response." in run_cli("calendar", reply=reply).out

    def test_dispatch_matches_the_whole_first_word_not_a_prefix(self, run_cli):
        """"listen to my voicemail" is a question, not `ivy list`."""
        run = run_cli("listen", "to", "my", "voicemail")
        assert run.agent.prompts == ["listen to my voicemail"]
        assert run.runner.run_calls == []

    def test_a_question_containing_run_is_still_a_question(self, run_cli):
        run = run_cli("did", "the", "picks", "run", "today?")
        assert run.agent.prompts == ["did the picks run today?"]
        assert run.runner.run_calls == []
