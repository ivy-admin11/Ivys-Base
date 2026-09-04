#!/usr/bin/env python3
"""Disk housekeeping: rotate logs, prune old ones, prune the outbox.

Nothing rotated logs and nothing enforced outbox retention.
`ivy_core.outbox.cleanup_old_reports()` has implemented a TTL since the outbox
was added, but no caller ever invoked it, and `job_runner` writes one
`logs/<job>_adhoc_<execution_id>.log` per on-demand run and never removes it.

SAFE BY DEFAULT: with no flags this only prints what WOULD change. Pass
--apply to act.

Rotation uses copy-then-truncate, NOT rename. launchd holds an open file
descriptor on StandardOutPath/StandardErrorPath; renaming the file leaves
launchd writing to the moved inode while the fresh file stays permanently
empty. Truncating in place keeps the descriptor valid.

Only files matching *.log are touched. logs/ also holds the execution-receipt
database and monitor state — deleting either would be destructive — so the
allowlist is by extension and backed by an explicit protected-name list.

Usage:
    ./scripts/housekeeping.py                  # dry run
    ./scripts/housekeeping.py --apply
    ./scripts/housekeeping.py --apply --max-mb 5 --keep 3 --adhoc-days 14
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

LOG_DIR = PROJECT_ROOT / "logs"

DEFAULT_MAX_MB = 5.0
DEFAULT_KEEP = 3
DEFAULT_ADHOC_DAYS = 14

# Live state that happens to sit in logs/. Never rotated, never deleted.
PROTECTED_NAMES = {
    "executions.db",            # job receipts — the record of what actually ran
    "imessage_worker.db",       # poller cursor state
    "picks.db",
    "gateway_monitor_state.json",
    "imessage-poller.lock",
}


def _is_rotatable(path: Path) -> bool:
    return path.is_file() and path.suffix == ".log" and path.name not in PROTECTED_NAMES


def rotate(path: Path, *, max_bytes: int, keep: int, apply: bool) -> list:
    """Copy-truncate ``path`` when it exceeds ``max_bytes``; shift older gens."""
    actions: list = []
    if not _is_rotatable(path):
        return actions
    size = path.stat().st_size
    if size <= max_bytes:
        return actions

    oldest = path.with_suffix(f".log.{keep}")
    if oldest.exists():
        actions.append(f"delete {oldest.name} (beyond --keep {keep})")
        if apply:
            oldest.unlink()
    for gen in range(keep - 1, 0, -1):
        src = path.with_suffix(f".log.{gen}")
        if src.exists():
            actions.append(f"{src.name} -> {path.with_suffix(f'.log.{gen + 1}').name}")
            if apply:
                src.rename(path.with_suffix(f".log.{gen + 1}"))

    actions.append(f"rotate {path.name} ({size / 1e6:.1f} MB) -> {path.name}.1 and truncate")
    if apply:
        path.with_suffix(".log.1").write_bytes(path.read_bytes())
        with open(path, "r+") as fh:
            fh.truncate(0)
    return actions


def prune_by_age(pattern: str, *, days: int, label: str, apply: bool) -> list:
    actions: list = []
    cutoff = time.time() - days * 86400
    for p in sorted(LOG_DIR.glob(pattern)):
        if p.name in PROTECTED_NAMES or not p.is_file():
            continue
        if p.stat().st_mtime < cutoff:
            actions.append(f"delete {p.name} ({label}, older than {days}d)")
            if apply:
                p.unlink()
    return actions


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually make changes")
    ap.add_argument("--max-mb", type=float, default=DEFAULT_MAX_MB)
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    ap.add_argument("--adhoc-days", type=int, default=DEFAULT_ADHOC_DAYS)
    args = ap.parse_args()

    print(f"Ivy housekeeping — {'APPLY' if args.apply else 'DRY-RUN (pass --apply to act)'}")
    print(f"  logs dir: {LOG_DIR}")

    actions: list = []
    if LOG_DIR.is_dir():
        for p in sorted(LOG_DIR.glob("*.log")):
            actions += rotate(p, max_bytes=int(args.max_mb * 1e6), keep=args.keep, apply=args.apply)
        actions += prune_by_age("*_adhoc_*.log", days=args.adhoc_days, label="ad-hoc", apply=args.apply)
        actions += prune_by_age("*.log.[0-9]", days=args.adhoc_days, label="rotation", apply=args.apply)
    else:
        print("  (no logs directory)")

    from ivy_core import outbox

    if args.apply:
        actions.append(f"outbox: removed {outbox.cleanup_old_reports()} report(s) past TTL")
    else:
        actions.append("outbox: would prune reports past TTL (run with --apply)")

    for a in actions:
        print(f"  - {a}")
    if not actions:
        print("  nothing to do.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
