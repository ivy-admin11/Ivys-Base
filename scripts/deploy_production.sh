#!/usr/bin/env bash
# Guarded in-place deployment of an already checked-out, reviewed git commit.
# The script never switches refs; rollback is handled explicitly by
# rollback_release.sh. Dry-run is the default.

set -eEuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "${IVY_PROJECT_ROOT_OVERRIDE:-}" ]; then
    PROJECT_ROOT="$(cd "$IVY_PROJECT_ROOT_OVERRIDE" && pwd)"
else
    PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
INSTALLER="$PROJECT_ROOT/deploy/install_launchd.sh"
if [ -n "${IVY_PROJECT_ROOT_OVERRIDE:-}" ] && [ -x "$SCRIPT_DIR/install_launchd.sh" ]; then
    INSTALLER="$SCRIPT_DIR/install_launchd.sh"
fi
DEPLOY_REF="HEAD"
BACKUP_DIR="$HOME/ivy_backups/deployments"
OPERATIONS_DIR="$HOME/.ivy-operations"
ENV_FILE="$PROJECT_ROOT/.env"
BASE_URL="http://127.0.0.1:8000"
APPLY=false
CONFIRM_LIVE=false
INSTALL_DEPENDENCIES=true
READINESS_TIMEOUT_SECONDS="${IVY_READINESS_TIMEOUT_SECONDS:-90}"
DEPLOYED_VENV="$PROJECT_ROOT/.venv"
VENV_RELEASE_CREATED=false
PLIST_SNAPSHOT=""
PLISTS_APPLIED=false
GATEWAY_WAS_LOADED=false

usage() {
    cat <<USAGE
Usage: $0 [--ref GIT_REF] [--backup-dir DIR] [--operations-dir DIR]
          [--env-file PATH] [--base-url LOOPBACK_URL]
          [--readiness-timeout-seconds 90..600]
          [--skip-dependency-install] [--apply --yes-i-know-this-is-live]

The worktree must be clean and already checked out at GIT_REF. Dry-run still
validates the ref, runs repository hygiene, and validates rendered plists.
USAGE
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --ref)
            [ "$#" -ge 2 ] || { echo "ERROR: --ref requires a value." >&2; exit 2; }
            DEPLOY_REF="$2"
            shift 2
            ;;
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

case "$DEPLOY_REF" in
    -*) echo "ERROR: git refs beginning with '-' are not accepted." >&2; exit 2 ;;
esac

if ! git -C "$PROJECT_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    echo "ERROR: project root is not a git repository: $PROJECT_ROOT" >&2
    exit 1
fi
if [ -n "$(git -C "$PROJECT_ROOT" status --porcelain)" ]; then
    echo "ERROR: deployment refuses a dirty worktree. Commit, stash, or remove every change first." >&2
    git -C "$PROJECT_ROOT" status --short >&2
    exit 1
fi
if ! target_sha="$(git -C "$PROJECT_ROOT" rev-parse --verify "${DEPLOY_REF}^{commit}" 2>/dev/null)"; then
    echo "ERROR: deployment ref does not resolve to a commit: $DEPLOY_REF" >&2
    exit 1
fi
current_sha="$(git -C "$PROJECT_ROOT" rev-parse HEAD)"
if [ "$current_sha" != "$target_sha" ]; then
    echo "ERROR: worktree HEAD is $current_sha, but --ref resolves to $target_sha." >&2
    echo "Check out the reviewed ref deliberately, then rerun deployment." >&2
    exit 1
fi
if [ ! -f "$PROJECT_ROOT/requirements.lock" ] || [ -L "$PROJECT_ROOT/requirements.lock" ]; then
    echo "ERROR: reviewed production lock file is missing or unsafe: $PROJECT_ROOT/requirements.lock" >&2
    exit 1
fi

echo "Ivy production deployment"
echo "  Validated commit: $target_sha"
echo "  Project root:     $PROJECT_ROOT"
echo "  Backup dir:       $BACKUP_DIR"
echo "  Operations dir:   $OPERATIONS_DIR"

echo "== repository hygiene (hermetic; never sends) =="
"$PROJECT_ROOT/scripts/check_hygiene.sh"

echo "== rendered launchd validation =="
IVY_PROJECT_ROOT_OVERRIDE="$PROJECT_ROOT" "$INSTALLER" --validate-only

