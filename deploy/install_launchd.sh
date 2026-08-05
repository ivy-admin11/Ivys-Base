#!/bin/bash
# Render deploy/launchd/*.plist.template for this machine and, only with
# explicit flags, install them in the current user's LaunchAgents directory.
#
# Usage:
#   ./deploy/install_launchd.sh                                  # dry-run
#   ./deploy/install_launchd.sh --validate-only                  # render + plutil only
#   ./deploy/install_launchd.sh --apply                          # new labels only
#   ./deploy/install_launchd.sh --apply --yes-i-know-this-is-live
#
# The installer never calls launchctl. Installation and service activation are
# deliberately separate operations so a reviewed plist cannot start by accident.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "${IVY_PROJECT_ROOT_OVERRIDE:-}" ]; then
    PROJECT_ROOT="$(cd "$IVY_PROJECT_ROOT_OVERRIDE" && pwd)"
else
    PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
TEMPLATE_DIR="$PROJECT_ROOT/deploy/launchd"
TARGET_DIR="$HOME/Library/LaunchAgents"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
VENV_OVERRIDE_SET=false
if [ -n "${IVY_VENV_PYTHON_OVERRIDE:-}" ]; then
    VENV_OVERRIDE_SET=true
    case "$IVY_VENV_PYTHON_OVERRIDE" in
        /*) ;;
        *) echo "ERROR: IVY_VENV_PYTHON_OVERRIDE must be an absolute path." >&2; exit 2 ;;
    esac
    case "/$IVY_VENV_PYTHON_OVERRIDE/" in
        */../*|*/./*|*$'\n'*|*$'\r'*)
            echo "ERROR: IVY_VENV_PYTHON_OVERRIDE contains an unsafe path component." >&2
            exit 2
            ;;
    esac
    if [ ! -f "$IVY_VENV_PYTHON_OVERRIDE" ] || [ ! -x "$IVY_VENV_PYTHON_OVERRIDE" ]; then
        echo "ERROR: IVY_VENV_PYTHON_OVERRIDE is not an executable interpreter." >&2
        exit 1
    fi
    VENV_PYTHON="$IVY_VENV_PYTHON_OVERRIDE"
fi
BACKUP_DIR="$HOME/ivy_repair_backups/$(date +%Y%m%d_%H%M%S)_launchd_install"

APPLY=false
CONFIRM_LIVE=false
VALIDATE_ONLY=false

usage() {
    sed -n '2,12s/^# \{0,1\}//p' "$0"
}

for arg in "$@"; do
    case "$arg" in
        --apply) APPLY=true ;;
        --yes-i-know-this-is-live) CONFIRM_LIVE=true ;;
        --validate-only) VALIDATE_ONLY=true ;;
        -h|--help) usage; exit 0 ;;
        *) echo "ERROR: unknown argument: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

if [ "$VALIDATE_ONLY" = true ] && { [ "$APPLY" = true ] || [ "$CONFIRM_LIVE" = true ]; }; then
    echo "ERROR: --validate-only cannot be combined with apply flags." >&2
    exit 2
fi

if ! command -v plutil >/dev/null 2>&1; then
    echo "ERROR: plutil is required to validate rendered launchd files." >&2
    exit 1
fi

if [ "$VALIDATE_ONLY" = true ] && [ "$VENV_OVERRIDE_SET" != true ] && [ ! -x "$VENV_PYTHON" ]; then
    if command -v python3 >/dev/null 2>&1; then
        VENV_PYTHON="$(command -v python3)"
    else
        VENV_PYTHON="/usr/bin/python3"
    fi
fi

preflight_error=false

require_path() {
    local description="$1"
    local path="$2"
    if [ ! -e "$path" ]; then
        echo "ERROR: missing $description: $path" >&2
        preflight_error=true
    fi
}

require_executable() {
    local description="$1"
    local path="$2"
    if [ ! -x "$path" ]; then
        echo "ERROR: missing or non-executable $description: $path" >&2
        preflight_error=true
    fi
}

private_file_check() {
    local description="$1"
    local path="$2"
    local mode=""

    require_path "$description" "$path"
    [ -f "$path" ] || return 0

    if mode="$(stat -f '%Lp' "$path" 2>/dev/null)"; then
        :
    elif mode="$(stat -c '%a' "$path" 2>/dev/null)"; then
        :
    else
        echo "WARNING: unable to inspect permissions for $path" >&2
        return 0
    fi

    case "$mode" in
        *00) ;;
        *)
            echo "ERROR: $description must not be group/world accessible (mode $mode): $path" >&2
            preflight_error=true
            ;;
    esac
}

if [ "$VALIDATE_ONLY" != true ]; then
    require_executable "project virtualenv Python" "$VENV_PYTHON"
    require_path "gateway entry point" "$PROJECT_ROOT/main.py"
    if grep -qF 'ivy_core.job_worker' "$TEMPLATE_DIR"/*.plist.template; then
        require_path "receipt-aware scheduled job worker" "$PROJECT_ROOT/ivy_core/job_worker.py"
    fi
    if grep -qF 'proactive_agents.sports_bettor' "$TEMPLATE_DIR"/*.plist.template; then
        require_path "Sharp Picks module" "$PROJECT_ROOT/proactive_agents/sports_bettor.py"
    fi
    if grep -qF 'proactive_agents.happy_hour_scout' "$TEMPLATE_DIR"/*.plist.template; then
        require_path "Happy Hour module" "$PROJECT_ROOT/proactive_agents/happy_hour_scout.py"
    fi
    if grep -qF 'proactive_agents.Familia_meal_planner' "$TEMPLATE_DIR"/*.plist.template; then
        require_path "Familia module" "$PROJECT_ROOT/proactive_agents/Familia_meal_planner.py"
    fi
    if grep -qF 'scripts/run_daily_picks.sh' "$TEMPLATE_DIR"/*.plist.template; then
        require_path "legacy Sharp Picks launcher" "$PROJECT_ROOT/scripts/run_daily_picks.sh"
    fi
    if grep -qF 'scripts/run_happy_hour_scout.sh' "$TEMPLATE_DIR"/*.plist.template; then
        require_path "legacy Happy Hour launcher" "$PROJECT_ROOT/scripts/run_happy_hour_scout.sh"
    fi
    private_file_check "runtime environment file" "$PROJECT_ROOT/.env"
    private_file_check "contact allowlist" "$PROJECT_ROOT/favorites.json"

    if [ "$(uname -s)" != "Darwin" ]; then
        echo "ERROR: launchd installation is supported only on macOS." >&2
        preflight_error=true
    fi

    if [ "$preflight_error" = true ]; then
        echo "Preflight failed; no launchd files were changed." >&2
        exit 1
    fi
fi

LIVE_LABELS=(
    "com.lexi.ivy"
    "com.ivy.gateway"
    "com.ivy.sharppicks"
    "com.ivy.happy_hour_scout"
    "com.ivy.familia_meal_planner"
    "com.ivy.brain"
)

is_live_label() {
    local label="$1"
    local live
    for live in "${LIVE_LABELS[@]}"; do
        if [ "$label" = "$live" ]; then
            return 0
        fi
    done
    return 1
}

escape_sed_replacement() {
    printf '%s' "$1" | sed 's/[\\&|]/\\&/g'
}

project_root_sed="$(escape_sed_replacement "$PROJECT_ROOT")"
venv_python_sed="$(escape_sed_replacement "$VENV_PYTHON")"
home_sed="$(escape_sed_replacement "$HOME")"

render_dir="$(mktemp -d "${TMPDIR:-/tmp}/ivy-launchd.XXXXXX")"
trap 'rm -rf "$render_dir"' EXIT
chmod 700 "$render_dir"

shopt -s nullglob
templates=("$TEMPLATE_DIR"/*.plist.template)
if [ "${#templates[@]}" -eq 0 ]; then
    echo "ERROR: no launchd templates found in $TEMPLATE_DIR" >&2
    exit 1
fi

# Render and validate every file before displaying or applying any changes.
for template in "${templates[@]}"; do
    filename="$(basename "$template" .template)"
    label="${filename%.plist}"
    rendered_path="$render_dir/$filename"

    if [ "$label" = "com.ivy.brain" ]; then
        echo "=== $label (reference-only external service) ==="
        echo "  Not inspected or installed by this repository."
        echo
        continue
    fi
    sed \
        -e "s|__PROJECT_ROOT__|$project_root_sed|g" \
        -e "s|__VENV_PYTHON__|$venv_python_sed|g" \
        -e "s|__HOME__|$home_sed|g" \
        "$template" > "$rendered_path"
    chmod 600 "$rendered_path"
    if ! plutil -lint "$rendered_path" >/dev/null; then
        echo "ERROR: rendered plist failed validation: $filename" >&2
        exit 1
    fi
done

if [ "$VALIDATE_ONLY" = true ]; then
    echo "Validated ${#templates[@]} rendered launchd plist(s) with plutil."
    exit 0
fi

echo "Ivy launchd installer"
echo "  Project root: $PROJECT_ROOT"
echo "  Python:       $VENV_PYTHON"
echo "  Target dir:   $TARGET_DIR"
if [ "$APPLY" = true ]; then
    if [ "$CONFIRM_LIVE" = true ]; then
        echo "  Mode:         APPLY (including live labels)"
    else
        echo "  Mode:         APPLY (new labels only)"
    fi
else
    echo "  Mode:         DRY-RUN (pass --apply to write files)"
fi
echo "  Preflight:    passed; all rendered plists passed plutil"
echo

if [ "$APPLY" = true ]; then
    mkdir -p "$TARGET_DIR" "$PROJECT_ROOT/logs" "$PROJECT_ROOT/data"
    chmod 700 "$TARGET_DIR" "$PROJECT_ROOT/logs" "$PROJECT_ROOT/data"
else
    echo "Would ensure private runtime directories exist:"
    echo "  $TARGET_DIR"
    echo "  $PROJECT_ROOT/logs"
    echo "  $PROJECT_ROOT/data"
    echo
fi

for template in "${templates[@]}"; do
    filename="$(basename "$template" .template)"
    label="${filename%.plist}"
    target_path="$TARGET_DIR/$filename"
    rendered_path="$render_dir/$filename"

    already_installed=false
    [ -f "$target_path" ] && already_installed=true

    if [ "$already_installed" = true ]; then
        status="currently installed"
    else
        status="not installed"
    fi
    echo "=== $label ($status) ==="

    if [ "$already_installed" = true ]; then
        if diff -q "$rendered_path" "$target_path" >/dev/null 2>&1; then
            echo "  No changes."
            echo
            continue
        fi
        # Installed plists may contain legacy embedded credentials. Never emit
        # their contents to a terminal, CI log, or remote support transcript.
        echo "  Installed content differs from the reviewed template (diff redacted)."
    else
        echo "  Would create $target_path"
    fi
    echo

    if [ "$APPLY" != true ]; then
        continue
    fi

    if is_live_label "$label" && [ "$CONFIRM_LIVE" != true ]; then
        echo "  SKIPPED: $label is live; add --yes-i-know-this-is-live to replace it."
        echo
        continue
    fi

    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR"
    if [ -f "$target_path" ]; then
        cp -p "$target_path" "$BACKUP_DIR/"
        chmod 600 "$BACKUP_DIR/$filename"
        echo "  Backed up existing plist to $BACKUP_DIR/"
    fi

    install_tmp="$TARGET_DIR/.${filename}.tmp.$$"
    install -m 600 "$rendered_path" "$install_tmp"
    mv -f "$install_tmp" "$target_path"
    plutil -lint "$target_path" >/dev/null
    echo "  Wrote and validated $target_path"
    echo "  NOTE: no launchctl command was run."
    echo
done

echo "Labels currently installed but superseded by this repair (not removed):"
found_obsolete=false
for obsolete in com.ivy.weeklyplanner com.ivy.bravoscout; do
    if [ -f "$TARGET_DIR/$obsolete.plist" ]; then
        found_obsolete=true
        echo "  - $obsolete.plist"
    fi
done
if [ "$found_obsolete" = false ]; then
    echo "  (none found)"
fi
echo "Removal remains a separate, explicit operation."
