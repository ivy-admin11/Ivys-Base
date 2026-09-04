#!/usr/bin/env python3
"""Disk housekeeping for Ivy: rotate logs, prune old ones, prune the outbox.

Nothing in this project rotated anything. `logs/` had grown to 8.7 MB across
78 files (65 of them one-off ad-hoc job logs dating back to July), the two
launchd-written gateway logs were 3.7 MB and 2.2 MB and only ever grew, and
`ivy_core.outbox.cleanup_old_reports()` implemented a 72-hour TTL that nothing
ever called — 63 of 70 reports were past it.

SAFE BY DEFAULT: with no flags this only prints what WOULD change. Pass
--apply to act.

Rotation uses copy-then-truncate, NOT rename. launchd holds an open file
descriptor on StandardOutPath/StandardErrorPath; renaming the file leaves
launchd writing to the moved inode and the fresh file stays empty forever.
Truncating in place keeps the descriptor valid.

Only files matching *.log are ever touched. logs/ also holds executions.db
(the job receipts database, the runtime's source of truth for what actually
ran) and gateway_monitor_state.json — deleting either would be destructive,
so the allowlist is by extension and enforced in one place.

Usage:
    ./scripts/housekeeping.py              # dry run
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

DEFAULT_MAX_MB = 5
DEFAULT_KEEP = 3
DEFAULT_ADHOC_DAYS = 14

# Never touched, whatever the pattern says. Belt and braces alongside the
# *.log allowlist: these are live state, not logs.
PROTECTED_NAMES = {"executions.db", "picks.db", "gateway_monitor_state.json"}


def _is_rotatable(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix == ".log"
        and path.name not in PROTECTED_NAMES
    )


def rotate(path: Path, *, max_bytes: int, keep: int, apply: bool) -> list[str]:
    """Copy-truncate ``path`` when it exceeds ``max_bytes``; shift older gens."""
    actions: list[str] = []
    if not _is_rotatable(path):
        return actions
    size = path.stat().st_size
    if size <= max_bytes:
        return actions

    # Shift .log.(keep-1) -> .log.keep, ..., .log.1 -> .log.2, dropping the oldest.
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
        # copy-then-truncate: preserves the inode launchd is writing to.
        dest = path.with_suffix(".log.1")
        dest.write_bytes(path.read_bytes())
        with open(path, "r+") as fh:
            fh.truncate(0)
    return actions


def prune_adhoc(*, days: int, apply: bool) -> list[str]:
    """Delete one-off ad-hoc job logs older than ``days``.

    job_runner writes logs/<job>_adhoc_<epoch>.log per on-demand run and never
    removes them.
    """
    actions: list[str] = []
    cutoff = time.time() - days * 86400
    for p in sorted(LOG_DIR.glob("*_adhoc_*.log")):
        if p.name in PROTECTED_NAMES:
            continue
        if p.stat().st_mtime < cutoff:
            actions.append(f"delete {p.name} (ad-hoc, older than {days}d)")
            if apply:
                p.unlink()
    return actions


def prune_rotations(*, days: int, apply: bool) -> list[str]:
    """Delete rotated generations (*.log.N) older than ``days``."""
    actions: list[str] = []
    cutoff = time.time() - days * 86400
    for p in sorted(LOG_DIR.glob("*.log.[0-9]")):
        if p.stat().st_mtime < cutoff:
            actions.append(f"delete {p.name} (rotation, older than {days}d)")
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

    mode = "APPLY" if args.apply else "DRY-RUN (pass --apply to act)"
    print(f"Ivy housekeeping — {mode}")
    print(f"  logs dir: {LOG_DIR}")

    actions: list[str] = []
    if LOG_DIR.is_dir():
        before = sum(f.stat().st_size for f in LOG_DIR.iterdir() if f.is_file())
        for p in sorted(LOG_DIR.glob("*.log")):
            actions += rotate(p, max_bytes=int(args.max_mb * 1e6), keep=args.keep, apply=args.apply)
        actions += prune_adhoc(days=args.adhoc_days, apply=args.apply)
        actions += prune_rotations(days=args.adhoc_days, apply=args.apply)
        after = sum(f.stat().st_size for f in LOG_DIR.iterdir() if f.is_file())
    else:
        before = after = 0
        print("  (no logs directory)")

    # Outbox retention. save_report() also self-prunes, so this is a backstop
    # for a long-idle system that has not written a report recently.
    from ivy_core import outbox

    if args.apply:
        removed = outbox.cleanup_old_reports()
        actions.append(f"outbox: removed {removed} report(s) past TTL")
    else:
        from datetime import datetime, timedelta, timezone
        import json

        now = datetime.now(timezone.utc)
        stale = 0
        for f in outbox.OUTBOX_DIR.glob("*.json"):
            if f.name.endswith(".detail.json"):
                continue
            try:
                m = json.loads(f.read_text())
                ts = datetime.fromisoformat(m.get("generated_at", ""))
            except Exception:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            deadline = (
                now - timedelta(days=outbox.PENDING_TTL_DAYS)
                if m.get("status") == "pending"
                else now - timedelta(hours=outbox.OUTBOX_TTL_HOURS)
            )
            if ts < deadline:
                stale += 1
        actions.append(f"outbox: would remove {stale} report(s) past TTL")

    for a in actions:
        print(f"  - {a}")
    if not actions:
        print("  nothing to do.")
    if LOG_DIR.is_dir() and args.apply:
        print(f"  logs/: {before / 1e6:.1f} MB -> {after / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
