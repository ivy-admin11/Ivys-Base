# Ivy production runbook

## Operating model

Ivy runs as user LaunchAgents on one trusted macOS account. The FastAPI gateway listens on loopback and every endpoint requires `X-API-Key`. Messages.app, `chat.db`, Apple privacy grants, local secret files, and the user's launchd domain are production dependencies that hosted CI cannot reproduce.

The guarded operations commands are dry-run-first:

- `deploy_production.sh`, `rollback_release.sh`, `restart_ivy.sh`, `backup_state.sh`, and `restore_state.sh` do not make their live changes without explicit apply flags.
- `production_smoke_test.sh` never sends by default. Its live-delivery mode requires a successful running-service check, exact recipient membership in `favorites.json` (or an explicitly supplied allowlist), and a recipient-specific interactive typed confirmation with no noninteractive bypass.
- `monitor_ivy.sh` is read-only. It sends the admin credential only to an HTTP loopback address, through a private curl config file.

Run all commands from the repository root as the same macOS user that owns the LaunchAgents. Do not run them with `sudo`.

## Production prerequisites

Before a first deployment, confirm:

- the reviewed git commit is present locally;
- `.venv/bin/python` exists and the reviewed `requirements-dev.lock` environment is installed for the pre-deployment hygiene suite;
- `.env` and `favorites.json` exist, are owned by the production user, and have mode `600`;
- `ADMIN_SECRET`, `HENRY_PHONE`, and `LEXI_PHONE` are configured;
- the required provider keys are present for the capabilities expected to be ready;
- Messages.app is signed in for this macOS account;
- the launchd Python process—not merely Terminal—has the required Automation and Full Disk Access permissions;
- `~/Library/Messages/chat.db` is readable by the production process.

The installer creates `data/` and `logs/` with private directory permissions when it applies changes. It validates every rendered plist with `plutil` before writing any plist and installs files atomically with mode `600`.

`deploy_production.sh` sets `IVY_VENV_PYTHON_OVERRIDE` internally to the absolute interpreter in the validated immutable production environment. The installer rejects relative, dot-component, missing, directory, and non-executable overrides. `IVY_HYGIENE_PYTHON` is available only for a deliberately selected absolute developer interpreter; ordinary local runs continue to use `.venv/bin/python`.

## CI gate and its boundary

The Linux CI job remains the full hygiene, lint, security, and unit-test gate. The hosted `macos-safety` job additionally runs the real, non-delivering osascript argv test with `PYTEST_MACOS_INTEGRATION=1` and validates raw and rendered launchd plists.

Hosted macOS CI does **not** certify Messages.app sign-in, Full Disk Access, `chat.db`, user LaunchAgent state, real provider authentication, or end-to-end iMessage delivery. Those checks require the trusted production Mac. See `MACOS_TESTING.md`.

## Routine monitoring

Run:

```bash
./scripts/monitor_ivy.sh
```

Success requires all three authenticated loopback probes to pass:

- `/health` returns HTTP 200 and `status: ok`;
- `/ready` returns HTTP 200 and `ready: true`;
- `/version` returns a usable git SHA and reports a clean worktree.

Any transport, authentication, HTTP, JSON, readiness, version, or dirty-tree failure produces a nonzero exit. The script never prints the admin key. An alternate local port may be supplied with `--base-url http://127.0.0.1:PORT`; non-loopback destinations are rejected.

Restart and first-bootstrap readiness checks use a deadline of 90 seconds by default, because a sequential provider probe can legitimately exceed a few seconds. The deadline may be raised, but never lowered below 90 seconds, with `--readiness-timeout-seconds` or `IVY_READINESS_TIMEOUT_SECONDS`.

Useful read-only service checks:

```bash
launchctl print "gui/$(id -u)/com.ivy.gateway"
tail -n 200 logs/ivy-gateway.log
tail -n 200 logs/ivy-gateway-error.log
git status --short
```

Treat message content, phone numbers, provider errors, and local paths in logs as sensitive operational data.

## Safe pre-deployment sequence

1. Create a runtime snapshot before changing the checkout:

   ```bash
   ./scripts/backup_state.sh
   ./scripts/backup_state.sh --apply
   ```

2. Fetch and review the exact commit. Ref selection and git switching remain deliberate operator actions:

   ```bash
   git fetch --tags origin
   git switch --detach <reviewed-commit-sha>
   git status --short
   ```

3. Run the deployment dry-run against the exact checked-out commit:

   ```bash
   ./scripts/deploy_production.sh --ref <reviewed-commit-sha>
   ```

   It refuses any tracked or untracked worktree change, verifies the ref resolves to the current `HEAD`, runs the hermetic repository hygiene suite, and validates rendered plists. It does not alter services in dry-run mode.

4. Apply only after the dry-run succeeds:

   ```bash
   ./scripts/deploy_production.sh \
     --ref <reviewed-commit-sha> \
     --apply \
     --yes-i-know-this-is-live
   ```

   Apply creates another state backup, builds production dependencies from the reviewed `requirements.lock` in an immutable SHA-addressed virtual environment under `~/.ivy-operations/venvs/`, verifies it with `pip check`, and passes its absolute interpreter path to the launchd renderer. The project `.venv` remains the developer/hygiene environment and is never replaced. Deployment snapshots all managed installed plists, performs the full installer preflight, installs validated plists, activates or restarts only `com.ivy.gateway`, runs authenticated monitoring, and records release metadata under `~/.ivy-operations/` with private permissions. If a later deployment step fails, the recorded prior commit and managed plists are restored automatically where their targets remain safe. If no trusted prior commit is available, the gateway remains stopped rather than starting mixed release artifacts.

