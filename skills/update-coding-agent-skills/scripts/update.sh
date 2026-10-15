#!/usr/bin/env bash
# Fast-forward a coding-agent-skills clone to origin/main, then redeploy it with `deploy.py --all`.
#
# Usage: update.sh <clone> [--cross-major] [--unpin]
# Prints one status line first, then details:
#   DIRTY            followed by the tracked files with uncommitted changes
#   PINNED <ref> <commit> alone: `deploy.py check` names the deployment as made with `deploy.py --at <ref>`, which
#                    deployed <commit>, and --unpin was not given
#   FETCH_FAILED     followed by Git's error
#   TAG_SHADOWS_BRANCH followed by each tag named main or origin/main, as "<tag> on origin" or "<tag> in the clone";
#                    nothing was fetched
#   MAJOR_UPDATE <current>..<target> followed by the commits it would deploy; origin/main's highest
#                    release tag raises the breaking component (the major, or the minor while the
#                    major is 0) above the release of the commit `deploy.py check` names as deployed,
#                    and --cross-major was not given; <current> is untagged, counted as 0.0.0, when no
#                    release tag reaches that commit, and unknown, which any release raises, when check
#                    names no deployed commit
#   CHECKOUT_FAILED  followed by Git's error from switching to main
#   NOT_FAST_FORWARD followed by the commits main has that origin/main lacks, found before switching branches;
#                    or by Git's error when Git refuses the fast-forward itself, after which the clone is
#                    switched back to the branch it was found on, or a "still on main:" line says why not
#   UP_TO_DATE <sha> or UPDATED <old>..<new> followed by the pulled commits, and by
#                    CROSSED <current>..<target> when --cross-major accepted a release boundary,
#                    then the deploy output and DEPLOYED or DEPLOY_FAILED <code>
#   FAILED <reason>  alone: the clone is not a repository with deploy.py, a step failed outright, or a release tag
#                    the stop compares has a version component too long to compare
# A stop leaves the clone on the branch it was found on unless a "still on main:" line says otherwise; an update
# switches it to main and leaves it there.
# Exit: 0 deployed; 1 any other status line, or FAILED; 2 usage (the wrong arguments, on stderr).
set -uo pipefail

# The command that runs Python, as the deployer renders it into the skills: python under Git for Windows, where python3
# is often a Microsoft Store placeholder, and python3 elsewhere, where a distribution may install no python command.
case "${OSTYPE:-}" in
  msys* | cygwin*) PYTHON=python ;;
  *) PYTHON=python3 ;;
esac

# Usage is checked before any work, so a usage error never becomes a FAILED line.
usage() {
  echo "usage: update.sh <clone containing deploy.py> [--cross-major] [--unpin]" >&2
  exit 2
}
[ "$#" -ge 1 ] || usage
CROSS_MAJOR=
UNPIN=
for OPTION in "${@:2}"; do
  case "$OPTION" in
    --cross-major) [ -z "$CROSS_MAJOR" ] || usage; CROSS_MAJOR=1 ;;
    --unpin) [ -z "$UNPIN" ] || usage; UNPIN=1 ;;
    *) usage ;;
  esac
done

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

