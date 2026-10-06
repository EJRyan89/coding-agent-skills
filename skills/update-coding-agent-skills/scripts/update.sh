#!/usr/bin/env bash
# Fast-forward a coding-agent-skills clone to origin/main, then redeploy it with `deploy.py --all`.
#
# Usage: update.sh <clone> [--cross-major]
# Prints one status line first, then details:
#   DIRTY            followed by the tracked files with uncommitted changes
#   FETCH_FAILED     followed by Git's error
#   MAJOR_UPDATE <current>..<target> followed by the commits it would pull; origin/main's nearest
#                    release tag raises the breaking component (the major, or the minor while the
#                    major is 0) above local main's, and --cross-major was not given
#   CHECKOUT_FAILED  followed by Git's error from switching to main
#   NOT_FAST_FORWARD followed by Git's error; main has commits origin/main lacks
#   UP_TO_DATE <sha> or UPDATED <old>..<new> followed by the pulled commits, and by
#                    CROSSED <current>..<target> when --cross-major accepted a release boundary,
#                    then the deploy output and DEPLOYED or DEPLOY_FAILED <code>
#   FAILED <reason>  alone: the clone is not a repository with deploy.py, or a step failed outright
# Exit: 0 deployed; 1 any other status line, or FAILED; 2 usage (the wrong arguments, on stderr).
set -uo pipefail

# Usage is checked before any work, so a usage error never becomes a FAILED line.
CROSS_MAJOR=
if [ "$#" -eq 2 ] && [ "$2" = "--cross-major" ]; then
  CROSS_MAJOR=1
elif [ "$#" -ne 1 ]; then
  echo "usage: update.sh <clone containing deploy.py> [--cross-major]" >&2
  exit 2
fi

# A command's error output as one line, for a FAILED reason.
one_line() {
  printf '%s' "$1" | tr -d '\r' | tr '\n' ' '
}

# Bash keeps its script open while running, and Windows refuses to rename a directory holding an open
# file, so the deploy below could not back up this skill's own directory. Run from a temporary copy.
if [ "${UPDATE_SKILLS_COPY:-}" != "$0" ]; then
  if ! COPY=$(mktemp 2>&1); then
    echo "FAILED cannot create a temporary copy of update.sh: $(one_line "$COPY")"
    exit 1
  fi
  if ! ERROR=$(cp "$0" "$COPY" 2>&1); then
    rm -f -- "$COPY"
    echo "FAILED cannot copy update.sh to $COPY: $(one_line "$ERROR")"
    exit 1
  fi
  UPDATE_SKILLS_COPY=$COPY exec bash "$COPY" "$@"
fi
trap 'rm -f -- "$0"' EXIT
unset UPDATE_SKILLS_COPY

CLONE=$1
if [ ! -f "$CLONE/deploy.py" ]; then
  echo "FAILED no deploy.py in $CLONE"
  exit 1
fi
if ! git -C "$CLONE" rev-parse --git-dir >/dev/null 2>&1; then
  echo "FAILED not a git repository: $CLONE"
  exit 1
fi

# Untracked files are allowed: Git refuses a fast-forward that would overwrite one.
if ! DIRTY=$(git -C "$CLONE" status --porcelain --untracked-files=no 2>&1); then
  echo "FAILED git status failed: $(one_line "$DIRTY")"
  exit 1
fi
if [ -n "$DIRTY" ]; then
  echo "DIRTY"
  printf '%s\n' "$DIRTY" | tr -d '\r'
  exit 1
fi

# Release tags are fetched too, so both sides' versions can be compared below.
if ! OUTPUT=$(git -C "$CLONE" fetch --quiet --tags origin main 2>&1); then
  echo "FETCH_FAILED"
  printf '%s\n' "$OUTPUT" | tr -d '\r'
  exit 1
fi

# The nearest release tag reachable from a commit, or nothing when there is none.
release_of() {
  git -C "$CLONE" describe --tags --abbrev=0 --match 'v[0-9]*.[0-9]*.[0-9]*' "$1" 2>/dev/null | tr -d '\r'
}
# The component a release may not raise without acceptance: the major, or the minor while the major is 0.
breaking_component() {
  local version=${1#v} major minor
  major=${version%%.*}
  minor=${version#*.}
  minor=${minor%%.*}
  if [ "$major" -gt 0 ]; then
    echo "$major.0"
  else
    echo "0.$minor"
  fi
}
CURRENT=$(release_of main)
TARGET=$(release_of origin/main)
CROSSED=
if [ -n "$CURRENT" ] && [ -n "$TARGET" ] && [ "$CURRENT" != "$TARGET" ]; then
  CURRENT_BREAKING=$(breaking_component "$CURRENT")
  TARGET_BREAKING=$(breaking_component "$TARGET")
  if [ "${CURRENT_BREAKING%%.*}" -lt "${TARGET_BREAKING%%.*}" ] ||
    { [ "${CURRENT_BREAKING%%.*}" -eq "${TARGET_BREAKING%%.*}" ] && [ "${CURRENT_BREAKING#*.}" -lt "${TARGET_BREAKING#*.}" ]; }; then
    if [ -z "$CROSS_MAJOR" ]; then
      echo "MAJOR_UPDATE $CURRENT..$TARGET"
      git -C "$CLONE" log --oneline --no-decorate main..origin/main | tr -d '\r'
      exit 1
    fi
    CROSSED="$CURRENT..$TARGET"
  fi
fi

if ! OUTPUT=$(git -C "$CLONE" checkout --quiet main 2>&1); then
  echo "CHECKOUT_FAILED"
  printf '%s\n' "$OUTPUT" | tr -d '\r'
  exit 1
fi
BEFORE=$(git -C "$CLONE" rev-parse --short HEAD | tr -d '\r')
if ! OUTPUT=$(git -C "$CLONE" merge --ff-only --quiet origin/main 2>&1); then
  echo "NOT_FAST_FORWARD"
  printf '%s\n' "$OUTPUT" | tr -d '\r'
  exit 1
fi
AFTER=$(git -C "$CLONE" rev-parse --short HEAD | tr -d '\r')

if [ "$BEFORE" = "$AFTER" ]; then
  echo "UP_TO_DATE $AFTER"
else
  echo "UPDATED $BEFORE..$AFTER"
  git -C "$CLONE" log --oneline --no-decorate "$BEFORE..$AFTER" | tr -d '\r'
fi
if [ -n "$CROSSED" ]; then
  echo "CROSSED $CROSSED"
fi

# A fresh process runs the deployer exactly as it now is on main.
python "$CLONE/deploy.py" --all </dev/null
CODE=$?
if [ "$CODE" -ne 0 ]; then
  echo "DEPLOY_FAILED $CODE"
  exit 1
fi
echo "DEPLOYED"
