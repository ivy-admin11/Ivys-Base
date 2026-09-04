# Ivy (openclaw-admin) — Production Readiness Audit

**Date:** 2026-09-02
**Branch audited:** `wip/imac-local-before-production-readiness` @ `8b69d39` (22 commits ahead of `main`, unmerged)
**Method:** read-only static review + live runtime inspection (no code changed)

**Baseline health:** 182/182 tests pass in 1.8s. Bandit reports 0 medium/high findings. Secrets are correctly untracked and CI has a secret-file guard. The architecture (dual-brain failover, receipt-backed delivery verification, poller self-healing) is genuinely well-built and heavily commented with real incident history.

**The gap is not architecture — it is operations.** The code is in better shape than the deployment. The single most urgent item is a live crash-loop that has been running unnoticed.

---

## Scorecard

| Area | State | Notes |
|---|---|---|
| Deployment / process supervision | 🟢 Fixed 2026-09-02 | `com.lexi.ivy` retired; `com.ivy.gateway` is the sole gateway (B1). Caveat: TCC grants are pinned to one uv-managed binary |
| Runtime parity (dev/CI/prod) | 🟢 Fixed 2026-09-04 | CI and prod install the same locked set; drift checker reports zero drift (B3) |
| CI/CD | 🟢 Green 2026-09-02 | All 5 CI steps pass locally (B2). Still no dependabot / SECURITY.md (N4) |
| Secrets & PII | 🟢 Fixed 2026-09-04 | All real numbers removed (13 across 6 files); `.env` key fixed; CI guards both (B5). Dead grocery creds remain (S8) |
| Input validation / injection | 🟢 Fixed 2026-09-03 | AppleScript sites argv-based + `--` option terminator + list allowlist (B4); 16 regression tests |
| Logging / disk | 🟢 Fixed 2026-09-04 | Copy-truncate rotation + retention job; outbox TTL now enforced with per-job retention (B6) |
| Observability | 🟠 Gap | Request-logging middleware written but never registered |
| Test coverage | 🟠 47% | `/voice/*` and `/cache-stats` entirely untested |
| Error handling | 🟢 Good | No bare excepts; typed provider errors; honest failure reporting |
| DB migrations | 🟠 None | `CREATE TABLE IF NOT EXISTS` only; no versioning, indexes, retention |
| Docs / runbooks | 🟠 Stale | README contradicts the code on auth, LLM order, and setup |

---

# 🔴 BLOCKERS

Things that are actively broken, actively leaking, or would break on first real deploy.

### B1. ✅ RESOLVED 2026-09-02 — Two competing gateway definitions; one was crash-looping
**Effort: 1–2 hours (actual ≈ 1.5 h incl. pre-flight and review)** · `deploy/launchd/com.ivy.gateway.plist.template`, retired plist now at `~/Library/LaunchAgents/disabled/com.lexi.ivy.plist.retired-20260902`

> The evidence below is the state **before** the fix, kept as the incident record. Resolution and receipts are at the end of this section.

Both `com.ivy.gateway` and `com.lexi.ivy` are installed and both try to bind `127.0.0.1:8000`.

Live evidence collected during this audit:

```
launchctl:  1398  0  com.ivy.gateway      <- winner, running since 02:22
            -     1  com.lexi.ivy         <- exit 1, restarting forever
lsof:       PID 1398 = .venv/bin/python -m uvicorn main:app  (holds :8000)
ivy_error.log:  6,986 × "[Errno 48] address already in use"
restart cadence: 23:12:15, :24, :35, :47, :57, 23:13:08, :18, :29  (~10.5 s)
```

That is roughly **8,200 failed process starts per day**. Each one starts an iMessage poller thread, fails to bind, and shuts down.

Two follow-on problems:
- The two definitions use **different interpreters**: `com.lexi.ivy` runs `/usr/bin/python3` (3.9.6), `com.ivy.gateway` runs `.venv/bin/python` (3.12.13). Whichever wins the race decides your runtime.
- `scripts/monitor_gateway.py:181` alerts with the text *"Ivy gateway (com.lexi.ivy) is DOWN"* while actually probing port 8000 — which is served by `com.ivy.gateway`. **The alert names the wrong service**, and it will never fire for the label that is genuinely broken.

**The winner is the one you want to keep.** Live `/version` + `/ready` confirm the survivor is healthy on the venv interpreter:

```
/version:  python_executable = /Users/lexi/openclaw-admin/.venv/bin/python
           pid 1398, up since 02:22, git_sha 8b69d39
/ready:    HTTP 200 — chat_db_readable ✓  imessage_poller_healthy ✓
                      receipts_db_writable ✓  llm_provider_authenticated ✓
```

This is worth calling out explicitly: `.venv/bin/python` **has working Full Disk Access to chat.db**. The long-standing assumption that TCC grants force the gateway onto `/usr/bin/python3` no longer holds — `chat_db_readable: true` on the venv interpreter disproves it.

**Resolution (2026-09-02 23:31 CDT).** Preceded by a 4-lens adversarial pre-flight (0/4 refuted) and followed by a 3-lens review (0 must-fix). Live steps, in order:

1. `launchctl bootout gui/$(id -u)/com.lexi.ivy` — at that moment: `runs = 7087`, `last exit code = 1`, `state = spawn scheduled`, `active count = 0`.
2. `mv ~/Library/LaunchAgents/com.lexi.ivy.plist ~/Library/LaunchAgents/disabled/com.lexi.ivy.plist.retired-20260902` — byte-identical to the Jul-12 `disabled/com.lexi.ivy.plist`; the sibling `.bak.1783886807` is the Homebrew-python trap and was left alone.
3. `com.ivy.gateway` (PID 1398) was **not touched**.

