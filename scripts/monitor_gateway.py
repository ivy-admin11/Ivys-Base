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

Since 2026-09-14 it also watches the tailnet. Tailscale is the only route to
the gateway from outside the house -- uvicorn binds loopback, and the tailnet
proxies to it -- and nothing in this repo set it up, asserted it, or noticed
when it went. On 2026-09-14 the iMac showed offline on the tailnet from
Henry's phone while every other check here was green. The tailnet state
rides the same warnings channel as /ready's: one text when it changes, not
one every five minutes.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Optional

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


def _ready_warnings(body) -> list:
    """Pull /ready's non-fatal warnings out of its payload.

    These are conditions where the gateway serves fine but a guarantee behind
    it is gone — a dead failover brain being the one that mattered. They are
    deliberately not check failures: flunking readiness for them would take a
    working gateway out of rotation. But nothing watched them either, which is
    how the DeepSeek->Gemini failover sat broken for weeks while every probe
    reported green.
    """
    if not isinstance(body, dict):
        return []
    payload = body.get("detail") if isinstance(body.get("detail"), dict) else body
    warnings = payload.get("warnings")
    if not isinstance(warnings, list):
        return []
    return sorted(str(w) for w in warnings)


def fetch_ready_warnings() -> list:
    """Probe /ready purely for its warnings. Separate from check_gateway() so
    the up/degraded/down verdict keeps its exact shape."""
    _, body = _probe_with_retries(GATEWAY_READY_URL)
    return _ready_warnings(body)


# Where the Tailscale CLI lives on a Mac, in the order worth trying: on PATH,
# the App Store / standalone app's symlink, Homebrew, and the app bundle
# itself. launchd jobs get a thin PATH, so the bare name alone is not enough.
TAILSCALE_CANDIDATES = (
    "tailscale",
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
)
TAILSCALE_TIMEOUT_SECONDS = 10


def _tailscale_binary() -> Optional[str]:
    for candidate in TAILSCALE_CANDIDATES:
        if os.sep in candidate:
            if os.access(candidate, os.X_OK):
                return candidate
            continue
        found = shutil.which(candidate)
        if found:
            return found
    return None


def check_tailnet() -> tuple:
    """Classify this Mac's tailnet membership: connected / disconnected / absent.

    Returns ``(status, reason)``. ``reason`` is for the log; the warning text
    that reaches Henry is built separately and kept stable, because the
    warnings channel alerts once per distinct string and a reason that
    carried a changing error message would page on every run.
    """
    binary = _tailscale_binary()
    if not binary:
        return "absent", "no tailscale CLI on PATH, /usr/local/bin, /opt/homebrew/bin or in Tailscale.app"
    try:
        proc = subprocess.run(
            [binary, "status", "--json"],
            capture_output=True, text=True, timeout=TAILSCALE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        return "disconnected", f"`tailscale status` could not run: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:160]
        return "disconnected", f"`tailscale status` exit {proc.returncode}: {detail}"
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return "disconnected", "`tailscale status --json` returned something that is not JSON"

    backend = str(data.get("BackendState") or "unknown")
    node = data.get("Self") or {}
    if backend != "Running":
        return "disconnected", f"BackendState={backend}"
    if not node.get("Online", False):
        return "disconnected", "BackendState=Running but this node reports Online=false"
    ips = node.get("TailscaleIPs") or []
    name = node.get("HostName") or "this node"
    return "connected", f"online as {name}" + (f" ({ips[0]})" if ips else "")


def tailnet_warnings() -> list:
    """Warning strings for the shared channel. Stable per condition."""
    status, reason = check_tailnet()
    if status == "connected":
        return []
    if status == "absent":
        return [
            "tailnet_absent: no Tailscale on this Mac. It is the only route to "
            "the gateway from outside the house, and nothing else stands in for it"
        ]
    # Fold the reason down to the one token that distinguishes conditions
    # (NeedsLogin vs Stopped vs a dead daemon), so the text stays stable.
    if reason.startswith("BackendState="):
        state = reason.split("=", 1)[1].split()[0]
        hint = " -- `tailscale up` and follow the link" if state in ("NeedsLogin", "NoState", "Stopped") else ""
        return [f"tailnet_down: Tailscale is installed but {state}{hint}. The gateway is unreachable from outside the house until it reconnects"]
    return ["tailnet_down: Tailscale is installed but not responding. The gateway is unreachable from outside the house until it reconnects"]


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
    """Always returns a dict. A state file that is valid JSON but not an
    object (a half-written file, a hand-edit, an older format) used to be
    handed back as-is and blew up main() with an AttributeError — the one
    failure mode a monitor must not have, since a crashed monitor is a silent
    monitor."""
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


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

    # An alert that failed to send was never delivered, so nothing about it
    # may be recorded as done: neither the re-alert clock below nor the status
    # transition further down. Advancing the status used to swallow the alert
    # entirely — a dropped "back UP" text was never retried at all, and a
    # dropped "DOWN" text stayed silent until an hour after some *earlier*
    # alert. Holding the old status makes the next run see the same transition
    # and try again.
    # Warnings ride alongside the status machine rather than inside it: the
    # gateway is up, so no transition fires, but a guarantee that quietly
    # disappeared still has to reach Henry once. Alert on newly-appeared
    # warnings only — repeating a known one every two minutes would train him
    # to ignore the channel.
    warnings = fetch_ready_warnings()
    tailnet_status, tailnet_reason = check_tailnet()
    print(f"[{timestamp}] tailnet={tailnet_status} ({tailnet_reason})")
    warnings = warnings + tailnet_warnings()
    previously_warned = set(state.get("warnings", []))
    new_warnings = [w for w in warnings if w not in previously_warned]
    if new_warnings and not alert_text:
        alert_text = (
            "⚠️ Ivy is serving, but a guarantee behind it is gone:\n"
            + "\n".join(f"• {w}" for w in new_warnings)
        )
    elif new_warnings:
        print(f"[{timestamp}] additional warnings held back behind a status alert: {new_warnings}")

    alert_delivered = True
    if alert_text:
        print(f"[{timestamp}] {alert_text}")
        alert_delivered = bool(send_imessage(HENRY_PHONE, alert_text))
        if alert_delivered:
            state["last_alert_ts"] = now
        else:
            print(f"[{timestamp}] WARNING: alert send failed — will retry on the next run")
    else:
        if prev_status is not None:
            print(f"[{timestamp}] gateway status={new_status} ({reason}), no alert needed")

    # Keep reporting "up" until a degraded reading is confirmed, so the
    # eventual confirmed alert still reads as an up->degraded transition.
    state["status"] = prev_status if (suppress_transition or not alert_delivered) else new_status
    state["reason"] = reason
    # Only record warnings as "seen" once the text carrying them actually went
    # out, for the same reason the status is held back on a failed send.
    if alert_delivered:
        state["warnings"] = warnings
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
