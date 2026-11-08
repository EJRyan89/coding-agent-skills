#!/usr/bin/env bash
# Fast-forward a coding-agent-skills clone to origin/main, then redeploy it with `deploy.py --all`: on the release
# channel at the highest release tag origin/main holds, through `deploy.py --at refs/tags/<tag>`, and on the main
# channel at main itself.
#
# Usage: update.sh <clone> <release|main> [--channel <release|main>] [--cross-major] [--unpin]
# The second argument is the configured channel, and --channel replaces it for this run.
# Prints one status line first, then details:
#   DIRTY            followed by the tracked files with uncommitted changes
#   PINNED <ref> <commit> alone: `deploy.py check` names the deployment as made with `deploy.py --at <ref>`, which
#                    deployed <commit>, the pin is not the release channel's own, and --unpin was not given; on the
#                    main channel nothing was fetched, and on the release channel the clone was fetched and the pin
#                    is at another commit than the release the update would deploy
#   FETCH_FAILED     followed by Git's error on one line: origin could not be reached, refused, wanted a credential
#                    the configured helpers could not give without asking, or did not answer within the time limit
#   TAG_SHADOWS_BRANCH followed by each tag named main or origin/main, as "<tag> on origin" or "<tag> in the clone";
#                    nothing was fetched
#   NO_RELEASE       alone, on the release channel: no release tag reaches origin/main; the clone was fetched
#   MAJOR_UPDATE <current>..<target> followed by the commits it would deploy; origin/main's highest
#                    release tag raises the breaking component (the major, or the minor while the
#                    major is 0) above the release of the commit `deploy.py check` names as deployed,
#                    and --cross-major was not given; <current> is untagged, counted as 0.0.0, when no
#                    release tag reaches that commit, and unknown, which any release raises, when check
#                    names no deployed commit or the clone does not hold it, which a NOT_IN_CLONE line then
#                    names before the commits
#   CHECKOUT_FAILED  followed by Git's error from switching to main
#   NOT_FAST_FORWARD followed by the commits main has that origin/main lacks, found before switching branches;
#                    or by Git's error when Git refuses the fast-forward itself, after which the clone is
#                    switched back to the branch it was found on, or a "still on main:" line says why not
#   UP_TO_DATE <sha> or UPDATED <old>..<new> on the main channel, followed by the pulled commits;
#   UP_TO_DATE <tag> or UPDATED <old>..<tag> on the release channel, where <old> is the deployed commit, or unknown
#                    when check names none or the clone does not hold it, followed by
#                    NOT_IN_CLONE <commit> when the clone does not hold the deployed commit check names, as when it
#                    was on a branch since deleted and pruned, so what the release adds to it and drops from it
#                    cannot be read, then by the commits the release adds to the deployed commit, or to local main
#                    as it was before the fast-forward when <old> is unknown, and by
#                    NOT_IN_RELEASE <tag>..<old> and the deployed commits the release does not carry, when there are
#                    any; either is followed by CROSSED <current>..<target> when --cross-major accepted a release
#                    boundary, then the deploy output and DEPLOYED, DEPLOY_FAILED <code>, or DEPLOY_PINNED <ref>
#                    <commit> when the deployer refused to replace a deployment made with `deploy.py --at <ref>`,
#                    which deployed <commit>, that check did not name: the clone was updated, and nothing was
#                    deployed
#   FAILED <reason>  alone, before anything is merged or deployed and with the clone on the branch it was found on:
#                    the clone is not a repository with deploy.py, no ejryan89-skill-core is deployed beside this
#                    skill, `deploy.py check` printed nothing, printed only its error, or named the deployed commit or
#                    the pin in a form the update cannot read, so whether the installation is pinned is unknown, a git
#                    lookup or comparison failed rather than answering, git's own reason following, or another step
#                    failed outright
# Each list of commits above that git cannot read, after any status line, is one UNLISTED <range> <reason> line in its
# place, never an empty list.
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
  echo "usage: update.sh <clone containing deploy.py> <release|main> [--channel <release|main>] [--cross-major]" \
    "[--unpin]" >&2
  exit 2
}
channel_named() {
  case "$1" in
    release | main) ;;
    *) usage ;;
  esac
}
[ "$#" -ge 2 ] || usage
channel_named "$2"
CHANNEL=$2
CHANNEL_GIVEN=
CHANNEL_NEXT=
CROSS_MAJOR=
UNPIN=
for OPTION in "${@:3}"; do
  if [ -n "$CHANNEL_NEXT" ]; then
    channel_named "$OPTION"
    CHANNEL=$OPTION
    CHANNEL_NEXT=
    continue
  fi
  case "$OPTION" in
    --channel) [ -z "$CHANNEL_GIVEN" ] || usage; CHANNEL_GIVEN=1; CHANNEL_NEXT=1 ;;
    --cross-major) [ -z "$CROSS_MAJOR" ] || usage; CROSS_MAJOR=1 ;;
    --unpin) [ -z "$UNPIN" ] || usage; UNPIN=1 ;;
    *) usage ;;
  esac
