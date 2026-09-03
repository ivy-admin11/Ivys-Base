# Ivy Local Admin API Environment

## System Architecture
- **Core Engine:** `main.py` (FastAPI app; the local iMessage database poller runs as a background thread spawned on startup)
- **Primary Database:** macOS Chat DB (`~/Library/Messages/chat.db`)
- **Primary LLM:** DeepSeek (`deepseek-v4-flash`); falls back to Gemini (`gemini-2.5-flash`) only on provider failure, timeout, or empty response — never merely because DeepSeek gave an honest answer.
- **Tool schema:** generated from one canonical source (`registry.py`) for both providers — never hand-edit `GEMINI_TOOL_DECLARATIONS`/`DEEPSEEK_TOOL_SCHEMA` separately, they don't exist as separate lists anymore.
- **Job agents:** `ivy_core/` (version-controlled — `env.py`, `messaging.py`, `llm.py`, `receipts.py`, `text_delivery.py`) is the shared library every proactive agent imports. There is no untracked `.ivy/ivy_core.py` dependency anymore.
- **Report delivery:** text-first, through `ivy_core.text_delivery.deliver_report`. Every job puts its content in the message body on every run; the PDF is copied to the outbox and sent ONLY when someone replies `PDF` / `RESEND`. Jobs must not call `send_imessage_attachment` directly.
- **Automation Pipeline:** AppleScript via `osascript`, invoked with `on run argv` so untrusted content (recipient, message body, attachment path) is passed as process arguments, never interpolated into AppleScript source text.
- **Google OAuth files:** `get_google_service` reads `~/ai-admin-api/token.json` and `~/ai-admin-api/google_credentials.json` — copy or symlink these from the project root if needed

## Development Guidelines
- Always preserve the dual-brain failover structure (DeepSeek → Gemini) inside `main.py`.
- Keep text replies short, concise, and direct (under 40 words). Exception: when the user explicitly asks for a full recipe, list, or step-by-step plan, give the complete thing compactly.
- Endpoints require the `X-API-Key` header to match `ADMIN_SECRET`. The process fails closed — it refuses to start at all if `ADMIN_SECRET` is unset (set `ALLOW_INSECURE_ADMIN_SECRET=true` for local/test use only).
- Job execution is automatic when the user mentions running jobs via iMessage — Ivy will offer and execute them.
- Reports are text. A job that has something to say must say it in the message body — including when a sweep surfaced picks that all fell below the quality bar. Silence is indistinguishable from the job not running, and that is the bug that produced this rule.
- Never claim a job ran, a message sent, or a file attached unless a real runtime receipt (see `/executions`, `logs/executions.db`) supports it. For attachments the receipt is chat.db itself: `ivy_core.messaging.send_imessage_attachment` sends via Messages' `participant … of account` scripting verb first, verifies the row through `GET /imessage/attachments`, and only falls back to clipboard-paste UI automation when the screen is unlocked. AppleScript's "SUCCESS" return is not evidence of delivery.
- The iMessage poller keeps the last 8 turns per sender for 45 minutes (`conversation_history` in `main.py`) so "yes, the full recipe" is answered in context instead of dispatched as a job. `run_job` fires only on an explicit run/start/send request.

## Report Replies (handled deterministically, before any LLM)
Every report footer advertises these; `main.py:handle_report_command` answers them without a model call:

| Reply | Effect |
| --- | --- |
| `MORE` | the items the concise report held back (from `data/outbox/{id}.detail.json`) |
| `WHY <n>` | the reasoning behind numbered item *n* |
| `PDF` / `RESEND PICKS` | sends the archived PDF, and reports honestly when chat.db can't confirm it |

A bare command resolves to the most recent report of any job, so `MORE` right after a picks text means the picks. Add a target (`MORE HAPPY HOUR`, `WHY 2 SP-20260903-1500`) to be explicit.

## Job Execution System
Ivy can run background jobs on-demand via natural language, dispatched through the single registry in `job_runner.py` — job names, aliases, and entrypoints live there. Run `./ivy list` (or `GET /capabilities`) to see them all, including unavailable ones.

Sharp Picks and Familia Meal Planner support both a real schedule (via launchd — see `deploy/launchd/`) and ad-hoc/on-demand dispatch (via a detached subprocess, no launchd required — see `job_runner._run_entrypoint_job`). An ad-hoc request always passes `force=True`, bypassing whatever duplicate-suppression/48h-gate the scheduled cadence uses, so "run picks now" always delivers.

Run jobs via:
- **iMessage:** `ivy run sharp picks` or `ivy run happy hour` (Ivy understands natural language)
- **Terminal:** `./ivy run picks` or `./ivy list` to see all jobs (each agent also has its own CLI: `python -m proactive_agents.sports_bettor --force --send`)
- **API:** `POST /run-job?job_name=sharp_picks` with `X-API-Key` header

## Common Operations
See the `ivy-ops` skill (`.claude/skills/ivy-ops/SKILL.md`) for running the engine, failover testing, health/readiness checks, execution history, and rendering launchd plists. Never install a launchd plist without reviewing the rendered output first.
