#!/usr/bin/env bash
# Validate an Ivy state archive in a private staging directory and, only after
# explicit live flags plus typed confirmation, restore allowlisted state.

set -eEuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "${IVY_PROJECT_ROOT_OVERRIDE:-}" ]; then
    PROJECT_ROOT="$(cd "$IVY_PROJECT_ROOT_OVERRIDE" && pwd)"
else
    PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
BACKUP_FILE=""
SAFETY_BACKUP_DIR="$HOME/ivy_backups/pre_restore"
APPLY=false
CONFIRM_LIVE=false
RESTORE_LAUNCHD=false
RESTORE_SENSITIVE=false
CONFIRM_SENSITIVE=false
CONFIRM_PHRASE="RESTORE IVY STATE"
STAGE_ROOT=""
ROLLBACK_ROOT=""
TRANSACTION_ACTIVE=false

usage() {
    cat <<USAGE
Usage: $0 --backup ARCHIVE [--restore-launchd] [--restore-sensitive]
          [--safety-backup-dir DIR] [--apply --yes-i-know-this-is-live]

Dry-run is the default and still performs safe extraction, checksum, JSON,
SQLite, and plist validation. Applying also requires typing: $CONFIRM_PHRASE
Sensitive restore additionally requires --yes-i-know-this-includes-secrets.
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --backup)
            [ "$#" -ge 2 ] || { echo "ERROR: --backup requires a value." >&2; exit 2; }
            BACKUP_FILE="$2"
            shift 2
            ;;
        --safety-backup-dir)
            [ "$#" -ge 2 ] || { echo "ERROR: --safety-backup-dir requires a value." >&2; exit 2; }
            SAFETY_BACKUP_DIR="$2"
            shift 2
            ;;
        --apply) APPLY=true; shift ;;
        --yes-i-know-this-is-live) CONFIRM_LIVE=true; shift ;;
        --restore-launchd) RESTORE_LAUNCHD=true; shift ;;
        --restore-sensitive) RESTORE_SENSITIVE=true; shift ;;
        --yes-i-know-this-includes-secrets) CONFIRM_SENSITIVE=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -z "$BACKUP_FILE" ]; then
    echo "ERROR: --backup ARCHIVE is required." >&2
    usage >&2
    exit 2
fi
if [ ! -f "$BACKUP_FILE" ] || [ -L "$BACKUP_FILE" ]; then
    echo "ERROR: backup must be a regular, non-symlink file: $BACKUP_FILE" >&2
    exit 1
fi
if [ "$RESTORE_SENSITIVE" = true ] && [ "$CONFIRM_SENSITIVE" != true ]; then
    echo "ERROR: --restore-sensitive requires --yes-i-know-this-includes-secrets." >&2
    exit 2
fi
if [ "$CONFIRM_SENSITIVE" = true ] && [ "$RESTORE_SENSITIVE" != true ]; then
    echo "ERROR: the sensitive confirmation flag requires --restore-sensitive." >&2
    exit 2
fi

if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    PYTHON="$PROJECT_ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
else
    echo "ERROR: Python 3 is required for secure archive validation." >&2
    exit 1
fi

umask 077
STAGE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/ivy-restore.XXXXXX")"
chmod 700 "$STAGE_ROOT"
cleanup() {
    if [ -n "$STAGE_ROOT" ] && [ -d "$STAGE_ROOT" ]; then
        rm -rf -- "$STAGE_ROOT"
    fi
    if [ -n "$ROLLBACK_ROOT" ] && [ -d "$ROLLBACK_ROOT" ]; then
        rm -rf -- "$ROLLBACK_ROOT"
    fi
}
trap cleanup EXIT

echo "Validating backup in a private staging directory..."
payload="$("$PYTHON" - "$BACKUP_FILE" "$STAGE_ROOT" <<'PY'
import pathlib
import shutil
import sys
import tarfile

archive_path = pathlib.Path(sys.argv[1])
stage = pathlib.Path(sys.argv[2]).resolve()
max_members = 100_000
max_bytes = 20 * 1024 * 1024 * 1024
max_archive_bytes = 2 * 1024 * 1024 * 1024
free_space_reserve = 256 * 1024 * 1024

archive_size = archive_path.stat().st_size
if archive_size <= 0 or archive_size > max_archive_bytes:
    raise SystemExit("archive size is empty or exceeds the 2 GiB compressed safety limit")

