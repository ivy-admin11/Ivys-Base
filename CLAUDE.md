# Ivy Local Admin API Environment

## System Architecture
- **Core Engine:** `main.py` (FastAPI app; the local iMessage database poller runs as a background thread spawned on startup)
- **Primary Database:** macOS Chat DB (`~/Library/Messages/chat.db`)
- **Primary LLM:** DeepSeek (`deepseek-chat`); falls back to Gemini (`gemini-2.5-flash`) only on provider failure, timeout, or empty response — never merely because DeepSeek gave an honest answer.
- **Tool schema:** generated from one canonical source (`registry.py`) for both providers — never hand-edit `GEMINI_TOOL_DECLARATIONS`/`DEEPSEEK_TOOL_SCHEMA` separately, they don't exist as separate lists anymore.
- **Job agents:** `ivy_core/` (version-controlled — `env.py`, `messaging.py`, `llm.py`, `receipts.py`) is the shared library every proactive agent imports. There is no untracked `.ivy/ivy_core.py` dependency anymore.
- **Integrations:** Google Docs API, Google Slides API
- **Automation Pipeline:** AppleScript via `osascript`, invoked with `on run argv` so untrusted content (recipient, message body, attachment path) is passed as process arguments, never interpolated into AppleScript source text.
- **Google OAuth files:** `get_google_service` reads `~/ai-admin-api/token.json` and `~/ai-admin-api/google_credentials.json` — copy or symlink these from the project root if needed

## Development Guidelines
- Always preserve the dual-brain failover structure (DeepSeek → Gemini) inside `main.py`.
- Keep text replies short, concise, and direct (under 40 words).
- Endpoints require the `X-API-Key` header to match `ADMIN_SECRET`. The process fails closed — it refuses to start at all if `ADMIN_SECRET` is unset (set `ALLOW_INSECURE_ADMIN_SECRET=true` for local/test use only).
- Job execution is automatic when the user mentions running jobs via iMessage — Ivy will offer and execute them.
- Never claim a job ran, a message sent, or a file attached unless a real runtime receipt (see `/executions`, `logs/executions.db`) supports it.

## Job Execution System
Ivy can run background jobs on-demand via natural language, dispatched through the single registry in `job_runner.py`. Available jobs:
- **Sharp Picks** (aliases: picks, sharppicks, sports picks, sports bettor, run picks, send me sharp picks) — daily sports matchup analysis. `proactive_agents/sports_bettor.py`.
- **Happy Hour Scout** (aliases: happy hour, scout, hh scout) — find nearby venues and deals. `proactive_agents/happy_hour_scout.py`.
- **Familia Meal Planner** (aliases: meals, meal plan, planner, weekly planner, household meal plan) — generate a weekly fusion meal plan. `proactive_agents/Familia_meal_planner.py`. (This replaces the old "Weekly Planner" name, which pointed at a `weekly_planner.py` that was never actually committed to any branch — the alias now resolves to the real, working implementation.)
- **Brain** (aliases: grok, xai) — knowledge queries via Grok. Lives outside this repo at `~/ai-admin-api/agent.py`.
- **Bravo Scout** (aliases: bravo, reality scout) — **currently unavailable**: `proactive_agents/bravo_scout.py` doesn't exist in this repo (no implementation has ever been committed to main). `/capabilities` and `./ivy list` report this honestly rather than silently omitting it or pretending it works.

Sharp Picks and Familia Meal Planner support both a real schedule (via launchd — see `deploy/launchd/`) and ad-hoc/on-demand dispatch (via a detached subprocess, no launchd required — see `job_runner._run_entrypoint_job`). An ad-hoc request always passes `force=True`, bypassing whatever duplicate-suppression/48h-gate the scheduled cadence uses, so "run picks now" always delivers.

Run jobs via:
- **iMessage:** `ivy run sharp picks` or `ivy run happy hour` (Ivy understands natural language)
- **Terminal:** `./ivy run picks` or `./ivy list` to see all jobs (each agent also has its own CLI: `python -m proactive_agents.sports_bettor --force --send`)
- **API:** `POST /run-job?job_name=sharp_picks` with `X-API-Key` header

## Common Operations
- Run the Live Engine: `uvicorn main:app --host 127.0.0.1 --port 8000` from `~/openclaw-admin/`
- Test Failover: `export DEEPSEEK_API_KEY="broken_key_test" && uvicorn main:app --host 127.0.0.1 --port 8000`
- Review Active Local Logs: logs are emitted to stdout; capture with `uvicorn main:app --host 127.0.0.1 --port 8000 2>&1 | tee run.log`
- List available jobs: `./ivy list`
- Run a job from terminal: `./ivy run meals` (run the Familia meal planner)
- Check readiness/health: `GET /health` (liveness), `GET /ready` (503 if a required component is down), `GET /version` (git SHA, PID, dirty-tree state), `GET /capabilities` (tools + jobs, including unavailable ones)
- Inspect job execution history: `GET /executions` or `GET /executions/{execution_id}` (backed by `logs/executions.db`)
- Render (never install without review) launchd plist templates: `./deploy/install_launchd.sh` (dry-run by default; `--apply` to write, `--yes-i-know-this-is-live` required on top to touch any currently-installed scheduled job)