Receipts:
```
launchctl print gui/501/com.lexi.ivy      -> Could not find service (rc 113)
launchctl list                            -> 1398  0  com.ivy.gateway   (only row)
launchctl print-disabled gui/501          -> com.lexi.ivy absent (bootout sets no flag; the mv is what prevents RunAtLoad)
ivy_error.log / ivy_output.log mtime      -> frozen at 23:31:20 across 30 s and 48 s windows (previously advanced every ~10.5 s)
GET /ready                                -> 200, all four checks true
PID 1398                                  -> same start time (02:22:18), runs = 1, never exited
gateway_monitor.log 23:32:16              -> status=up, no alert needed   (no text went to Henry)
```

Repo changes (local, uncommitted): `scripts/monitor_gateway.py` alert text and docstring → `com.ivy.gateway`; `ivy_core/attachment_verify.py:21` docstring; `deploy/install_launchd.sh` — `LIVE_LABELS` drops `com.lexi.ivy` with a retirement comment, the equality check strips trailing newlines on both sides, and a resurrected `com.lexi.ivy.plist` is reported in the obsolete-labels section; `deploy/launchd/com.ivy.gateway.plist.template` rewritten to match the installed plist exactly (installer dry-run now reports **No changes.**, `plutil -lint` clean, `plutil -convert xml1` identical); `.claude/skills/ivy-ops/SKILL.md` restart section rewritten with an exact-label kill; `README.md` gained a "Gateway Not Answering" troubleshooting entry; `tests/test_delivery_and_context.py` pins the label in the monitor alert assertions.

**What the pre-flight surfaced that was not in the original finding:**
- **TCC grants are pinned to one binary.** Full Disk Access and Messages Automation for the surviving gateway are keyed to the cdhash of an ad-hoc-signed, uv-managed interpreter: `~/.local/share/uv/python/cpython-3.12.13-macos-aarch64-none/bin/python3.12`, reached through the *floating* symlink `cpython-3.12-macos-aarch64-none` (`.venv/pyvenv.cfg` `home` points at the symlink). **`uv python upgrade` / `uv python install 3.12` would silently strip Full Disk Access from the only gateway.** A `.python-version` file does *not* guard this (uv is not in project mode here — no `pyproject.toml`/`uv.lock` — and the file doesn't stop `uv python upgrade` from repointing the symlink). The effective guard today is `com.ivy.gateway_monitor`, which would surface `chat_db_readable: false` as a "degraded" alert after two 5-minute sightings. A real pin means recreating the venv against the versioned directory, which requires a TCC re-grant — tracked under B3.
- **The crash loop was worse than log spam.** Every ~10.5 s cycle ran the full FastAPI lifespan before failing to bind: a live DeepSeek/Gemini auth probe (~8,600/day) and a duplicate Python-3.9 iMessage poller for ~1 s.
- **`com.lexi.ivy` was never a standby.** Both labels raced at every boot (02:22:38 vs 02:22:43 today). On 2026-09-01 the `/usr/bin/python3` side held the port for 5+ minutes with **no Full Disk Access**, the poller died, and Henry got alerts. Removing it made KeepAlive relaunches TCC-deterministic.
- **Calendar/Reminders Automation consent for the venv interpreter is unproven from logs.** `/health` shows those tools "ready", but that is config, not a TCC receipt, and the DeepSeek tool path doesn't log tool output. Verify by texting Ivy a calendar question and checking the reply isn't `❌ AppleScript Database Error: … Not authorized`.
- **BTM row:** the pre-flight saw an `8.com.lexi.ivy` Background Task Management row; one post-change `sfltool dumpbtm` showed it gone, another couldn't read per-user records without root. Treat as unverified — check System Settings › General › Login Items if it matters. It cannot relaunch anything either way (launchd loads from the plist on disk, which is gone).

Optional hardening not done (outside the repo, not needed for the fix to survive reboot): `launchctl disable gui/$(id -u)/com.lexi.ivy` as a second independent guard; renaming `disabled/com.lexi.ivy.plist` so nothing in that folder ends in `.plist`.

---

### B2. ✅ RESOLVED 2026-09-02 — CI was red on `ruff check .`
**Effort: 15 minutes (actual ≈ 15 min)** · `.github/workflows/ci.yml:38`

7 errors on tracked files. Any PR to `main` failed at the Ruff step before tests ever ran:

```
ivy_core/picks_tracker.py:10:8    F401  `json` imported but unused
ivy_core/picks_tracker.py:13:22   F401  `datetime.datetime` imported but unused
ivy_core/result_updater.py:10:42  F401  `datetime.timedelta` imported but unused
ivy_core/sheets_logger.py:7:8     F401  `json` imported but unused
ivy_core/sheets_logger.py:12:44   F401  `Request` imported but unused
proactive_agents/sports_bettor.py:1292:5  F841  `min_confidence_single` unused
proactive_agents/sports_bettor.py:1293:5  F841  `min_sharps_consensus` unused
```

The two `F841`s in `sports_bettor.py` were **lint-only, not a logic bug** — the thresholds they named are correctly applied inline at line 1302; the variables were simply never read.

**Resolution.** Each of the five `F401` symbols was verified dead with a **tokenizer** pass, not grep — the apparent `datetime(...)` hits in `picks_tracker.py` are SQL function calls inside query strings and the `.json` hits in `sheets_logger.py` are credential *file paths*, so a naive text search would have wrongly spared both. Confirmed no `__all__`, no dynamic `getattr`, and no other module re-exporting them. The two `F841` variables were deleted and the rule they documented folded into the comment above the loop; **the `if` condition is byte-identical**, so pick filtering is unchanged. Each edited module was then imported at runtime — `compileall` only proves syntax, and `result_updater.py` has 0% test coverage behind a live `com.ivy.result-updater` launchd job.

Net diff: 4 files, +4 −11.

Full CI pipeline re-run locally, step for step — **all five green**:

```
compileall   PASS      secret scan  PASS (no tracked secret files)
ruff check . PASS      bandit -ll   PASS
pytest       182 passed
```

Also re-run under CI's exact pin (`uvx ruff@0.15.22`, ephemeral — the venv's 0.15.21 was left alone): **All checks passed.**