with tarfile.open(archive_path, "r:gz") as archive:
    members = archive.getmembers()
    if not members or len(members) > max_members:
        raise SystemExit("archive member count is empty or unreasonable")
    expanded_bytes = sum(member.size for member in members)
    if expanded_bytes > max_bytes:
        raise SystemExit("archive expands beyond the 20 GiB safety limit")
    available_bytes = shutil.disk_usage(stage).free
    required_bytes = expanded_bytes + free_space_reserve
    if available_bytes < required_bytes:
        raise SystemExit("insufficient free space for private archive staging")

    names = set()
    roots = set()
    for member in members:
        pure = pathlib.PurePosixPath(member.name)
        has_control = any(ord(character) < 32 or ord(character) == 127 for character in member.name)
        if (
            pure.is_absolute()
            or not pure.parts
            or len(member.name) > 1024
            or has_control
            or any(part in ("", ".", "..") for part in pure.parts)
        ):
            raise SystemExit(f"unsafe archive path: {member.name!r}")
        if member.name in names:
            raise SystemExit(f"duplicate archive member: {member.name!r}")
        names.add(member.name)
        roots.add(pure.parts[0])
        if not (member.isdir() or member.isfile()):
            raise SystemExit(f"links and special files are forbidden: {member.name!r}")
    if len(roots) != 1:
        raise SystemExit("archive must contain exactly one top-level directory")
    root_name = next(iter(roots))
    if not root_name.startswith("ivy-state-"):
        raise SystemExit("unexpected backup top-level directory")

    for member in members:
        pure = pathlib.PurePosixPath(member.name)
        target = stage.joinpath(*pure.parts)
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            continue
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        source = archive.extractfile(member)
        if source is None:
            raise SystemExit(f"unable to read archive member: {member.name!r}")
        with source, target.open("xb") as destination:
            shutil.copyfileobj(source, destination)
        target.chmod(0o600)

print(stage / root_name)
PY
)"

if [ ! -f "$payload/manifest.txt" ] || [ ! -f "$payload/checksums.sha256" ]; then
    echo "ERROR: backup lacks manifest.txt or checksums.sha256." >&2
    exit 1
fi

"$PYTHON" - "$payload" <<'PY'
import hashlib
import json
import pathlib
import plistlib
import re
import sqlite3
import sys

root = pathlib.Path(sys.argv[1]).resolve()
manifest = {}
for raw_line in (root / "manifest.txt").read_text(encoding="utf-8").splitlines():
    if "=" not in raw_line:
        raise SystemExit("invalid manifest line")
    key, value = raw_line.split("=", 1)
    manifest[key] = value
if manifest.get("BACKUP_FORMAT_VERSION") != "1":
    raise SystemExit("unsupported backup format")

pattern = re.compile(r"^([0-9a-fA-F]{64}) [ *](.+)$")
expected = {}
for line in (root / "checksums.sha256").read_text(encoding="utf-8").splitlines():
    match = pattern.match(line)
    if not match:
        raise SystemExit("malformed checksum entry")
    digest, raw_name = match.groups()
    pure = pathlib.PurePosixPath(raw_name)
    if pure.is_absolute() or any(part in ("", "..") for part in pure.parts):
        raise SystemExit("unsafe checksum path")
    path = root.joinpath(*pure.parts).resolve()
    if root not in path.parents or not path.is_file() or path.is_symlink():
        raise SystemExit("checksum references an invalid file")
    relative = path.relative_to(root).as_posix()
    if relative in expected:
        raise SystemExit("duplicate checksum path")
    expected[relative] = digest.lower()

actual_files = {
    path.relative_to(root).as_posix()
    for path in root.rglob("*")
    if path.is_file() and path != root / "checksums.sha256"
}
if set(expected) != actual_files:
    raise SystemExit("checksum inventory does not match archive files")

