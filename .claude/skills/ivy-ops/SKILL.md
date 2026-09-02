---
name: ivy-ops
description: Operate the Ivy local admin API — run the live engine with uvicorn, test the DeepSeek→Gemini failover, check health/readiness/version/capabilities endpoints, inspect job execution history, and render launchd plist templates. Use when starting, testing, or diagnosing the running Ivy service.
---

# Ivy Operations

Run everything from `~/openclaw-admin/`.

## Run the live engine

```
uvicorn main:app --host 127.0.0.1 --port 8000
```

Logs go to stdout; capture with `2>&1 | tee run.log` if you need a transcript.

## Test the dual-brain failover

```
export DEEPSEEK_API_KEY="broken_key_test" && uvicorn main:app --host 127.0.0.1 --port 8000
```

DeepSeek should fail and Gemini (`gemini-2.5-flash`) should take over. Failover is for provider failure, timeout, or empty response only — never for an answer DeepSeek gave honestly.

## Restarting the live gateway (com.lexi.ivy)

Prefer letting launchd do the relaunch: `kill $(launchctl list | grep com.lexi.ivy | cut -f1)`.
KeepAlive brings it back in ~5 s. Then confirm `GET /ready` shows `chat_db_readable: true`.

If the new process logs `Cannot access chat.db … Retrying`, macOS TCC is denying the
relaunched interpreter Full Disk Access. The poller self-heals by exiting after 3 denials
(~10 s) so launchd relaunches again; on 2026-09-01 the third relaunch got access back.
Do not run `sqlite3 ~/Library/Messages/chat.db` from a Terminal/Claude shell to "check
something" — that shell has no Full Disk Access, and the running gateway lost its own
access within the same minute the last time it was tried (cause unproven, timing exact).
Use `GET /imessage/attachments?since=<unix>` instead; the gateway reads chat.db for you.

## Health and readiness

All endpoints require the `X-API-Key` header matching `ADMIN_SECRET`.

- `GET /health` — liveness
- `GET /ready` — 503 if a required component is down
- `GET /version` — git SHA, PID, dirty-tree state
- `GET /capabilities` — tools and jobs, including ones reported unavailable
- `GET /imessage/attachments?since=<unix seconds>[&filename=…][&handle=…]` — outgoing
  attachment rows from chat.db with a `state` of delivered / failed / pending. This is
  the ground truth for "did the PDF actually go out"; `ivy_core.messaging` polls it
  after every attachment send and only reports `verified_delivered` on a
  `transfer_state=5, is_sent=1, error=0` row.

`/health` never touches the network: provider auth comes from a background probe cache
(`cached_provider_status`), so it answers in milliseconds even when DeepSeek/Gemini are slow.

## Job execution history

- `GET /executions` — recent runs
- `GET /executions/{execution_id}` — one run

Backed by `logs/executions.db`. Never claim a job ran, a message sent, or a file attached without a receipt here.

## launchd plists

```
./deploy/install_launchd.sh                                  # dry run — renders only
./deploy/install_launchd.sh --apply                          # writes plists
./deploy/install_launchd.sh --apply --yes-i-know-this-is-live # required to touch an installed scheduled job
```

Never install a plist without reviewing the rendered output first. Scheduled job definitions live in `deploy/launchd/`.
