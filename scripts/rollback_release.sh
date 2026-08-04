#!/usr/bin/env bash
# Roll back the in-place production checkout to a validated commit. Dry-run is
# the default. Live rollback takes a state backup before switching code.

set -eEuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "${IVY_PROJECT_ROOT_OVERRIDE:-}" ]; then
    PROJECT_ROOT="$(cd "$IVY_PROJECT_ROOT_OVERRIDE" && pwd)"
else
    PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
TARGET_REF=""
USE_LAST=false
BACKUP_DIR="$HOME/ivy_backups/rollbacks"
OPERATIONS_DIR="$HOME/.ivy-operations"
ENV_FILE="$PROJECT_ROOT/.env"
BASE_URL="http://127.0.0.1:8000"
APPLY=false
CONFIRM_LIVE=false
INSTALL_DEPENDENCIES=true
READINESS_TIMEOUT_SECONDS="${IVY_READINESS_TIMEOUT_SECONDS:-90}"
TOOL_SNAPSHOT=""
PERSISTENT_TOOL_SNAPSHOT=""
VENV_TARGET="$PROJECT_ROOT/.venv"
VENV_TARGET_CREATED=false
ORIGINAL_PROD_VENV=""
GATEWAY_WAS_LOADED=false

usage() {
    cat <<USAGE
Usage: $0 (--ref GIT_REF | --last) [--backup-dir DIR] [--operations-dir DIR]
          [--env-file PATH] [--base-url LOOPBACK_URL]
          [--readiness-timeout-seconds 90..600]
          [--skip-dependency-install] [--apply --yes-i-know-this-is-live]

--last uses PREVIOUS_SHA from the most recent successful deployment record.
Applying leaves the production checkout detached at the exact rollback SHA.
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --ref)
            [ "$#" -ge 2 ] || { echo "ERROR: --ref requires a value." >&2; exit 2; }
            TARGET_REF="$2"
            shift 2
            ;;
        --last) USE_LAST=true; shift ;;
        --backup-dir)
            [ "$#" -ge 2 ] || { echo "ERROR: --backup-dir requires a value." >&2; exit 2; }
            BACKUP_DIR="$2"
            shift 2
            ;;
        --operations-dir)
            [ "$#" -ge 2 ] || { echo "ERROR: --operations-dir requires a value." >&2; exit 2; }
            OPERATIONS_DIR="$2"
            shift 2
            ;;
        --env-file)
            [ "$#" -ge 2 ] || { echo "ERROR: --env-file requires a value." >&2; exit 2; }
            ENV_FILE="$2"
            shift 2
            ;;
        --base-url)
            [ "$#" -ge 2 ] || { echo "ERROR: --base-url requires a value." >&2; exit 2; }
            BASE_URL="$2"
            shift 2
            ;;
        --readiness-timeout-seconds)
            [ "$#" -ge 2 ] || { echo "ERROR: --readiness-timeout-seconds requires a value." >&2; exit 2; }
            READINESS_TIMEOUT_SECONDS="$2"
            shift 2
            ;;
        --skip-dependency-install) INSTALL_DEPENDENCIES=false; shift ;;
        --apply) APPLY=true; shift ;;
        --yes-i-know-this-is-live) CONFIRM_LIVE=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

case "$READINESS_TIMEOUT_SECONDS" in
    ''|*[!0-9]*) echo "ERROR: readiness timeout must be an integer from 90 through 600 seconds." >&2; exit 2 ;;
esac
if [ "$READINESS_TIMEOUT_SECONDS" -lt 90 ] || [ "$READINESS_TIMEOUT_SECONDS" -gt 600 ]; then
    echo "ERROR: readiness timeout must be from 90 through 600 seconds." >&2
    exit 2
fi

if [ "$USE_LAST" = true ] && [ -n "$TARGET_REF" ]; then
    echo "ERROR: choose exactly one of --ref or --last." >&2
    exit 2
fi
if [ "$USE_LAST" != true ] && [ -z "$TARGET_REF" ]; then
    echo "ERROR: choose --ref GIT_REF or --last." >&2
    exit 2
fi
if [ "$USE_LAST" = true ]; then
    current_manifest="$OPERATIONS_DIR/current.env"
    if [ ! -f "$current_manifest" ] || [ -L "$current_manifest" ]; then
        echo "ERROR: no trusted deployment record exists at $current_manifest" >&2
        exit 1
    fi
    TARGET_REF="$(sed -n 's/^PREVIOUS_SHA=//p' "$current_manifest" | tail -n 1)"
    if [ -z "$TARGET_REF" ]; then
        echo "ERROR: the current deployment record has no previous release SHA." >&2
        exit 1
    fi
