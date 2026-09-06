"""Proactive findings: what Ivy tells Henry without being asked.

Two rules shape this module, both learned the hard way on this project.

**A finding must be a fact, not an opinion.** Every check below answers a
question with a definite answer -- is the sheet behind the database, has a job
been silent past its schedule, are picks aging out ungraded. Nothing here asks
a model what it thinks. During the September repair, three confident findings
produced from real data turned out to be wrong; a model polling for "anything
notable" on a schedule would be worse, and every wrong text spends the
attention that the right one needs.

**A channel that cries wolf stops being read.** The watchdog written earlier
that day would have paged weekly about a job running exactly as scheduled.
So ordinary findings are capped at one a day, repeat findings stay quiet for
24 hours, and nothing arrives overnight. Genuinely broken things -- the
gateway down, grading stopped -- bypass all of it, which is the only reason
the cap is safe.

Suggestions, as opposed to faults, are not texted at all. They accumulate and
go out in one weekly digest, where a mediocre idea costs ten seconds instead
of an interruption.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("ivy.proactive")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = PROJECT_ROOT / "data" / "proactive_state.json"

# Ordinary findings: one text a day, and a given finding repeats at most daily.
MAX_ORDINARY_PER_DAY = 1
REPEAT_SUPPRESS_H = 24
# Nothing overnight unless it is critical.
QUIET_START, QUIET_END = dtime(22, 0), dtime(7, 0)

CRITICAL = "critical"
NORMAL = "normal"
SUGGESTION = "suggestion"


@dataclass(frozen=True)
class Finding:
    """One thing Ivy noticed.

    key       stable across runs -- it is what suppresses repeats, so it must
              not contain today's date or a changing count
    severity  CRITICAL bypasses the cap and quiet hours; NORMAL is capped;
              SUGGESTION is never texted, only digested
    title     the first line of the text
    detail    one or two sentences of what and why
    fix       the concrete next step, or "" if there isn't one
    """
    key: str
    severity: str
    title: str
    detail: str
    fix: str = ""

    def render(self) -> str:
        parts = [self.title, "", self.detail]
        if self.fix:
            parts += ["", f"Fix: {self.fix}"]
        return "\n".join(parts)


# --------------------------------------------------------------------------
# Checks. Each returns the findings that are true right now, or [].
# A check that raises is logged and skipped -- one broken check must not
# silence the others.
# --------------------------------------------------------------------------
CheckFn = Callable[[], List[Finding]]
_CHECKS: List[CheckFn] = []


def check(fn: CheckFn) -> CheckFn:
    _CHECKS.append(fn)
    return fn


@check
def sheet_behind_database() -> List[Finding]:
    """Grades recorded locally that never reached the dashboard."""
    from ivy_core.picks_tracker import unsynced_grades

    missing = unsynced_grades()
    if not missing:
        return []
    return [Finding(
        key="sheet_behind",
        severity=NORMAL,
        title=f"⚠️ {len(missing)} graded pick(s) missing from the dashboard",
        detail=(
            "The results are recorded here but never reached the sheet, so the "
            "dashboard is understating the record."
        ),
        fix="scripts/repair_dashboard.py --apply",
    )]


# A stale-agent check deliberately does NOT live here. agent_watchdog already
# owns that alert and runs on the same poller interval; adding one here would
# text Henry twice about a single dead job, which is the noise problem this
# module exists to avoid. One fault, one owner, one text.


@check
def picks_aging_out() -> List[Finding]:
    """Ungraded picks about to pass the point where they can ever be graded.

    The scores endpoint serves roughly two days. A pick that goes ungraded
    past that is unverifiable forever, which is how 80 of 88 picks ended up
    with no result. This is the one finding that is worth acting on the same
    day, because tomorrow it is too late.
    """
    from ivy_core.picks_tracker import PICKS_DB
    import sqlite3

    if not Path(PICKS_DB).exists():
        return []
    conn = sqlite3.connect(PICKS_DB)
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
        n = conn.execute("""
            SELECT COUNT(*) FROM picks p
            LEFT JOIN results r ON r.pick_id = p.id
            WHERE r.result IS NULL
              AND COALESCE(NULLIF(p.game_day,''), p.report_date) <= ?
        """, (cutoff,)).fetchone()[0]
    finally:
        conn.close()
    if not n:
        return []
    return [Finding(
        key="picks_aging_out",
        severity=NORMAL,
        title=f"⏳ {n} pick(s) about to become ungradeable",
        detail=(
            "Their games are at the edge of the two-day scores window. Once past "
            "it no result can ever be established for them."
        ),
        fix="scripts/repair_dashboard.py --apply",
    )]


@check
def logs_growing_unchecked() -> List[Finding]:
    """Housekeeping is meant to rotate these; if it is not installed, nothing does."""
    log_dir = PROJECT_ROOT / "logs"
    if not log_dir.is_dir():
        return []
    big = []
    for p in log_dir.glob("*.log"):
        try:
            mb = p.stat().st_size / 1e6
        except OSError:
            continue
        if mb > 25:
            big.append((p.name, mb))
    if not big:
        return []
    big.sort(key=lambda x: -x[1])
    names = ", ".join(f"{n} ({m:.0f}MB)" for n, m in big[:3])
    return [Finding(
        key="logs_unrotated",
        severity=NORMAL,
        title="📄 Log files are growing without rotation",
        detail=f"{names}. The housekeeping job rotates these — it may not be installed.",
        fix="launchctl list | grep com.ivy.housekeeping",
    )]


@check
def no_qualifying_picks_recently() -> List[Finding]:
    """A dry spell long enough to mean the threshold, not the slate.

    Reported as a suggestion, not an alert: a quiet week is a legitimate
    outcome, and the remedy is a roster decision rather than a repair.
    """
    from ivy_core import outbox

    reports = outbox.list_reports(job_name="sharp_picks")[:8]
    if len(reports) < 6:
        return []
    qualifying = 0
    for meta in reports:
        try:
            if (meta.get("content_summary") or "").strip().startswith("0 "):
                continue
            qualifying += 1
        except AttributeError:
            continue
    if qualifying:
        return []
    return [Finding(
        key="no_qualifying_picks",
        severity=SUGGESTION,
        title="No consensus picks cleared in the last 8 reports",
        detail=(
            "Consensus needs two independent sharps agreeing, and most boards "
            "arrive from a single source, which makes it arithmetically rare "
            "rather than merely unlikely. More producing handicappers is the "
            "only real remedy."
        ),
        fix="scripts/vet_x_handles.py --current",
    )]


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------
def _load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump(state, f)
    except OSError as exc:
        logger.warning("Could not persist proactive state: %s", exc)


def in_quiet_hours(now: datetime) -> bool:
    t = now.astimezone().time()
    return t >= QUIET_START or t < QUIET_END


def collect(now: Optional[datetime] = None) -> List[Finding]:
    """Run every check. A check that raises is skipped, never fatal."""
    findings: List[Finding] = []
    for fn in _CHECKS:
        try:
            findings.extend(fn() or [])
        except Exception as exc:
            logger.warning("Proactive check %s failed: %s", fn.__name__, exc)
    order = {CRITICAL: 0, NORMAL: 1, SUGGESTION: 2}
    findings.sort(key=lambda f: order.get(f.severity, 3))
    return findings


def select_to_send(findings: List[Finding], state: dict, now: datetime) -> List[Finding]:
    """Apply the budget. Criticals are exempt from all of it."""
    seen: Dict[str, str] = state.get("last_alerted", {})
    today = now.date().isoformat()
    sent_today = state.get("sent_on", {}).get(today, 0)

    chosen: List[Finding] = []
    for f in findings:
        if f.severity == SUGGESTION:
            continue                      # digest only, never texted
        last = seen.get(f.key)
        if last:
            try:
                if (now - datetime.fromisoformat(last)).total_seconds() < REPEAT_SUPPRESS_H * 3600:
                    continue              # already said this recently
            except ValueError:
                pass
        if f.severity == CRITICAL:
            chosen.append(f)
            continue
        if in_quiet_hours(now):
            continue
        if sent_today + sum(1 for c in chosen if c.severity == NORMAL) >= MAX_ORDINARY_PER_DAY:
            continue
        chosen.append(f)
    return chosen


def run_once(send: Callable[[str], bool], now: Optional[datetime] = None) -> List[Finding]:
    """Collect, apply the budget, send. Returns what was actually delivered.

    A failed send is not recorded as sent, so the finding is retried on the
    next pass rather than silently dropped.
    """
    now = now or datetime.now(timezone.utc)
    state = _load_state()
    to_send = select_to_send(collect(now), state, now)

    delivered: List[Finding] = []
    for f in to_send:
        try:
            ok = bool(send(f.render()))
        except Exception as exc:
            ok = False
            logger.warning("Proactive send failed for %s: %s", f.key, exc)
        if not ok:
            continue
        delivered.append(f)
        state.setdefault("last_alerted", {})[f.key] = now.isoformat()
        if f.severity == NORMAL:
            day = now.date().isoformat()
            state.setdefault("sent_on", {})[day] = state.get("sent_on", {}).get(day, 0) + 1

    if delivered:
        # Keep the per-day counters from growing forever.
        cutoff = (now.date() - timedelta(days=7)).isoformat()
        state["sent_on"] = {d: n for d, n in state.get("sent_on", {}).items() if d >= cutoff}
        _save_state(state)
    return delivered


def pending_suggestions(now: Optional[datetime] = None) -> List[Finding]:
    """Suggestions for the weekly digest — never sent as alerts."""
    return [f for f in collect(now) if f.severity == SUGGESTION]


# --------------------------------------------------------------------------
# Weekly digest
#
# Suggestions never interrupt. They wait here for Sunday, where a mediocre
# observation costs ten seconds of reading rather than a notification. The
# digest also carries the record, so the week has a number attached even when
# nothing went wrong.
# --------------------------------------------------------------------------
DIGEST_WEEKDAY = 6   # Sunday, matching the other weekly jobs
DIGEST_HOUR = 17


def _week_key(now: datetime) -> str:
    iso = now.astimezone().isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def format_digest(suggestions: List[Finding], record: Optional[dict] = None) -> str:
    lines = ["📋 Ivy — week in review", ""]
    if record:
        lines.append(
            f"Record: {record['wins']}W-{record['losses']}L-{record['pushes']}P"
            f" · {record['decided']} decided"
        )
        tail = []
        if record.get("pending"):
            tail.append(f"{record['pending']} awaiting results")
        if record.get("unverifiable"):
            tail.append(f"{record['unverifiable']} ungradeable")
        if tail:
            lines.append("  " + " · ".join(tail))
        lines.append("")
    if suggestions:
        lines.append("Worth a look:")
        for s in suggestions:
            lines.append(f"• {s.title}")
            lines.append(f"  {s.detail}")
            if s.fix:
                lines.append(f"  → {s.fix}")
    else:
        lines.append("Nothing outstanding. Every check passed this week.")
    return "\n".join(lines)


def maybe_send_digest(send: Callable[[str], bool], now: Optional[datetime] = None) -> bool:
    """Send the weekly digest at most once per calendar week."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone()
    if local.weekday() != DIGEST_WEEKDAY or local.hour < DIGEST_HOUR:
        return False

    state = _load_state()
    week = _week_key(now)
    if state.get("last_digest_week") == week:
        return False

    record = None
    try:
        from ivy_core.picks_tracker import get_stats_overall
        record = get_stats_overall(days_back=7)
    except Exception as exc:
        logger.warning("Digest could not read the record: %s", exc)

    try:
        ok = bool(send(format_digest(pending_suggestions(now), record)))
    except Exception as exc:
        logger.warning("Digest send failed: %s", exc)
        return False
    if ok:
        # Only recorded on success, so a failed digest is retried rather than
        # skipped until next week.
        state["last_digest_week"] = week
        _save_state(state)
    return ok
