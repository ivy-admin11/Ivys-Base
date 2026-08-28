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

## Health and readiness

All endpoints require the `X-API-Key` header matching `ADMIN_SECRET`.

- `GET /health` — liveness
- `GET /ready` — 503 if a required component is down
- `GET /version` — git SHA, PID, dirty-tree state
- `GET /capabilities` — tools and jobs, including ones reported unavailable

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