allowed_state_files = {
    "state/data/picks.db",
    "state/logs/executions.db",
    "state/logs/imessage_worker.db",
    "state/data/meal_plan_state.json",
    "state/proactive_agents/sports_last_report.json",
}
allowed_launchd_files = {
    "launchd/com.ivy.gateway.plist",
    "launchd/com.ivy.sharppicks.plist",
    "launchd/com.ivy.happy_hour_scout.plist",
    "launchd/com.ivy.familia_meal_planner.plist",
}
allowed_sensitive_files = {
    "sensitive/.env",
    "sensitive/favorites.json",
    "sensitive/mcp-servers/imessage/favorites.json",
    "sensitive/service-account-key.json",
    "sensitive/token.json",
    "sensitive/google_credentials.json",
    "sensitive/discord_backup_codes.txt",
    "sensitive/store_configs.json",
}
sensitive_in_manifest = manifest.get("SENSITIVE_FILES_INCLUDED")
if sensitive_in_manifest not in {"true", "false"}:
    raise SystemExit("manifest has an invalid sensitive-files flag")
for relative in actual_files:
    allowed = relative == "manifest.txt"
    allowed = allowed or relative in allowed_state_files
    allowed = allowed or relative in allowed_launchd_files
    if relative.startswith("state/data/outbox/"):
        suffix = relative.removeprefix("state/data/outbox/")
        allowed = "/" not in suffix and suffix.endswith((".json", ".pdf"))
    if relative in allowed_sensitive_files:
        allowed = True
    if relative.startswith("sensitive/") and relative.endswith((".pem", ".key", ".p12", ".pfx", ".jks")):
        suffix = relative.removeprefix("sensitive/")
        allowed = "/" not in suffix
    if not allowed:
        raise SystemExit(f"file is outside the restore allowlist: {relative}")
    if relative.startswith("sensitive/") and sensitive_in_manifest != "true":
        raise SystemExit("archive contains sensitive files but its manifest says otherwise")
for relative, expected_digest in expected.items():
    hasher = hashlib.sha256()
    with (root / relative).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    if digest != expected_digest:
        raise SystemExit(f"checksum mismatch: {relative}")

for database in (
    root / "state/data/picks.db",
    root / "state/logs/executions.db",
    root / "state/logs/imessage_worker.db",
):
    if database.exists():
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            row = connection.execute("PRAGMA quick_check").fetchone()
            if not row or row[0] != "ok":
                raise SystemExit(f"SQLite quick_check failed: {database.name}")
        finally:
            connection.close()

json_paths = (root / "state").rglob("*.json") if (root / "state").exists() else ()
for json_path in json_paths:
    with json_path.open("r", encoding="utf-8") as handle:
        json.load(handle)
plist_paths = (root / "launchd").glob("*.plist") if (root / "launchd").exists() else ()
for plist_path in plist_paths:
    with plist_path.open("rb") as handle:
        plistlib.load(handle)

print("Backup checksums and structured state passed integrity checks.")
PY

manifest_sensitive="$(sed -n 's/^SENSITIVE_FILES_INCLUDED=//p' "$payload/manifest.txt" | tail -n 1)"
if [ "$RESTORE_SENSITIVE" = true ] && [ "$manifest_sensitive" != "true" ]; then
    echo "ERROR: sensitive restore requested, but this backup excluded sensitive files." >&2
    exit 1
fi

echo "Ivy state restore"
echo "  Backup:    $BACKUP_FILE"
echo "  State:     will restore allowlisted files present in the archive"
echo "  Launchd:   $RESTORE_LAUNCHD"
echo "  Sensitive: $RESTORE_SENSITIVE"

if [ "$APPLY" != true ]; then
    echo "  Mode:      DRY-RUN"
    echo "Archive validation passed; no production file was changed."
    echo "Apply with --apply --yes-i-know-this-is-live after stopping com.ivy.gateway."
    exit 0
fi
if [ "$CONFIRM_LIVE" != true ]; then
    echo "ERROR: --apply requires --yes-i-know-this-is-live." >&2
    exit 2
fi
if [ "$(uname -s)" != "Darwin" ]; then
    echo "ERROR: live restore is supported only on macOS." >&2
    exit 1
fi
writer_labels=(
    "com.ivy.gateway"
    "com.ivy.sharppicks"
    "com.ivy.happy_hour_scout"
    "com.ivy.familia_meal_planner"
)
for writer_label in "${writer_labels[@]}"; do
    if /bin/launchctl print "gui/$(id -u)/$writer_label" >/dev/null 2>&1; then
        echo "ERROR: $writer_label is loaded. Unload every Ivy writer before restoring state." >&2
        exit 1
    fi
