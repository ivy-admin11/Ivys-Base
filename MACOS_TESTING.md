# macOS integration and production smoke testing

Ivy has two deliberately separate macOS test layers:

1. Hosted GitHub Actions proves that real `osascript` preserves hostile/tricky argv content and that every launchd template renders as a valid plist.
2. A trusted, signed-in production Mac proves local permissions, Messages.app access, launchd state, provider readiness, and—only after a human confirmation—one live delivery.

## What hosted macOS CI runs

The `macos-safety` job in `.github/workflows/ci.yml` sets `PYTEST_MACOS_INTEGRATION=1` and runs:

```bash
python -m pytest -v \
  tests/test_ivy_core.py::test_argv_round_trip_with_tricky_characters_real_osascript
plutil -lint deploy/launchd/*.plist.template
./deploy/install_launchd.sh --validate-only
```

The pytest case invokes the real system `osascript`, but its static AppleScript only joins two argv values and returns them. It never opens or addresses Messages.app. CI also uses placeholder contact/provider values, and no CI step passes `--live-delivery`.

Hosted runners cannot prove any of the following:

- that Messages.app is signed into the production Apple ID;
- that the production user has Automation, Accessibility, or Full Disk Access permissions;
- that `~/Library/Messages/chat.db` is readable by the launchd process;
- that the production user's LaunchAgents are loaded or scheduled correctly;
- that provider credentials authenticate or external APIs are reachable from production;
- that a real iMessage reaches a destination device.

A green hosted-macOS job is therefore a safe platform compatibility signal, not production certification.

## Local integration test

On a trusted Mac with the development dependencies installed:

```bash
export PYTEST_MACOS_INTEGRATION=1
python -m pytest -v -m macos_integration
```

Or run only the real-osascript round-trip test:

```bash
export PYTEST_MACOS_INTEGRATION=1
python -m pytest -v \
  tests/test_ivy_core.py::test_argv_round_trip_with_tricky_characters_real_osascript
```

This test remains non-delivering.

## Production Mac smoke test

The default smoke test validates runtime prerequisites, renders and validates all launchd plists, diffs installed plists without writing them, and executes a safe real-osascript argv round trip:

```bash
./scripts/production_smoke_test.sh
```

To include authenticated local `/health`, `/ready`, and `/version` probes:

```bash
./scripts/production_smoke_test.sh --check-running
```

The monitor reads `ADMIN_SECRET` without sourcing `.env`, supplies it to `curl` through a private temporary config file, and refuses non-loopback URLs.

## Explicit live-delivery test

Only an operator on the production Mac should run this:

```bash
./scripts/production_smoke_test.sh \
  --check-running \
  --live-delivery \
  --recipient '+1XXXXXXXXXX'
```

The script will not send unless all of these are true:

- `--live-delivery` was supplied;
- a recipient was supplied explicitly;
- an interactive terminal is available;
- the operator types the exact phrase `SEND IVY SMOKE MESSAGE`.

There is no noninteractive confirmation flag. Test automation must never invoke this mode.

## Production permission checks

In System Settings, confirm the account that owns the LaunchAgent has the required privacy grants. After changing a grant, restart the gateway and rerun the authenticated smoke test. A shell granted Full Disk Access does not automatically grant the same access to a separate launchd-launched Python process.

Useful read-only checks:

```bash
test -r "$HOME/Library/Messages/chat.db"
launchctl print "gui/$(id -u)/com.ivy.gateway"
./scripts/monitor_ivy.sh
```

## Troubleshooting

- `osascript: command not found`: the test is not running on macOS or the system executable is unavailable.
- plist validation failure: do not install anything; fix the corresponding template and rerun `./deploy/install_launchd.sh --validate-only`.
- `401` from a probe: confirm `ADMIN_SECRET` in the production `.env`; never paste the value into a command or ticket.
- `/ready` returns `503`: inspect the readiness checks. Typical causes are unreadable `chat.db`, an unwritable receipts database, or no authenticated LLM provider.
- a live smoke returns success but the device receives nothing: AppleScript success is not end-to-end delivery proof. Check Messages.app sign-in, the destination address, network state, and the destination device before retrying to avoid duplicates.