fi
case "$TARGET_REF" in
    -*|*[!0-9a-zA-Z._/~^-]*) echo "ERROR: rollback ref contains unsupported characters." >&2; exit 2 ;;
esac

if ! git -C "$PROJECT_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    echo "ERROR: project root is not a git repository: $PROJECT_ROOT" >&2
    exit 1
fi
if [ -n "$(git -C "$PROJECT_ROOT" status --porcelain)" ]; then
    echo "ERROR: rollback refuses a dirty worktree. Preserve or remove every change first." >&2
    git -C "$PROJECT_ROOT" status --short >&2
    exit 1
fi
if ! target_sha="$(git -C "$PROJECT_ROOT" rev-parse --verify "${TARGET_REF}^{commit}" 2>/dev/null)"; then
    echo "ERROR: rollback ref does not resolve to a local commit: $TARGET_REF" >&2
    exit 1
fi
original_sha="$(git -C "$PROJECT_ROOT" rev-parse HEAD)"
if [ "$target_sha" = "$original_sha" ]; then
    echo "ERROR: rollback target is already checked out: $target_sha" >&2
    exit 1
fi

required_objects=(
    "requirements.txt"
    "requirements.lock"
    "scripts/check_hygiene.sh"
    "deploy/launchd/com.ivy.gateway.plist.template"
)
for required_object in "${required_objects[@]}"; do
    if ! git -C "$PROJECT_ROOT" cat-file -e "$target_sha:$required_object" 2>/dev/null; then
        echo "ERROR: rollback target lacks required production file: $required_object" >&2
        exit 1
    fi
done

echo "Ivy release rollback"
echo "  Current SHA: $original_sha"
echo "  Target SHA:  $target_sha"
echo "  Backup dir:  $BACKUP_DIR"
if [ "$APPLY" != true ]; then
    echo "  Mode:        DRY-RUN"
    echo "Would back up runtime state, detach the checkout at the target, run target hygiene,"
    echo "reconcile dependencies, safely render/install target plists, restart only the gateway,"
    echo "probe authenticated endpoints, and record the release transition."
    echo "No ref, file, process, or service was changed."
    exit 0
fi

if [ "$CONFIRM_LIVE" != true ]; then
    echo "ERROR: --apply requires --yes-i-know-this-is-live." >&2
    exit 2
fi
if [ "$(uname -s)" != "Darwin" ]; then
    echo "ERROR: live rollback is supported only on macOS." >&2
    exit 1
fi
if [ -L "$OPERATIONS_DIR" ]; then
    echo "ERROR: operations directory must not be a symlink: $OPERATIONS_DIR" >&2
    exit 1
fi