done
if ps -axo command= | awk -v root="$PROJECT_ROOT" '
    index($0, root "/main.py") || index($0, "ivy_core.job_worker") { found = 1 }
    END { exit found ? 0 : 1 }
'; then
    echo "ERROR: an Ivy gateway or scheduled job worker is still running. Stop every writer before restoring state." >&2
    exit 1
fi
if [ ! -r /dev/tty ]; then
    echo "ERROR: restore requires an interactive terminal for typed confirmation." >&2
    exit 1
fi

echo
echo "WARNING: this replaces production state files after creating a safety backup."
printf 'Type exactly "%s" to continue: ' "$CONFIRM_PHRASE" > /dev/tty
confirmation=""
IFS= read -r confirmation < /dev/tty || true
if [ "$confirmation" != "$CONFIRM_PHRASE" ]; then
    echo "Confirmation did not match; nothing was restored." >&2
    exit 1
fi

backup_args=(--apply --destination "$SAFETY_BACKUP_DIR")
if [ "$RESTORE_SENSITIVE" = true ]; then
    backup_args+=(--include-sensitive --yes-i-know-this-includes-secrets)
fi
echo "Creating pre-restore safety backup..."
"$SCRIPT_DIR/backup_state.sh" "${backup_args[@]}"

ROLLBACK_ROOT="$(mktemp -d "$PROJECT_ROOT/.ivy-restore-rollback.XXXXXX")"
chmod 700 "$ROLLBACK_ROOT"
rollback_targets=()
rollback_copies=()
rollback_kinds=()

ensure_directory() {
    local directory="$1"
    if [ -e "$directory" ] || [ -L "$directory" ]; then
        if [ ! -d "$directory" ] || [ -L "$directory" ]; then
            echo "ERROR: restore target parent must be a real directory: $directory" >&2
            return 1
        fi
        return 0
    fi
    local parent="$(dirname "$directory")"
    if [ ! -d "$parent" ] || [ -L "$parent" ]; then
        echo "ERROR: restore target parent is unavailable or unsafe: $parent" >&2
        return 1
    fi
    mkdir "$directory"
    chmod 700 "$directory"
}

snapshot_target() {
    local target="$1"
    local expected_kind="$2"
    local index="${#rollback_targets[@]}"
    local copy="$ROLLBACK_ROOT/$index"

    if [ -L "$target" ]; then
        echo "ERROR: restore target must not be a symlink: $target" >&2
        return 1
    fi
    if [ ! -e "$target" ]; then
        rollback_targets+=("$target")
        rollback_copies+=("$copy")
        rollback_kinds+=("missing")
        return 0
    fi
    if [ "$expected_kind" = "file" ]; then
        if [ ! -f "$target" ]; then
            echo "ERROR: restore target has an unexpected type: $target" >&2
            return 1
        fi
        cp -p "$target" "$copy"
        rollback_targets+=("$target")
        rollback_copies+=("$copy")
        rollback_kinds+=("file")
    else
        if [ ! -d "$target" ]; then
            echo "ERROR: restore target has an unexpected type: $target" >&2
            return 1
        fi
        cp -pR "$target" "$copy"
        rollback_targets+=("$target")
        rollback_copies+=("$copy")
        rollback_kinds+=("directory")
    fi
}

rollback_restore_transaction() {
    local status="$?"
    local index=0
    local rollback_failed=false
    trap - ERR
    set +e
    if [ "$TRANSACTION_ACTIVE" = true ]; then
        echo "ERROR: restore failed; restoring every target from the private transaction snapshot." >&2
        for ((index=${#rollback_targets[@]} - 1; index >= 0; index--)); do
            target="${rollback_targets[$index]}"
            copy="${rollback_copies[$index]}"
            kind="${rollback_kinds[$index]}"
            if [ -e "$target" ] || [ -L "$target" ]; then
                rm -rf -- "$target" || rollback_failed=true
            fi
            case "$kind" in
                file) cp -p "$copy" "$target" || rollback_failed=true ;;
                directory) cp -pR "$copy" "$target" || rollback_failed=true ;;
                missing) ;;
            esac
        done
        if [ "$rollback_failed" = true ]; then
            echo "CRITICAL: automatic restore rollback was incomplete; keep every Ivy writer stopped and use the pre-restore safety backup." >&2
        else
            echo "Restore transaction rolled back. The gateway and scheduled workers remain stopped." >&2
        fi
    fi
    exit "$status"
}
TRANSACTION_ACTIVE=true
trap rollback_restore_transaction ERR

