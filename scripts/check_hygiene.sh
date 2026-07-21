#!/usr/bin/env bash
# Single local/CI entrypoint for repository hygiene + the same checks
# .github/workflows/ci.yml runs, so local and CI verification cannot drift.
#
# Never installs dependencies, and never touches the real .env, credential
# files, the real picks database, Google, Odds API, Grok, or iMessage — all
# secrets/contacts are set to hermetic placeholder values below.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    PYTHON="$REPO_ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
else
    echo "ERROR: no Python interpreter found (.venv/bin/python or python3)." >&2
    echo "       Create the venv: python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt" >&2
    exit 1
fi

# Hermetic env — mirrors .github/workflows/ci.yml exactly, so nothing here
# ever reads the real .env / credential files / hits a live provider.
export ALLOW_INSECURE_ADMIN_SECRET="true"
export ADMIN_SECRET="hygiene-check-secret"
export HENRY_PHONE="+15555550100"
export LEXI_PHONE="+15555550101"
export GEMINI_API_KEY=""
export DEEPSEEK_API_KEY=""
export XAI_API_KEY="hygiene-check-not-real"
export ODDS_API_KEY=""
export READWISE_API_KEY=""
export ENABLE_IMESSAGE_POLLER="false"

_require_tool() {
    local module="$1"
    if ! "$PYTHON" -c "import ${module}" >/dev/null 2>&1; then
        echo "ERROR: required dev tool '${module}' is not installed for ${PYTHON}." >&2
        echo "       Install it: ${PYTHON} -m pip install -r requirements-dev.txt" >&2
        exit 1
    fi
}

_require_tool ruff
_require_tool bandit
_require_tool pytest

echo "== Sanitized repo hygiene checker =="
"$PYTHON" "$SCRIPT_DIR/check_repo_hygiene.py"

echo "== compileall =="
"$PYTHON" -m compileall -q main.py config.py registry.py job_runner.py trigger_capabilities_alert.py cache_manager.py voice_assistant.py ivy_core proactive_agents utils tests

echo "== Ruff =="
"$PYTHON" -m ruff check .

echo "== Bandit =="
"$PYTHON" -m bandit -r main.py config.py registry.py job_runner.py ivy_core proactive_agents utils cache_manager.py voice_assistant.py trigger_capabilities_alert.py -ll

echo "== pytest =="
"$PYTHON" -m pytest -v

echo "== pip check =="
"$PYTHON" -m pip check

echo "== git diff --check (whitespace errors) =="
git diff --check

echo "All hygiene checks passed."