done
[ -z "$CHANNEL_NEXT" ] || usage

# A command's error output as one line, for a FAILED reason: its lines joined by spaces, blank ones left out, as the
# deployer prints its errors between blank lines.
one_line() {
  printf '%s\n' "$1" | tr -d '\r' | awk 'NF { printf "%s%s", separator, $0; separator = " " }'
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
  UPDATE_SKILLS_COPY=$COPY UPDATE_SKILLS_SCRIPTS=$(dirname -- "$0") exec bash "$COPY" "$@"
fi
DEPLOY_ERRORS=
GIT_ERRORS=
trap 'rm -f -- "$0" ${DEPLOY_ERRORS:+"$DEPLOY_ERRORS"} ${GIT_ERRORS:+"$GIT_ERRORS"}' EXIT
SCRIPTS=$UPDATE_SKILLS_SCRIPTS
unset UPDATE_SKILLS_COPY UPDATE_SKILLS_SCRIPTS

# The deployer's stderr is kept in a file before it is shown, so its refusal to replace a pin can be read below.
if ! DEPLOY_ERRORS=$(mktemp 2>&1); then
  echo "FAILED cannot create a temporary file for the deployer's errors: $(one_line "$DEPLOY_ERRORS")"
  DEPLOY_ERRORS=
  exit 1
fi
# git's stderr is kept apart from its output, so git_quiet below can tell git's "no" from its failure.
if ! GIT_ERRORS=$(mktemp 2>&1); then
  echo "FAILED cannot create a temporary file for git's errors: $(one_line "$GIT_ERRORS")"
  GIT_ERRORS=
  exit 1
fi

# git's repository-selecting variables, which a git hook exports and a shell may pass on, would point every
# `git -C "$CLONE"` below at another repository, index, or object store: those git clears itself before it works in
# another repository, but for the command-scope settings in its list, and GIT_NAMESPACE. Each is unset in any case, as
# Windows reads a name, so git there takes git_dir as GIT_DIR. ejryan89-skill-core's bounded_process.py leaves the same out of
# every command a skill's Python starts. awk's ENVIRON names each exported variable in the case it was given. A list git
# cannot give would leave every variable in place, so it stops the update.
if ! LOCAL_ENV_VARS=$(git rev-parse --local-env-vars 2>&1); then
  echo "FAILED cannot list git's repository-selecting variables: $(one_line "$LOCAL_ENV_VARS")"
  exit 1
fi
for NAME in $(printf '%s\n' "$LOCAL_ENV_VARS" | awk '
  { sub(/\r$/, ""); wanted[$0] = 1 }
  END {
    wanted["GIT_NAMESPACE"] = 1
    delete wanted["GIT_CONFIG_PARAMETERS"]
    delete wanted["GIT_CONFIG_COUNT"]
    for (NAME in ENVIRON) if (toupper(NAME) in wanted) print NAME
  }'); do
  unset "$NAME"
done

# What a release tag is, and their order, is ejryan89-skill-core's release_tags.py, the one definition the release notes
# and the upgrade-notes check read too: vMAJOR.MINOR.PATCH and nothing after it, so a pre-release such as v1.0.0-rc.1
# and every other tag are not releases. It runs from the copy of ejryan89-skill-core the deployment that installed this
# script put beside it, never from the clone, whose later commits may move or rename it before the update has deployed
# them. deploy.py at the clone's root is the one file the update runs from the clone.
RELEASE_TAGS="$SCRIPTS/../../ejryan89-skill-core/scripts/release_tags.py"
if [ ! -f "$RELEASE_TAGS" ]; then
  echo "FAILED no ejryan89-skill-core release_tags.py deployed beside update.sh: $RELEASE_TAGS"
  exit 1
fi

CLONE=$1
if [ ! -f "$CLONE/deploy.py" ]; then
  echo "FAILED no deploy.py in $CLONE"
  exit 1
fi

# A git command in the clone, read as ejryan89-skill-core's git_client.py reads git's exit status: status 0 when git
# succeeded, GIT_OUT then its output; 1 when git said no, which a --quiet command, merge-base --is-ancestor, and
# symbolic-ref --quiet say only by exiting 1 with nothing on stderr; and 2 for any other failure, such as a clone git
# refuses as of dubious ownership, a broken config, or a warning before exit 1, GIT_REASON then git's error as one line,
# or its exit status when it printed none. So a git that cannot answer is never read as git's no.
GIT_OUT=
GIT_REASON=
git_quiet() {
  local code=0
  GIT_OUT=$(git -C "$CLONE" "$@" 2>"$GIT_ERRORS") || code=$?
  GIT_OUT=$(printf '%s' "$GIT_OUT" | tr -d '\r')
  GIT_REASON=$(one_line "$(cat -- "$GIT_ERRORS")")
  if [ "$code" -eq 0 ]; then
    return 0
  elif [ "$code" -eq 1 ] && [ -z "$GIT_REASON" ]; then
    return 1
  fi
  [ -n "$GIT_REASON" ] || GIT_REASON="git $1 exited $code and printed nothing"
  return 2
}

if ! git_quiet rev-parse --git-dir; then
  echo "FAILED cannot read $CLONE as a git repository: $GIT_REASON"
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

# The highest release tag reachable from a commit, or nothing when there is none; when it cannot be read, a FAILED line
# naming the second argument, and status 1.
release_of() {
  local answer
  answer=$("$PYTHON" "$RELEASE_TAGS" "$CLONE" "$1" </dev/null 2>&1 | tr -d '\r')
  if [[ "$answer" =~ ^RELEASE\ (v[0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  elif [ "$answer" != NO_RELEASE ]; then
    echo "FAILED cannot read the release tags reaching $2: $(one_line "${answer#FAILED }")"
    return 1
  fi
}
# The commits git log lists for a range, one a line; when git cannot list them, an UNLISTED line naming the range and
# git's error in their place, and status 1, so a failure never reads as a range holding no commits.
log_lines() {
  local listed code=0
  listed=$(git -C "$CLONE" log --oneline --no-decorate "$1" 2>&1) || code=$?
  if [ "$code" -ne 0 ]; then
    echo "UNLISTED $1 $(one_line "$listed")"
    return 1
  fi
  [ -z "$listed" ] || printf '%s\n' "$listed" | tr -d '\r'
}
# A commit's short name; when git cannot give it, a FAILED line naming the second argument, and status 1.
short_name() {
  local named
  if ! named=$(git -C "$CLONE" rev-parse --short "$1" 2>&1); then
    echo "FAILED cannot read the short name of $2: $(one_line "$named")"
    return 1
  fi
  printf '%s\n' "$named" | tr -d '\r'
}
# Whether the release $2 raises the breaking component (the major, or the minor while the major is 0) above the
# release $1, or untagged: release_tags.py compares them, as the one reader of a version, which holds a number of any
# length exactly. Status 0 when it does, 1 when it does not, and 2 with a FAILED line when the two cannot be compared.
raises_breaking() {
  local answer
  answer=$("$PYTHON" "$RELEASE_TAGS" --raises "$1" "$2" </dev/null 2>&1 | tr -d '\r')
  case "$answer" in
    RAISES) return 0 ;;
    WITHIN) return 1 ;;
  esac
  echo "FAILED cannot compare release $1 with $2: $(one_line "${answer#FAILED }")"
  return 2
}

# check names the deployed commit as "<name> (<commit>)", the commit alone, or unknown, followed by the pin when the
# deployment was made with `deploy.py --at <ref>`. Its exit status alone does not stop the update: a check that reports
# a missing tool exits 1 and still names the commit. That line is the only word on whether the installation is pinned,
# so a check that prints none, or one the update cannot read, stops the update rather than deploy over a pin it could
# not see. DEPLOYED_COMMIT is the commit's full name, or empty when check names none, so it is never taken for an option.
PIN_MARK=", pinned with --at "
PIN_UNKNOWN="FAILED cannot read whether the installation is pinned:"
CHECK_CODE=0
CHECK_OUTPUT=$("$PYTHON" "$CLONE/deploy.py" check </dev/null 2>&1) || CHECK_CODE=$?
CHECK_OUTPUT=$(printf '%s' "$CHECK_OUTPUT" | tr -d '\r')
DEPLOYED_LINE=$(grep -m 1 -e "^Deployed commit: " <<<"$CHECK_OUTPUT")
if [ -z "$DEPLOYED_LINE" ] && [ -z "$CHECK_OUTPUT" ]; then
  echo "$PIN_UNKNOWN deploy.py check exited $CHECK_CODE and printed nothing"
  exit 1
elif [ -z "$DEPLOYED_LINE" ]; then
  echo "$PIN_UNKNOWN deploy.py check exited $CHECK_CODE without naming the deployed commit: $(one_line "$CHECK_OUTPUT")"
  exit 1
fi
DEPLOYED=${DEPLOYED_LINE#Deployed commit: }
PIN_REF=
case "$DEPLOYED" in
  *"$PIN_MARK"*) PIN_REF=${DEPLOYED##*"$PIN_MARK"} && DEPLOYED=${DEPLOYED%"$PIN_MARK"*} ;;
esac
# A commit check or the deployer names as "<name> (<commit>)" is the commit inside the parentheses.
commit_named() {
  local named=$1
  case "$named" in
    *")") named=${named##*"("} && named=${named%")"} ;;
  esac
  printf '%s' "$named"
}
DEPLOYED=$(commit_named "$DEPLOYED")
# A commit that is neither a full name nor unknown, or a pin mark with no ref after it or a ref holding a space, which
# no ref the deployer records can, is a line the update cannot read.
DEPLOYED_COMMIT=
if [[ "$DEPLOYED" =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ ]]; then
  DEPLOYED_COMMIT=$DEPLOYED
fi
if { [ -z "$DEPLOYED_COMMIT" ] && [ "$DEPLOYED" != unknown ]; } ||
  { [[ "$DEPLOYED_LINE" == *"$PIN_MARK"* ]] && ! [[ "$PIN_REF" =~ ^[^[:space:]]+$ ]]; }; then
  echo "$PIN_UNKNOWN deploy.py check printed: $(one_line "$DEPLOYED_LINE")"
  exit 1
fi

# The release channel pins the installation at the release it deploys, naming the tag in full, as refs/tags/<tag>, so
# its own pin is one whose ref is the full name of the last release its commit holds. Any other pin is the user's,
# such as one made by hand with `deploy.py --at <tag>` to step back from a regression, and stops the update until
# --unpin; the release channel moves its own pin to each later release, and the main channel replaces it.
OWN_PIN=
if [ -n "$PIN_REF" ] && [ -n "$DEPLOYED_COMMIT" ] && [ "${PIN_REF#refs/tags/}" != "$PIN_REF" ]; then
  PINNED_RELEASE=$(release_of "$DEPLOYED_COMMIT" "the pinned commit $DEPLOYED_COMMIT") ||
    { echo "$PINNED_RELEASE"; exit 1; }
  if [ -n "$PINNED_RELEASE" ] && [ "$PIN_REF" = "refs/tags/$PINNED_RELEASE" ]; then
    OWN_PIN=1
  fi
fi
USER_PIN=
if [ -n "$PIN_REF" ] && [ -z "$OWN_PIN" ] && [ -z "$UNPIN" ]; then
  USER_PIN=1
fi

# On the main channel a user's pin would be replaced by main, so the update stops before the fetch. The release channel
# stops at a user's pin after the fetch, once it knows the release it would deploy. A pin check did not name, as when
# the manifest changed after it ran, does not stop the update here; on either channel the deployer itself refuses to
# replace a user's pin without --unpin, which ends the update with DEPLOY_PINNED below.
if [ "$CHANNEL" = main ] && [ -n "$USER_PIN" ]; then
  echo "PINNED $PIN_REF $DEPLOYED"
  exit 1
fi

# Every ref below is named in full: Git resolves a short name as refs/tags/<name> before refs/heads/<name> or
# refs/remotes/<name>, so a tag named main or origin/main would stand in for the branch. The checkout and the switch
# back name a branch by its short name, which checkout resolves as a branch first.
MAIN=refs/heads/main
ORIGIN_MAIN=refs/remotes/origin/main

# The two calls to origin, the tag check here and the fetch below, run through remote_refs.py beside this script, which
# runs git through ejryan89-skill-core's GitClient: with no prompt, no askpass helper that an inherited GIT_ASKPASS,
# core.askPass, or SSH_ASKPASS names, no Git Credential Manager dialog, and a time limit, so a credential the helpers
# cannot give without asking, or a remote that never answers, ends the update with FETCH_FAILED and git's reason
# instead of holding it. Its last line is CHECKED, FETCHED, or FAILED <reason>; any other last line is a failure too.
REMOTE_REFS="$SCRIPTS/remote_refs.py"
REMOTE_OUT=
remote_refs() {
  local output last
  output=$("$PYTHON" "$REMOTE_REFS" "$@" </dev/null 2>&1 | tr -d '\r')
  last=$(printf '%s\n' "$output" | tail -n 1)
  case "$last" in
    CHECKED | FETCHED)
      REMOTE_OUT=$output
      return 0
      ;;
  esac
  echo "FETCH_FAILED"
  case "$last" in
    "FAILED "*) printf '%s\n' "${last#FAILED }" ;;
    *) printf 'remote_refs.py %s printed no result: %s\n' "$1" "$(one_line "$output")" ;;
  esac
  return 1
}

# A tag named like a branch the update reads, on origin or in the clone, stops it before anything is fetched: such a
# tag still shadows the branch in the user's own Git commands.
remote_refs shadows "$CLONE" || exit 1
SHADOWS=$(printf '%s\n' "$REMOTE_OUT" | awk '$1 == "ON_ORIGIN" { print $2 " on origin" }')
# The clone's own tags are listed rather than probed one by one, so tags git cannot read stop the update instead of
# reading as none.
if ! OUTPUT=$(git -C "$CLONE" for-each-ref --format='%(refname)' refs/tags/main refs/tags/origin/main 2>&1); then
  echo "FAILED cannot read the clone's tags: $(one_line "$OUTPUT")"
  exit 1
fi
LOCAL_SHADOWS=$(printf '%s\n' "$OUTPUT" | tr -d '\r' |
  awk '$0 == "refs/tags/main" || $0 == "refs/tags/origin/main" { print $0 " in the clone" }')
SHADOWS+="${SHADOWS:+${LOCAL_SHADOWS:+$'\n'}}$LOCAL_SHADOWS"
if [ -n "$SHADOWS" ]; then
  echo "TAG_SHADOWS_BRANCH"
  printf '%s\n' "$SHADOWS"
  exit 1
fi

# Release tags are fetched too, so both sides' versions can be compared below and the release channel finds its
# release. The negative refspecs keep the tag fetch from creating a shadowing tag that origin published after the
# check above. remote_refs.py names the same refs as MAIN and ORIGIN_MAIN above.
remote_refs fetch "$CLONE" || exit 1

# The highest release on origin/main: the release the release channel deploys, and the one the stop below compares on
# either channel. RELEASE_REF is that tag's full name and RELEASE_COMMIT its commit; DEPLOY_END is the commit the
# update would deploy, which the stop's commit list ends at.
TARGET=$(release_of "$ORIGIN_MAIN" origin/main) || { echo "$TARGET"; exit 1; }
DEPLOY_END=$ORIGIN_MAIN
if [ "$CHANNEL" = release ]; then
  if [ -z "$TARGET" ]; then
    echo "NO_RELEASE"
    exit 1
  fi
  RELEASE_REF="refs/tags/$TARGET"
  # Without --quiet, which silences --verify's error, git names why the tag cannot be read.
  if ! RELEASE_COMMIT=$(git -C "$CLONE" rev-parse --verify "$RELEASE_REF^{commit}" 2>&1); then
    echo "FAILED cannot read the commit of $RELEASE_REF: $(one_line "$RELEASE_COMMIT")"
    exit 1
  fi
  RELEASE_COMMIT=$(printf '%s' "$RELEASE_COMMIT" | tr -d '\r')
  DEPLOY_END=$RELEASE_REF
  # A user's pin at the release's own commit is the deployment the update would make, so it does not stop it; the
  # deployer keeps the pin's ref as the user wrote it, so the next release stops here again.
  if [ -n "$USER_PIN" ] && [ "$DEPLOYED_COMMIT" != "$RELEASE_COMMIT" ]; then
    echo "PINNED $PIN_REF $DEPLOYED"
    exit 1
  fi
fi

# The deployed commit as the fetched clone holds it: KNOWN_COMMIT is DEPLOYED_COMMIT when the clone holds it as a
# commit, and empty when check names none or the clone does not hold it, as when it was on a branch since deleted and
# pruned. git's --verify --quiet answers 1 with nothing printed only for a name it cannot find; any other failure is a
# FAILED line, so a git that cannot answer is never read as a commit the clone lacks.
KNOWN_COMMIT=
NOT_IN_CLONE=
if [ -n "$DEPLOYED_COMMIT" ]; then
  VERIFIED=0
  git_quiet rev-parse --verify --quiet "$DEPLOYED_COMMIT^{commit}" || VERIFIED=$?
  if [ "$VERIFIED" -eq 0 ]; then
    KNOWN_COMMIT=$DEPLOYED_COMMIT
  elif [ "$VERIFIED" -eq 1 ]; then
    NOT_IN_CLONE="NOT_IN_CLONE $DEPLOYED_COMMIT"
  else
    echo "FAILED cannot look up the deployed commit $DEPLOYED_COMMIT: $GIT_REASON"
    exit 1
  fi
fi

# The installed version is the release of the deployed commit, not of local main, which a pull by hand may have moved
# past it. Without a release on origin/main there is nothing to cross. A deployed commit no release tag reaches counts
# as version zero, and one check does not name, or the clone does not hold, stops at any release.
CROSSED=
if [ -n "$TARGET" ]; then
  CURRENT=unknown
  if [ -n "$KNOWN_COMMIT" ]; then
    CURRENT=$(release_of "$KNOWN_COMMIT" "the deployed commit $KNOWN_COMMIT") || { echo "$CURRENT"; exit 1; }
  fi
fi
if [ -n "$TARGET" ] && [ "$CURRENT" != "$TARGET" ]; then
  RAISED=1
  if [ "$CURRENT" != unknown ]; then
    CURRENT=${CURRENT:-untagged}
    COMPARED=0
    ANSWER=$(raises_breaking "$CURRENT" "$TARGET") || COMPARED=$?
    if [ "$COMPARED" -eq 1 ]; then
      RAISED=
    elif [ "$COMPARED" -ne 0 ]; then
      echo "$ANSWER"
      exit 1
    fi
  fi
  if [ -n "$RAISED" ]; then
    if [ -z "$CROSS_MAJOR" ]; then
      echo "MAJOR_UPDATE $CURRENT..$TARGET"
      [ -z "$NOT_IN_CLONE" ] || echo "$NOT_IN_CLONE"
      # The commits the update would deploy: from the deployed commit, or from local main when check names none or
      # the clone does not hold it. A clone with no local main yet, which git's quiet exit 1 alone says, has none to
      # list from; any other answer of the lookup is an UNLISTED line, never an empty list.
      LISTED_FROM=${KNOWN_COMMIT:-$MAIN}
      VERIFIED=0
      git_quiet rev-parse --verify --quiet "$LISTED_FROM^{commit}" || VERIFIED=$?
      if [ "$VERIFIED" -eq 0 ]; then
        log_lines "$LISTED_FROM..$DEPLOY_END"
      elif [ "$VERIFIED" -ne 1 ]; then
        echo "UNLISTED $LISTED_FROM..$DEPLOY_END $GIT_REASON"
      fi
      exit 1
    fi
    CROSSED="$CURRENT..$TARGET"
  fi
fi

# A diverged main stops before the checkout, so the clone stays on the branch it was found on. A clone with no local
# main yet, which git's quiet no to the lookup alone says, leaves the decision to the checkout, which creates main from
# origin/main, and to the fast-forward below; a lookup or a comparison git cannot answer is a FAILED line with git's
# reason, never a diverged main.
HAS_MAIN=0
git_quiet rev-parse --verify --quiet "$MAIN^{commit}" || HAS_MAIN=$?
if [ "$HAS_MAIN" -eq 2 ]; then
  echo "FAILED cannot look up $MAIN: $GIT_REASON"
  exit 1
elif [ "$HAS_MAIN" -eq 0 ]; then
  ANCESTOR=0
  git_quiet merge-base --is-ancestor "$MAIN" "$ORIGIN_MAIN" || ANCESTOR=$?
  if [ "$ANCESTOR" -eq 1 ]; then
    echo "NOT_FAST_FORWARD"
    log_lines "$ORIGIN_MAIN..$MAIN"
    exit 1
  elif [ "$ANCESTOR" -eq 2 ]; then
    echo "FAILED cannot tell whether $MAIN is an ancestor of $ORIGIN_MAIN: $GIT_REASON"
    exit 1
  fi
fi

# The branch or, when HEAD is detached, the commit the clone was found on, to return to if the fast-forward fails.
# symbolic-ref --quiet says HEAD is detached only by its quiet no; any other failure of it, or of the commit lookup,
# which runs without --quiet so git names why it cannot resolve HEAD, leaves nothing to return to, so the update stops
# before it switches branches, with git's reason. An answer naming nothing is refused too.
SYMBOLIC=0
git_quiet symbolic-ref --quiet --short HEAD || SYMBOLIC=$?
if [ "$SYMBOLIC" -eq 2 ]; then
  echo "FAILED cannot read the branch the clone is on: $GIT_REASON"
  exit 1
elif [ "$SYMBOLIC" -eq 1 ] && ! git_quiet rev-parse --verify HEAD; then
  echo "FAILED cannot read the commit the clone is on: ${GIT_REASON:-git answered no}"
  exit 1
fi
FOUND_ON=$GIT_OUT
if [ -z "$FOUND_ON" ]; then
  echo "FAILED cannot read the branch or the commit the clone is on: git named none"
  exit 1
fi
# Every short name the report prints is read before the checkout, so a FAILED line never follows a change to the clone:
# main after the fast-forward is origin/main, main before it is origin/main too when the clone has no local main yet,
# which the checkout then creates there, and the deployed commit is known already.
AFTER=$(short_name "$ORIGIN_MAIN" "main after the fast-forward") || { echo "$AFTER"; exit 1; }
BEFORE=$AFTER
if [ "$HAS_MAIN" -eq 0 ]; then
  BEFORE=$(short_name "$MAIN" "main before the fast-forward") || { echo "$BEFORE"; exit 1; }
fi
OLD=
if [ -n "$KNOWN_COMMIT" ]; then
  OLD=$(short_name "$KNOWN_COMMIT" "the deployed commit $KNOWN_COMMIT") || { echo "$OLD"; exit 1; }
fi
if ! OUTPUT=$(git -C "$CLONE" checkout --quiet main 2>&1); then
  echo "CHECKOUT_FAILED"
  printf '%s\n' "$OUTPUT" | tr -d '\r'
  exit 1
fi
if ! OUTPUT=$(git -C "$CLONE" merge --ff-only --quiet "$ORIGIN_MAIN" 2>&1); then
  echo "NOT_FAST_FORWARD"
  printf '%s\n' "$OUTPUT" | tr -d '\r'
  if [ -n "$FOUND_ON" ] && [ "$FOUND_ON" != "main" ] &&
    ! OUTPUT=$(git -C "$CLONE" checkout --quiet "$FOUND_ON" 2>&1); then
    echo "still on main: cannot switch back to $FOUND_ON: $(one_line "$OUTPUT")"
  fi
  exit 1
fi

# The main channel reports how local main moved. The release channel reports how the installation moves, from the
# deployed commit to the release: the commits the release adds, and those the deployment had that the release does
# not carry, as when the installation followed main past the release.
if [ "$CHANNEL" = main ]; then
  if [ "$BEFORE" = "$AFTER" ]; then
    echo "UP_TO_DATE $AFTER"
  else
    echo "UPDATED $BEFORE..$AFTER"
    log_lines "$BEFORE..$AFTER"
  fi
elif [ "$DEPLOYED_COMMIT" = "$RELEASE_COMMIT" ]; then
  echo "UP_TO_DATE $TARGET"
elif [ -z "$KNOWN_COMMIT" ]; then
  # Without a deployed commit the clone holds, the commits listed are those the release adds to local main, as the
  # stop listed them, and what the release drops from the deployed commit is not claimed either way.
  echo "UPDATED unknown..$TARGET"
  [ -z "$NOT_IN_CLONE" ] || echo "$NOT_IN_CLONE"
  log_lines "$BEFORE..$RELEASE_REF"
else
  echo "UPDATED $OLD..$TARGET"
  log_lines "$KNOWN_COMMIT..$RELEASE_REF"
  if LEFT=$(log_lines "$RELEASE_REF..$KNOWN_COMMIT"); then
    if [ -n "$LEFT" ]; then
      echo "NOT_IN_RELEASE $TARGET..$OLD"
      printf '%s\n' "$LEFT"
    fi
  else
    printf '%s\n' "$LEFT"
  fi
fi
if [ -n "$CROSSED" ]; then
  echo "CROSSED $CROSSED"
fi

# A fresh process runs the deployer exactly as it now is on main. On the release channel it deploys the release's tree
# with --at, which moves the release channel's own pin and refuses to replace any other without --unpin. On the main
# channel it refuses to replace any pin without --unpin, which it is given for the release channel's own pin. Either
# way --unpin is given when the user passed it, and a pin check did not name stops there: the deployer exits 1 with the
# line below, which names the ref the deployment was pinned with and its commit as check does, and which the deployer's
# tests hold.
DEPLOY_OPTIONS=(--all)
if [ "$CHANNEL" = release ]; then
  DEPLOY_OPTIONS+=(--at "$RELEASE_REF")
fi
if [ -n "$UNPIN" ] || { [ "$CHANNEL" = main ] && [ -n "$OWN_PIN" ]; }; then
  DEPLOY_OPTIONS+=(--unpin)
fi
"$PYTHON" "$CLONE/deploy.py" "${DEPLOY_OPTIONS[@]}" </dev/null 2>"$DEPLOY_ERRORS"
CODE=$?
cat -- "$DEPLOY_ERRORS" >&2
PIN_REFUSAL="ERROR: Refusing to replace the deployment pinned with --at "
PIN_REFUSAL_END=" without --unpin."
if [ "$CODE" -eq 1 ]; then
  REFUSED=$(tr -d '\r' <"$DEPLOY_ERRORS" | grep -m 1 -F -e "$PIN_REFUSAL")
  case "$REFUSED" in
    "$PIN_REFUSAL"*" to "*"$PIN_REFUSAL_END")
      # A ref holds no space, so the first " to " ends it.
      REFUSED=${REFUSED#"$PIN_REFUSAL"} && REFUSED=${REFUSED%"$PIN_REFUSAL_END"}
      echo "DEPLOY_PINNED ${REFUSED%% to *} $(commit_named "${REFUSED#* to }")"
      exit 1
      ;;
  esac
fi
if [ "$CODE" -ne 0 ]; then
  echo "DEPLOY_FAILED $CODE"
  exit 1
fi
echo "DEPLOYED"
