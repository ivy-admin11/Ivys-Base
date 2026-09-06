# Retired launchd jobs

Templates here are kept for reference and are **not** installed —
`install_launchd.sh` globs `deploy/launchd/*.plist.template`, so anything in
this subdirectory is out of its reach.

## com.ivy.brain — retired 2026-09-05

Its implementation lives outside this repo at `~/ai-admin-api/agent.py`. It
stopped producing output on 2026-07-16 and its plist was already gone from
`~/Library/LaunchAgents` by the time anyone checked, so it had been unloaded
rather than crashing.

Retired rather than revived because of what it did while running:

- Its only gate on an inbound iMessage was `if "ivy" not in text.lower()`.
  There was no sender allowlist.
- Among the tools it handed to Gemini was `execute_terminal_command(command)`.
  Anyone who knew the number and wrote "ivy" in a message could reach an LLM
  holding arbitrary shell execution on this Mac.
- It ran `KeepAlive=true`, and `~/ai-admin-api/.env` is a symlink to this
  repo's `.env`, so it shared every API key.
- It polled the same `chat.db` as `com.ivy.gateway`, so running both would
  answer each message twice.

The gateway validates senders against `favorites.json` and exposes no shell
tool to the model. If the Google Docs and Slides tools are ever wanted, port
them into `registry.py`, where they inherit that sender check.
