# Ivy Local Admin API Environment

## System Architecture
- **Core Engine:** `main.py` (FastAPI app; the local iMessage database poller runs as a background thread spawned on startup)
- **Primary Database:** macOS Chat DB (`~/Library/Messages/chat.db`)
- **Primary LLM:** Gemini (`gemini-2.5-flash`) via `google-genai`; falls back to DeepSeek (`deepseek-chat`) on error
- **Integrations:** Google Docs API, Google Slides API
- **Automation Pipeline:** AppleScript via subprocess for outbound iMessage routing
- **Google OAuth files:** `get_google_service` reads `~/ai-admin-api/token.json` and `~/ai-admin-api/google_credentials.json` — copy or symlink these from the project root if needed

## Development Guidelines
- Always preserve the dual-brain failover structure (Gemini → DeepSeek) inside `main.py`.
- Keep text replies short, concise, and direct (under 40 words).
- Endpoints are currently unauthenticated; add a shared-secret header before exposing beyond localhost.

## Common Operations
- Run the Live Engine: `uvicorn main:app --host 127.0.0.1 --port 8000` from `~/openclaw-admin/`
- Test Failover: `export GEMINI_API_KEY="broken_key_test" && uvicorn main:app --host 127.0.0.1 --port 8000`
- Review Active Local Logs: logs are emitted to stdout; capture with `uvicorn main:app --host 127.0.0.1 --port 8000 2>&1 | tee run.log`
