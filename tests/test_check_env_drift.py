"""Tests for the live-environment drift check.

scripts/check_env_drift.py is the only thing standing between a quietly
mutated venv and a production run, and it had no tests. Its failure mode is
the dangerous kind: it prints "OK" and exits 0, so a miss looks exactly like a
clean environment. Four live bugs were found while writing these -- all in
check_declared_vs_installed, all from hand-rolled requirement parsing -- and
each is pinned below.

Nothing here touches the real venv: distributions() is always replaced and
requirements files are written into tmp_path.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from scripts import check_env_drift as drift


class Dist:
    """The slice of importlib.metadata.Distribution the checker actually uses."""

    def __init__(self, name: str | None, version: str = "1.0.0", requires: list[str] | None = None):
        self.metadata = {} if name is None else {"Name": name}
        self.version = version
        self.requires = requires


@pytest.fixture
def installed(monkeypatch):
    """Replace the live environment with a declared set of distributions."""

    def _installed(*dists: Dist) -> None:
        monkeypatch.setattr(drift, "distributions", lambda: list(dists))

    _installed()
    return _installed


@pytest.fixture
def requirements(tmp_path):
    def _write(text: str) -> Path:
        path = tmp_path / "requirements.txt"
        path.write_text(text)
        return path

    return _write


class TestDeclaredVsInstalled:
    def test_matching_pin_is_clean(self, installed, requirements):
        installed(Dist("fastapi", "0.141.1"))
        assert drift.check_declared_vs_installed(requirements("fastapi==0.141.1\n")) == []

    def test_wrong_version_is_reported(self, installed, requirements):
        installed(Dist("fastapi", "0.140.0"))
        (problem,) = drift.check_declared_vs_installed(requirements("fastapi==0.141.1\n"))
        assert "fastapi" in problem and "0.141.1" in problem and "0.140.0" in problem

    def test_declared_but_absent_is_reported(self, installed, requirements):
        installed(Dist("fastapi", "0.141.1"))
        (problem,) = drift.check_declared_vs_installed(requirements("reportlab==5.0.0\n"))
        assert "reportlab" in problem and "NOT installed" in problem

    def test_extras_qualified_pin_is_still_checked(self, installed, requirements):
        """Regression: `uvicorn[standard]==0.28.0` was skipped entirely.

        The old regex could not match the `[standard]` suffix, and an
        unmatched line was silently dropped -- so the pin most likely to be
        written with extras was the one the drift check stopped watching, and
        it reported OK on a venv that had drifted.
        """
        installed(Dist("uvicorn", "0.27.0"))
        (problem,) = drift.check_declared_vs_installed(requirements("uvicorn[standard]==0.28.0\n"))
        assert "uvicorn" in problem and "0.27.0" in problem

    def test_minimum_floor_is_enforced(self, installed, requirements):
        """Regression: `>=` was parsed and then never compared.

        The old code captured the operator but only acted on `==`, so anything
        declared with a floor passed the check at any installed version --
        including one below the floor the pin exists to guarantee.
        """
        installed(Dist("requests", "1.0.0"))
        (problem,) = drift.check_declared_vs_installed(requirements("requests>=2.34.2\n"))
        assert "requests" in problem and "1.0.0" in problem

    def test_satisfied_floor_is_clean(self, installed, requirements):
        installed(Dist("requests", "2.35.0"))
        assert drift.check_declared_vs_installed(requirements("requests>=2.34.2\n")) == []

    def test_zero_padded_equivalent_is_not_drift(self, installed, requirements):
        """Regression: versions were compared as strings.

        PEP 440 pads the release segment, so `==1.0` is satisfied by `1.0.0`.
        The old string compare called that drift and exited 1 on a correct
        environment -- a false alarm on the one check meant to be trusted.
        """
        installed(Dist("reportlab", "5.0.0"))
        assert drift.check_declared_vs_installed(requirements("reportlab==5.0\n")) == []

    def test_requirement_gated_to_another_platform_is_not_demanded(self, installed, requirements):
        """Regression: environment markers were parsed off and then ignored.

        A `; sys_platform == "win32"` pin is not supposed to be installed on
        the iMac. The old code stopped the version at the `;` and then
        reported the package as missing, failing the check on a clean venv.
        """
        installed(Dist("fastapi", "0.141.1"))
        line = 'pywin32==306; sys_platform == "win32"\n'
        assert drift.check_declared_vs_installed(requirements(line)) == []

    def test_marker_that_does_apply_is_still_enforced(self, installed, requirements):
        # The marker fix must skip only the inapplicable ones, not all of them.
        installed(Dist("fastapi", "0.140.0"))
        line = 'fastapi==0.141.1; python_version >= "3.0"\n'
        (problem,) = drift.check_declared_vs_installed(requirements(line))
        assert "fastapi" in problem

    def test_comments_blanks_and_flag_lines_are_ignored(self, installed, requirements):
        installed(Dist("fastapi", "0.141.1"))
        text = (
            "# Web layer\n"
            "\n"
            "--extra-index-url https://example.invalid/simple\n"
            "-r requirements-dev.txt\n"
            "fastapi==0.141.1  # pinned as one unit\n"
        )
        assert drift.check_declared_vs_installed(requirements(text)) == []

    def test_name_spelling_differences_are_normalized(self, installed, requirements):
        # Declared one way, published another -- the same distribution.
        installed(Dist("google-auth-httplib2", "0.4.0"))
        assert drift.check_declared_vs_installed(requirements("Google_Auth.HTTPLib2==0.4.0\n")) == []

    def test_unparseable_line_does_not_crash_the_check(self, installed, requirements):
        installed(Dist("fastapi", "0.141.1"))
        text = "this is not a requirement !!\nfastapi==0.141.1\n"
        assert drift.check_declared_vs_installed(requirements(text)) == []

    def test_unparseable_installed_version_is_reported_not_waved_through(self, installed, requirements):
        installed(Dist("weird", "not-a-version"))
        (problem,) = drift.check_declared_vs_installed(requirements("weird==1.0\n"))
        assert "weird" in problem and "not-a-version" in problem

    def test_missing_requirements_file_is_one_named_problem(self, installed, tmp_path):
        (problem,) = drift.check_declared_vs_installed(tmp_path / "requirements.txt")
        assert "requirements.txt" in problem and "not found" in problem

    def test_distribution_without_a_name_does_not_crash(self, installed, requirements):
        installed(Dist(None), Dist("fastapi", "0.141.1"))
        assert drift.check_declared_vs_installed(requirements("fastapi==0.141.1\n")) == []


class TestInterpreterIdentity:
    def test_same_binary_is_clean(self, monkeypatch, tmp_path):
        binary = tmp_path / "python3.12"
        binary.touch()
        monkeypatch.setattr(sys, "_base_executable", str(binary), raising=False)
        assert drift.check_interpreter(str(binary)) == []

    def test_symlinked_spelling_of_the_same_binary_is_clean(self, monkeypatch, tmp_path):
        binary = tmp_path / "python3.12"
        binary.touch()
        link = tmp_path / "floating"
        link.symlink_to(binary)
        monkeypatch.setattr(sys, "_base_executable", str(link), raising=False)
        assert drift.check_interpreter(str(binary)) == []

    def test_different_binary_names_both_paths(self, monkeypatch):
        monkeypatch.setattr(sys, "_base_executable", "/uv/cpython-3.12.14/bin/python3.12", raising=False)
        (problem,) = drift.check_interpreter("/uv/cpython-3.12.13/bin/python3.12")
        assert "3.12.13" in problem and "3.12.14" in problem
        assert "Full Disk Access" in problem

    def test_the_venv_shim_is_not_what_gets_compared(self, monkeypatch):
        """The TCC grant is recorded against the base interpreter.

        .venv/bin/python is a per-project shim; comparing it would pass while
        the granted binary underneath had been swapped out.
        """
        monkeypatch.setattr(sys, "_base_executable", "/uv/cpython-3.12.13/bin/python3.12", raising=False)
        monkeypatch.setattr(sys, "executable", "/project/.venv/bin/python")
        assert drift.check_interpreter("/uv/cpython-3.12.13/bin/python3.12") == []
        assert drift.check_interpreter("/project/.venv/bin/python") != []

    def test_falls_back_to_sys_executable_when_there_is_no_base(self, monkeypatch):
        monkeypatch.delattr(sys, "_base_executable", raising=False)
        monkeypatch.setattr(sys, "executable", "/usr/bin/python3.12")
        assert drift.check_interpreter("/usr/bin/python3.12") == []
        assert drift.check_interpreter("/opt/other/python3.12") != []


class TestConflicts:
    def test_satisfied_dependency_is_clean(self, installed):
        installed(Dist("app", "1.0", ["dep>=2.0"]), Dist("dep", "2.5"))
        assert drift.check_conflicts() == []

    def test_violated_dependency_is_reported(self, installed):
        installed(Dist("app", "1.0", ["dep>=2.0"]), Dist("dep", "1.4"))
        (problem,) = drift.check_conflicts()
        assert "app 1.0 requires dep>=2.0" in problem
        assert "1.4 is installed" in problem

    def test_dependency_for_an_unselected_platform_is_ignored(self, installed):
        installed(Dist("app", "1.0", ['dep>=2.0; sys_platform == "win32"']), Dist("dep", "1.4"))
        assert drift.check_conflicts() == []

    def test_dependency_behind_an_unrequested_extra_is_ignored(self, installed):
        installed(Dist("app", "1.0", ['dep>=2.0; extra == "fast"']), Dist("dep", "1.4"))
        assert drift.check_conflicts() == []

    def test_missing_dependency_is_left_to_the_declared_check(self, installed):
        # Current, deliberate-looking behaviour: an entirely absent dependency
        # is not reported here. Pinned so a change to it is a decision, not a
        # surprise.
        installed(Dist("app", "1.0", ["dep>=2.0"]))
        assert drift.check_conflicts() == []

    def test_unparseable_requirement_string_does_not_crash(self, installed):
        installed(Dist("app", "1.0", ["!!! not a requirement"]), Dist("dep", "1.4"))
        assert drift.check_conflicts() == []

    def test_unparseable_installed_version_does_not_crash(self, installed):
        installed(Dist("app", "1.0", ["dep>=2.0"]), Dist("dep", "not-a-version"))
        assert drift.check_conflicts() == []


class TestMain:
    @pytest.fixture(autouse=True)
    def stub_sections(self, monkeypatch):
        """Every section clean by default; each test overrides what it needs."""
        monkeypatch.setattr(sys, "argv", ["check_env_drift.py"])
        monkeypatch.setattr(drift, "check_interpreter", lambda expected: [])
        monkeypatch.setattr(drift, "check_declared_vs_installed", lambda req: [])
        monkeypatch.setattr(drift, "check_conflicts", lambda: [])

    def test_clean_environment_exits_zero(self, capsys):
        assert drift.main() == 0
        assert "matches what is declared and granted" in capsys.readouterr().out

    def test_any_problem_exits_one(self, monkeypatch, capsys):
        monkeypatch.setattr(drift, "check_conflicts", lambda: ["dep conflict"])
        assert drift.main() == 1
        assert "dep conflict" in capsys.readouterr().out

    def test_problems_are_counted_across_every_section(self, monkeypatch, capsys):
        monkeypatch.setattr(drift, "check_interpreter", lambda expected: ["a"])
        monkeypatch.setattr(drift, "check_declared_vs_installed", lambda req: ["b", "c"])
        monkeypatch.setattr(drift, "check_conflicts", lambda: ["d"])
        assert drift.main() == 1
        assert "4 problem(s) found." in capsys.readouterr().out

    def test_interpreter_failure_alone_fails_the_run(self, monkeypatch):
        # The grant check is the one that fails silently in production.
        monkeypatch.setattr(drift, "check_interpreter", lambda expected: ["interpreter changed"])
        assert drift.main() == 1

    def test_expect_interpreter_flag_reaches_the_check(self, monkeypatch):
        seen = []
        monkeypatch.setattr(sys, "argv", ["check_env_drift.py", "--expect-interpreter", "/opt/py"])
        monkeypatch.setattr(drift, "check_interpreter", lambda expected: seen.append(expected) or [])
        drift.main()
        assert seen == ["/opt/py"]

    def test_default_expected_interpreter_is_the_versioned_path(self, monkeypatch):
        """A floating `cpython-3.12-...` symlink would silently lose the grant."""
        seen = []
        monkeypatch.setattr(drift, "check_interpreter", lambda expected: seen.append(expected) or [])
        drift.main()
        assert seen == [drift.DEFAULT_EXPECTED_INTERPRETER]
        assert "cpython-3.12.13-macos-aarch64-none" in seen[0]

    def test_requirements_flag_reaches_the_check(self, monkeypatch, tmp_path):
        seen = []
        target = tmp_path / "other-requirements.txt"
        monkeypatch.setattr(sys, "argv", ["check_env_drift.py", "--requirements", str(target)])
        monkeypatch.setattr(drift, "check_declared_vs_installed", lambda req: seen.append(req) or [])
        drift.main()
        assert seen == [Path(str(target))]

    def test_requirements_defaults_to_the_project_file(self, monkeypatch):
        seen = []
        monkeypatch.setattr(drift, "check_declared_vs_installed", lambda req: seen.append(req) or [])
        drift.main()
        assert seen == [drift.PROJECT_ROOT / "requirements.txt"]
        assert os.path.basename(str(seen[0])) == "requirements.txt"
