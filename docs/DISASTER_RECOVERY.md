# Ivy disaster recovery

## Recovery guarantees and limits

Ivy's backup command creates a point-in-time archive of an explicit runtime-state allowlist. SQLite databases are copied through the SQLite backup API and checked with `PRAGMA quick_check`. Every archived file is inventoried with SHA-256, the archive is created under a private directory, and the final archive has mode `600`.

Checksums detect accidental corruption; they do not provide encryption or authenticity against an attacker who can rewrite both the archive and checksum file. Store backups on an encrypted, access-controlled volume. No automatic schedule, off-host replication, encryption, retention, or deletion policy is implemented here. The practical recovery-point objective is therefore the age of the most recent successful archive, and recovery time depends on Mac replacement, Messages.app sign-in, privacy approvals, dependency installation, and provider access.

## Default backup contents

When present, a normal backup includes:

- `data/picks.db`, copied consistently and integrity-checked;
- `logs/executions.db`, copied consistently and integrity-checked;
- `logs/imessage_worker.db`, preserving the collector cursor and privacy-minimized deduplication journal;
- regular files in the durable `data/outbox/`;
- `data/meal_plan_state.json`;
- `proactive_agents/sports_last_report.json`;
- installed plists for the four repository-managed Ivy labels that pass plist validation;
- a manifest containing creation time, source git SHA, dirty-state flag, format version, and whether sensitive files were included.

The command copies only this allowlist. It does not recursively archive the repository or logs.

Default backups explicitly exclude `.env`, contact allowlists, API tokens, credentials, certificates, private keys, backup codes, and local configuration that may contain secrets. They also do not back up Messages.app, `chat.db`, Apple privacy permissions, the Python virtual environment, provider-side data, or the git repository.

`com.ivy.brain` is reference-only and implemented in a separate project. This repository neither installs nor backs up that external service; recover it from its owning project.

The backup also inspects tracked launchd plists and fails closed if one embeds a credential-like value. Move credentials into the private runtime environment. Only the explicitly acknowledged sensitive-backup mode may archive such a plist.

## Create a backup

Preview, with no write:

```bash
./scripts/backup_state.sh
```

Create the default credential-excluding archive:

```bash
./scripts/backup_state.sh --apply
```

Choose an encrypted destination if needed. Custom destinations outside `~/ivy_backups` must be pre-created as a non-symlink directory with mode `700` (or stricter); the backup script will not change permissions on a pre-existing mount point or broad directory:

```bash
./scripts/backup_state.sh \
  --destination /Volumes/EncryptedDisk/IvyBackups \
  --apply
```

The destination directory is forced to mode `700`; the archive is mode `600`. The final `BACKUP_ARCHIVE=...` line identifies the exact artifact.

Credential exclusion does not make an archive non-sensitive: outbox metadata can contain recipients and report summaries, and PDFs contain report content. Protect every archive as private production data.

## Sensitive backup exception

Prefer a dedicated secrets manager or an independently encrypted escrow. If an incident plan explicitly requires secrets in the Ivy archive, both flags are mandatory:

```bash
./scripts/backup_state.sh \
  --include-sensitive \
  --yes-i-know-this-includes-secrets \
  --destination /Volumes/EncryptedIvyBackups \
  --apply
```

This adds only the script's explicit sensitive-file allowlist and root key/certificate patterns. It still does not recursively copy unknown files. Treat the archive as a production credential bundle. After restoring it to a different or potentially compromised host, rotate provider tokens and `ADMIN_SECRET`.

## Verify an archive without restoring

The restore dry-run performs the recovery validation path without changing production:

```bash
./scripts/restore_state.sh --backup /path/to/ivy-state-...tar.gz
```

It rejects compressed archives larger than 2 GiB before opening them, verifies that staging has the expanded size plus a free-space reserve, and then extracts to an unpredictable private staging directory. It rejects:

- absolute paths, traversal, duplicate members, links, devices, and multiple top-level roots;
- unreasonable member counts, more than 20 GiB of expanded content, or insufficient staging space;
- a missing or unsupported manifest;
- checksum entries outside the payload, unexpected or unchecksummed files, and SHA-256 mismatches;
- corrupt SQLite databases, malformed state JSON, and malformed launchd plists.

The private staging directory is deleted on exit. A passing dry-run means the archive is internally consistent; it does not prove the backup contains every state item an operator expected.

## Restore runtime state on the current Mac

1. Record the incident, current release SHA, selected archive, and monitor output. Confirm the archive dry-run passes.

2. Stop and unload every repository-managed writer so no process holds or rewrites the databases during replacement:

   ```bash
   launchctl bootout \
     "gui/$(id -u)" \
     "$HOME/Library/LaunchAgents/com.ivy.gateway.plist"

   Repeat the controlled bootout for `com.ivy.sharppicks`, `com.ivy.happy_hour_scout`, and `com.ivy.familia_meal_planner` if loaded. Confirm no `ivy_core.job_worker` process remains.
   ```

   Verify `launchctl print` no longer succeeds for any of those labels. The restore command refuses to apply while any managed writer is loaded or an Ivy gateway/job worker is still running.

