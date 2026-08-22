#!/usr/bin/env python3
"""Standalone health check for com.lexi.ivy (the real iMessage gateway).

Runs as its own launchd job (com.ivy.gateway_monitor), independent of the
gateway process itself, so it can still alert Henry when the gateway is
completely down. Hits /health over HTTP rather than importing main.py,
since importing only proves the module loads — not that the actual running
server is up.

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
REQUEST_TIMEOUT_SECONDS = 5
STATE_PATH = os.path.join(PROJECT_ROOT, "logs", "gateway_monitor_state.json")
REALERT_INTERVAL_SECONDS = 3600
HEALTH_CHECK_ATTEMPTS = 3
HEALTH_CHECK_RETRY_DELAY_SECONDS = 2.5


def _probe_once() -> bool:
    try:
        resp = requests.get(
            GATEWAY_HEALTH_URL,
            headers={"X-API-Key": ADMIN_SECRET},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        return resp.status_code == 200
    except requests.exceptions.RequestException:
        return False


def check_gateway_up() -> bool:
    for attempt in range(HEALTH_CHECK_ATTEMPTS):
        if _probe_once():
            return True
        if attempt < HEALTH_CHECK_ATTEMPTS - 1:
            time.sleep(HEALTH_CHECK_RETRY_DELAY_SECONDS)
    return False


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
    new_status = "up" if check_gateway_up() else "down"

    state = load_state()
    prev_status = state.get("status")

    alert_text = None
    if prev_status is None:
        print(f"[{timestamp}] first run, establishing baseline: gateway status={new_status}")
    elif new_status != prev_status:
        if new_status == "down":
            alert_text = (
                "⚠️ Ivy gateway (com.lexi.ivy) is DOWN — "
                "/health check failed. iMessage replies won't work until it's restarted."
            )
        else:
            alert_text = "✅ Ivy gateway (com.lexi.ivy) is back UP — /health passing again."
    elif new_status == "down" and (now - state.get("last_alert_ts", 0.0)) > REALERT_INTERVAL_SECONDS:
        alert_text = "⚠️ Ivy gateway (com.lexi.ivy) is STILL DOWN — has not recovered."

    if alert_text:
        print(f"[{timestamp}] {alert_text}")
        if send_imessage(HENRY_PHONE, alert_text):
            state["last_alert_ts"] = now
        else:
            print(f"[{timestamp}] WARNING: alert send failed")
    else:
        if prev_status is not None:
            print(f"[{timestamp}] gateway status={new_status}, no alert needed")

    state["status"] = new_status
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