The deployment script does not boot out or kickstart scheduled delivery labels. That prevents a deploy from unexpectedly running a delivery job. If a reviewed schedule plist changed, reload that label separately during a chosen maintenance window after checking its arguments and calendar interval.

Tracked scheduled labels are:

- `com.ivy.familia_meal_planner`
- `com.ivy.sharppicks`
- `com.ivy.happy_hour_scout`

Each scheduled plist enters through `ivy_core.job_worker` with its canonical job name and `module:run` entrypoint. This makes the worker—not a short-lived launcher—the owner of start, heartbeat, terminal outcome, and delivery receipt state. Scheduled invocations pass `--send` but omit `--force`, preserving Sharp Picks and Familia duplicate gates.

## Post-deployment verification

Run the non-delivering production smoke test:

```bash
./scripts/production_smoke_test.sh --check-running
```

It validates runtime prerequisites and plists, executes a safe real-osascript argv round trip, and runs authenticated probes. It does not open Messages.app.

If end-to-end delivery must be proven, use a designated test recipient and the explicit interactive flow:

```bash
./scripts/production_smoke_test.sh \
  --check-running \
  --live-delivery \
  --recipient '+1XXXXXXXXXX'
```

The operator must type the displayed recipient-specific phrase, for example `SEND IVY SMOKE MESSAGE TO ***1234`. The full address is neither printed nor placed on a command line. Do this once, then verify receipt on the destination device before any retry.

## Gateway restart

Preview:

```bash
./scripts/restart_ivy.sh
```

Apply:

```bash
./scripts/restart_ivy.sh --apply --yes-i-know-this-is-live
```

The command refuses an unloaded service, restarts only `com.ivy.gateway`, and retries authenticated monitoring for at least the configured readiness deadline. Its normal mode uses `kickstart -k`; deployments and rollbacks add `--reload-plist`, which boot out the gateway and bootstrap the reviewed installed plist so changed launchd arguments actually take effect. It never kickstarts a scheduled delivery agent.

## Code rollback

Preview the prior recorded release:

```bash
./scripts/rollback_release.sh --last
```

Or name an exact local commit:

```bash
./scripts/rollback_release.sh --ref <known-good-sha>
```

Apply:

```bash
./scripts/rollback_release.sh \
  --last \
  --apply \
  --yes-i-know-this-is-live
```

Rollback refuses a dirty tree, validates the target commit and required production files, backs up state, detaches the checkout at the exact target SHA, runs that target's hygiene suite, builds or verifies a SHA-addressed immutable target environment from that release's `requirements.lock`, and passes that interpreter explicitly while installing target templates. The developer `.venv` is unchanged. It then reloads only the gateway, probes it, and records the transition. If a rollback step fails after switching refs, it attempts to restore the original code, production interpreter reference, plists, and gateway.

`--last` is available only after a successful deployment record contains `PREVIOUS_SHA`. Use `--ref` for an explicitly known release otherwise. A code rollback does not automatically downgrade runtime state; use the disaster-recovery process only when a reviewed compatibility decision requires state restoration.

## Common failures

### Dirty worktree

Deployment and rollback stop before any live action. Inspect `git status --short`. Preserve legitimate work by committing it on an appropriate branch or moving it out of the production checkout. Never force-clean a production tree without identifying every file first.

### Gateway is not loaded

`restart_ivy.sh` will not bootstrap implicitly. Validate the installed plist first:

```bash
plutil -lint "$HOME/Library/LaunchAgents/com.ivy.gateway.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.ivy.gateway.plist"
./scripts/monitor_ivy.sh
```

### `/ready` returns 503

The response is intentionally fail-closed. Check gateway logs and the readiness components: `chat.db` readability, receipts database writability, and at least one authenticated LLM provider. Do not weaken readiness to make monitoring green.

### Authentication failure

Confirm the service and monitor read the intended `.env`. Do not put `ADMIN_SECRET` on a command line, paste it into logs, or enable verbose curl output. If exposure is suspected, rotate the secret and restart the gateway.

### launchd plist failure

Run:

```bash
./deploy/install_launchd.sh --validate-only
./deploy/install_launchd.sh
```

The second command is a no-write diff with full production preflight. Existing plists are backed up before replacement; the installer never calls `launchctl`.

## Security invariants

- Keep the API bound to loopback; do not expose it directly to a LAN or the internet.
- Preserve the DeepSeek-primary/Gemini-fallback provider order.
- Keep `.env`, allowlists, backup directories, archives, and operations metadata private to the production user.
- Do not source `.env` from operations scripts.
- Do not put secrets or real contact values in git, shell history, CI variables used for tests, tickets, or logs.
- Never interpret AppleScript response text as proof of end-to-end iMessage delivery.
- Treat state restore and live delivery as separate, human-confirmed operations.

For backup contents, staged restore, and full-host recovery, use `docs/DISASTER_RECOVERY.md`.