if [ "$APPLY" != true ]; then
    echo "  Mode:             DRY-RUN"
    echo "Would create a state backup, install production dependencies and reviewed plists,"
    echo "activate or restart only com.ivy.gateway, run authenticated probes, and record rollback metadata."
    if [ "$INSTALL_DEPENDENCIES" = false ]; then
        echo "Dependency installation would be skipped by explicit request."
    fi
    echo "No deployment action was taken."
    exit 0
fi

if [ "$CONFIRM_LIVE" != true ]; then
    echo "ERROR: --apply requires --yes-i-know-this-is-live." >&2
    exit 2
fi
if [ "$(uname -s)" != "Darwin" ]; then
    echo "ERROR: production deployment is supported only on macOS." >&2
    exit 1
fi
if [ ! -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    echo "ERROR: project virtualenv Python is missing: $PROJECT_ROOT/.venv/bin/python" >&2
    exit 1
fi
if [ -L "$OPERATIONS_DIR" ]; then
    echo "ERROR: operations directory must not be a symlink: $OPERATIONS_DIR" >&2
    exit 1
fi
case "$OPERATIONS_DIR" in
    *$'\n'*|*$'\r'*) echo "ERROR: operations directory contains an unsupported newline." >&2; exit 2 ;;
esac

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

cleanup_deployment_staging() {
    if [ -n "$PLIST_SNAPSHOT" ] && [ -d "$PLIST_SNAPSHOT" ]; then
        rm -rf -- "$PLIST_SNAPSHOT"
    fi
}
trap cleanup_deployment_staging EXIT

snapshot_managed_plists() {
    local launch_agents="$HOME/Library/LaunchAgents"
    local filename=""
    local source=""
    PLIST_SNAPSHOT="$(mktemp -d "$OPERATIONS_DIR/.plist-snapshot.XXXXXX")"
    chmod 700 "$PLIST_SNAPSHOT"
    for filename in \
        com.ivy.gateway.plist \
        com.ivy.sharppicks.plist \
        com.ivy.happy_hour_scout.plist \
        com.ivy.familia_meal_planner.plist
    do
        source="$launch_agents/$filename"
        if [ -L "$source" ]; then
            echo "ERROR: managed launchd plist must not be a symlink: $source" >&2
            return 1
        fi
        if [ -e "$source" ]; then
            if [ ! -f "$source" ]; then
                echo "ERROR: managed launchd plist has an unexpected type: $source" >&2
                return 1
            fi
            cp -p "$source" "$PLIST_SNAPSHOT/$filename"
            chmod 600 "$PLIST_SNAPSHOT/$filename"
        else
            : > "$PLIST_SNAPSHOT/$filename.absent"
        fi
    done
    if /bin/launchctl print "gui/$(id -u)/com.ivy.gateway" >/dev/null 2>&1; then
        GATEWAY_WAS_LOADED=true
    fi
}

restore_managed_plists() {
    local launch_agents="$HOME/Library/LaunchAgents"
    local filename=""
    local target=""
    local temporary=""
    local recovery_failed=false
    if [ "$PLISTS_APPLIED" != true ] || [ -z "$PLIST_SNAPSHOT" ] || [ ! -d "$PLIST_SNAPSHOT" ]; then
        return 0
    fi
    if /bin/launchctl print "gui/$(id -u)/com.ivy.gateway" >/dev/null 2>&1; then
        /bin/launchctl bootout "gui/$(id -u)/com.ivy.gateway" >/dev/null 2>&1 || recovery_failed=true
    fi
    for filename in \
        com.ivy.gateway.plist \
        com.ivy.sharppicks.plist \
        com.ivy.happy_hour_scout.plist \
        com.ivy.familia_meal_planner.plist
    do
        target="$launch_agents/$filename"
        if [ -f "$PLIST_SNAPSHOT/$filename" ]; then
            if [ -L "$target" ] || { [ -e "$target" ] && [ ! -f "$target" ]; }; then
                echo "ERROR: refusing to overwrite an unexpected plist target during recovery: $target" >&2
                recovery_failed=true
                continue
            fi
            if ! temporary="$(mktemp "$launch_agents/.ivy-plist-recovery.XXXXXX")" \
                || ! install -m 600 "$PLIST_SNAPSHOT/$filename" "$temporary" \
                || ! mv -f "$temporary" "$target"; then
                recovery_failed=true
            fi
        elif [ -f "$PLIST_SNAPSHOT/$filename.absent" ]; then
            if [ -f "$target" ] && [ ! -L "$target" ]; then
                rm -- "$target" || recovery_failed=true
            fi
        fi
    done
    if [ "$GATEWAY_WAS_LOADED" = true ] && [ -f "$launch_agents/com.ivy.gateway.plist" ]; then
        /bin/launchctl bootstrap "gui/$(id -u)" "$launch_agents/com.ivy.gateway.plist" >/dev/null 2>&1 || recovery_failed=true
    fi
    PLISTS_APPLIED=false
    [ "$recovery_failed" = false ]
}

