"""scripts/check_repo_hygiene.py: sanitized rule checks.

Uses synthetic path lists and tmp_path fixtures — never scans this
session's own real git history, and never prints/asserts on matched
secret values, only rule identifiers and paths.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_repo_hygiene as hygiene  # noqa: E402


def test_env_file_rejected_except_example():
    violations = hygiene.check_env_files([".env", ".env.local", ".env.example", "sub/.env.production"])
    flagged = {v.path for v in violations}
    assert flagged == {".env", ".env.local", "sub/.env.production"}
    assert ".env.example" not in flagged


def test_credential_files_rejected():
    violations = hygiene.check_credential_files([
        "token.json", "google_credentials.json", "service-account-key.json",
        "some/my-credentials.pem", "keys/prod.key", "README.md",
    ])
    flagged = {v.path for v in violations}
    assert "token.json" in flagged
    assert "google_credentials.json" in flagged
    assert "service-account-key.json" in flagged
    assert "some/my-credentials.pem" in flagged
    assert "keys/prod.key" in flagged
    assert "README.md" not in flagged


def test_contact_allowlist_rejected_except_example():
    violations = hygiene.check_contact_allowlists([
        "favorites.json", "favorites.example.json", "mcp-servers/imessage/favorites.json",
    ])
    flagged = {v.path for v in violations}
    assert flagged == {"favorites.json", "mcp-servers/imessage/favorites.json"}


def test_db_cache_build_runtime_rejected():
    violations = hygiene.check_db_cache_build_runtime([
        "data/picks.db", "x.sqlite3", "__pycache__/mod.pyc", ".pytest_cache/README.md",
        "build/out.txt", "dist/pkg.whl", "logs/app.log", ".coverage",
        "proactive_agents/sports_bettor.py",
    ])
    flagged = {v.path for v in violations}
    assert "data/picks.db" in flagged
    assert "x.sqlite3" in flagged
    assert "__pycache__/mod.pyc" in flagged
    assert ".pytest_cache/README.md" in flagged
    assert "build/out.txt" in flagged
    assert "dist/pkg.whl" in flagged
    assert "logs/app.log" in flagged
    assert ".coverage" in flagged
    assert "proactive_agents/sports_bettor.py" not in flagged


def test_private_key_marker_detected_without_printing_the_key(tmp_path, capsys):
    repo_root = tmp_path
    offender = repo_root / "leaked.pem"
    offender.write_text("-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----\n")
    safe = repo_root / "clean.py"
    safe.write_text("print('hello world')\n")

    violations = hygiene.check_private_key_markers(repo_root, ["leaked.pem", "clean.py"])

    assert len(violations) == 1
    assert violations[0].path == "leaked.pem"
    assert violations[0].rule == "private-key-marker"
    # The checker itself must never have printed the matched key material.
    captured = capsys.readouterr()
    assert "MIIEpAIBAAKCAQEA" not in captured.out


def test_machine_specific_path_detected_in_executable_source(tmp_path):
    repo_root = tmp_path
    offender = repo_root / "script.py"
    offender.write_text("#!/Users/someone/project/.venv/bin/python\nprint('hi')\n")
    safe = repo_root / "docs.md"
    safe.write_text("Comment mentioning /Users/, /var/, /tmp/ paths generically.\n")
    safe_py = repo_root / "clean.py"
    safe_py.write_text("print('no machine paths here')\n")

    violations = hygiene.check_machine_specific_paths(repo_root, ["script.py", "docs.md", "clean.py"])

    flagged = {v.path for v in violations}
    assert flagged == {"script.py"}, "generic /Users/ mentions without a real path must not false-positive"


def test_run_all_checks_reports_no_violations_for_a_clean_tree(tmp_path):
    repo_root = tmp_path
    (repo_root / "app.py").write_text("print('clean')\n")
    violations = hygiene.run_all_checks(repo_root, [".env.example", "favorites.example.json", "app.py"])
    assert violations == []


def test_violation_str_never_includes_file_contents():
    v = hygiene.Violation("credential-file-tracked", "token.json")
    assert str(v) == "[credential-file-tracked] token.json"
