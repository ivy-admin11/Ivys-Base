#!/usr/bin/env bash
# Authenticated local health/readiness/version monitor. The API key is supplied
# to curl through a mode-600 config file, never on the command line or in logs.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "${IVY_PROJECT_ROOT_OVERRIDE:-}" ]; then
    PROJECT_ROOT="$(cd "$IVY_PROJECT_ROOT_OVERRIDE" && pwd)"
else
    PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
BASE_URL="http://127.0.0.1:8000"
ENV_FILE="$PROJECT_ROOT/.env"
TIMEOUT_SECONDS=10

usage() {
    echo "Usage: $0 [--base-url http://127.0.0.1:PORT] [--env-file PATH] [--timeout SECONDS]"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --base-url)
            [ "$#" -ge 2 ] || { echo "ERROR: --base-url requires a value." >&2; exit 2; }
            BASE_URL="$2"
            shift 2
            ;;
        --env-file)
            [ "$#" -ge 2 ] || { echo "ERROR: --env-file requires a value." >&2; exit 2; }
            ENV_FILE="$2"
            shift 2
            ;;
        --timeout)
            [ "$#" -ge 2 ] || { echo "ERROR: --timeout requires a value." >&2; exit 2; }
            TIMEOUT_SECONDS="$2"
            shift 2
            ;;
        -h|--help) usage; exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ ! "$BASE_URL" =~ ^http://(127\.0\.0\.1|localhost)(:[0-9]+)?/?$ ]] \
    && [[ ! "$BASE_URL" =~ ^http://\[::1\](:[0-9]+)?/?$ ]]; then
    echo "ERROR: refusing to send the admin credential to a non-loopback URL: $BASE_URL" >&2
    exit 2
fi
BASE_URL="${BASE_URL%/}"

case "$TIMEOUT_SECONDS" in
    ''|*[!0-9]*) echo "ERROR: --timeout must be a positive integer." >&2; exit 2 ;;
esac
if [ "$TIMEOUT_SECONDS" -lt 1 ]; then
    echo "ERROR: --timeout must be at least 1 second." >&2
    exit 2
fi

if ! command -v curl >/dev/null 2>&1; then
    echo "ERROR: curl is required." >&2
    exit 1
fi

if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    PYTHON="$PROJECT_ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
else
    echo "ERROR: Python 3 is required to validate probe responses." >&2
    exit 1
fi

read_admin_secret() {
    local line value
    [ -f "$ENV_FILE" ] || return 1
    line="$(sed -n -E '/^[[:space:]]*(export[[:space:]]+)?ADMIN_SECRET[[:space:]]*=/p' "$ENV_FILE" | tail -n 1)"
    [ -n "$line" ] || return 1
    value="${line#*=}"
    value="$(printf '%s' "$value" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')"
    case "$value" in
        \"*\") value="${value#\"}"; value="${value%\"}" ;;
        \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    [ -n "$value" ] || return 1
    printf '%s' "$value"
}

secret="${ADMIN_SECRET:-}"
if [ -z "$secret" ]; then
    if ! secret="$(read_admin_secret)"; then
        echo "ERROR: ADMIN_SECRET is neither exported nor present in $ENV_FILE" >&2
        exit 1
    fi
fi

case "$secret" in
    *$'\n'*|*$'\r'*) echo "ERROR: ADMIN_SECRET contains an unsupported newline." >&2; exit 1 ;;
esac

umask 077
work_dir="$(mktemp -d "${TMPDIR:-/tmp}/ivy-monitor.XXXXXX")"
trap 'rm -rf "$work_dir"' EXIT
chmod 700 "$work_dir"
auth_config="$work_dir/curl.conf"
escaped_secret="$(printf '%s' "$secret" | sed 's/\\/\\\\/g; s/"/\\"/g')"
printf 'header = "X-API-Key: %s"\n' "$escaped_secret" > "$auth_config"
chmod 600 "$auth_config"
unset secret escaped_secret ADMIN_SECRET

validate_payload() {
    local endpoint="$1"
    local body_file="$2"
    "$PYTHON" - "$endpoint" "$body_file" <<'PY'
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone

endpoint, body_path = sys.argv[1:]
try:
    with open(body_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
except (OSError, UnicodeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"invalid JSON response: {exc}")

if not isinstance(payload, dict):
    raise SystemExit("response must be a JSON object")
if endpoint == "/health" and payload.get("status") != "ok":
    raise SystemExit("health status is not ok")
if endpoint == "/ready" and payload.get("ready") is not True:
    raise SystemExit("readiness is false")
if endpoint == "/version":
    sha = payload.get("git_sha")
    if not isinstance(sha, str) or not sha or sha == "unknown":
        raise SystemExit("version has no usable git SHA")
    if payload.get("dirty_working_tree") is not False:
        raise SystemExit("running checkout is dirty or its state is unknown")
if endpoint == "/runtime":
    imessage = payload.get("imessage")
    if not isinstance(imessage, dict) or imessage.get("ready") is not True:
        raise SystemExit("iMessage runtime is not ready")
    oldest = imessage.get("oldest_queued_age_seconds")
    if isinstance(oldest, (int, float)) and oldest > 30:
        raise SystemExit("oldest queued request exceeds 30 seconds")
if endpoint.startswith("/executions"):
    executions = payload.get("executions")
    if not isinstance(executions, list):
        raise SystemExit("execution history is unavailable")
    by_job = defaultdict(list)
    active = {"queued", "dispatched", "running"}
    failures = {"failed", "dispatch_failed", "timed_out", "completion_unknown"}
    now = datetime.now(timezone.utc)
    for record in executions:
        if not isinstance(record, dict):
            continue
        job_name = str(record.get("job_name") or "unknown")
        by_job[job_name].append(str(record.get("status") or "unknown"))
        if record.get("status") in active:
            try:
                started = datetime.fromisoformat(
                    str(record.get("started_at") or "").replace("Z", "+00:00")
                )
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                if (now - started).total_seconds() > 21_900:
                    raise SystemExit(f"active job exceeded maximum observable runtime: {job_name}")
            except ValueError:
                raise SystemExit(f"active job has invalid start time: {job_name}")
    for job_name, statuses in by_job.items():
        if len(statuses) >= 3 and all(status in failures for status in statuses[:3]):
            raise SystemExit(f"three consecutive failures recorded for job: {job_name}")
PY
}

failed=false
for endpoint in /health /ready /runtime /version '/executions?limit=50'; do
    body_file="$work_dir/$(printf '%s' "$endpoint" | tr '/' '_').json"
    http_code=""
    if ! http_code="$(curl \
        --disable \
        --silent \
        --show-error \
        --noproxy '*' \
        --proto '=http' \
        --output "$body_file" \
        --write-out '%{http_code}' \
        --connect-timeout "$TIMEOUT_SECONDS" \
        --max-time "$TIMEOUT_SECONDS" \
        --config "$auth_config" \
        "$BASE_URL$endpoint")"; then
        echo "FAIL $endpoint: connection or transport error" >&2
        failed=true
        continue
    fi

    if [ "$http_code" != "200" ]; then
        echo "FAIL $endpoint: HTTP $http_code" >&2
        failed=true
        continue
    fi

    validation_error="$work_dir/validation.err"
    if ! validate_payload "$endpoint" "$body_file" 2> "$validation_error"; then
        echo "FAIL $endpoint: $(sed -n '1p' "$validation_error")" >&2
        failed=true
        continue
    fi
    echo "OK   $endpoint"
done

if [ "$failed" = true ]; then
    echo "Ivy monitor failed." >&2
    exit 1
fi

echo "Ivy is healthy, ready, versioned, queue-safe, and free of repeated job failures."
