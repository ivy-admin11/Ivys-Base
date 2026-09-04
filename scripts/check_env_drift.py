#!/usr/bin/env python3
"""Verify the LIVE environment still matches what is declared and granted.

CI cannot catch this class of problem: CI builds a fresh environment from the
lockfile and never sees the iMac's venv. On this project the venv *is*
production, and it had drifted badly — 11 packages at versions other than the
declared ones, 4 declared packages not installed at all, and a dependency
conflict that made the venv unreproducible (`pip install -r` on its own
`pip freeze` failed with ResolutionImpossible).

It also asserts the interpreter identity. The gateway's macOS Full Disk Access
grant is recorded against a specific interpreter binary; `.venv/bin/python` is
a symlink into uv's store, and a `uv python upgrade` would repoint it at a
different binary. chat.db reads would then start failing with "authorization
denied" and nothing would say why. This turns that into a named check.

Exit status: 0 clean, 1 problems found.

Usage:
    ./scripts/check_env_drift.py
    ./scripts/check_env_drift.py --expect-interpreter /path/to/python3.12
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from importlib.metadata import distributions
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The interpreter the TCC grants were issued against. Kept as the VERSIONED
# path: uv's `cpython-3.12-macos-aarch64-none` symlink floats across patch
# releases, and following it is exactly how the grant would be silently lost.
DEFAULT_EXPECTED_INTERPRETER = (
    "/Users/lexi/.local/share/uv/python/"
    "cpython-3.12.13-macos-aarch64-none/bin/python3.12"
)


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def check_interpreter(expected: str) -> list[str]:
    """The resolved base interpreter must still be the TCC-granted binary."""
    actual = os.path.realpath(getattr(sys, "_base_executable", sys.executable))
    if actual != os.path.realpath(expected):
        return [
            "interpreter changed — macOS Full Disk Access is granted per binary, "
            "so chat.db reads will fail with 'authorization denied':\n"
            f"      expected {expected}\n"
            f"      actual   {actual}"
        ]
    return []


def check_declared_vs_installed(req_file: Path) -> list[str]:
    """Every pinned requirement must be installed at the pinned version."""
    problems: list[str] = []
    if not req_file.exists():
        return [f"{req_file.name} not found"]
    installed = {_norm(d.metadata["Name"]): d.version
                 for d in distributions() if d.metadata.get("Name")}
    for raw in req_file.read_text().splitlines():
        line = raw.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*(==|>=)\s*([^\s,;]+)", line)
        if not m:
            continue
        name, op, want = m.group(1), m.group(2), m.group(3)
        have = installed.get(_norm(name))
        if have is None:
            problems.append(f"{name} is declared but NOT installed")
        elif op == "==" and have != want:
            problems.append(f"{name} declared =={want} but installed {have}")
    return problems


def check_conflicts() -> list[str]:
    """A pip-check equivalent via importlib.metadata (no pip subprocess)."""
    from packaging.requirements import Requirement
    from packaging.version import InvalidVersion, Version

    problems: list[str] = []
    versions = {_norm(d.metadata["Name"]): d.version
                for d in distributions() if d.metadata.get("Name")}
    for dist in distributions():
        name = dist.metadata.get("Name")
        if not name:
            continue
        for raw in dist.requires or []:
            try:
                req = Requirement(raw)
            except Exception:
                continue
            if req.marker and not req.marker.evaluate():
                continue
            have = versions.get(_norm(req.name))
            if have is None or not req.specifier:
                continue
            try:
                if Version(have) not in req.specifier:
                    problems.append(
                        f"{name} {dist.version} requires {req.name}{req.specifier}, "
                        f"but {have} is installed"
                    )
            except InvalidVersion:
                continue
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--expect-interpreter", default=DEFAULT_EXPECTED_INTERPRETER)
    ap.add_argument("--requirements", default=str(PROJECT_ROOT / "requirements.txt"))
    args = ap.parse_args()

    sections = [
        ("interpreter identity", check_interpreter(args.expect_interpreter)),
        ("declared vs installed", check_declared_vs_installed(Path(args.requirements))),
        ("dependency conflicts", check_conflicts()),
    ]

    total = 0
    for title, problems in sections:
        if problems:
            total += len(problems)
            print(f"\n{title}: {len(problems)} problem(s)")
            for p in problems:
                print(f"  - {p}")
        else:
            print(f"{title}: OK")

    print()
    if total:
        print(f"{total} problem(s) found.")
        return 1
    print("Environment matches what is declared and granted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