# git's repository-selecting variables, which a git hook exports and a shell may pass on, would point every
# `git -C "$CLONE"` below at another repository, index, or object store: those git clears itself before it works in
# another repository, but for the command-scope settings in its list, and GIT_NAMESPACE. Each is unset in any case, as
# Windows reads a name, so git there takes git_dir as GIT_DIR. skill-core's bounded_process.py leaves the same out of
# every command a skill's Python starts. awk's ENVIRON names each exported variable in the case it was given.
for NAME in $(git rev-parse --local-env-vars 2>/dev/null | awk '
  { sub(/\r$/, ""); wanted[$0] = 1 }
  END {
    wanted["GIT_NAMESPACE"] = 1
    delete wanted["GIT_CONFIG_PARAMETERS"]
    delete wanted["GIT_CONFIG_COUNT"]
    for (NAME in ENVIRON) if (toupper(NAME) in wanted) print NAME
  }'); do
  unset "$NAME"
done

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

# check names the deployed commit as "<name> (<commit>)", the commit alone, or unknown, followed by the pin when the
# deployment was made with `deploy.py --at <ref>`. Its exit status is not read: a check that reports a missing tool
# still names the commit. DEPLOYED_COMMIT is the commit's full name, or empty when check names none, so it is never
# taken for an option.
PIN_MARK=", pinned with --at "
DEPLOYED_LINE=$("$PYTHON" "$CLONE/deploy.py" check </dev/null 2>/dev/null | tr -d '\r' | grep -m 1 -e "^Deployed commit: ")
DEPLOYED=${DEPLOYED_LINE#Deployed commit: }
PIN_REF=
case "$DEPLOYED" in
  *"$PIN_MARK"*) PIN_REF=${DEPLOYED##*"$PIN_MARK"} && DEPLOYED=${DEPLOYED%"$PIN_MARK"*} ;;
esac
case "$DEPLOYED" in
  *")") DEPLOYED=${DEPLOYED##*"("} && DEPLOYED=${DEPLOYED%")"} ;;
esac
DEPLOYED_COMMIT=
if [[ "$DEPLOYED" =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ ]]; then
  DEPLOYED_COMMIT=$DEPLOYED
fi

# A pinned deployment would be replaced by main, so the update stops until --unpin. A check that fails or names no
# pin does not stop the update here; the deployer itself refuses to replace a pin without --unpin.
if [ -n "$PIN_REF" ] && [ -z "$UNPIN" ]; then
  echo "PINNED $PIN_REF $DEPLOYED"
  exit 1
fi

# Every ref below is named in full: Git resolves a short name as refs/tags/<name> before refs/heads/<name> or
# refs/remotes/<name>, so a tag named main or origin/main would stand in for the branch. The checkout and the switch
# back name a branch by its short name, which checkout resolves as a branch first.
MAIN=refs/heads/main
ORIGIN_MAIN=refs/remotes/origin/main

# A tag named like a branch the update reads, on origin or in the clone, stops it before anything is fetched: such a
# tag still shadows the branch in the user's own Git commands.
if ! OUTPUT=$(git -C "$CLONE" ls-remote --tags --refs origin "refs/tags/main" "refs/tags/origin/main" 2>&1); then
  echo "FETCH_FAILED"
  printf '%s\n' "$OUTPUT" | tr -d '\r'
  exit 1
fi
SHADOWS=$(printf '%s\n' "$OUTPUT" | tr -d '\r' |
  awk -F '\t' '$2 == "refs/tags/main" || $2 == "refs/tags/origin/main" { print $2 " on origin" }')
for TAG in refs/tags/main refs/tags/origin/main; do
  if git -C "$CLONE" show-ref --verify --quiet "$TAG"; then
    SHADOWS+="${SHADOWS:+$'\n'}$TAG in the clone"
  fi
done
if [ -n "$SHADOWS" ]; then
  echo "TAG_SHADOWS_BRANCH"
  printf '%s\n' "$SHADOWS"
  exit 1
fi

# Release tags are fetched too, so both sides' versions can be compared below. The negative refspecs keep the tag
# fetch from creating a shadowing tag that origin published after the check above.
if ! OUTPUT=$(git -C "$CLONE" fetch --quiet --tags origin "+$MAIN:$ORIGIN_MAIN" \
  "^refs/tags/main" "^refs/tags/origin/main" 2>&1); then
  echo "FETCH_FAILED"
  printf '%s\n' "$OUTPUT" | tr -d '\r'
  exit 1
fi

# The highest release tag reachable from a commit, or nothing when there is none; when Git refuses the lookup, a FAILED
# line naming the second argument, and status 1. A release tag is vMAJOR.MINOR.PATCH with an optional pre-release
# suffix, which sorts before its release; any other tag is ignored. Only refs/tags/ is read.
release_of() {
  local tags
  if ! tags=$(git -C "$CLONE" -c versionsort.suffix=- for-each-ref --merged "$1" --sort=-version:refname \
    --format='%(refname:strip=2)' 'refs/tags/v*' 2>&1); then
    echo "FAILED cannot read the release tags reaching $2: $(one_line "$tags")"
    return 1
  fi
  printf '%s\n' "$tags" | tr -d '\r' | grep -E -m 1 '^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$' || true
}
# A version component is compared as a number without its leading zeros, of at most MAX_DIGITS digits, so Bash's
# 64-bit integers hold it exactly.
MAX_DIGITS=18
# The component a release may not raise without acceptance, as "<major> <minor>": the major and 0, or 0 and the minor
# while the major is 0. A component longer than MAX_DIGITS is a FAILED line naming the tag, and status 1.
breaking_component() {
  local version=${1#v} part major minor
  for part in major minor; do
    local digits=${version%%.*}
    version=${version#*.}
    digits=${digits#"${digits%%[!0]*}"}
    if [ "${#digits}" -gt "$MAX_DIGITS" ]; then
      echo "FAILED cannot compare release tag $1: its $part version has more than $MAX_DIGITS digits"
      return 1
    fi
    printf -v "$part" '%s' "${digits:-0}"
  done
  if [ "$major" -gt 0 ]; then
    echo "$major 0"
  else
    echo "0 $minor"
  fi
}
# The installed version is the release of the deployed commit, not of local main, which a pull by hand may have moved
# past it. Without a release on origin/main there is nothing to cross. A deployed commit no release tag reaches counts
# as version zero, and one check does not name stops at any release.
CROSSED=
TARGET=$(release_of "$ORIGIN_MAIN" origin/main) || { echo "$TARGET"; exit 1; }
if [ -n "$TARGET" ]; then
  CURRENT=unknown
  if [ -n "$DEPLOYED_COMMIT" ]; then
    CURRENT=$(release_of "$DEPLOYED_COMMIT" "the deployed commit $DEPLOYED_COMMIT") || { echo "$CURRENT"; exit 1; }
  fi
fi
if [ -n "$TARGET" ] && [ "$CURRENT" != "$TARGET" ]; then
  TARGET_BREAKING=$(breaking_component "$TARGET") || { echo "$TARGET_BREAKING"; exit 1; }
  RAISED=1
  if [ "$CURRENT" != unknown ]; then
    CURRENT_BREAKING=$(breaking_component "${CURRENT:-v0.0.0}") || { echo "$CURRENT_BREAKING"; exit 1; }
    CURRENT=${CURRENT:-untagged}
    if [ "${CURRENT_BREAKING% *}" -gt "${TARGET_BREAKING% *}" ] ||
      { [ "${CURRENT_BREAKING% *}" -eq "${TARGET_BREAKING% *}" ] && [ "${CURRENT_BREAKING#* }" -ge "${TARGET_BREAKING#* }" ]; }; then
      RAISED=
    fi
  fi
  if [ -n "$RAISED" ]; then
    if [ -z "$CROSS_MAJOR" ]; then
      echo "MAJOR_UPDATE $CURRENT..$TARGET"
      # The commits the update would deploy: from the deployed commit, or from local main when check names none.
      LISTED_FROM=${DEPLOYED_COMMIT:-$MAIN}
      if git -C "$CLONE" rev-parse --verify --quiet "$LISTED_FROM^{commit}" >/dev/null; then
        git -C "$CLONE" log --oneline --no-decorate "$LISTED_FROM..$ORIGIN_MAIN" | tr -d '\r'
      fi
      exit 1
    fi
    CROSSED="$CURRENT..$TARGET"
  fi
fi

# A diverged main stops before the checkout, so the clone stays on the branch it was found on. Any other answer,
# such as no local main yet, leaves the decision to the checkout and the fast-forward below.
ANCESTOR=0
git -C "$CLONE" merge-base --is-ancestor "$MAIN" "$ORIGIN_MAIN" >/dev/null 2>&1 || ANCESTOR=$?
if [ "$ANCESTOR" -eq 1 ]; then
  echo "NOT_FAST_FORWARD"
  git -C "$CLONE" log --oneline --no-decorate "$ORIGIN_MAIN..$MAIN" | tr -d '\r'
  exit 1
fi

# The branch or, when HEAD is detached, the commit the clone was found on, to return to if the fast-forward fails.
FOUND_ON=$(git -C "$CLONE" symbolic-ref --quiet --short HEAD 2>/dev/null || git -C "$CLONE" rev-parse --verify --quiet HEAD)
FOUND_ON=$(printf '%s' "$FOUND_ON" | tr -d '\r')
if ! OUTPUT=$(git -C "$CLONE" checkout --quiet main 2>&1); then
  echo "CHECKOUT_FAILED"
  printf '%s\n' "$OUTPUT" | tr -d '\r'
  exit 1
fi
BEFORE=$(git -C "$CLONE" rev-parse --short HEAD | tr -d '\r')
if ! OUTPUT=$(git -C "$CLONE" merge --ff-only --quiet "$ORIGIN_MAIN" 2>&1); then
  echo "NOT_FAST_FORWARD"
  printf '%s\n' "$OUTPUT" | tr -d '\r'
  if [ -n "$FOUND_ON" ] && [ "$FOUND_ON" != "main" ] &&
    ! OUTPUT=$(git -C "$CLONE" checkout --quiet "$FOUND_ON" 2>&1); then
    echo "still on main: cannot switch back to $FOUND_ON: $(one_line "$OUTPUT")"
  fi
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

# A fresh process runs the deployer exactly as it now is on main. It refuses to replace a pin without --unpin, so a
# pin check could not read stops there too.
DEPLOY_OPTIONS=(--all)
if [ -n "$UNPIN" ]; then
  DEPLOY_OPTIONS+=(--unpin)
fi
"$PYTHON" "$CLONE/deploy.py" "${DEPLOY_OPTIONS[@]}" </dev/null
CODE=$?
if [ "$CODE" -ne 0 ]; then
  echo "DEPLOY_FAILED $CODE"
  exit 1
fi
echo "DEPLOYED"
