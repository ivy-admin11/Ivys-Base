#!/bin/bash
# Scheduled entrypoint for Happy Hour Scout (com.ivy.happy_hour_scout).
#
# .env is loaded by the agent's own auto-loader — never export secrets from
# a shell script; `export $(cat .env | xargs)` leaks every value into `ps`
# output and breaks on anything containing spaces/quotes/$.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
    echo "ERROR: project venv python not found at $PYTHON" >&2
    exit 1
fi

cd "$PROJECT_ROOT" || exit 1
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONPATH="$PROJECT_ROOT"

mkdir -p "$PROJECT_ROOT/logs"
exec "$PYTHON" -m proactive_agents.happy_hour_scout --scheduled --send >> "$PROJECT_ROOT/logs/happy_hour_scheduled.log" 2>&1
