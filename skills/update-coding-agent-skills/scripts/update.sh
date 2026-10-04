#!/usr/bin/env bash
# Fast-forward a coding-agent-skills clone to origin/main, then redeploy it with `deploy.py --all`.
#
# Usage: update.sh <clone>
# Prints one status line first, then details:
#   DIRTY            followed by the tracked files with uncommitted changes
#   FETCH_FAILED     followed by Git's error
#   CHECKOUT_FAILED  followed by Git's error from switching to main
#   NOT_FAST_FORWARD followed by Git's error; main has commits origin/main lacks
#   UP_TO_DATE <sha> or UPDATED <old>..<new> followed by the pulled commits,
#                    then the deploy output and DEPLOYED or DEPLOY_FAILED <code>
# Exit: 0 deployed; 1 deploy failed; 2 usage; 3 dirty; 4 fetch failed; 5 checkout failed or not a fast-forward.
set -uo pipefail

# Bash keeps its script open while running, and Windows refuses to rename a directory holding an open
# file, so the deploy below could not back up this skill's own directory. Run from a temporary copy.
if [ "${UPDATE_SKILLS_COPY:-}" != "$0" ]; then
  if ! COPY=$(mktemp) || ! cp "$0" "$COPY"; then
    echo "ERROR: cannot copy update.sh to a temporary file" >&2
    exit 2
  fi
  UPDATE_SKILLS_COPY=$COPY exec bash "$COPY" "$@"
fi
trap 'rm -f -- "$0"' EXIT
unset UPDATE_SKILLS_COPY

if [ "$#" -ne 1 ] || [ ! -f "$1/deploy.py" ]; then
  echo "usage: update.sh <clone containing deploy.py>" >&2
  exit 2
fi
CLONE=$1
if ! git -C "$CLONE" rev-parse --git-dir >/dev/null 2>&1; then
  echo "ERROR: not a git repository: $CLONE" >&2
  exit 2
fi

# Untracked files are allowed: Git refuses a fast-forward that would overwrite one.
if ! DIRTY=$(git -C "$CLONE" status --porcelain --untracked-files=no 2>&1); then
  echo "ERROR: git status failed: $DIRTY" >&2
  exit 2
fi
if [ -n "$DIRTY" ]; then
  echo "DIRTY"
  printf '%s\n' "$DIRTY" | tr -d '\r'
  exit 3
fi

if ! OUTPUT=$(git -C "$CLONE" fetch --quiet origin main 2>&1); then
  echo "FETCH_FAILED"
  printf '%s\n' "$OUTPUT" | tr -d '\r'
  exit 4
fi

if ! OUTPUT=$(git -C "$CLONE" checkout --quiet main 2>&1); then
  echo "CHECKOUT_FAILED"
  printf '%s\n' "$OUTPUT" | tr -d '\r'
  exit 5
fi
BEFORE=$(git -C "$CLONE" rev-parse --short HEAD | tr -d '\r')
if ! OUTPUT=$(git -C "$CLONE" merge --ff-only --quiet origin/main 2>&1); then
  echo "NOT_FAST_FORWARD"
  printf '%s\n' "$OUTPUT" | tr -d '\r'
  exit 5
fi
AFTER=$(git -C "$CLONE" rev-parse --short HEAD | tr -d '\r')

if [ "$BEFORE" = "$AFTER" ]; then
  echo "UP_TO_DATE $AFTER"
else
  echo "UPDATED $BEFORE..$AFTER"
  git -C "$CLONE" log --oneline --no-decorate "$BEFORE..$AFTER" | tr -d '\r'
fi

# A fresh process runs the deployer exactly as it now is on main.
python "$CLONE/deploy.py" --all </dev/null
CODE=$?
if [ "$CODE" -ne 0 ]; then
  echo "DEPLOY_FAILED $CODE"
  exit 1
fi
echo "DEPLOYED"