ensure_private_directory() {
    local directory="$1"
    if [ -e "$directory" ] || [ -L "$directory" ]; then
        if [ ! -d "$directory" ] || [ -L "$directory" ]; then
            echo "ERROR: expected a real directory: $directory" >&2
            return 1
        fi
        local mode=""
        if mode="$(stat -f '%Lp' "$directory" 2>/dev/null)"; then
            :
        else
            mode="$(stat -c '%a' "$directory")"
        fi
        if [ $((8#$mode & 8#077)) -ne 0 ]; then
            echo "ERROR: existing operations directory must already be private: $directory" >&2
            return 1
        fi
        return 0
    fi
    local parent=""
    parent="$(dirname "$directory")"
    if [ ! -d "$parent" ] || [ -L "$parent" ]; then
        echo "ERROR: directory parent is missing or unsafe: $parent" >&2
        return 1
    fi
    mkdir "$directory"
    chmod 700 "$directory"
}

ensure_private_directory "$OPERATIONS_DIR"
ensure_private_directory "$OPERATIONS_DIR/venvs"
ORIGINAL_PROD_VENV="$OPERATIONS_DIR/venvs/$original_sha"
if [ ! -x "$ORIGINAL_PROD_VENV/bin/python" ]; then
    ORIGINAL_PROD_VENV="$PROJECT_ROOT/.venv"
fi
if /bin/launchctl print "gui/$(id -u)/com.ivy.gateway" >/dev/null 2>&1; then
    GATEWAY_WAS_LOADED=true
else
    guarded_test_root=false
    if [ "${IVY_OPERATIONS_TEST_FAIL_AFTER_VENV_SWITCH:-}" = "1" ] \
        && [ -f "$PROJECT_ROOT/.ivy-operations-test-root" ]; then
        test_tmp_root="$(cd "${TMPDIR:-/tmp}" && pwd -P)"
        case "$PROJECT_ROOT" in
            "$test_tmp_root"/*) guarded_test_root=true ;;
        esac
    fi
    if [ "$guarded_test_root" != true ]; then
        echo "ERROR: rollback requires the gateway to be loaded so its prior running state can be recovered safely." >&2
        exit 1
    fi
fi

echo "== pre-rollback runtime backup (secrets excluded) =="
"$SCRIPT_DIR/backup_state.sh" --apply --destination "$BACKUP_DIR"

# Keep the current guarded operations tools available even when rolling back to
# a commit that predates them. The project-root override makes them operate on
# the target checkout and its templates, not on this temporary directory.
umask 077
TOOL_SNAPSHOT="$(mktemp -d "${TMPDIR:-/tmp}/ivy-rollback-tools.XXXXXX")"
chmod 700 "$TOOL_SNAPSHOT"
safe_installer_source="$PROJECT_ROOT/deploy/install_launchd.sh"
if [ -x "$SCRIPT_DIR/install_launchd.sh" ]; then
    safe_installer_source="$SCRIPT_DIR/install_launchd.sh"
fi
cp "$safe_installer_source" "$TOOL_SNAPSHOT/install_launchd.sh"
tool_names=(
    "backup_state.sh"
    "deploy_production.sh"
    "monitor_ivy.sh"
    "production_smoke_test.sh"
    "restart_ivy.sh"
    "restore_state.sh"
    "rollback_release.sh"
)
for tool_name in "${tool_names[@]}"; do
    cp "$SCRIPT_DIR/$tool_name" "$TOOL_SNAPSHOT/$tool_name"
done
chmod 700 "$TOOL_SNAPSHOT"/*.sh

cleanup_tools() {
    if [ -n "$TOOL_SNAPSHOT" ] && [ -d "$TOOL_SNAPSHOT" ]; then
        rm -rf -- "$TOOL_SNAPSHOT"
    fi
}
trap cleanup_tools EXIT

recover_original_release() {
    local status="$?"
    local recovery_failed=false
    trap - ERR
    set +e
    echo "ERROR: rollback failed; attempting to restore the original release $original_sha" >&2
    if /bin/launchctl print "gui/$(id -u)/com.ivy.gateway" >/dev/null 2>&1; then
        /bin/launchctl bootout "gui/$(id -u)/com.ivy.gateway" >/dev/null 2>&1 || recovery_failed=true
    fi
    git -C "$PROJECT_ROOT" switch --detach "$original_sha" >/dev/null 2>&1 || recovery_failed=true
    if [ "$VENV_TARGET_CREATED" = true ] && [ -n "$VENV_TARGET" ] && [ -d "$VENV_TARGET" ]; then
        rm -rf -- "$VENV_TARGET"
    fi
    IVY_PROJECT_ROOT_OVERRIDE="$PROJECT_ROOT" \
        IVY_VENV_PYTHON_OVERRIDE="$ORIGINAL_PROD_VENV/bin/python" \
        "$TOOL_SNAPSHOT/install_launchd.sh" --apply --yes-i-know-this-is-live >/dev/null 2>&1 \
        || recovery_failed=true
    if [ "$GATEWAY_WAS_LOADED" = true ]; then
        /bin/launchctl bootstrap \
            "gui/$(id -u)" \
            "$HOME/Library/LaunchAgents/com.ivy.gateway.plist" >/dev/null 2>&1 \
            || recovery_failed=true
    fi
    if [ "$recovery_failed" = true ]; then
        echo "CRITICAL: original release recovery was incomplete. Keep Ivy stopped and use the safety backup." >&2
    else
        echo "Original code, plists, and gateway were restored. The developer virtualenv was never changed." >&2
    fi
    exit "$status"
}

git -C "$PROJECT_ROOT" switch --detach "$target_sha"
trap recover_original_release ERR

prepare_target_venv() {
    local marker=""
    VENV_TARGET="$OPERATIONS_DIR/venvs/$target_sha"
    marker="$VENV_TARGET/.ivy-complete"

    if [ -e "$VENV_TARGET" ] || [ -L "$VENV_TARGET" ]; then
        if [ ! -d "$VENV_TARGET" ] || [ -L "$VENV_TARGET" ] || [ ! -x "$VENV_TARGET/bin/python" ]; then
            echo "ERROR: target immutable virtualenv is incomplete or unsafe: $VENV_TARGET" >&2
            return 1
        fi
        if [ ! -f "$marker" ] || [ "$(sed -n 's/^GIT_SHA=//p' "$marker" | tail -n 1)" != "$target_sha" ]; then
            echo "ERROR: target immutable virtualenv lacks a matching completion marker." >&2
            return 1
        fi
        "$VENV_TARGET/bin/python" -m pip check
    else
        VENV_TARGET_CREATED=true
        "$PROJECT_ROOT/.venv/bin/python" -m venv "$VENV_TARGET"
        "$VENV_TARGET/bin/python" -m pip install -r "$PROJECT_ROOT/requirements.lock"
        "$VENV_TARGET/bin/python" -m pip check
        printf 'GIT_SHA=%s\n' "$target_sha" > "$marker"
        chmod 600 "$marker"
    fi
    VENV_TARGET_CREATED=false
}

echo "== target release hygiene (hermetic; never sends) =="
"$PROJECT_ROOT/scripts/check_hygiene.sh"

if [ "$INSTALL_DEPENDENCIES" = true ]; then
    echo "== staged immutable target virtualenv =="
    prepare_target_venv
fi
if [ "${IVY_OPERATIONS_TEST_FAIL_AFTER_VENV_SWITCH:-}" = "1" ]; then
    test_tmp_root="$(cd "${TMPDIR:-/tmp}" && pwd -P)"
    case "$PROJECT_ROOT" in
        "$test_tmp_root"/*) ;;
        *) echo "ERROR: guarded failure injection is restricted to the private temporary root." >&2; false ;;
    esac
    if [ ! -f "$PROJECT_ROOT/.ivy-operations-test-root" ]; then
        echo "ERROR: guarded failure injection requires a disposable test-root marker." >&2
        false
    fi
    echo "Injecting guarded test failure after rollback virtualenv staging." >&2
    false
fi

echo "== target launchd validation and installation =="
IVY_PROJECT_ROOT_OVERRIDE="$PROJECT_ROOT" \
    IVY_VENV_PYTHON_OVERRIDE="$VENV_TARGET/bin/python" \
    "$TOOL_SNAPSHOT/install_launchd.sh" --validate-only
IVY_PROJECT_ROOT_OVERRIDE="$PROJECT_ROOT" \
    IVY_VENV_PYTHON_OVERRIDE="$VENV_TARGET/bin/python" \
    "$TOOL_SNAPSHOT/install_launchd.sh"
IVY_PROJECT_ROOT_OVERRIDE="$PROJECT_ROOT" \
    IVY_VENV_PYTHON_OVERRIDE="$VENV_TARGET/bin/python" \
    "$TOOL_SNAPSHOT/install_launchd.sh" --apply --yes-i-know-this-is-live

IVY_PROJECT_ROOT_OVERRIDE="$PROJECT_ROOT" \
    "$TOOL_SNAPSHOT/restart_ivy.sh" \
    --apply \
    --yes-i-know-this-is-live \
    --env-file "$ENV_FILE" \
    --base-url "$BASE_URL" \
    --reload-plist \
    --readiness-timeout-seconds "$READINESS_TIMEOUT_SECONDS"

timestamp="$(date -u '+%Y%m%dT%H%M%SZ')"
PERSISTENT_TOOL_SNAPSHOT="$OPERATIONS_DIR/tool_snapshots/$timestamp-$original_sha"
if [ -e "$PERSISTENT_TOOL_SNAPSHOT" ] || [ -L "$PERSISTENT_TOOL_SNAPSHOT" ]; then
    echo "ERROR: persistent operations snapshot already exists: $PERSISTENT_TOOL_SNAPSHOT" >&2
    exit 1
fi
ensure_private_directory "$OPERATIONS_DIR/history"
ensure_private_directory "$OPERATIONS_DIR/tool_snapshots"
mkdir "$PERSISTENT_TOOL_SNAPSHOT"
chmod 700 "$PERSISTENT_TOOL_SNAPSHOT"
for tool_path in "$TOOL_SNAPSHOT"/*.sh; do
    install -m 700 "$tool_path" "$PERSISTENT_TOOL_SNAPSHOT/$(basename "$tool_path")"
done
manifest_tmp="$OPERATIONS_DIR/.current.env.tmp.$$"
printf '%s\n' \
    "DEPLOYMENT_FORMAT_VERSION=1" \
    "DEPLOYED_UTC=$timestamp" \
    "DEPLOYED_SHA=$target_sha" \
    "PREVIOUS_SHA=$original_sha" \
    "DEPLOYED_VENV=$VENV_TARGET" \
    "BACKUP_DIR=$BACKUP_DIR" \
    > "$manifest_tmp"
chmod 600 "$manifest_tmp"
cp "$manifest_tmp" "$OPERATIONS_DIR/history/rollback-$timestamp-$target_sha.env"
chmod 600 "$OPERATIONS_DIR/history/rollback-$timestamp-$target_sha.env"
mv -f "$manifest_tmp" "$OPERATIONS_DIR/current.env"

trap - ERR
echo "Rollback completed and passed authenticated health, readiness, and version probes."
echo "Production checkout is detached at $target_sha; return to a named branch only during a later reviewed deployment."
echo "Guarded operations tools from the prior release were retained at $PERSISTENT_TOOL_SNAPSHOT"
