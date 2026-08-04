#!/usr/bin/env bash
# Safe production preflight. With no flags this validates launchd rendering and
# a real argv-only osascript round trip; it never opens Messages or sends data.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "${IVY_PROJECT_ROOT_OVERRIDE:-}" ]; then
    PROJECT_ROOT="$(cd "$IVY_PROJECT_ROOT_OVERRIDE" && pwd)"
else
    PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
INSTALLER="$PROJECT_ROOT/deploy/install_launchd.sh"
if [ -n "${IVY_PROJECT_ROOT_OVERRIDE:-}" ] && [ -x "$SCRIPT_DIR/install_launchd.sh" ]; then
    INSTALLER="$SCRIPT_DIR/install_launchd.sh"
fi
CHECK_RUNNING=false
LIVE_DELIVERY=false
RECIPIENT=""
ENV_FILE="$PROJECT_ROOT/.env"
ALLOWLIST_FILE="$PROJECT_ROOT/favorites.json"

usage() {
    cat <<USAGE
Usage: $0 [--check-running] [--env-file PATH]
       $0 --check-running --live-delivery --recipient IMESSAGE_ADDRESS
          [--allowlist PATH]

The default test is non-delivering. --live-delivery additionally requires an
exact recipient membership in the configured allowlist and an interactive,
recipient-specific typed confirmation.
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --check-running) CHECK_RUNNING=true; shift ;;
        --live-delivery) LIVE_DELIVERY=true; shift ;;
        --recipient)
            [ "$#" -ge 2 ] || { echo "ERROR: --recipient requires a value." >&2; exit 2; }
            RECIPIENT="$2"
            shift 2
            ;;
        --env-file)
            [ "$#" -ge 2 ] || { echo "ERROR: --env-file requires a value." >&2; exit 2; }
            ENV_FILE="$2"
            shift 2
            ;;
        --allowlist)
            [ "$#" -ge 2 ] || { echo "ERROR: --allowlist requires a value." >&2; exit 2; }
            ALLOWLIST_FILE="$2"
            shift 2
            ;;
        -h|--help) usage; exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ "$(uname -s)" != "Darwin" ]; then
    echo "ERROR: the production smoke test requires macOS." >&2
    exit 1
fi
if [ ! -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    echo "ERROR: project virtualenv Python is missing: $PROJECT_ROOT/.venv/bin/python" >&2
    exit 1
fi
if [ ! -x /usr/bin/osascript ]; then
    echo "ERROR: /usr/bin/osascript is unavailable." >&2
    exit 1
fi

PYTHON="$PROJECT_ROOT/.venv/bin/python"

echo "== launchd render, runtime preflight, and plist validation =="
IVY_PROJECT_ROOT_OVERRIDE="$PROJECT_ROOT" "$INSTALLER"

echo "== safe real osascript argv round trip (does not touch Messages.app) =="
PYTHONPATH="$PROJECT_ROOT" "$PYTHON" - <<'PY'
from utils.applescript import AppleScriptRunner

script = """
on run argv
    return item 1 of argv & "|" & item 2 of argv
end run
"""
tricky = 'a "quoted\\backslash" value with \'apostrophes\''
result = AppleScriptRunner().run_argv(script, ["smoke recipient", tricky])
if result != "smoke recipient|" + tricky:
    raise SystemExit("safe osascript argv round trip failed")
print("Safe osascript argv round trip passed.")
PY

if [ "$CHECK_RUNNING" = true ]; then
    echo "== authenticated local service probes =="
    "$SCRIPT_DIR/monitor_ivy.sh" --env-file "$ENV_FILE"
else
    echo "Authenticated service probes skipped; add --check-running to enable them."
fi

if [ "$LIVE_DELIVERY" != true ]; then
    echo "No Messages.app delivery was attempted."
    exit 0
fi

if [ "$CHECK_RUNNING" != true ]; then
    echo "ERROR: --live-delivery requires --check-running so readiness is proven immediately before sending." >&2
    exit 2
fi
if [ -z "$RECIPIENT" ]; then
    echo "ERROR: --live-delivery requires --recipient." >&2
    exit 2
fi
if [ ! -f "$ALLOWLIST_FILE" ] || [ -L "$ALLOWLIST_FILE" ]; then
    echo "ERROR: live delivery allowlist must be a regular, non-symlink JSON file." >&2
    exit 1
fi

masked_recipient="$({ IVY_SMOKE_RECIPIENT="$RECIPIENT" IVY_SMOKE_ALLOWLIST="$ALLOWLIST_FILE" "$PYTHON" - <<'PY'
import json
import os
from pathlib import Path
import stat

recipient = os.environ["IVY_SMOKE_RECIPIENT"]
allowlist_path = Path(os.environ["IVY_SMOKE_ALLOWLIST"])
if stat.S_IMODE(allowlist_path.stat().st_mode) & 0o077:
    raise SystemExit("allowlist must not be group or world accessible")
try:
    with allowlist_path.open("r", encoding="utf-8") as handle:
        contacts = json.load(handle)
except (OSError, UnicodeError, json.JSONDecodeError):
    raise SystemExit("allowlist could not be parsed") from None
if not isinstance(contacts, list) or not all(isinstance(item, str) for item in contacts):
    raise SystemExit("allowlist must be a JSON list containing only strings")
if recipient not in contacts:
    raise SystemExit("recipient is not an exact member of the configured allowlist")

if "@" in recipient:
    local, domain = recipient.rsplit("@", 1)
    visible = local[:1] if local else ""
    masked = f"{visible}***@{domain}"
else:
    suffix = recipient[-4:] if len(recipient) >= 4 else recipient[-1:]
    masked = f"***{suffix}"
print(masked)
PY
} 2>&1)" || {
    echo "ERROR: live-delivery recipient validation failed: $masked_recipient" >&2
    exit 1
}
confirm_phrase="SEND IVY SMOKE MESSAGE TO $masked_recipient"
if [ ! -r /dev/tty ]; then
    echo "ERROR: live delivery requires an interactive terminal for typed confirmation." >&2
    exit 1
fi

echo
echo "WARNING: the next step sends one real iMessage to allowlisted recipient $masked_recipient."
printf 'Type exactly "%s" to continue: ' "$confirm_phrase" > /dev/tty
confirmation=""
IFS= read -r confirmation < /dev/tty || true
if [ "$confirmation" != "$confirm_phrase" ]; then
    echo "Confirmation did not match; no message was sent." >&2
    exit 1
fi

message="Ivy production smoke test $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
if ! IVY_SMOKE_RECIPIENT="$RECIPIENT" IVY_SMOKE_MESSAGE="$message" PYTHONPATH="$PROJECT_ROOT" \
    "$PYTHON" - <<'PY'
import os

from ivy_core.messaging import send_imessage

if not send_imessage(os.environ["IVY_SMOKE_RECIPIENT"], os.environ["IVY_SMOKE_MESSAGE"]):
    raise SystemExit("Messages.app did not return a success receipt")
PY
then
    echo "Live delivery smoke test failed." >&2
    exit 1
fi

echo "Live delivery smoke test returned a success receipt. Confirm receipt on the destination device."
