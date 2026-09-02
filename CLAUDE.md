# Ivy Local Admin API Environment

## System Architecture
- **Core Engine:** `main.py` (FastAPI app; the local iMessage database poller runs as a background thread spawned on startup)
- **Primary Database:** macOS Chat DB (`~/Library/Messages/chat.db`)
- **Primary LLM:** DeepSeek (`deepseek-chat`); falls back to Gemini (`gemini-2.5-flash`) only on provider failure, timeout, or empty response — never merely because DeepSeek gave an honest answer.
- **Tool schema:** generated from one canonical source (`registry.py`) for both providers — never hand-edit `GEMINI_TOOL_DECLARATIONS`/`DEEPSEEK_TOOL_SCHEMA` separately, they don't exist as separate lists anymore.
- **Job agents:** `ivy_core/` (version-controlled — `env.py`, `messaging.py`, `llm.py`, `receipts.py`) is the shared library every proactive agent imports. There is no untracked `.ivy/ivy_core.py` dependency anymore.
- **Automation Pipeline:** AppleScript via `osascript`, invoked with `on run argv` so untrusted content (recipient, message body, attachment path) is passed as process arguments, never interpolated into AppleScript source text.
- **Google OAuth files:** `get_google_service` reads `~/ai-admin-api/token.json` and `~/ai-admin-api/google_credentials.json` — copy or symlink these from the project root if needed

## Development Guidelines
- Always preserve the dual-brain failover structure (DeepSeek → Gemini) inside `main.py`.
- Keep text replies short, concise, and direct (under 40 words).
- Endpoints require the `X-API-Key` header to match `ADMIN_SECRET`. The process fails closed — it refuses to start at all if `ADMIN_SECRET` is unset (set `ALLOW_INSECURE_ADMIN_SECRET=true` for local/test use only).
- Job execution is automatic when the user mentions running jobs via iMessage — Ivy will offer and execute them.
- Never claim a job ran, a message sent, or a file attached unless a real runtime receipt (see `/executions`, `logs/executions.db`) supports it. For attachments the receipt is chat.db itself: `ivy_core.messaging.send_imessage_attachment` sends via Messages' `participant … of account` scripting verb first, verifies the row through `GET /imessage/attachments`, and only falls back to clipboard-paste UI automation when the screen is unlocked. AppleScript's "SUCCESS" return is not evidence of delivery.
- The iMessage poller keeps the last 8 turns per sender for 45 minutes (`conversation_history` in `main.py`) so "yes, the full recipe" is answered in context instead of dispatched as a job. `run_job` fires only on an explicit run/start/send request.

## Job Execution System
Ivy can run background jobs on-demand via natural language, dispatched through the single registry in `job_runner.py` — job names, aliases, and entrypoints live there. Run `./ivy list` (or `GET /capabilities`) to see them all, including unavailable ones.

Sharp Picks and Familia Meal Planner support both a real schedule (via launchd — see `deploy/launchd/`) and ad-hoc/on-demand dispatch (via a detached subprocess, no launchd required — see `job_runner._run_entrypoint_job`). An ad-hoc request always passes `force=True`, bypassing whatever duplicate-suppression/48h-gate the scheduled cadence uses, so "run picks now" always delivers.

Run jobs via:
- **iMessage:** `ivy run sharp picks` or `ivy run happy hour` (Ivy understands natural language)
- **Terminal:** `./ivy run picks` or `./ivy list` to see all jobs (each agent also has its own CLI: `python -m proactive_agents.sports_bettor --force --send`)
- **API:** `POST /run-job?job_name=sharp_picks` with `X-API-Key` header

## Common Operations
See the `ivy-ops` skill (`.claude/skills/ivy-ops/SKILL.md`) for running the engine, failover testing, health/readiness checks, execution history, and rendering launchd plists. Never install a launchd plist without reviewing the rendered output first.
