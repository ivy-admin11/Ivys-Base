#!/usr/bin/env bash
# Create a permission-restricted, checksummed backup of Ivy runtime state.
# Dry-run by default. Secret-bearing files are excluded unless two explicit
# sensitive-backup flags are supplied.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "${IVY_PROJECT_ROOT_OVERRIDE:-}" ]; then
    PROJECT_ROOT="$(cd "$IVY_PROJECT_ROOT_OVERRIDE" && pwd)"
else
    PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
DESTINATION="$HOME/ivy_backups"
DEFAULT_BACKUP_ROOT="$HOME/ivy_backups"
LAUNCH_AGENTS_DIR="${IVY_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
APPLY=false
INCLUDE_SENSITIVE=false
CONFIRM_SENSITIVE=false
STAGE_ROOT=""
ARCHIVE_TMP=""

usage() {
    cat <<USAGE
Usage: $0 [--destination DIR] [--apply]
       $0 --include-sensitive --yes-i-know-this-includes-secrets [--apply]

Default contents: SQLite state, durable outbox files, agent state JSON, and
installed Ivy launchd plists. .env, contact allowlists, credentials, and keys
are excluded by default. No archive is created without --apply.
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --apply) APPLY=true; shift ;;
        --destination)
            [ "$#" -ge 2 ] || { echo "ERROR: --destination requires a value." >&2; exit 2; }
            DESTINATION="$2"
            shift 2
            ;;
        --include-sensitive) INCLUDE_SENSITIVE=true; shift ;;
        --yes-i-know-this-includes-secrets) CONFIRM_SENSITIVE=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ "$INCLUDE_SENSITIVE" = true ] && [ "$CONFIRM_SENSITIVE" != true ]; then
    echo "ERROR: --include-sensitive requires --yes-i-know-this-includes-secrets." >&2
    exit 2
fi
if [ "$CONFIRM_SENSITIVE" = true ] && [ "$INCLUDE_SENSITIVE" != true ]; then
    echo "ERROR: the sensitive confirmation flag requires --include-sensitive." >&2
    exit 2
fi

echo "Ivy state backup"
echo "  Source:      $PROJECT_ROOT"
echo "  Destination: $DESTINATION"
if [ "$INCLUDE_SENSITIVE" = true ]; then
    echo "  Secrets:     INCLUDED by explicit request"
else
    echo "  Secrets:     EXCLUDED"
fi

if [ "$APPLY" != true ]; then
    echo "  Mode:        DRY-RUN"
    echo
    echo "Would back up, when present:"
    echo "  data/picks.db (SQLite online backup)"
    echo "  logs/executions.db (SQLite online backup)"
    echo "  data/outbox/* regular files"
    echo "  data/meal_plan_state.json and proactive_agents/sports_last_report.json"
    echo "  tracked Ivy plists under $LAUNCH_AGENTS_DIR"
    if [ "$INCLUDE_SENSITIVE" = true ]; then
        echo "  explicitly allowlisted secret/contact/credential files"
    else
        echo "  NOT .env, favorites.json, tokens, credentials, certificates, or keys"
    fi
    echo "No archive was created. Add --apply to create it."
    exit 0
fi

if command -v shasum >/dev/null 2>&1; then
    SHASUM=(shasum -a 256)
elif command -v sha256sum >/dev/null 2>&1; then
    SHASUM=(sha256sum)
else
    echo "ERROR: shasum or sha256sum is required." >&2
    exit 1
fi
if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    PYTHON="$PROJECT_ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
else
    echo "ERROR: Python 3 is required for consistent SQLite backups." >&2
    exit 1
fi
if ! command -v tar >/dev/null 2>&1; then
    echo "ERROR: tar is required." >&2
    exit 1
fi
case "$DESTINATION" in
    *$'\n'*|*$'\r'*) echo "ERROR: destination contains an unsupported newline." >&2; exit 2 ;;
esac
if [ -L "$DESTINATION" ]; then
    echo "ERROR: backup destination must not be a symlink: $DESTINATION" >&2
    exit 1
fi

umask 077

directory_mode() {
    local path="$1"
    if stat -f '%Lp' "$path" >/dev/null 2>&1; then
        stat -f '%Lp' "$path"
    else
        stat -c '%a' "$path"
    fi
}

