#!/bin/bash
# Scheduled entrypoint for the consolidated brief (com.ivy.daily_brief_*).
#
# Takes the slot as its one argument: morning or evening. Both launchd units
# call this same script, so there is one place to change how the brief runs.
#
# .env is loaded by the agent's own auto-loader — never export secrets from
# a shell script; `export $(cat .env | xargs)` leaks every value into `ps`
# output and breaks on anything containing spaces/quotes/$.

SLOT="$1"
if [ "$SLOT" != "morning" ] && [ "$SLOT" != "evening" ]; then
    echo "ERROR: expected 'morning' or 'evening' as the first argument, got '$SLOT'" >&2
    exit 2
fi

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

exec "$PYTHON" -m proactive_agents.daily_brief --slot "$SLOT" --send
