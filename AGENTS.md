# Ivy Local Admin API Environment

## System Architecture
- **Core Engine:** `main.py` (FastAPI app; the local iMessage database poller runs as a background thread spawned on startup)
- **Primary Database:** macOS Chat DB (`~/Library/Messages/chat.db`)
- **Primary LLM:** DeepSeek (`deepseek-chat`); falls back to Gemini (`gemini-2.5-flash`) only on provider failure, timeout, or empty response
- **Integrations:** Google Docs API, Google Slides API
- **Automation Pipeline:** AppleScript via `osascript`, invoked with `on run argv` so untrusted content is passed as process arguments, never interpolated into AppleScript source text
- **Google OAuth files:** copy or symlink `token.json` and `google_credentials.json` from `~/ai-admin-api/` into the project root if needed

## Development Guidelines
- Always preserve the dual-brain failover structure (DeepSeek → Gemini) inside `main.py`.
- Keep text replies short, concise, and direct (under 40 words).
- Endpoints require the `X-API-Key` header to match `ADMIN_SECRET`. The process fails closed — it refuses to start if `ADMIN_SECRET` is unset (set `ALLOW_INSECURE_ADMIN_SECRET=true` for local/test use only).

## Common Operations
- Run the Live Engine: `uvicorn main:app --host 127.0.0.1 --port 8000` from `~/openclaw-admin/`
- Test Failover: `export DEEPSEEK_API_KEY="broken_key_test" && uvicorn main:app --host 127.0.0.1 --port 8000`
- Review Active Local Logs: logs are emitted to stdout; capture with `uvicorn main:app --host 127.0.0.1 --port 8000 2>&1 | tee run.log`