install_state_file() {
    local relative_source="$1"
    local target="$2"
    local source="$payload/$relative_source"
    [ -f "$source" ] || return 0
    ensure_directory "$(dirname "$target")"
    snapshot_target "$target" "file"
    local temporary=""
    temporary="$(mktemp "$(dirname "$target")/.ivy-restore-file.XXXXXX")"
    install -m 600 "$source" "$temporary"
    mv -f "$temporary" "$target"
    echo "Restored $target"
}

ensure_directory "$PROJECT_ROOT/data"
ensure_directory "$PROJECT_ROOT/logs"
install_state_file "state/data/picks.db" "$PROJECT_ROOT/data/picks.db"
install_state_file "state/logs/executions.db" "$PROJECT_ROOT/logs/executions.db"
install_state_file "state/logs/imessage_worker.db" "$PROJECT_ROOT/logs/imessage_worker.db"
install_state_file "state/data/meal_plan_state.json" "$PROJECT_ROOT/data/meal_plan_state.json"
install_state_file "state/proactive_agents/sports_last_report.json" "$PROJECT_ROOT/proactive_agents/sports_last_report.json"

if [ -d "$payload/state/data/outbox" ]; then
    ensure_directory "$PROJECT_ROOT/data"
    snapshot_target "$PROJECT_ROOT/data/outbox" "directory"
    new_outbox="$(mktemp -d "$PROJECT_ROOT/data/.outbox.restore.XXXXXX")"
    chmod 700 "$new_outbox"
    shopt -s nullglob dotglob
    for source_file in "$payload/state/data/outbox"/*; do
        [ -f "$source_file" ] || continue
        install -m 600 "$source_file" "$new_outbox/$(basename "$source_file")"
    done
    if [ -e "$PROJECT_ROOT/data/outbox" ] || [ -L "$PROJECT_ROOT/data/outbox" ]; then
        if [ ! -d "$PROJECT_ROOT/data/outbox" ] || [ -L "$PROJECT_ROOT/data/outbox" ]; then
            echo "ERROR: durable outbox restore target has an unexpected type." >&2
            false
        fi
        rm -rf -- "$PROJECT_ROOT/data/outbox"
    fi
    mv "$new_outbox" "$PROJECT_ROOT/data/outbox"
    echo "Restored durable outbox snapshot."
fi

if [ "$RESTORE_LAUNCHD" = true ]; then
    ensure_directory "$HOME/Library/LaunchAgents"
    shopt -s nullglob
    for source_plist in "$payload/launchd"/*.plist; do
        plist_name="$(basename "$source_plist")"
        case "$plist_name" in
            com.ivy.gateway.plist|com.ivy.sharppicks.plist|com.ivy.happy_hour_scout.plist|com.ivy.familia_meal_planner.plist) ;;
            *) echo "ERROR: refusing unexpected launchd plist: $plist_name" >&2; false ;;
        esac
        install_state_file "launchd/$plist_name" "$HOME/Library/LaunchAgents/$plist_name"
    done
fi

if [ "$RESTORE_SENSITIVE" = true ]; then
    sensitive_files=(
        ".env"
        "favorites.json"
        "mcp-servers/imessage/favorites.json"
        "service-account-key.json"
        "token.json"
        "google_credentials.json"
        "discord_backup_codes.txt"
        "store_configs.json"
    )
    for relative_path in "${sensitive_files[@]}"; do
        install_state_file "sensitive/$relative_path" "$PROJECT_ROOT/$relative_path"
    done
    shopt -s nullglob
    for source_key in "$payload/sensitive"/*.pem "$payload/sensitive"/*.key "$payload/sensitive"/*.p12 "$payload/sensitive"/*.pfx "$payload/sensitive"/*.jks; do
        install_state_file "sensitive/$(basename "$source_key")" "$PROJECT_ROOT/$(basename "$source_key")"
    done
fi

TRANSACTION_ACTIVE=false
trap - ERR
echo "Restore completed with the gateway still stopped. Review files, bootstrap com.ivy.gateway, then run scripts/monitor_ivy.sh."
