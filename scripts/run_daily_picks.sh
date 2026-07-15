#!/bin/bash
# Scheduled entrypoint for Sharp Picks (com.ivy.sharppicks).
#
# Secrets are loaded by sports_bettor.py itself (its .env auto-loader,
# anchored to the project root) — never export secrets from a shell script;
# `export $(cat .env | xargs)` leaks every value into `ps` output and breaks
# on anything containing spaces/quotes/$.

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
# --scheduled preserves the duplicate-suppression gate; --send actually
# delivers (the scheduled cadence has always been expected to text Henry —
# unlike an ad-hoc CLI invocation, this is not a dry run by default).
exec "$PYTHON" -m proactive_agents.sports_bettor --scheduled --send >> "$PROJECT_ROOT/logs/sharppicks_scheduled.log" 2>&1
