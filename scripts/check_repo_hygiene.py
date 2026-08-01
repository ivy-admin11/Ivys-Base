#!/usr/bin/env python3
"""Sanitized repository hygiene checker.

Inspects tracked paths (and, for a few content-based rules, tracked text)
in the current git checkout. Reports only repository-relative paths and
rule identifiers — it never prints matched secret values, file contents,
or PII, regardless of what it finds.

Exit code 0 means no violations; exit code 1 means at least one violation
was found (printed to stdout, one per line).
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List


@dataclass(frozen=True)
class Violation:
    rule: str
    path: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.path}"


# ---------------------------------------------------------------------------
# Rule: .env files (except sanitized examples)
# ---------------------------------------------------------------------------

_ENV_FILE_RE = re.compile(r"(^|/)\.env(\.[^/]+)?$")
_ENV_ALLOWED_BASENAMES = {".env.example"}


def check_env_files(paths: List[str]) -> List[Violation]:
    violations = []
    for p in paths:
        if _ENV_FILE_RE.search(p) and Path(p).name not in _ENV_ALLOWED_BASENAMES:
            violations.append(Violation("env-file-tracked", p))
    return violations


# ---------------------------------------------------------------------------
# Rule: credential / token / private-key / service-account files
# ---------------------------------------------------------------------------

_CREDENTIAL_BASENAMES = {
    "token.json",
    "google_credentials.json",
    "service-account-key.json",
    "discord_backup_codes.txt",
}
_CREDENTIAL_NAME_HINTS = ("credential", "service-account", "service_account")
_CREDENTIAL_EXTENSIONS = (".pem", ".key", ".p12", ".pfx", ".jks")


def check_credential_files(paths: List[str]) -> List[Violation]:
    violations = []
    for p in paths:
        base = Path(p).name.lower()
        if (
            base in _CREDENTIAL_BASENAMES
            or any(hint in base for hint in _CREDENTIAL_NAME_HINTS)
            or base.endswith(_CREDENTIAL_EXTENSIONS)
        ):
            violations.append(Violation("credential-file-tracked", p))
    return violations


# ---------------------------------------------------------------------------
# Rule: non-example contact allowlists (e.g. favorites.json)
# ---------------------------------------------------------------------------

_FAVORITES_BASENAME = "favorites.json"
_EXAMPLE_SUFFIX = ".example.json"


def check_contact_allowlists(paths: List[str]) -> List[Violation]:
    violations = []
    for p in paths:
        base = Path(p).name
        if base.endswith(_EXAMPLE_SUFFIX):
            continue
        if base == _FAVORITES_BASENAME or ("allowlist" in base.lower() and base.endswith(".json")):
            violations.append(Violation("contact-allowlist-tracked", p))
    return violations


# ---------------------------------------------------------------------------
# Rule: tracked databases, caches, build output, and runtime state
# ---------------------------------------------------------------------------

_CACHE_BUILD_RE = re.compile(
    r"(^|/)(__pycache__|\.pytest_cache|\.ruff_cache|\.mypy_cache|htmlcov|build|dist)(/|$)"
)
_DB_FILE_RE = re.compile(r"\.(db|sqlite|sqlite3)$")
_RUNTIME_STATE_RE = re.compile(r"(^|/)(logs/.*\.log|\.coverage)$")


def check_db_cache_build_runtime(paths: List[str]) -> List[Violation]:
    violations = []
    for p in paths:
        if _CACHE_BUILD_RE.search(p) or _DB_FILE_RE.search(p) or _RUNTIME_STATE_RE.search(p):
            violations.append(Violation("db-cache-build-runtime-tracked", p))
    return violations


# ---------------------------------------------------------------------------
# Rule: private-key markers in tracked text
# ---------------------------------------------------------------------------

_PRIVATE_KEY_MARKER_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_TEXT_SCAN_EXTENSIONS = (
    ".py", ".sh", ".json", ".md", ".txt", ".cfg", ".ini", ".toml",
    ".yml", ".yaml", ".template", ".plist", ".env", ".pem", ".key",
)


def _read_text(repo_root: Path, rel_path: str) -> str:
    try:
        return (repo_root / rel_path).read_text(errors="ignore")
    except (OSError, UnicodeDecodeError):
        return ""


def check_private_key_markers(repo_root: Path, paths: List[str]) -> List[Violation]:
    violations = []
    for p in paths:
        if not p.endswith(_TEXT_SCAN_EXTENSIONS):
            continue
        content = _read_text(repo_root, p)
        if content and _PRIVATE_KEY_MARKER_RE.search(content):
            violations.append(Violation("private-key-marker", p))
    return violations


# ---------------------------------------------------------------------------
# Rule: machine-specific /Users/<name>/... paths in executable source
# ---------------------------------------------------------------------------

_MACHINE_PATH_RE = re.compile(r"/Users/[A-Za-z0-9_.\-]+")
_EXECUTABLE_SOURCE_EXTENSIONS = (".py", ".sh")


def check_machine_specific_paths(repo_root: Path, paths: List[str]) -> List[Violation]:
    violations = []
    for p in paths:
        if not p.endswith(_EXECUTABLE_SOURCE_EXTENSIONS):
            continue
        content = _read_text(repo_root, p)
        if content and _MACHINE_PATH_RE.search(content):
            violations.append(Violation("machine-specific-path", p))
    return violations


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

CHECKS_PATHS_ONLY = (
    check_env_files,
    check_credential_files,
    check_contact_allowlists,
    check_db_cache_build_runtime,
)
CHECKS_WITH_CONTENT = (
    check_private_key_markers,
    check_machine_specific_paths,
)


def _repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    )
    return Path(result.stdout.strip())


def _tracked_files(repo_root: Path) -> List[str]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files"],
        capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def run_all_checks(repo_root: Path, paths: List[str]) -> List[Violation]:
    violations: List[Violation] = []
    for check in CHECKS_PATHS_ONLY:
        violations.extend(check(paths))
    for check in CHECKS_WITH_CONTENT:
        violations.extend(check(repo_root, paths))
    return violations


def main() -> int:
    repo_root = _repo_root()
    paths = _tracked_files(repo_root)
    violations = run_all_checks(repo_root, paths)

    if not violations:
        print(f"repo-hygiene: OK — 0 violations across {len(paths)} tracked paths.")
        return 0

    violations.sort(key=lambda v: (v.rule, v.path))
    print(f"repo-hygiene: {len(violations)} violation(s) found:")
    for v in violations:
        print(f"  {v}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
