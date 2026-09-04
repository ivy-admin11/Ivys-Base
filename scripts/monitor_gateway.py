#!/usr/bin/env python3
"""Standalone health check for com.ivy.gateway (the real iMessage gateway).

Runs as its own launchd job (com.ivy.gateway_monitor), independent of the
gateway process itself, so it can still alert Henry when the gateway is
completely down. Hits the gateway over HTTP rather than importing main.py,
since importing only proves the module loads — not that the actual running
server is up.

Probes two endpoints, because they answer different questions:
  /health  — is the process alive at all?          (liveness)
  /ready   — can it actually serve requests?       (readiness, 503 + reasons)
Watching /health alone hid a real 3-day outage: on 2026-08-24 the iMessage
polling worker exited after repeated "authorization denied" errors reading
chat.db, so Ivy could still text out but could no longer see incoming
messages. /health kept returning 200 the whole time and this monitor stayed
silent. A readiness failure is now its own "degraded" state, alerted with
the specific failing checks named.

Alerts on state transitions (up->down, down->up) and re-alerts hourly while
still down, so a missed first alert doesn't mean total silence for a month
like the outage that prompted this script (2026-07-19 to 2026-08-21). The
very first run (no prior state on disk) only establishes a baseline and
never alerts — there's no real transition to report yet.

check_gateway_up() retries a few times before declaring the gateway down —
a single flaky request (one dropped connection, one slow response) already
triggered a false "DOWN" alert to Henry once while the gateway process
never actually stopped (2026-08-22). A real outage still fails every retry
within seconds, so this doesn't meaningfully slow real detection.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config import ADMIN_SECRET, HENRY_PHONE  # noqa: E402
from ivy_core import send_imessage  # noqa: E402

GATEWAY_HEALTH_URL = "http://127.0.0.1:8000/health"
GATEWAY_READY_URL = "http://127.0.0.1:8000/ready"
REQUEST_TIMEOUT_SECONDS = 5
STATE_PATH = os.path.join(PROJECT_ROOT, "logs", "gateway_monitor_state.json")
REALERT_INTERVAL_SECONDS = 3600
HEALTH_CHECK_ATTEMPTS = 3
HEALTH_CHECK_RETRY_DELAY_SECONDS = 2.5
# A "down" verdict is re-checked once after this pause before alerting. The
# gateway deliberately exits and lets launchd relaunch it when chat.db access
# is lost (main.py: _escalate_poller_restart); that restart takes ~5-10 s, and
# the first probe after the Mac wakes from sleep can also fail while the
# network stack is still coming up. Neither is an outage worth a text.
DOWN_RECHECK_DELAY_SECONDS = 20
# "degraded" (alive, /ready failing) must be seen on two consecutive runs
# before it is alerted: right after wake-from-sleep the poller's heartbeat is
# a few seconds stale and /ready reports it unhealthy for one cycle.
DEGRADED_CONSECUTIVE_RUNS_BEFORE_ALERT = 2


def _probe_once(url: str):
    """Return (status_code, body) — status_code None means the request itself
    failed (connection refused, timeout), which is what retries are for. A
    503 is a definitive answer from a live server and is NOT retried."""
    try:
        resp = requests.get(
            url,
            headers={"X-API-Key": ADMIN_SECRET},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException:
        return None, None
    try:
        body = resp.json()
    except ValueError:
        body = None
    return resp.status_code, body


def _probe_with_retries(url: str):
    """Retry only transport failures — a single dropped connection already
    triggered a false DOWN alert once (2026-08-22)."""
    for attempt in range(HEALTH_CHECK_ATTEMPTS):
        code, body = _probe_once(url)
        if code is not None:
            return code, body
        if attempt < HEALTH_CHECK_ATTEMPTS - 1:
            time.sleep(HEALTH_CHECK_RETRY_DELAY_SECONDS)
    return None, None


def _failed_checks(body) -> list:
    """Pull the false checks out of /ready's payload. The 503 body nests them
    under FastAPI's "detail"; a 200 body carries them at the top level."""
    if not isinstance(body, dict):
        return []
    payload = body.get("detail") if isinstance(body.get("detail"), dict) else body
    checks = payload.get("checks")
    if not isinstance(checks, dict):
        return []
    return sorted(name for name, ok in checks.items() if not ok)


def check_gateway() -> tuple:
    """Classify the gateway as up / degraded / down, with a reason.

    down     — /health unreachable: the process is gone or wedged.
    degraded — /health passes but /ready does not: alive, cannot serve.
    up       — both pass.
    """
    code, _ = _probe_with_retries(GATEWAY_HEALTH_URL)
    if code != 200:
        return "down", "/health unreachable" if code is None else f"/health returned {code}"

    code, body = _probe_with_retries(GATEWAY_READY_URL)
    if code is None:
        return "degraded", "/ready unreachable while /health passes"
    if code != 200:
        failing = _failed_checks(body)
        detail = ", ".join(failing) if failing else f"/ready returned {code}"
        return "degraded", detail
    return "up", "/health and /ready both passing"


def check_gateway_up() -> bool:
    """Back-compat shim for any caller that just wants a boolean."""
    return check_gateway()[0] == "up"


def load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


def main() -> int:
    now = time.time()
    timestamp = datetime.now(timezone.utc).isoformat()
    new_status, reason = check_gateway()
    if new_status == "down":
        time.sleep(DOWN_RECHECK_DELAY_SECONDS)
        new_status, reason = check_gateway()

    state = load_state()
    prev_status = state.get("status")

    # Debounce "degraded": count consecutive sightings, alert on the Nth.
    degraded_streak = (state.get("degraded_streak", 0) + 1) if new_status == "degraded" else 0
    state["degraded_streak"] = degraded_streak
    suppress_transition = (
        new_status == "degraded"
        and prev_status == "up"
        and degraded_streak < DEGRADED_CONSECUTIVE_RUNS_BEFORE_ALERT
    )

    alert_text = None
    if prev_status is None:
        print(f"[{timestamp}] first run, establishing baseline: gateway status={new_status} ({reason})")
    elif suppress_transition:
        print(
            f"[{timestamp}] gateway status=degraded ({reason}) — first sighting, "
            f"waiting for confirmation before alerting"
        )
    elif new_status != prev_status:
        if new_status == "down":
            alert_text = (
                "⚠️ Ivy gateway (com.ivy.gateway) is DOWN — "
                "/health check failed. iMessage replies won't work until it's restarted."
            )
        elif new_status == "degraded":
            alert_text = (
                f"⚠️ Ivy gateway is UP but NOT READY — failing: {reason}. "
                "It can still text out; incoming iMessages may not be processed."
            )
        else:
            alert_text = "✅ Ivy gateway (com.ivy.gateway) is back UP — /health and /ready passing."
    elif new_status != "up" and (now - state.get("last_alert_ts", 0.0)) > REALERT_INTERVAL_SECONDS:
        alert_text = (
            f"⚠️ Ivy gateway is STILL {new_status.upper()} — {reason}. Has not recovered."
        )

    if alert_text:
        print(f"[{timestamp}] {alert_text}")
        if send_imessage(HENRY_PHONE, alert_text):
            state["last_alert_ts"] = now
        else:
            print(f"[{timestamp}] WARNING: alert send failed")
    else:
        if prev_status is not None:
            print(f"[{timestamp}] gateway status={new_status} ({reason}), no alert needed")

    # Keep reporting "up" until a degraded reading is confirmed, so the
    # eventual confirmed alert still reads as an up->degraded transition.
    state["status"] = prev_status if suppress_transition else new_status
    state["reason"] = reason
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