---

### B3. ✅ RESOLVED 2026-09-04 — Production ran different dependencies than CI
**Effort: 3–4 hours (actual ≈ 2.5 h)**

> **The audit had this backwards in one important way.** I framed it as "prod has drifted ahead of the pins." The drift direction is real, but the *risk* runs the other way: **CI was installing a materially more vulnerable set than production runs**, and two of my stated premises were wrong. Corrected analysis below.

Three separate drifts, all silent:

1. ~~**Interpreter:** `com.lexi.ivy` pins `/usr/bin/python3` = 3.9.6 while CI and the venv use 3.12.~~ **Resolved by B1** — the only gateway now runs the 3.12 venv, and its template matches the installed plist. The one remaining `/usr/bin/python3` reference is `deploy/launchd/com.ivy.brain.plist.template:12`, for a retired label. **Replacement hazard (from B1's pre-flight):** the venv resolves through uv's floating minor-version symlink, so `uv python upgrade` / `install 3.12` silently swaps the binary that the gateway's TCC grants are keyed to. A `.python-version` file does not guard this; the real fix is a venv whose `home` targets the versioned `cpython-3.12.13-…` directory, which needs a one-time TCC re-grant from an unlocked session.
2. **Installed vs pinned:** the venv has drifted far from `requirements.txt`:
   | Package | Pinned | Installed |
   |---|---|---|
   | cryptography | 42.0.5 | **49.0.0** |
   | requests | 2.31.0 | **2.34.2** |
   | pillow | 11.3.0 | **12.2.0** |
   | google-genai | 1.2.0 | **1.47.0** |
   | pydantic | (>=2.7,<3) | 2.13.4 |
3. **Unresolved conflict:** `pip check` fails —
   `google-genai 1.47.0 requires httpx>=0.28.1, but you have httpx 0.27.2`.
   The httpx pin is deliberate (TestClient vs google-genai), but the conflict is now real and unmanaged.

**What was actually wrong** (verified in throwaway venvs; the live venv was never modified during analysis):

| Finding | Evidence |
|---|---|
| **CI installed a *pre-release*.** `pydantic>=2.7,<3` was the only unpinned range; a real resolve produced **2.14.0b1** while production ran stable 2.13.4. | `pip install -r requirements-dev.txt` → `pydantic-2.14.0b1` |
| **CI tested a different HTTP transport.** `google-genai==1.2.0` was pinned; that build depends on `requests` and has **no httpx dependency at all**. Production ran 1.47.0, which is httpx-based. A venv rebuild would have silently rolled the Gemini SDK back 45 minor versions and swapped its transport. | 1.2.0 metadata has no `httpx` line; live venv `Requires: … httpx …` |
| **CI validated a more vulnerable set than production.** cryptography 42.0.5 carries 7 advisories, requests 2.31.0 carries 3 — including a `.netrc` credential leak (CVE-2024-47081). Every one is fixed in the installed versions. | OSV.dev per-version queries |
| **The venv could not be rebuilt at all.** `pip install -r` on its own `pip freeze` failed — so there was no recovery path if `.venv` were lost. | `ResolutionImpossible` |
| **starlette 0.36.3 carried 7 advisories in *both* sets** — the one exposure the drift did not mitigate, pinned there by fastapi 0.110.0. | through CVE-2026-48818 |

**Two of my own audit claims were wrong**, and I am correcting them rather than quietly dropping them: `reportlab==5.0.0` **is** a real PyPI release (5.0.1 is latest), so CI's install was never broken by it; and the httpx/google-genai conflict was **metadata-only, not a runtime defect** — google-genai 1.47.0 genuinely works on httpx 0.27.2 (a real call returns a clean `400 API_KEY_INVALID`). It still mattered, because it was what made the venv unreproducible.

**Resolution.** `fastapi`/`starlette`/`httpx` move as one unit — bumping httpx alone breaks all endpoint tests at *collection* time, because starlette below 0.37.2 passes httpx's removed `app=` shortcut. Verified end state: **fastapi 0.141.1 + starlette 1.6.0 + httpx 0.28.1 + pydantic 2.13.4 + cryptography 50.0.1** → `pip check` clean, **244/244 tests pass**, ruff/bandit/compileall clean, and the app boots with auth still enforced. starlette 1.6.0 also clears all 7 of its advisories.

Landed:
- **`requirements.txt` rewritten** — converged *upward*, pydantic pinned exactly, 5 dead packages dropped (`aiofiles`, `slowapi`, `google-auth-oauthlib`, `playwright`, `pymupdf` — zero imports, zero reverse deps), and `cryptography`/`pillow`/`aiohttp`/`pyasn1` kept only as CVE floors.
- **`requirements.lock.txt`** (77 packages, `uv pip compile`) with **CI installing from it** plus a `pip check` step. Generated from `uv pip compile`, *not* the live freeze — that freeze is unresolvable.
- **`scripts/check_env_drift.py`** — the guard CI structurally cannot provide. CI builds a fresh env and never sees the iMac's venv, which *is* production; a CI-side `pip check` is permanently green and would have caught none of the above. This runs on the machine: asserts interpreter identity, diffs installed-vs-declared, and reimplements `pip check` via `importlib.metadata`.
- **`/ready` gains `interpreter_matches_tcc_grant`; `/version` gains `python_base_executable`.** B1 recorded that Full Disk Access is keyed to the interpreter binary, so a `uv python upgrade` would silently break chat.db reads with no stated cause. The check sits next to `chat_db_readable` because they fail together. (**B1's premise was also partly wrong**: the grant is recorded against the *versioned* path, not the floating symlink — so pinning the venv to it needs no rebuild and no TCC re-grant.)

**✅ Live venv converged 2026-09-04 00:48 CDT** (user-approved; the sandbox had correctly blocked the unapproved attempt). `fastapi 0.110.0 -> 0.141.1`, `starlette 0.36.3 -> 1.6.0`, `httpx 0.27.2 -> 0.28.1`. Receipts:

```
pip check                     No broken requirements found.   (was failing all session)
check_env_drift.py            interpreter OK / declared OK / conflicts OK
pip install --dry-run -r <own freeze>   resolves cleanly       (was ResolutionImpossible)
pytest (live venv)            244 passed
gateway restart               PID 86449 -> 89785 in ~4 s
/ready                        200, all FIVE checks true incl. interpreter_matches_tcc_grant
/version                      python_base_executable = .../cpython-3.12.13-.../python3.12
chat.db via /imessage/attachments   readable — Full Disk Access survived the upgrade
gateway_monitor               status=up, no alert — Henry was not paged
```

Revert point: `~/ivy_repair_backups/20260904_004750_b3_venv/freeze_before.txt`
(`pip install 'fastapi==0.110.0' 'starlette==0.36.3' 'httpx==0.27.2'`).

**CI and production now install the same set**, and `check_env_drift.py` reports zero drift.

---

### B4. ✅ RESOLVED 2026-09-02 — AppleScript injection reachable from an inbound iMessage
**Effort: 1 hour (actual ≈ 1 h)** · `main.py:670-719`, `utils/applescript.py`

> Pre-fix analysis below is the incident record; resolution and receipts follow it.

The project standard is argv-passing (`utils/applescript.py` — `on run argv`, explicitly documented as injection-proof, with a real round-trip test at `tests/test_ivy_core.py:28`). **Two functions bypass it and interpolate directly into AppleScript source:**

```python
# main.py:672  fetch_apple_reminders
script = f'''
tell application "Reminders"
    tell list "{list_name}"          # <-- interpolated
...

# main.py:697  add_apple_reminder
f'        set targetList to list "{list_name}"',
f'            make new reminder with properties {{name:"{title}"}}',   # <-- interpolated
```

`title` and `list_name` come from LLM tool arguments, which are derived from inbound message text. A `"` in either terminates the string literal. The reminders path is partly shielded because `list_name` is force-overwritten to `"Household"` for Gemini (`main.py:950`, `:1890`) — but **`title` is never sanitized on any path**, and the DeepSeek path does not apply the overwrite at all.

Same file, same commit, also missing: **no `timeout=` on the three `osascript` calls** at `main.py:610`, `:685`, `:714`. `AppleScriptRunner` defaults to 30 s; these can hang the poller thread indefinitely.

**Resolution.** Both functions now call new `AppleScriptRunner.fetch_reminders_argv` / `add_reminder_argv`, which pass content as process argv through `on run argv` scripts — the same pattern the iMessage senders already used. `check_apple_calendar` now goes through `AppleScriptRunner.run`, inheriting its 30 s timeout. The shared `_GATEWAY_APPLESCRIPT` instance was hoisted above its first use. Net: `main.py`, `utils/applescript.py`, plus a new `tests/test_applescript_injection.py` (16 tests). Full CI green.

**The vulnerability was demonstrated, not assumed.** Feeding the old code a list name of `Errands"` + newline + `tell application "Finder"` produces this script text:

```applescript
        if not (exists list "Errands"
        end tell
        tell application "Finder"
            return "PWNED") then
```

The literal closes after `Errands` and the attacker's `tell` block becomes executable. Under the fix the identical payload round-trips byte-for-byte as an *argument*, with the script source unchanged — verified against real `osascript`.

**Two honesty bugs fixed alongside the injection**, both instances of the "silence is indistinguishable from failure" rule in `CLAUDE.md`:
- `fetch_apple_reminders` returned `"No active reminders found."` whenever stdout was empty — *including when osascript had errored outright*. A failed read now says so.
- `add_apple_reminder` used `if "SUCCESS" in raw_output`, a substring test that would report success on an error message quoting a title containing the word. Now an exact match.

**The missing timeout is real, and it bit during this work.** While testing, an `osascript` call against Reminders blocked on a TCC Automation prompt and had to be killed after 2 minutes. The pre-fix `check_apple_calendar`, `fetch_apple_reminders`, and `add_apple_reminder` all called `subprocess.run` with no `timeout=` — **on the poller thread**. A single hung Apple app would have wedged all inbound iMessage handling indefinitely, exactly like the 2026-08-24 outage. All three now inherit the runner's 30 s cap.

**The first fix was incomplete, and an adversarial review caught it.** Passing content as argv closed the *string-literal* injection but **relocated it into `osascript`'s option vector**. `osascript` documents that multiple `-e` flags concatenate into one script, and its `getopt` keeps scanning past `-e <script>`. So a `list_name` of `-e` promoted the attacker-controlled `title` to a **second script fragment** — and because an AppleScript `property` initialiser runs at *load* time, before the run handler, a `do shell script` payload executed even though `argv` was then empty. Demonstrated end to end against real `osascript`:

```
pre-fix shape  (no --):  handler ran, argv count=0   -> payload EXECUTED (wrote /tmp/B4_PROOF)
post-fix shape (with --): handler ran, argv count=2  -> payload inert, delivered as data
```

Closed with two independent layers:

1. **`run_argv` now emits `["osascript", "-e", script, "--", *args]`.** One token, and it covers **all five** argv call sites — including the pre-existing iMessage senders, which had the same latent bug: a report body beginning with `- ` (a bulleted list) was liable to be eaten as an option.
2. **A reminder-list allowlist (`Household`, `Meal Plan`) enforced in the handlers.** Both Gemini paths already clamped `list_name`; the **DeepSeek path — the primary brain — did not**, so an inbound text could name any list and the add script would create it on demand. The clamp runs after the keyword auto-categoriser, so `meal`/`chore` routing still works. This asymmetry is what made the `-e` attack reachable at all.

**Test quality.** The regression tests were mutation-checked rather than trusted: replaying the pre-fix code's own generated script through the new assertions makes them fail, and the fixed form makes them pass. The review then found the first cut still had gaps, now closed — the assertions pin the **exact argv vector** (order matters: the scripts read `item 1`/`item 2`, and a transposition would silently swap a reminder's title with its list name), require the `--` terminator, and check that a real `timeout` reaches `subprocess`. Suite 182 → **198 passed**.

---

### B5. ✅ RESOLVED 2026-09-04 — Real phone numbers committed, and the `.env` override silently did nothing
**Effort: 1–2 hours (actual ≈ 1.5 h)** · `config.py`, `favorites.json`, `mcp-servers/imessage/favorites.json`, 3 agents, 1 docstring, 1 test file

> **The audit understated this.** I originally reported 2 tracked files and 4 numbers. The real footprint was **13 real numbers across 6 tracked files** — including `mcp-servers/imessage/favorites.json`, a 9-entry contact whitelist of which **8 belong to third parties**, committed to a GitHub repo. Pre-fix analysis below; resolution follows.

Two tracked files contain real personal phone numbers:

```python
# config.py:98
HENRY_PHONE: str = os.environ.get("HENRY_PHONE", "+1214XXXXXXX")   # real number as default
LEXI_PHONE:  str = os.environ.get("LEXI_PHONE",  "+1817XXXXXXX")   # real number as default
```
```json
// favorites.json  — TRACKED in git
["+1214XXXXXXX", "+1817XXXXXXX"]
```

Worse, the `.env` override **is never read**. `.env` defines `Henry_PHONE` (mixed case); the code reads `HENRY_PHONE`. Environment variables are case-sensitive. Verified:

```
os.environ HENRY_PHONE present after load_dotenv: False
os.environ Henry_PHONE present: True
config.HENRY_PHONE resolved from: hardcoded default in config.py
```

This works today **only because the hardcoded fallback happens to equal the real number.** Edit `.env` to change the recipient and nothing changes — all four proactive agents plus `scripts/monitor_gateway.py` keep texting the compiled-in number. This is exactly the class of bug that sends a report to the wrong person after a "fix."

**Resolution.**

| Site | Was | Now |
|---|---|---|
| `.env` | key spelled `Henry_PHONE` | `HENRY_PHONE` — value hash-verified identical, only the key changed |
| `config.py` | real number as the `os.environ.get` default | `_require_contact()` raises with a message naming case-sensitivity |
| `proactive_agents/sports_bettor.py` | **bare literal**, no env lookup at all | `require_env("HENRY_PHONE")` |
| `happy_hour_scout.py`, `Familia_meal_planner.py` | real numbers as env fallbacks | `require_env(...)` |
| `trigger_capabilities_alert.py` | real number as fallback | fallback removed |
| `ivy_core/attachment_verify.py` | real number in a docstring, **twice, in two formats** | `+1 (555) 555-0100` / `+15555550100` |
| `tests/test_delivery_and_context.py` | Henry's + Lexi's real numbers, one of them formatted | reserved-range numbers |
| `favorites.json` (2 numbers) | tracked | untracked + `favorites.example.json` |
| `mcp-servers/imessage/favorites.json` (9 numbers) | tracked | untracked + `.example` |

**Multi-format PII is the trap here.** A `\+1[0-9]{10}` grep misses `+1 (555) 555-0100` and `(555) 555-0100`. Two of the sites — the `attachment_verify` docstring and a test that exercises suffix-matching — existed *only* in formatted form, and the test broke when I scrubbed the digits but not the formatted variant. Verification therefore strips every non-digit from each tracked file before searching, which catches any punctuation. Result: **zero real contact digits in any tracked file, in any format.**

**Two new CI guards** so this cannot regress: `favorites.json` added to the tracked-secret scan, and a new step rejecting any `+1NNNNNNNNNN` outside the `+1555` fictional area code.

**Three regression tests** (`tests/test_config.py`) pin the behaviour: a missing contact must fail loudly, a **mis-cased** key must *not* satisfy the requirement, and a set contact must come from the environment. They copy `config.py` into a scratch dir first — the real `.env` is loaded by absolute path, so without that isolation the tests pass for the wrong reason.

Contacts are now required with no default, so a fresh clone fails fast instead of silently texting a stranger. The one knock-on: `test_admin_secret_escape_hatch_allows_import` supplied only `ADMIN_SECRET` and now needs contacts too — supplied, so it still isolates what it claims to test.

---

### B6. ✅ RESOLVED 2026-09-04 — Nothing rotated logs; nothing pruned the outbox
**Effort: 2–3 hours (actual ≈ 1.5 h)** · `ivy_core/outbox.py`, new `scripts/housekeeping.py`, new `deploy/launchd/com.ivy.housekeeping.plist.template`

> Pre-fix analysis below; resolution follows.

- **Zero log rotation** anywhere in the repo — no `RotatingFileHandler`, no `logrotate`, no `newsyslog`. Measured growth before B1: **+1,837 bytes / 15 s ≈ 10 MB/day**, almost entirely the crash-loop. **Post-B1:** root-level `ivy_error.log` (13 MB) and `ivy_output.log` (4 MB) froze at 23:31:20 on 2026-09-02 and are now dead artifacts of the retired label — gitignored, safe to delete once. The survivor writes to `logs/ivy-gateway*.log`, which still has no rotation.
- **`cleanup_old_reports()` is never called from anywhere.** It implements a correct 72-hour TTL (`OUTBOX_TTL_HOURS = 72`) and is dead code. `data/outbox/` holds **136 files dating back to 2026-07-19** — a 45-day backlog under a 3-day policy.
- Ad-hoc job logs accumulate one file per run (`job_runner.py:288`) with no cleanup — 77 files in `logs/`.

**Resolution.** New `scripts/housekeeping.py` (dry-run by default, matching `install_launchd.sh`) plus a daily `com.ivy.housekeeping` launchd template. Measured effect of the first run:

```
logs/        78 files -> 26      (52 stale ad-hoc job logs, oldest from Jul 15)
data/outbox  143 files -> 21     588 KB -> 88 KB   (63 reports past TTL)
```

**Rotation must copy-then-truncate, not rename.** launchd holds an open descriptor on `StandardOutPath`/`StandardErrorPath`; renaming the file leaves launchd writing to the *moved inode* while the fresh file stays empty forever — a rotation scheme that silently loses every subsequent log line. Proven in a test: the inode is preserved, the rotated copy holds the old bytes, and a write through a descriptor opened *before* rotation still lands in the truncated file.

**`logs/` is not only logs.** It holds `executions.db` (the job-receipt source of truth), `picks.db`, and `gateway_monitor_state.json`. Deleting `executions.db` would destroy the execution history that `CLAUDE.md` designates as the only acceptable proof a job ran. Pruning is therefore restricted to a `*.log` allowlist *and* an explicit protected-name denylist, with a parametrised test per protected file.

**Enforcing the TTL exposed a latent design flaw — and briefly caused one.** `OUTBOX_TTL_HOURS = 72` was written when nothing enforced it, so it had never met the weekly job cadences. Happy Hour runs Mondays and the meal planner Sundays, so their only report is older than 72h for **four days out of every seven**. The first apply duly deleted both, and `MORE HAPPY HOUR` immediately began answering *"I don't have a recent Happy Hour Scout report to work from."* Retention is now three rules, not one:

1. **The newest report of each job is always kept, whatever its age** — the MORE / WHY / PDF commands resolve against it.
2. A `pending` report survives the TTL (it may still be resent) up to a new `PENDING_TTL_DAYS = 30` backstop — previously `pending` was skipped unconditionally, so one stuck entry would have lived forever.
3. Everything else goes past the 72h TTL.

The two over-pruned reports were restored from a pre-run backup, and a second run is a no-op (`removed 0`).

**The wiring itself was the bug.** `cleanup_old_reports()` is now called from `save_report()`, so the outbox self-prunes on every write and retention holds even if the launchd job is never installed. The call is wrapped so a housekeeping failure can never break a delivery. 13 tests cover rotation, protection, and all three retention rules.

**Not done, deliberately:** the two dead root-level logs (`ivy_error.log` 12 MB, `ivy_output.log` 4 MB) are frozen artifacts of the retired `com.lexi.ivy` and sit outside `logs/`, so housekeeping ignores them. They are gitignored and safe to remove, but deleting 16 MB of your files is your call:
```bash
rm /Users/lexi/openclaw-admin/ivy_error.log /Users/lexi/openclaw-admin/ivy_output.log
```
The `com.ivy.housekeeping` plist is also **rendered but not installed** — `./deploy/install_launchd.sh` reports it as a new label; installing it is a separate, deliberate step per this repo's convention.

---

# 🟠 SHOULD-HAVE

Real gaps that will bite, but nothing is on fire.

### S1. No rate limiting — the dependency is installed but never wired
**Effort: 2 hours** · `requirements.txt:14`, `config.py:51`

`slowapi==0.1.9` is a declared dependency with **zero references in the codebase**. `config.py:51` defines `API_RATE_LIMIT` documented as *"for future rate limiting."* `/run-job` and `/voice/query` are both unthrottled and both trigger expensive work (LLM calls, subprocess spawns).

### S2. Request-logging middleware is written, tested-by-nobody, and never registered
**Effort: 30 minutes** · `middleware/logging.py`

`CorrelationIdMiddleware` is complete and well-written — UUID per request, `X-Request-ID` echo, in/out logging, exception capture. It is referenced **nowhere**: `main.py` never calls `add_middleware`, and coverage is 0%. There is currently no HTTP access log and no way to correlate a request across the boundary.

### S3. No job concurrency guard — `ALREADY_RUNNING` is unreachable
**Effort: 3–4 hours** · `job_runner.py:29`, `:143`

`JobStatus.ALREADY_RUNNING` is defined and `main.py:738` handles it, but **nothing ever returns it**. `self.running_jobs = {}` (`job_runner.py:143`) is initialized and never read or written. Two concurrent "run picks" requests both spawn detached subprocesses and both text the household. Given `force=True` bypasses each agent's own duplicate-suppression (`main.py:722-734`), the runner is the only place this can be caught.

### S4. `/executions` reports dispatch, not outcome
**Effort: 4–6 hours** · `job_runner.py:208-211`

`record_finish()` fires immediately after `Popen` returns. For `entrypoint` jobs — which is 4 of the 5 registered jobs — the subprocess is detached and runs for minutes afterward. A job that starts and crashes two seconds later is recorded as **`success`**. This directly undercuts the "never claim a job ran without a receipt" rule in `CLAUDE.md`, because the receipt only proves the process was launched.

### S5. Test coverage 47%, with whole subsystems at zero
**Effort: 1–2 days** · `tests/`

182 tests pass, but coverage is uneven:

| Module | Stmts | Cover | Note |
|---|---|---|---|
| `ivy_core/result_updater.py` | 244 | **0%** | has a live launchd job (`com.ivy.result-updater`) |
| `trigger_capabilities_alert.py` | 97 | 0% | |
| `mcp_bridge.py` | 106 | 0% | |
| `middleware/logging.py` | 22 | 0% | never registered (S2) |
| `ivy_core/picks_tracker.py` | 156 | 12% | |
| `cache_manager.py` | 89 | 31% | |
| `voice_assistant.py` | 116 | 35% | |
| `main.py` | 876 | 43% | |

**6 of 15 endpoints have no test at all**: all five `/voice/*` routes and `/cache-stats`. `result_updater.py` running unattended in production with 0% coverage is the sharpest edge here.

### S6. README contradicts the code in ways that will cause real mistakes
**Effort: 2 hours** · `README.md`

| Line | Says | Reality |
|---|---|---|
| 9 | "Primary LLM: Gemini 2.5 Flash (DeepSeek fallback)" | **Backwards** — DeepSeek primary, Gemini failover |
| 109 | "Endpoints currently unauthenticated (localhost only)" | **False and dangerous** — `verify_api_key` is enforced on all 15 routes and `config.py:89` refuses to boot without `ADMIN_SECRET` |
| 17 | `python -m venv venv` | Must be `.venv` — `job_runner.py:23` looks for `.venv/bin/python`; following the README breaks **every** job |
| 57-59 | `agent/`, `services/`, `tools/` | None of these directories exist |
| 103-105 | Happy Hour "Sundays", Planner "on demand", Bravo "daily" | `job_runner.py` says Mondays / Sundays 8am / on-demand-no-plist |

No mention that `ADMIN_SECRET` is mandatory. A new operator following this README gets a broken install and a false sense of the security posture.

### S7. No DB migrations, indexes, or retention on `executions.db`
**Effort: 3–4 hours** · `ivy_core/receipts.py:14-31`

`_connect()` runs `CREATE TABLE IF NOT EXISTS` on every call. There is no `PRAGMA user_version`, no migration path, and no ALTER handling — a schema change against an existing DB silently keeps the old shape. `list_recent()` does `ORDER BY started_at DESC` with **no index** on `started_at` or `job_name` (fine at current volume, degrades linearly). No retention policy — the table grows forever.

### S8. Secrets hygiene regressed and `.env` has drifted from its own example
**Effort: 2–3 hours**

- **`detect-secrets` was removed.** `.secrets.baseline` exists in git history (`a918d59`, `9bf0a70`) but is gone from the tree. No `.pre-commit-config.yaml`. Scanning is now a single `git ls-files | grep` in CI (`ci.yml:31`) that only catches four exact filenames — it would not catch a key pasted into a `.py` file.
- **Dead credentials sitting in plaintext:** `.env` holds `HEB_USERNAME`, `HEB_PASSWORD`, `KROGER_USERNAME`, `KROGER_PASSWORD` for grocery automation that is removed from the code and known non-viable.
- **Bidirectional `.env` / `.env.example` drift:** 8 keys in `.env` are undocumented (`OPENAI_API_KEY`, `GOOGLE_SHEET_ID`, `SPORTS_DASHBOARD_SPREADSHEET_ID`, `Google_AIStudio_Key`, the 4 grocery creds); 9 keys in `.env.example` are absent from `.env`.
- `SECRETS_MANAGEMENT.md` is a thorough 10 KB guide recommending Keychain/1Password — **none of it is implemented.**
- `.env.local` contains the literal string `404: Not Found`.

### S9. Two live launchd jobs have no template in the repo
**Effort: 1 hour** · `deploy/launchd/`

`com.ivy.result-updater` is installed and running but has **no `.plist.template`** in `deploy/launchd/` — its production configuration exists only on this one Mac. (`com.lexi.ivy` was the other one; retired in B1. `com.ivy.gateway`'s template now matches its installed plist exactly.) `deploy/install_launchd.sh` is otherwise excellent: dry-run by default, backs up, refuses to touch live labels without an explicit flag, and never calls `launchctl` for you.

One pre-existing trap the B1 review surfaced: the dry-run reports `com.ivy.brain` as *"not installed — Would create new file"* because only `com.ivy.brain.plist.disabled` exists on disk. `LIVE_LABELS` protects it from a bare `--apply`, but `--apply --yes-i-know-this-is-live` would resurrect the retired brain agent (which exposed arbitrary command execution over iMessage). Either teach the installer to treat `<label>.plist.disabled` as "retired, skip", or delete the template.

### S10. Non-constant-time API key comparison
**Effort: 10 minutes** · `main.py:509`

```python
if not x_api_key or x_api_key != ADMIN_SECRET:
```
Use `hmac.compare_digest`. Low severity — localhost-only, 64-char secret — but it is a one-line fix.

---

### 🔴 B7. A committed NameError disabled tool use on the primary brain (found during B5, fixed)
**Effort: 1 min fix; found only because B5's lint run reached it** · `main.py`, commit `fd73fa2`

Not part of B5 — surfaced by it. HEAD commit `fd73fa2` ("Answer report follow-ups from the outbox instead of re-running the sweep", +299 lines in `main.py`) added a re-run guard threaded through `_execute_tool_call(..., inbound_text=...)`. Inside `execute_deepseek_call` it passed `inbound_text=text`, but that function's parameter is **`text_content`**. `text` is undefined there.

The blanket `except Exception` two lines below swallowed the `NameError` into a message, so it looked like a provider fault rather than a bug. Proven against a mocked provider response:

```
main.execute_deepseek_call("add milk to my list", "sys")
  -> "❌ DeepSeek Execution Layer Exception: name 'text' is not defined"
```

**DeepSeek is the primary brain**, so *every* tool call through the normal path — add a reminder, read a list, run a job — returned that string instead of doing the work. Gemini's path was unaffected (its parameter really is `text`), so the failure only showed when DeepSeek answered, which is almost always. `ruff` flags it as `F821`, meaning **CI on `fd73fa2` is red** — B2 got CI green, and this commit re-broke it.

Fixed (`text` → `text_content`) with a regression test asserting the inbound message actually reaches the guard. A one-line fix, but it is worth asking how a committed change to the primary dispatch path shipped without the lint step that catches it.

---

### Follow-ups surfaced by B4's review (not fixed — deliberately out of scope)

- **In-band error signalling.** `fetch_apple_reminders` decides failure with `result.startswith("ERROR:")`, but that same channel carries user-authored reminder names — a list whose first item is literally `ERROR: call the plumber` reads as a failed fetch. `check_apple_calendar` has the inherited version of this (`"Error:" in raw_output` against text built from event summaries). The real fix is for `AppleScriptRunner` to signal failure out of band (a `(ok, text)` pair or an exception) instead of overloading stdout. **Effort: 2–3 h.**
- **`Hen_Lex.py` is the last f-string-into-AppleScript sink** (lines 77–84), and it embeds Calendar event summaries — a mailed-in invite with a quote in its title would inject. Nothing imports or schedules it, and N1 already flags it as dead. Deleting it closes this; otherwise route it through the runner. **Effort: 15 min.**
- **The legacy escaping helpers are dead** — `escape_applescript_string`, `build_imessage_send_script`, `AppleScriptRunner.send_imessage` are reachable only from tests, yet `utils/applescript.py`'s module docstring still advertises escaping as the module's safety property. That is now false and invites someone to reintroduce the pattern by following it. **Effort: 30 min.**

---

# 🟢 NICE-TO-HAVE

### N1. Dead code and dead dependencies — **4–6 hours**
- `Hen_Lex.py` (46 stmts, 0% coverage, tracked) — legacy grocery script; its top-level `from playwright.sync_api import ...` is why `playwright==1.44.0` is a hard dependency.
- `config.py:235-256` `STORE_CONFIG_FALLBACKS` + `ENABLE_GROCERY_STAGING` (`config.py:61`) reference `/stage_groceries`, an endpoint that no longer exists.
- `job_runner.py:312` `_run_shell_job` — no job uses the `shell` executor.
- `utils/applescript.py:151-177` `build_imessage_send_script`/`send_imessage` — the superseded escaping-based path.
- Removing Playwright + the grocery config also lets you delete 4 plaintext credentials (S8).

### N2. Deprecated SDK and stdlib calls — **4–6 hours**
`main.py:41` imports `google.generativeai`, which prints on every startup: *"All support for this package has ended."* Migration to `google-genai` (already a dependency) is the fix, and would likely also resolve the httpx conflict in B3. Separately, `datetime.utcnow()` is deprecated in `Familia_meal_planner.py:397,448` and `happy_hour_scout.py:372,412`.

### N3. Repo and worktree hygiene — **2–3 hours**
- **10 stale git worktrees** under `.claude/worktrees/` — ~100,000 lines of duplicated source in the working tree (`port-to-main/main.py` alone is 3,637 lines vs. the real 2,064). Excluded from ruff/pytest, but they pollute every non-git grep.
- Untracked but present in the repo root: `ivys-base.2026-07-03.private-key.pem`, `service-account-key.json`, `discord_backup_codes.txt`, 1.7 MB of vendor PDFs. Correctly gitignored — but a `git add -f` or a careless archive exposes them.
- `Dockerfile` and `docker-compose.yml` are gitignored yet present, and nothing references them.

### N4. Governance files missing — **2 hours**
No `SECURITY.md`, `CONTRIBUTING.md`, `.github/CODEOWNERS`, PR template, or `dependabot.yml`. With 27 pinned dependencies and no automated update path, CVE exposure is invisible.

### N5. Assorted robustness — **3–4 hours**
- `job_runner.py:161-166`: substring fuzzy matching means a short or partial query can silently dispatch the wrong job.
- `main.py:1353` `_CONVERSATIONS` grows one entry per distinct sender forever; TTL prunes turns within a sender but never removes the sender key.
- `main.py:1523` reads `favorites.json` by relative path. Safe today only because every plist template sets `WorkingDirectory` — it fails closed (blocks the sender) if that ever changes, which is the right direction but silent.
- `scripts/monitor_gateway.py:48-49` hardcodes `127.0.0.1:8000` while `config.py` has `UVICORN_HOST`/`UVICORN_PORT` (which are themselves absent from `.env`).
- This branch is **22 commits ahead of `main` and unmerged**; everything above ships only once B2 unblocks CI.

---

## Suggested sequence

| # | Item | Effort | Why first |
|---|---|---|---|
| 1 | ~~**B1** kill the duplicate gateway~~ ✅ **done 2026-09-02** | ≈1.5 h | Live incident; removed the source of B6's log growth |
| 2 | ~~**B2** fix ruff~~ ✅ **done 2026-09-02** | 15 m | Nothing else could merge until CI was green |
| 3 | ~~**B5** phone key + PII~~ ✅ **done 2026-09-04** | 1.5 h | Silent misrouting of every alert |
| 4 | ~~**B4** AppleScript argv~~ ✅ **done 2026-09-02** | 1 h | Only injection path reachable from an inbound message |
| 5 | ~~**B6** rotation + outbox TTL~~ ✅ **done 2026-09-04** | 1.5 h | Disk growth is unbounded |
| 6 | ~~**B3** runtime parity~~ ✅ **done 2026-09-04** | 2.5 h | Largest task; do it deliberately after the fires are out |
| 7 | S1–S4, S6, S9, S10 | ~2 days | Guardrails and honest receipts |
| 8 | S5 coverage, S7 migrations, S8 secrets | ~3 days | |
| 9 | N1–N5 | ~2 days | |

**All six blockers resolved.** Remaining: should-have items (S1–S10), ~1 week.

---

## What is already right

Worth stating plainly, because the audit above is by construction a list of problems:

- **Honest failure reporting is a design principle here, and it shows.** `send_imessage_attachment` verifies against chat.db and returns `submitted_unverified` rather than claiming success (`ivy_core/messaging.py:82-194`); `handle_pdf_command` surfaces that distinction to the user in plain language (`main.py:1272-1279`).
- **Error handling is disciplined** — zero bare `except:`, typed provider exceptions, and `safe_fetch_last_message` deliberately raises rather than returning `None`, because "no message" and "read failed" must not look alike (`main.py:1052-1054`).
- **The poller self-heals with real incident history encoded in the comments** (`main.py:1317-1345`) — including using `time.monotonic()` so a sleeping Mac isn't mistaken for a stale heartbeat.
- **`deploy/install_launchd.sh` is genuinely careful**: dry-run default, backups, refuses live labels without an explicit flag, and separates writing a plist from loading it.
- **`tests/conftest.py` is properly hermetic** — env defaults set before any app import, and an autouse fixture that redirects the receipts DB so tests can never touch the real one or text the household.
- **CI already does more than most**: compileall, a secret-file guard, ruff, bandit, and the full suite.

The foundation is sound. What is missing is the operational layer around it.