deploy_failure_recovery() {
    local status="$?"
    local code_restored=false
    local recovery_failed=false
    trap - ERR
    set +e
    if [ -n "${previous_sha:-}" ] \
        && git -C "$PROJECT_ROOT" cat-file -e "$previous_sha^{commit}" 2>/dev/null \
        && [ -z "$(git -C "$PROJECT_ROOT" status --porcelain 2>/dev/null)" ] \
        && git -C "$PROJECT_ROOT" switch --detach "$previous_sha" >/dev/null 2>&1; then
        code_restored=true
    fi
    if [ "$code_restored" != true ]; then
        GATEWAY_WAS_LOADED=false
        recovery_failed=true
        echo "WARNING: no previous release commit could be restored automatically; the gateway will remain stopped." >&2
    fi
    restore_managed_plists || recovery_failed=true
    if [ "$VENV_RELEASE_CREATED" = true ] && [ -n "$DEPLOYED_VENV" ] && [ -d "$DEPLOYED_VENV" ]; then
        rm -rf -- "$DEPLOYED_VENV"
    fi
    if [ "$recovery_failed" = true ]; then
        echo "CRITICAL: deployment failed and automatic rollback was incomplete; keep Ivy stopped and use the pre-deployment archive." >&2
    else
        echo "ERROR: deployment failed; prior code and managed plists were restored. The developer virtualenv was never changed." >&2
        echo "The pre-deployment state archive remains available for incident recovery." >&2
    fi
    exit "$status"
}

prepare_release_venv() {
    local release_venv="$OPERATIONS_DIR/venvs/$target_sha"
    local marker="$release_venv/.ivy-complete"

    DEPLOYED_VENV="$release_venv"
    if [ -e "$release_venv" ] || [ -L "$release_venv" ]; then
        if [ ! -d "$release_venv" ] || [ -L "$release_venv" ] || [ ! -x "$release_venv/bin/python" ]; then
            echo "ERROR: immutable release virtualenv is incomplete or unsafe: $release_venv" >&2
            return 1
        fi
        if [ ! -f "$marker" ] || [ "$(sed -n 's/^GIT_SHA=//p' "$marker" | tail -n 1)" != "$target_sha" ]; then
            echo "ERROR: immutable release virtualenv lacks a matching completion marker." >&2
            return 1
        fi
        "$release_venv/bin/python" -m pip check
    else
        echo "== staged immutable production virtualenv =="
        VENV_RELEASE_CREATED=true
        "$PROJECT_ROOT/.venv/bin/python" -m venv "$release_venv"
        "$release_venv/bin/python" -m pip install -r "$PROJECT_ROOT/requirements.lock"
        "$release_venv/bin/python" -m pip check
        printf 'GIT_SHA=%s\n' "$target_sha" > "$marker"
        chmod 600 "$marker"
    fi
    VENV_RELEASE_CREATED=false
}

current_manifest="$OPERATIONS_DIR/current.env"
previous_sha=""
if [ -f "$current_manifest" ] && [ ! -L "$current_manifest" ]; then
    recorded_deployed_sha="$(sed -n 's/^DEPLOYED_SHA=//p' "$current_manifest" | tail -n 1)"
    recorded_previous_sha="$(sed -n 's/^PREVIOUS_SHA=//p' "$current_manifest" | tail -n 1)"
    case "$recorded_deployed_sha" in
        ''|*[!0-9a-fA-F]*) recorded_deployed_sha="" ;;
    esac
    case "$recorded_previous_sha" in
        ''|*[!0-9a-fA-F]*) recorded_previous_sha="" ;;
    esac
    if [ "$recorded_deployed_sha" = "$target_sha" ]; then
        previous_sha="$recorded_previous_sha"
    else
        previous_sha="$recorded_deployed_sha"
    fi
fi

echo "== pre-deployment runtime backup (secrets excluded) =="
"$SCRIPT_DIR/backup_state.sh" --apply --destination "$BACKUP_DIR"

trap deploy_failure_recovery ERR
if [ "$INSTALL_DEPENDENCIES" = true ]; then
    prepare_release_venv
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
    echo "Injecting guarded test failure after immutable virtualenv staging." >&2
    false