require_private_existing_directory() {
    local path="$1"
    local mode=""
    if [ ! -d "$path" ] || [ -L "$path" ]; then
        echo "ERROR: backup destination must be a regular directory, not a link: $path" >&2
        exit 1
    fi
    mode="$(directory_mode "$path")"
    if [ $((8#$mode & 8#077)) -ne 0 ]; then
        echo "ERROR: existing backup destination must already be private (mode 700 or stricter): $path" >&2
        exit 1
    fi
}

create_default_destination_tree() {
    local requested="$1"
    local relative=""
    local current="$DEFAULT_BACKUP_ROOT"
    local component=""

    case "$requested" in
        "$DEFAULT_BACKUP_ROOT") relative="" ;;
        "$DEFAULT_BACKUP_ROOT"/*) relative="${requested#"$DEFAULT_BACKUP_ROOT"/}" ;;
        *) return 1 ;;
    esac
    case "/$relative/" in
        */../*|*/./*) echo "ERROR: backup destination contains an unsafe path component." >&2; exit 2 ;;
    esac

    if [ -e "$current" ] || [ -L "$current" ]; then
        require_private_existing_directory "$current"
    else
        mkdir "$current"
        chmod 700 "$current"
    fi
    if [ -n "$relative" ]; then
        local old_ifs="$IFS"
        IFS='/'
        read -r -a components <<< "$relative"
        IFS="$old_ifs"
        for component in "${components[@]}"; do
            [ -n "$component" ] || { echo "ERROR: backup destination contains an empty path component." >&2; exit 2; }
            current="$current/$component"
            if [ -e "$current" ] || [ -L "$current" ]; then
                require_private_existing_directory "$current"
            else
                mkdir "$current"
                chmod 700 "$current"
            fi
        done
    fi
}

if ! create_default_destination_tree "$DESTINATION"; then
    case "$DESTINATION" in
        /*) ;;
        *) echo "ERROR: a custom backup destination must be an absolute path." >&2; exit 2 ;;
    esac
    case "$DESTINATION" in
        /|/Users|/Volumes|/private|/private/tmp|/tmp|"$HOME"|"$PROJECT_ROOT")
            echo "ERROR: refusing a broad custom backup destination: $DESTINATION" >&2
            exit 2
            ;;
    esac
    custom_relative="${DESTINATION#/}"
    case "$custom_relative" in
        */*/*) ;;
        *) echo "ERROR: refusing a broad custom backup destination: $DESTINATION" >&2; exit 2 ;;
    esac
    case "$PROJECT_ROOT/" in
        "$DESTINATION"/*) echo "ERROR: backup destination must not contain the project checkout." >&2; exit 2 ;;
    esac
    case "$HOME/" in
        "$DESTINATION"/*) echo "ERROR: backup destination must not contain the user home directory." >&2; exit 2 ;;
    esac
    if [ ! -e "$DESTINATION" ] && [ ! -L "$DESTINATION" ]; then
        echo "ERROR: a custom backup destination must be pre-created with private permissions." >&2
        echo "Use $DEFAULT_BACKUP_ROOT (or a child of it) for safely created default storage." >&2
        exit 1
    fi
    require_private_existing_directory "$DESTINATION"
fi
STAGE_ROOT="$(mktemp -d "$DESTINATION/.ivy-backup-stage.XXXXXX")"
chmod 700 "$STAGE_ROOT"

cleanup() {
    if [ -n "$STAGE_ROOT" ] && [ -d "$STAGE_ROOT" ]; then
        rm -rf -- "$STAGE_ROOT"
    fi
    if [ -n "$ARCHIVE_TMP" ] && [ -f "$ARCHIVE_TMP" ]; then
        rm -f -- "$ARCHIVE_TMP"
    fi
}
trap cleanup EXIT

timestamp="$(date -u '+%Y%m%dT%H%M%SZ')"
git_sha="$(git -C "$PROJECT_ROOT" rev-parse HEAD 2>/dev/null || printf 'unknown')"
short_sha="$(printf '%s' "$git_sha" | cut -c1-12)"
payload_name="ivy-state-$timestamp-$short_sha"
payload="$STAGE_ROOT/$payload_name"
mkdir -p "$payload/state/data" "$payload/state/logs" "$payload/launchd"

backup_sqlite() {
    local source_path="$1"
    local destination_path="$2"
    if [ ! -e "$source_path" ] && [ ! -L "$source_path" ]; then
        return 0
    fi
    if [ ! -f "$source_path" ] || [ -L "$source_path" ]; then
        echo "ERROR: SQLite source must be a regular, non-symlink file: $source_path" >&2
        exit 1
    fi
    "$PYTHON" - "$source_path" "$destination_path" <<'PY'
import sqlite3
import sys

source_path, destination_path = sys.argv[1:]
source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
destination = sqlite3.connect(destination_path)
try:
    source.backup(destination)
    row = destination.execute("PRAGMA quick_check").fetchone()
    if not row or row[0] != "ok":
        raise SystemExit(f"SQLite quick_check failed for {source_path}")
finally:
    destination.close()
    source.close()
PY
    chmod 600 "$destination_path"
}

copy_regular_file() {
    local source_path="$1"
    local destination_path="$2"
    if [ ! -e "$source_path" ] && [ ! -L "$source_path" ]; then
        return 0
    fi
    if [ ! -f "$source_path" ] || [ -L "$source_path" ]; then
        echo "ERROR: backup source must be a regular, non-symlink file: $source_path" >&2
        exit 1
    fi
    case "$source_path" in
        *$'\n'*|*$'\r'*) echo "ERROR: refusing a state filename containing a newline." >&2; exit 1 ;;
    esac
    mkdir -p "$(dirname "$destination_path")"
    cp -p "$source_path" "$destination_path"
    chmod 600 "$destination_path"
}

plist_contains_sensitive_value() {
    local plist_path="$1"
    "$PYTHON" - "$plist_path" <<'PY'
import plistlib
import re
import sys

with open(sys.argv[1], "rb") as handle:
    payload = plistlib.load(handle)

sensitive_key = re.compile(
    r"(?:api[_-]?key|secret|token|password|authorization|credential)",
    re.IGNORECASE,
)
sensitive_assignment = re.compile(
    r"(?:api[_-]?key|secret|token|password|authorization|credential)\s*[=:]\s*\S+",
    re.IGNORECASE,
)


def has_sensitive_value(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if sensitive_key.search(str(key)) and item not in (None, "", False):
                return True
            if has_sensitive_value(item):
                return True
        return False
    if isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str) and sensitive_key.search(item):
                if index + 1 < len(value) and value[index + 1] not in (None, "", False):
                    return True
            if has_sensitive_value(item):
                return True
        return False
    return isinstance(value, str) and bool(sensitive_assignment.search(value))


raise SystemExit(0 if has_sensitive_value(payload) else 1)
PY
}

validate_plist_file() {
    local plist_path="$1"
    "$PYTHON" - "$plist_path" >/dev/null 2>&1 <<'PY'
import plistlib
import sys

with open(sys.argv[1], "rb") as handle:
    plistlib.load(handle)
PY
}

backup_sqlite "$PROJECT_ROOT/data/picks.db" "$payload/state/data/picks.db"
backup_sqlite "$PROJECT_ROOT/logs/executions.db" "$payload/state/logs/executions.db"
backup_sqlite "$PROJECT_ROOT/logs/imessage_worker.db" "$payload/state/logs/imessage_worker.db"
copy_regular_file "$PROJECT_ROOT/data/meal_plan_state.json" "$payload/state/data/meal_plan_state.json"
copy_regular_file "$PROJECT_ROOT/proactive_agents/sports_last_report.json" "$payload/state/proactive_agents/sports_last_report.json"

shopt -s nullglob dotglob
if [ -L "$PROJECT_ROOT/data/outbox" ]; then
    echo "ERROR: durable outbox must not be a symlink: $PROJECT_ROOT/data/outbox" >&2
    exit 1
elif [ -e "$PROJECT_ROOT/data/outbox" ] && [ ! -d "$PROJECT_ROOT/data/outbox" ]; then
    echo "ERROR: durable outbox path is not a directory: $PROJECT_ROOT/data/outbox" >&2
    exit 1
elif [ -d "$PROJECT_ROOT/data/outbox" ]; then
    mkdir -p "$payload/state/data/outbox"
    for outbox_file in "$PROJECT_ROOT/data/outbox"/*; do
        outbox_name="$(basename "$outbox_file")"
        case "$outbox_name" in
            .DS_Store) continue ;;
            *.json|*.pdf) ;;
            *) echo "ERROR: unexpected durable outbox entry: $outbox_name" >&2; exit 1 ;;
        esac
        copy_regular_file "$outbox_file" "$payload/state/data/outbox/$outbox_name"
    done
fi

launchd_names=(
    "com.ivy.gateway.plist"
    "com.ivy.sharppicks.plist"
    "com.ivy.happy_hour_scout.plist"
    "com.ivy.familia_meal_planner.plist"
)
for launchd_name in "${launchd_names[@]}"; do
    plist="$LAUNCH_AGENTS_DIR/$launchd_name"
    if [ -e "$plist" ] || [ -L "$plist" ]; then
        if [ ! -f "$plist" ] || [ -L "$plist" ]; then
            echo "ERROR: launchd source must be a regular, non-symlink file: $plist" >&2
            exit 1
        fi
        if ! validate_plist_file "$plist"; then
            echo "ERROR: refusing to back up invalid plist: $plist" >&2
            exit 1
        fi
        if command -v plutil >/dev/null 2>&1 && ! plutil -lint "$plist" >/dev/null; then
            echo "ERROR: refusing to back up invalid plist: $plist" >&2
            exit 1
        fi
        if [ "$INCLUDE_SENSITIVE" != true ] && plist_contains_sensitive_value "$plist"; then
            echo "ERROR: launchd plist contains an embedded credential-like value: $plist" >&2
            echo "Move the value to the private runtime environment, or use the explicit sensitive-backup mode." >&2
            exit 1
        fi
        copy_regular_file "$plist" "$payload/launchd/$(basename "$plist")"
    fi
done

if [ "$INCLUDE_SENSITIVE" = true ]; then
    mkdir -p "$payload/sensitive/mcp-servers/imessage"
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
        copy_regular_file "$PROJECT_ROOT/$relative_path" "$payload/sensitive/$relative_path"
    done
    for key_file in "$PROJECT_ROOT"/*.pem "$PROJECT_ROOT"/*.key "$PROJECT_ROOT"/*.p12 "$PROJECT_ROOT"/*.pfx "$PROJECT_ROOT"/*.jks; do
        copy_regular_file "$key_file" "$payload/sensitive/$(basename "$key_file")"
    done
fi

"$PYTHON" - "$payload/state" <<'PY'
import json
import pathlib
import sys

state_root = pathlib.Path(sys.argv[1])
for json_path in state_root.rglob("*.json"):
    with json_path.open("r", encoding="utf-8") as handle:
        json.load(handle)
for pdf_path in state_root.rglob("*.pdf"):
    if pdf_path.stat().st_size == 0:
        raise SystemExit(f"refusing to archive an empty PDF: {pdf_path.name}")
PY

dirty=false
if [ -n "$(git -C "$PROJECT_ROOT" status --porcelain 2>/dev/null || true)" ]; then
    dirty=true
fi
printf '%s\n' \
    "BACKUP_FORMAT_VERSION=1" \
    "CREATED_UTC=$timestamp" \
    "SOURCE_GIT_SHA=$git_sha" \
    "SOURCE_WORKTREE_DIRTY=$dirty" \
    "SENSITIVE_FILES_INCLUDED=$INCLUDE_SENSITIVE" \
    "STATE_LAYOUT=allowlisted-runtime-state" \
    > "$payload/manifest.txt"

(
    cd "$payload"
    : > checksums.sha256
    while IFS= read -r relative_path; do
        "${SHASUM[@]}" "$relative_path" >> checksums.sha256
    done < <(find . -type f ! -name checksums.sha256 -print | LC_ALL=C sort)
)

find "$payload" -type d -exec chmod 700 {} \;
find "$payload" -type f -exec chmod 600 {} \;

archive="$DESTINATION/$payload_name.tar.gz"
if [ -e "$archive" ]; then
    echo "ERROR: backup archive already exists: $archive" >&2
    exit 1
fi
ARCHIVE_TMP="$(mktemp "$DESTINATION/.ivy-archive.XXXXXX")"
chmod 600 "$ARCHIVE_TMP"
(
    cd "$STAGE_ROOT"
    COPYFILE_DISABLE=1 tar -czf "$ARCHIVE_TMP" "$payload_name"
)
chmod 600 "$ARCHIVE_TMP"
mv "$ARCHIVE_TMP" "$archive"
ARCHIVE_TMP=""

echo "  Mode:        APPLY"
echo "Backup created and permission-restricted: $archive"
echo "BACKUP_ARCHIVE=$archive"