3. Preview the exact restore mode again:

   ```bash
   ./scripts/restore_state.sh --backup /path/to/ivy-state-...tar.gz
   ```

4. Apply the runtime-state restore:

   ```bash
   ./scripts/restore_state.sh \
     --backup /path/to/ivy-state-...tar.gz \
     --apply \
     --yes-i-know-this-is-live
   ```

   The script requires an interactive terminal and the exact typed phrase `RESTORE IVY STATE`. Before replacement, it creates another backup under `~/ivy_backups/pre_restore/`. Every existing target is copied to an unpredictable private transaction snapshot before replacement; if any later target fails, all prior replacements are rolled back in reverse order. Files are installed through unpredictable same-directory temporary files, and unexpected symlink/directory/file target types are rejected. Files absent from the selected archive are left unchanged, except that the archived outbox snapshot is restored exactly.

5. Review the restored files and bootstrap the gateway:

   ```bash
   plutil -lint "$HOME/Library/LaunchAgents/com.ivy.gateway.plist"
   launchctl bootstrap \
     "gui/$(id -u)" \
     "$HOME/Library/LaunchAgents/com.ivy.gateway.plist"
   ./scripts/monitor_ivy.sh
   ```

6. Run the non-delivering smoke test:

   ```bash
   ./scripts/production_smoke_test.sh --check-running
   ```

Do not use a live-delivery smoke test until state, provider readiness, allowlists, and the intended recipient have been reviewed.

## Optional launchd and sensitive restore

To restore archived launchd files as well as runtime state:

```bash
./scripts/restore_state.sh \
  --backup /path/to/ivy-state-...tar.gz \
  --restore-launchd \
  --apply \
  --yes-i-know-this-is-live
```

The gateway must remain unloaded. Validate and bootstrap the restored plist manually afterward.

Sensitive files require an archive whose manifest says they were included and another explicit acknowledgment:

```bash
./scripts/restore_state.sh \
  --backup /path/to/sensitive-ivy-state-...tar.gz \
  --restore-sensitive \
  --yes-i-know-this-includes-secrets \
  --apply \
  --yes-i-know-this-is-live
```

Review file ownership and mode, rotate credentials as the incident requires, and never attach the archive or its extracted files to a ticket.

## Recover on a replacement Mac

1. Patch macOS and enable disk encryption.
2. Sign into the intended production user and Messages.app account.
3. Clone the repository from the trusted source and detach at the `SOURCE_GIT_SHA` recorded in the backup manifest.
4. Create `.venv` and install `requirements-dev.lock`; run `./scripts/check_hygiene.sh`.
5. Restore `.env`, allowlists, and credentials from the separate secret escrow, or use the explicit sensitive restore only from an encrypted trusted archive.
6. Run `./deploy/install_launchd.sh --validate-only`, then its dry-run. Resolve every preflight issue.
7. Validate the archive with a restore dry-run, stop any automatically loaded gateway, and apply state restoration.
8. Install reviewed plists with `./deploy/install_launchd.sh --apply --yes-i-know-this-is-live`.
9. Re-grant Automation, Accessibility, and Full Disk Access as required. Privacy grants are host-local security state and are not restored from the archive.
10. Bootstrap only `com.ivy.gateway`, run authenticated monitoring, and run the non-delivering production smoke test.
11. Reload scheduled delivery labels individually during a controlled maintenance window after reviewing recipients, schedule arguments, and state gates.

## Code rollback versus state restore

Use `rollback_release.sh` for a known-bad code release. It preserves current runtime state and creates a safety backup. Use `restore_state.sh` only when state is corrupt, lost, or explicitly incompatible with the selected code. A state downgrade can discard newer executions, picks, or outbox changes; make that compatibility decision deliberately.

If a release includes a data-model migration, record forward and backward compatibility in the change review before deployment. These scripts do not infer schema compatibility.

## Failed recovery

- If archive validation fails, do not override it. Select another archive and preserve the failing file for incident analysis.
- If the mandatory pre-restore safety backup fails, leave production stopped and resolve the source-state or storage failure. Do not bypass the last recoverable copy casually.
- If bootstrap fails, keep the gateway unloaded, validate the installed plist, inspect launchd output, and compare it with the rendered installer dry-run.
- If monitoring fails after restore, keep scheduled delivery agents untouched. Diagnose authentication, `chat.db`, database writability, provider readiness, and the checked-out SHA.
- If the host or secret archive may be compromised, isolate it and rotate credentials before returning Ivy to service.

Practice a dry-run validation and replacement-host recovery periodically. Record the observed recovery point and elapsed recovery time; those measurements, not the existence of this document, establish real RPO and RTO.
