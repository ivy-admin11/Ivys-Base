#!/bin/bash
# 1. Secrets are loaded by sports_bettor.py itself (its .env auto-loader, anchored
#    to the project root). The previous `export $(grep ... | xargs)` broke on any
#    value containing spaces/quotes/$ and leaked every secret into `ps` output, so
#    it was removed — the Python loader is the single, safe source.

# 2. Path correction for background binaries and project root module imports
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONPATH="/Users/lexi/openclaw-admin"

# 3. Fire script execution payload (uses project .venv pinned to Python 3.12 for xai-sdk support)
/Users/lexi/openclaw-admin/.venv/bin/python /Users/lexi/openclaw-admin/proactive_agents/sports_bettor.py >> /Users/lexi/openclaw-admin/sports_cron.log 2>&1
