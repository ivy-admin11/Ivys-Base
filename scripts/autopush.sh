#!/bin/bash
# Push commits this machine already has, without waiting for someone to be
# sitting at it.
#
# Why this rather than a deploy key: for the cloud sandbox to push, the
# sandbox would have to hold a write credential to the repo, stored in a
# folder that syncs off the machine and is readable by every future session.
# That is the "credential files live inside the project folder" finding from
# the September health check, deliberately recreated. This runs on the Mac
# instead, as the user, with the SSH key that is already there — so no
# credential ever leaves it.
#
# It never commits, never touches the working tree, and never force-pushes.
# It pushes the current branch to its existing upstream, or does nothing.
#
# Install: deploy/launchd/com.ivy.autopush.plist.template (every 15 minutes)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT" || exit 1

export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
STAMP="$(date '+%Y-%m-%d %H:%M:%S')"
say() { echo "[$STAMP] $*"; }

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
if [ -z "$BRANCH" ] || [ "$BRANCH" = "HEAD" ]; then
    say "no branch checked out (detached HEAD) — nothing to push"
    exit 0
fi

# An upstream must already exist. Creating a remote branch unattended is a
# bigger decision than pushing to one that is already tracked.
if ! UPSTREAM="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)"; then
    say "$BRANCH has no upstream — not creating one unattended"
    exit 0
fi

# Push to the ref the branch actually tracks, not to a remote branch that
# merely shares its name. `git push origin "$BRANCH"` silently created a
# second remote branch whenever local and upstream names differed, leaving
# the tracked ref behind and the commits still counted as unpushed.
REMOTE="$(git config --get "branch.$BRANCH.remote" 2>/dev/null || true)"
[ -n "$REMOTE" ] || REMOTE="origin"
REMOTE_REF="$(git config --get "branch.$BRANCH.merge" 2>/dev/null || true)"
REMOTE_BRANCH="${REMOTE_REF#refs/heads/}"
[ -n "$REMOTE_BRANCH" ] || REMOTE_BRANCH="$BRANCH"

AHEAD="$(git rev-list --count "$UPSTREAM".."$BRANCH" 2>/dev/null || echo 0)"
if [ "$AHEAD" -eq 0 ]; then
    exit 0                                  # nothing to do, and nothing to say
fi

# ---------------------------------------------------------------------------
# Nobody reviews an unattended push, so the scan that would have happened by
# eye happens here instead. A refusal is the safe outcome: the commits stay on
# this machine, which is where they already are.
# ---------------------------------------------------------------------------
FILES="$(git diff --name-only "$UPSTREAM".."$BRANCH")"
if echo "$FILES" | grep -qiE '(^|/)\.env$|\.pem$|service-account.*\.json$|backup_codes|\.p12$|id_rsa|id_ed25519'; then
    say "REFUSING to push: the outgoing diff touches a credential-shaped file"
    echo "$FILES" | grep -iE '(^|/)\.env$|\.pem$|service-account.*\.json$|backup_codes|\.p12$|id_rsa|id_ed25519' | sed 's/^/    /'
    exit 2
fi

# Values, not just filenames. Matches an assignment of something long enough
# to be a real secret; short placeholders like "your_api_key" do not trip it.
if git diff "$UPSTREAM".."$BRANCH" -- . ':(exclude)*.md' \
     | grep '^+' | grep -v '^+++' \
     | grep -qiE '(api[_-]?key|secret|token|password|passwd)[[:space:]]*[=:][[:space:]]*["'"'"']?[A-Za-z0-9_/+-]{20,}'; then
    say "REFUSING to push: an added line looks like a real credential value"
    exit 2
fi

say "pushing $AHEAD commit(s): $BRANCH -> $REMOTE/$REMOTE_BRANCH"
if git push "$REMOTE" "$BRANCH:$REMOTE_BRANCH" 2>&1 | sed 's/^/    /'; then
    say "pushed $AHEAD commit(s)"
    exit 0
fi

# Loud, not silent. The usual cause is a passphrase-protected key with no
# agent under launchd — which looks identical to a network failure unless the
# error is kept.
say "PUSH FAILED — $AHEAD commit(s) still local"
say "if this says 'Permission denied (publickey)', the SSH key needs an agent"
say "under launchd; a passphrase-free deploy key for this repo is the fix."
exit 1