fi

snapshot_managed_plists
echo "== launchd runtime preflight and reviewed plist diff =="
IVY_PROJECT_ROOT_OVERRIDE="$PROJECT_ROOT" \
    IVY_VENV_PYTHON_OVERRIDE="$DEPLOYED_VENV/bin/python" \
    "$INSTALLER"

echo "== install reviewed launchd plists =="
PLISTS_APPLIED=true
IVY_PROJECT_ROOT_OVERRIDE="$PROJECT_ROOT" \
    IVY_VENV_PYTHON_OVERRIDE="$DEPLOYED_VENV/bin/python" \
    "$INSTALLER" --apply --yes-i-know-this-is-live
if [ "${IVY_OPERATIONS_TEST_FAIL_AFTER_PLIST_INSTALL:-}" = "1" ]; then
    test_tmp_root="$(cd "${TMPDIR:-/tmp}" && pwd -P)"
    case "$PROJECT_ROOT" in
        "$test_tmp_root"/*) ;;
        *) echo "ERROR: guarded failure injection is restricted to the private temporary root." >&2; false ;;
    esac
    if [ ! -f "$PROJECT_ROOT/.ivy-operations-test-root" ]; then
        echo "ERROR: guarded failure injection requires a disposable test-root marker." >&2
        false
    fi
    echo "Injecting guarded test failure after launchd plist installation." >&2
    false
fi

domain="gui/$(id -u)"
gateway_label="com.ivy.gateway"
gateway_plist="$HOME/Library/LaunchAgents/$gateway_label.plist"
if /bin/launchctl print "$domain/$gateway_label" >/dev/null 2>&1; then
    "$SCRIPT_DIR/restart_ivy.sh" \
        --apply \
        --yes-i-know-this-is-live \
        --env-file "$ENV_FILE" \
        --base-url "$BASE_URL" \
        --reload-plist \
        --readiness-timeout-seconds "$READINESS_TIMEOUT_SECONDS"
else
    echo "Bootstrapping the new gateway service (scheduled delivery agents remain untouched)..."
    /bin/launchctl bootstrap "$domain" "$gateway_plist"
    started_at="$(date +%s)"
    deadline=$((started_at + READINESS_TIMEOUT_SECONDS))
    attempt=1
    while :; do
        if "$SCRIPT_DIR/monitor_ivy.sh" --env-file "$ENV_FILE" --base-url "$BASE_URL"; then
            break
        fi
        now="$(date +%s)"
        if [ "$now" -ge "$deadline" ]; then
            echo "ERROR: bootstrapped gateway did not become ready." >&2
            if [ -n "$previous_sha" ]; then
                echo "Rollback: $SCRIPT_DIR/rollback_release.sh --ref $previous_sha --apply --yes-i-know-this-is-live" >&2
            fi
            exit 1
        fi
        remaining=$((deadline - now))
        sleep_seconds=2
        if [ "$remaining" -lt "$sleep_seconds" ]; then
            sleep_seconds="$remaining"
        fi
        echo "Gateway not ready yet (attempt $attempt; ${remaining}s remain); retrying in ${sleep_seconds}s..." >&2
        sleep "$sleep_seconds"
        attempt=$((attempt + 1))
    done
fi

timestamp="$(date -u '+%Y%m%dT%H%M%SZ')"
ensure_private_directory "$OPERATIONS_DIR/history"
manifest_tmp="$OPERATIONS_DIR/.current.env.tmp.$$"
printf '%s\n' \
    "DEPLOYMENT_FORMAT_VERSION=1" \
    "DEPLOYED_UTC=$timestamp" \
    "DEPLOYED_SHA=$target_sha" \
    "PREVIOUS_SHA=$previous_sha" \
    "DEPLOYED_VENV=$DEPLOYED_VENV" \
    "BACKUP_DIR=$BACKUP_DIR" \
    > "$manifest_tmp"
chmod 600 "$manifest_tmp"
cp "$manifest_tmp" "$OPERATIONS_DIR/history/$timestamp-$target_sha.env"
chmod 600 "$OPERATIONS_DIR/history/$timestamp-$target_sha.env"
mv -f "$manifest_tmp" "$current_manifest"

trap - ERR
PLISTS_APPLIED=false
echo "Deployment completed and passed authenticated health, readiness, and version probes."
if [ -n "$previous_sha" ]; then
    echo "Rollback target recorded: $previous_sha"
else
    echo "This is the first recorded deployment; no prior release SHA is available for --last."
fi
