#!/usr/bin/env bash
# Restart only the always-on Ivy gateway. Scheduled delivery agents are
# intentionally excluded because kickstarting one could trigger a live send.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "${IVY_PROJECT_ROOT_OVERRIDE:-}" ]; then
    PROJECT_ROOT="$(cd "$IVY_PROJECT_ROOT_OVERRIDE" && pwd)"
else
    PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
LABEL="com.ivy.gateway"
DOMAIN="gui/$(id -u)"
ENV_FILE="$PROJECT_ROOT/.env"
BASE_URL="http://127.0.0.1:8000"
APPLY=false
CONFIRM_LIVE=false
RUN_MONITOR=true
RELOAD_PLIST=false
READINESS_TIMEOUT_SECONDS="${IVY_READINESS_TIMEOUT_SECONDS:-90}"

usage() {
    cat <<USAGE
Usage: $0 [--apply --yes-i-know-this-is-live] [--env-file PATH]
          [--base-url http://127.0.0.1:PORT]
          [--readiness-timeout-seconds 90..600] [--no-monitor]
          [--reload-plist]

With no apply flags, prints the exact gateway restart and health-check plan.
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --apply) APPLY=true; shift ;;
        --yes-i-know-this-is-live) CONFIRM_LIVE=true; shift ;;
        --no-monitor) RUN_MONITOR=false; shift ;;
        --reload-plist) RELOAD_PLIST=true; shift ;;
        --env-file)
            [ "$#" -ge 2 ] || { echo "ERROR: --env-file requires a value." >&2; exit 2; }
            ENV_FILE="$2"
            shift 2
            ;;
        --base-url)
            [ "$#" -ge 2 ] || { echo "ERROR: --base-url requires a value." >&2; exit 2; }
            BASE_URL="$2"
            shift 2
            ;;
        --readiness-timeout-seconds)
            [ "$#" -ge 2 ] || { echo "ERROR: --readiness-timeout-seconds requires a value." >&2; exit 2; }
            READINESS_TIMEOUT_SECONDS="$2"
            shift 2
            ;;
        -h|--help) usage; exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

case "$READINESS_TIMEOUT_SECONDS" in
    ''|*[!0-9]*) echo "ERROR: readiness timeout must be an integer from 90 through 600 seconds." >&2; exit 2 ;;
esac
if [ "$READINESS_TIMEOUT_SECONDS" -lt 90 ] || [ "$READINESS_TIMEOUT_SECONDS" -gt 600 ]; then
    echo "ERROR: readiness timeout must be from 90 through 600 seconds." >&2
    exit 2
fi

echo "Ivy gateway restart"
echo "  Service: $DOMAIN/$LABEL"
if [ "$APPLY" = false ]; then
    echo "  Mode:    DRY-RUN"
    if [ "$RELOAD_PLIST" = true ]; then
        echo "Would verify the service is loaded, boot it out, bootstrap the reviewed installed plist, then probe readiness."
    else
        echo "Would verify the service is loaded, run launchctl kickstart -k, then probe readiness."
    fi
    echo "No process was restarted. Apply with:"
    echo "  $0 --apply --yes-i-know-this-is-live"
    exit 0
fi

if [ "$CONFIRM_LIVE" != true ]; then
    echo "ERROR: --apply requires --yes-i-know-this-is-live." >&2
    exit 2
fi
if [ "$(uname -s)" != "Darwin" ]; then
    echo "ERROR: live restart is supported only on macOS." >&2
    exit 1
fi
if [ ! -x /bin/launchctl ]; then
    echo "ERROR: /bin/launchctl is unavailable." >&2
    exit 1
fi
if [ ! -f "$HOME/Library/LaunchAgents/$LABEL.plist" ]; then
    echo "ERROR: installed plist is missing: $HOME/Library/LaunchAgents/$LABEL.plist" >&2
    exit 1
fi
if ! /bin/launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "ERROR: $LABEL is not loaded. Review and bootstrap it explicitly before restart." >&2
    exit 1
fi

echo "Restarting $LABEL..."
if [ "$RELOAD_PLIST" = true ]; then
    /bin/launchctl bootout "$DOMAIN/$LABEL"
    /bin/launchctl bootstrap "$DOMAIN" "$HOME/Library/LaunchAgents/$LABEL.plist"
else
    /bin/launchctl kickstart -k "$DOMAIN/$LABEL"
fi

if [ "$RUN_MONITOR" = false ]; then
    echo "Gateway restart requested; post-restart monitoring was explicitly skipped."
    exit 0
fi

started_at="$(date +%s)"
deadline=$((started_at + READINESS_TIMEOUT_SECONDS))
attempt=1
while :; do
    if "$SCRIPT_DIR/monitor_ivy.sh" --env-file "$ENV_FILE" --base-url "$BASE_URL"; then
        echo "Gateway restart completed and passed authenticated monitoring."
        exit 0
    fi
    now="$(date +%s)"
    if [ "$now" -ge "$deadline" ]; then
        break
    fi
    remaining=$((deadline - now))
    sleep_seconds=2
    if [ "$remaining" -lt "$sleep_seconds" ]; then
        sleep_seconds="$remaining"
    fi
    echo "Gateway not ready yet (attempt $attempt; ${remaining}s remain); retrying in ${sleep_seconds}s..." >&2
    sleep "$sleep_seconds"
    attempt=$((attempt + 1))
done

echo "ERROR: gateway restarted but did not become ready within ${READINESS_TIMEOUT_SECONDS}s." >&2
exit 1
