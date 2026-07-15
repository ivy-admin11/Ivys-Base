#!/bin/bash
# deploy/install_launchd.sh — Render deploy/launchd/*.plist.template into
# ~/Library/LaunchAgents/, substituting this machine's actual paths.
#
# SAFE BY DEFAULT: with no flags, this only prints what WOULD change. It
# never writes a file and never calls launchctl. Even with --apply, it
# refuses to touch any of Ivy's currently-installed LIVE labels (com.lexi.ivy,
# com.ivy.gateway, com.ivy.sharppicks, com.ivy.happy_hour_scout, com.ivy.brain)
# unless --yes-i-know-this-is-live is also passed. This script never itself
# runs `launchctl bootstrap/load/kickstart` — writing the file and loading it
# into launchd are kept as two separate, deliberate steps.
#
# Usage:
#   ./deploy/install_launchd.sh                                    # dry-run, prints diff
#   ./deploy/install_launchd.sh --apply                             # writes only NOT-currently-installed labels
#   ./deploy/install_launchd.sh --apply --yes-i-know-this-is-live   # also overwrites currently-live labels' plists

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE_DIR="$SCRIPT_DIR/launchd"
TARGET_DIR="$HOME/Library/LaunchAgents"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
BACKUP_DIR="$HOME/ivy_repair_backups/$(date +%Y%m%d_%H%M%S)_launchd_install"

APPLY=false
CONFIRM_LIVE=false
for arg in "$@"; do
    case "$arg" in
        --apply) APPLY=true ;;
        --yes-i-know-this-is-live) CONFIRM_LIVE=true ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            exit 1
            ;;
    esac
done

if [ ! -x "$VENV_PYTHON" ]; then
    echo "ERROR: project venv python not found at $VENV_PYTHON" >&2
    exit 1
fi

LIVE_LABELS=("com.lexi.ivy" "com.ivy.gateway" "com.ivy.sharppicks" "com.ivy.happy_hour_scout" "com.ivy.brain")

is_live_label() {
    local label="$1"
    for live in "${LIVE_LABELS[@]}"; do
        if [ "$label" = "$live" ]; then
            return 0
        fi
    done
    return 1
}

echo "Ivy launchd installer"
echo "  Project root: $PROJECT_ROOT"
echo "  venv python:  $VENV_PYTHON"
echo "  Target dir:   $TARGET_DIR"
if [ "$APPLY" = true ]; then
    echo "  Mode:         APPLY $([ "$CONFIRM_LIVE" = true ] && echo "(including live labels)" || echo "(new labels only)")"
else
    echo "  Mode:         DRY-RUN (pass --apply to write files)"
fi
echo

shopt -s nullglob
for template in "$TEMPLATE_DIR"/*.plist.template; do
    filename="$(basename "$template" .template)"
    label="${filename%.plist}"
    target_path="$TARGET_DIR/$filename"

    rendered="$(sed \
        -e "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" \
        -e "s|__VENV_PYTHON__|$VENV_PYTHON|g" \
        -e "s|__HOME__|$HOME|g" \
        "$template")"

    already_installed=false
    [ -f "$target_path" ] && already_installed=true

    echo "=== $label ($([ "$already_installed" = true ] && echo "currently installed" || echo "not installed")) ==="

    if [ "$already_installed" = true ]; then
        if diff -q <(printf '%s' "$rendered") "$target_path" > /dev/null 2>&1; then
            echo "  No changes."
            echo
            continue
        fi
        echo "  Diff (installed -> rendered):"
        diff -u "$target_path" <(printf '%s' "$rendered") | sed 's/^/    /' || true
    else
        echo "  Would create new file at $target_path"
    fi
    echo

    if [ "$APPLY" != true ]; then
        continue
    fi

    if is_live_label "$label" && [ "$CONFIRM_LIVE" != true ]; then
        echo "  SKIPPED: $label is a currently-live label. Re-run with --yes-i-know-this-is-live to write it."
        echo
        continue
    fi

    mkdir -p "$BACKUP_DIR"
    if [ -f "$target_path" ]; then
        cp "$target_path" "$BACKUP_DIR/"
        echo "  Backed up existing plist to $BACKUP_DIR/"
    fi

    printf '%s' "$rendered" > "$target_path"
    echo "  Wrote $target_path"
    echo "  NOTE: this script does not call launchctl for you. Review the file, then"
    echo "        run 'launchctl bootstrap gui/\$(id -u) $target_path' (new label) or"
    echo "        'launchctl kickstart -k gui/\$(id -u)/$label' (already loaded) yourself."
    echo
done

echo "Labels currently installed but superseded by this repair (not removed by this script):"
found_obsolete=false
for obsolete in com.ivy.weeklyplanner com.ivy.bravoscout; do
    if [ -f "$TARGET_DIR/$obsolete.plist" ]; then
        found_obsolete=true
        echo "  - $obsolete.plist — points at a script that has never existed in this repo; no replacement template is installed for it."
    fi
done
if [ "$found_obsolete" = false ]; then
    echo "  (none found)"
fi
echo "Removing them is a separate, explicit decision — this script only reports their presence."
