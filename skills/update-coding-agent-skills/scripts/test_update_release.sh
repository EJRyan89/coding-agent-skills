#!/usr/bin/env bash
set -Eeuo pipefail

# The release channel: the highest release tag origin/main holds, deployed through deploy.py --at, and the
# pins it moves or stops at.
# test_update_fixture.sh makes the fixture and the helpers every update.sh suite shares.
# shellcheck source=skills/update-coding-agent-skills/scripts/test_update_fixture.sh
source "$(dirname "$0")/test_update_fixture.sh"

# The cases start from the releases the release gate's cases publish, through v4.0.0.
publish_through v4.0.0

# The release channel deploys the highest release tag origin/main holds through deploy.py --at with the tag's full
# name, lists the commits it adds to the deployed one, and leaves the clone on main at origin/main: the commits on main
# since, a pre-release tag, and a tag that is not a version do not move it.
begin_case 'release'
recorded_line() { sed -n "${1}p" "$CLONE/.fake-deployed" | tr -d '\r'; }
new_clone
BEFORE=$(head_of "$CLONE" HEAD)
publish "release four one"
tag_upstream v4.1.0
publish "release four two"
tag_upstream v4.2.0
RELEASE_COMMIT=$(git -C "$SEED" rev-parse "v4.2.0^{commit}")
publish "after the last release"
tag_upstream v4.3.0-rc.1
tag_upstream latest
LATEST_COMMIT=$(git -C "$SEED" rev-parse "latest^{commit}")
run_subject "$CLONE" release
expect_status 0 "release"
expect_first_line "UPDATED $BEFORE..v4.2.0" "release"
expect_no_stderr "release"
for PULLED in "release four one" "release four two"; do
  grep -q " $PULLED$" <<<"$OUTPUT" || fail "release: commit '$PULLED' not listed: $OUTPUT"
done
if grep -q -e "after the last release" -e "NOT_IN_RELEASE" <<<"$OUTPUT"; then
  fail "release: listed a commit the release does not carry: $OUTPUT"
fi
expect_deployed "release" "--all --at refs/tags/v4.2.0"
[ "$(recorded_line 1)" = "$RELEASE_COMMIT" ] || fail "release: deployed $(recorded_line 1), not v4.2.0"
[ "$(git -C "$CLONE" branch --show-current)" = "main" ] || fail "release: the clone is not on main"
[ "$(head_of "$CLONE" HEAD)" = "$(head_of "$SEED" HEAD)" ] || fail "release: main not fast-forwarded to origin/main"

# A later run is up to date with the release, whatever main gained since, and deploys it again.
begin_case 'release again'
publish "later on main"
run_subject "$CLONE" release
expect_status 0 "release again"
expect_first_line "UP_TO_DATE v4.2.0" "release again"
expect_deployed "release again" "--all --at refs/tags/v4.2.0"
[ "$(head_of "$CLONE" HEAD)" = "$(head_of "$SEED" HEAD)" ] || fail "release again: main not fast-forwarded"

# For one run, --channel main follows main as the main channel always has, and replaces the release channel's own
# pin, which the deployer refuses without --unpin.
begin_case 'main for one run'
run_subject "$CLONE" release --channel main
expect_status 0 "main for one run"
expect_first_line "UP_TO_DATE $(head_of "$CLONE" HEAD)" "main for one run"
expect_deployed "main for one run" "--all --unpin"
[ "$(recorded_line 1)" = "$(git -C "$CLONE" rev-parse HEAD)" ] || fail "main for one run: main's head not deployed"

# Back on the release channel, an installation that followed main past the release steps back to it, and the commits
# the release does not carry are named after NOT_IN_RELEASE.
begin_case 'step back'
FOLLOWED=$(head_of "$CLONE" HEAD)
run_subject "$CLONE" release
expect_status 0 "step back"
expect_first_line "UPDATED $FOLLOWED..v4.2.0" "step back"
grep -qx "NOT_IN_RELEASE v4.2.0..$FOLLOWED" <<<"$OUTPUT" || fail "step back: no NOT_IN_RELEASE line: $OUTPUT"
for LEFT in "after the last release" "later on main"; do
  grep -q " $LEFT$" <<<"$OUTPUT" || fail "step back: commit '$LEFT' not listed: $OUTPUT"
done
expect_deployed "step back" "--all --at refs/tags/v4.2.0"

# The configured main channel follows main as before, replacing the release channel's own pin, and --channel release
# deploys the release for one run.
begin_case 'configured main'
BEFORE=$(head_of "$CLONE" HEAD)
publish "main channel change"
run_subject "$CLONE" main
expect_status 0 "configured main"
expect_first_line "UPDATED $BEFORE..$(head_of "$SEED" HEAD)" "configured main"
grep -q " main channel change$" <<<"$OUTPUT" || fail "configured main: pulled commit not listed: $OUTPUT"
expect_deployed "configured main" "--all --unpin"
FOLLOWED=$(head_of "$CLONE" HEAD)
run_subject "$CLONE" main --channel release
expect_status 0 "release for one run"
expect_first_line "UPDATED $FOLLOWED..v4.2.0" "release for one run"
expect_deployed "release for one run" "--all --at refs/tags/v4.2.0"
[ "$(recorded_line 2)" = "refs/tags/v4.2.0" ] || fail "release for one run: the pin is $(recorded_line 2)"

# A pin the user made, at another commit than the release, stops the release channel after the fetch, before main
# moves or anything is deployed: one made by hand with a tag's short name, or with a full name that is not the last
# release its commit holds.
begin_case 'user pin'
V41=$(git -C "$SEED" rev-parse "v4.1.0^{commit}")
for PIN in "$V41 v4.1.0" "$LATEST_COMMIT refs/tags/latest" "$V41 main~5"; do
  BEFORE=$(git -C "$CLONE" rev-parse HEAD)
  FETCHED=$(git -C "$CLONE" rev-parse origin/main)
  publish "change over a pin at ${PIN#* }"
  printf '%s\n%s\n' "${PIN%% *}" "${PIN#* }" >"$CLONE/.fake-deployed"
  run_subject "$CLONE" release
  expect_status 1 "user pin ${PIN#* }"
  [ "$OUTPUT" = "PINNED ${PIN#* } ${PIN%% *}" ] || fail "user pin ${PIN#* }: expected only the PINNED line, got: $OUTPUT"
  expect_no_stderr "user pin ${PIN#* }"
  [ "$(git -C "$CLONE" rev-parse origin/main)" != "$FETCHED" ] || fail "user pin ${PIN#* }: not fetched"
  [ "$(git -C "$CLONE" rev-parse HEAD)" = "$BEFORE" ] || fail "user pin ${PIN#* }: main moved"
  expect_not_deployed "user pin ${PIN#* }"
done

# A user's pin at the release's own commit is the deployment the update makes, so it does not stop it, and the pin
# stays the user's, under the name the user gave it.
begin_case 'user pin at the release'
printf '%s\nv4.2.0\n' "$RELEASE_COMMIT" >"$CLONE/.fake-deployed"
run_subject "$CLONE" release
expect_status 0 "user pin at the release"
expect_first_line "UP_TO_DATE v4.2.0" "user pin at the release"
expect_deployed "user pin at the release" "--all --at refs/tags/v4.2.0"
[ "$(recorded_line 2)" = "v4.2.0" ] || fail "user pin at the release: the pin is $(recorded_line 2)"

# A user's pin check cannot read stops the release channel too, before the fetch, and --cross-major does not move it:
# without a deployed commit the update would otherwise stop only at the major release, and once accepted, deploy.
begin_case 'user pin check cannot read'
printf '%s\nv4.1.0\n' "$V41" >"$CLONE/.fake-deployed"
FETCHED=$(git -C "$CLONE" rev-parse origin/main)
for ARGUMENTS in "release" "release --cross-major"; do
  read -r -a SPLIT <<<"$ARGUMENTS"
  FAKE_CHECK_OUTPUT="" FAKE_CHECK_EXIT=1 run_subject "$CLONE" "${SPLIT[@]}"
  expect_failed "FAILED cannot read whether the installation is pinned: deploy.py check exited 1 and printed nothing" \
    "user pin check cannot read $ARGUMENTS"
  expect_not_deployed "user pin check cannot read $ARGUMENTS"
  [ "$(git -C "$CLONE" rev-parse origin/main)" = "$FETCHED" ] || fail "user pin check cannot read $ARGUMENTS: fetched"
  [ "$(recorded_line 2)" = "v4.1.0" ] || fail "user pin check cannot read $ARGUMENTS: the pin is $(recorded_line 2)"
done

# A user's pin check does not name, as when the pin was made after check ran, still stops the release channel: the
# deployer refuses to replace it through --at without --unpin, and the update ends with DEPLOY_PINNED after the
# fast-forward, deploying nothing.
begin_case 'user pin the release channel deploys over'
FAKE_CHECK_OUTPUT="Deployed commit: v4.1.0 ($V41)" run_subject "$CLONE" release
expect_status 1 "user pin the release channel deploys over"
expect_first_line "UPDATED $(git -C "$CLONE" rev-parse --short "$V41")..v4.2.0" "user pin the release channel deploys over"
[ "$(printf '%s\n' "$OUTPUT" | tail -n 1)" = "DEPLOY_PINNED v4.1.0 $V41" ] ||
  fail "user pin the release channel deploys over: expected DEPLOY_PINNED v4.1.0 $V41 last, got: $OUTPUT"
expect_deployed "user pin the release channel deploys over" "--all --at refs/tags/v4.2.0"
[ "$(recorded_line 2)" = "v4.1.0" ] || fail "user pin the release channel deploys over: the pin is $(recorded_line 2)"

# With --unpin the release channel moves a user's pin to the release through --at, passing --unpin to the deployer,
# which refuses to replace a user's pin without it.
begin_case 'user pin unpinned'
run_subject "$CLONE" release --unpin
expect_status 0 "user pin unpinned"
expect_first_line "UPDATED $(git -C "$CLONE" rev-parse --short "$V41")..v4.2.0" "user pin unpinned"
grep -q " release four two$" <<<"$OUTPUT" || fail "user pin unpinned: commit since the pin not listed: $OUTPUT"
expect_deployed "user pin unpinned" "--all --at refs/tags/v4.2.0 --unpin"
[ "$(recorded_line 2)" = "refs/tags/v4.2.0" ] || fail "user pin unpinned: the pin is $(recorded_line 2)"

# A release that raises the breaking component stops the release channel at that tag, listing the commits up to it
# and none after, until --cross-major.
begin_case 'release major'
BEFORE=$(git -C "$CLONE" rev-parse HEAD)
publish "fifth major"
tag_upstream v5.0.0
publish "after the fifth major"
run_subject "$CLONE" release
expect_status 1 "release major"
expect_first_line "MAJOR_UPDATE v4.2.0..v5.0.0" "release major"
expect_no_stderr "release major"
grep -q " fifth major$" <<<"$OUTPUT" || fail "release major: the release's commit not listed: $OUTPUT"
if grep -q "after the fifth major" <<<"$OUTPUT"; then fail "release major: listed a commit after the tag"; fi
[ "$(git -C "$CLONE" rev-parse HEAD)" = "$BEFORE" ] || fail "release major: main moved"
expect_not_deployed "release major"
run_subject "$CLONE" release --cross-major
expect_status 0 "release major accepted"
expect_first_line "UPDATED $(git -C "$CLONE" rev-parse --short "$RELEASE_COMMIT")..v5.0.0" "release major accepted"
grep -qx "CROSSED v4.2.0..v5.0.0" <<<"$OUTPUT" || fail "release major accepted: crossing not named: $OUTPUT"
expect_deployed "release major accepted" "--all --at refs/tags/v5.0.0"

# A user's pin the update deployed its release over is still the user's at the next release: the release channel
# stops at it, the deployer's refusal ends an update that could not read it with DEPLOY_PINNED, and only --unpin moves
# it to the release.
begin_case 'user pin at the release, then the next release'
V5=$(git -C "$SEED" rev-parse "v5.0.0^{commit}")
printf '%s\nv5.0.0\n' "$V5" >"$CLONE/.fake-deployed"
run_subject "$CLONE" release
expect_status 0 "user pin at the release before the next"
expect_deployed "user pin at the release before the next" "--all --at refs/tags/v5.0.0"
[ "$(recorded_line 2)" = "v5.0.0" ] || fail "user pin at the release before the next: the pin is $(recorded_line 2)"
publish "five one"
tag_upstream v5.1.0
run_subject "$CLONE" release
expect_status 1 "user pin at the next release"
[ "$OUTPUT" = "PINNED v5.0.0 $V5" ] || fail "user pin at the next release: expected only the PINNED line, got: $OUTPUT"
expect_not_deployed "user pin at the next release"
FAKE_CHECK_OUTPUT="Deployed commit: v5.0.0 ($V5)" run_subject "$CLONE" release
expect_status 1 "user pin at the next release unread"
[ "$(printf '%s\n' "$OUTPUT" | tail -n 1)" = "DEPLOY_PINNED v5.0.0 $V5" ] ||
  fail "user pin at the next release unread: expected DEPLOY_PINNED v5.0.0 $V5 last, got: $OUTPUT"
[ "$(recorded_line 2)" = "v5.0.0" ] || fail "user pin at the next release unread: the pin is $(recorded_line 2)"
run_subject "$CLONE" release --unpin
expect_status 0 "user pin at the next release unpinned"
expect_deployed "user pin at the next release unpinned" "--all --at refs/tags/v5.1.0 --unpin"
[ "$(recorded_line 2)" = "refs/tags/v5.1.0" ] ||
  fail "user pin at the next release unpinned: the pin is $(recorded_line 2)"

# A user who pins by hand, with the tag's short name or the commit, over the update's own pin at the same commit makes
# the pin theirs: the deployer records the user's ref, and the next release stops at it with PINNED.
begin_case 'user pin over the release pin, then the next release'
V51=$(git -C "$SEED" rev-parse "v5.1.0^{commit}")
publish "five two"
tag_upstream v5.2.0
case "${OSTYPE:-}" in
  msys* | cygwin*) BY_HAND=python ;;
  *) BY_HAND=python3 ;;
esac
for PIN in v5.1.0 "$V51"; do
  printf '%s\nrefs/tags/v5.1.0\n' "$V51" >"$CLONE/.fake-deployed"
  FAKE_DEPLOY_LOG="$FIXTURE/by-hand.log" "$BY_HAND" "$CLONE/deploy.py" --all --at "$PIN" >/dev/null ||
    fail "user pin $PIN over the release pin: the deployment by hand failed"
  [ "$(recorded_line 2)" = "$PIN" ] || fail "user pin $PIN over the release pin: the pin is $(recorded_line 2)"
  run_subject "$CLONE" release
  expect_status 1 "user pin $PIN over the release pin at the next release"
  [ "$OUTPUT" = "PINNED $PIN $V51" ] ||
    fail "user pin $PIN over the release pin at the next release: expected only the PINNED line, got: $OUTPUT"
  expect_not_deployed "user pin $PIN over the release pin at the next release"
done
# The update's own pin again, which the next case's release moves.
printf '%s\nrefs/tags/v5.1.0\n' "$V51" >"$CLONE/.fake-deployed"

# With no deployed commit to read, the release channel stops at any release, and once --cross-major accepts it the
# update names the deployed commit unknown and lists the commits the release adds to local main, as the stop did.
begin_case 'release from no deployed commit'
BEFORE=$(git -C "$CLONE" rev-parse HEAD)
publish "sixth major"
tag_upstream v6.0.0
publish "after the sixth major"
FAKE_CHECK_OUTPUT="Deployed commit: unknown" run_subject "$CLONE" release
expect_status 1 "release from no deployed commit"
expect_first_line "MAJOR_UPDATE unknown..v6.0.0" "release from no deployed commit"
grep -q " sixth major$" <<<"$OUTPUT" || fail "release from no deployed commit: commit not listed: $OUTPUT"
[ "$(git -C "$CLONE" rev-parse HEAD)" = "$BEFORE" ] || fail "release from no deployed commit: main moved"
expect_not_deployed "release from no deployed commit"
FAKE_CHECK_OUTPUT="Deployed commit: unknown" run_subject "$CLONE" release --cross-major
expect_status 0 "release from no deployed commit accepted"
expect_first_line "UPDATED unknown..v6.0.0" "release from no deployed commit accepted"
expect_no_stderr "release from no deployed commit accepted"
[ "$(printf '%s\n' "$OUTPUT" | sed -n 2p)" = "$(git -C "$CLONE" log --oneline --no-decorate -1 v6.0.0)" ] ||
  fail "release from no deployed commit accepted: the release's commit does not follow UPDATED: $OUTPUT"
if grep -q -e "after the sixth major" -e " fifth major$" -e "NOT_IN_RELEASE" <<<"$OUTPUT"; then
  fail "release from no deployed commit accepted: listed a commit the release does not add to local main: $OUTPUT"
fi
grep -qx "CROSSED unknown..v6.0.0" <<<"$OUTPUT" ||
  fail "release from no deployed commit accepted: crossing not named: $OUTPUT"
expect_deployed "release from no deployed commit accepted" "--all --at refs/tags/v6.0.0"

# A deployed commit the clone does not hold, as one on a branch since deleted and pruned, is named on a NOT_IN_CLONE line
# wherever the update would read it, never read as one the release adds nothing to and drops nothing from: its release
# cannot be read, so the release channel stops at any release as with no deployed commit, and once --cross-major
# accepts it the update lists the commits the release adds to local main.
begin_case 'release from a deployed commit the clone does not hold'
LOST=0123456789abcdef0123456789abcdef01234567
! git -C "$CLONE" cat-file -e "$LOST" 2>/dev/null || fail "release from a lost commit: the clone holds $LOST"
BEFORE=$(git -C "$CLONE" rev-parse HEAD)
publish "seventh major"
tag_upstream v7.0.0
printf '%s\n' "$LOST" >"$CLONE/.fake-deployed"
run_subject "$CLONE" release
expect_status 1 "release from a lost commit"
expect_first_line "MAJOR_UPDATE unknown..v7.0.0" "release from a lost commit"
expect_no_stderr "release from a lost commit"
[ "$(printf '%s\n' "$OUTPUT" | sed -n 2p)" = "NOT_IN_CLONE $LOST" ] ||
  fail "release from a lost commit: NOT_IN_CLONE does not follow MAJOR_UPDATE: $OUTPUT"
grep -q " seventh major$" <<<"$OUTPUT" || fail "release from a lost commit: the release's commit not listed: $OUTPUT"
[ "$(git -C "$CLONE" rev-parse HEAD)" = "$BEFORE" ] || fail "release from a lost commit: main moved"
expect_not_deployed "release from a lost commit"
run_subject "$CLONE" release --cross-major
expect_status 0 "release from a lost commit accepted"
expect_first_line "UPDATED unknown..v7.0.0" "release from a lost commit accepted"
expect_no_stderr "release from a lost commit accepted"
[ "$(printf '%s\n' "$OUTPUT" | sed -n 2p)" = "NOT_IN_CLONE $LOST" ] ||
  fail "release from a lost commit accepted: NOT_IN_CLONE does not follow UPDATED: $OUTPUT"
grep -q " seventh major$" <<<"$OUTPUT" || fail "release from a lost commit accepted: commit not listed: $OUTPUT"
if grep -q "NOT_IN_RELEASE" <<<"$OUTPUT"; then
  fail "release from a lost commit accepted: claimed what the release drops from a commit it cannot read: $OUTPUT"
fi
grep -qx "CROSSED unknown..v7.0.0" <<<"$OUTPUT" || fail "release from a lost commit accepted: crossing not named: $OUTPUT"
expect_deployed "release from a lost commit accepted" "--all --at refs/tags/v7.0.0"

# A list of commits git cannot read, here because the clone's configuration names a date format git log refuses, is an
# UNLISTED line naming the range and git's error in its place, never an empty list: the release's added commits, and
# in place of NOT_IN_RELEASE, the commits it may drop.
begin_case 'commits git cannot list'
V7=$(git -C "$SEED" rev-parse "v7.0.0^{commit}")
publish "seven one"
tag_upstream v7.1.0
git_quiet -C "$CLONE" config log.date bogus
run_subject "$CLONE" release
git_quiet -C "$CLONE" config --unset log.date
expect_status 0 "commits git cannot list"
expect_first_line "UPDATED $(git -C "$CLONE" rev-parse --short "$V7")..v7.1.0" "commits git cannot list"
expect_no_stderr "commits git cannot list"
for RANGE in "$V7..refs/tags/v7.1.0" "refs/tags/v7.1.0..$V7"; do
  grep -qx "UNLISTED $RANGE fatal: unknown date format bogus" <<<"$OUTPUT" ||
    fail "commits git cannot list: no UNLISTED line for $RANGE: $OUTPUT"
done
if grep -q "NOT_IN_RELEASE" <<<"$OUTPUT"; then
  fail "commits git cannot list: claimed what the release drops from a list git could not read: $OUTPUT"
fi
expect_deployed "commits git cannot list" "--all --at refs/tags/v7.1.0"

# A git that fails where the update reads it ends the update with a FAILED line naming what it could not read, or an
# UNLISTED line in place of a list, never an empty answer. A stand-in for git, first on the update's PATH, runs git
# itself for every call, and for a call whose arguments hold one of the |-separated forms in FAKE_GIT_FAIL, from its
# FAKE_GIT_FAIL_NTH matching call on (the first by default), runs it against another repository, so the failure and
# its words are real git's: a form written as <form>=><git dir> runs in that git dir, and any other in one that does
# not exist.
REAL_GIT=$(command -v git)
SHIM="$FIXTURE/git-shim"
mkdir -p "$SHIM"
cat >"$SHIM/git" <<'EOF'
#!/usr/bin/env bash
IFS='|' read -r -a entries <<<"${FAKE_GIT_FAIL:-}"
for entry in "${entries[@]}"; do
  form=${entry%%=>*}
  directory=$FAKE_GIT_MISSING
  [[ "$entry" != *"=>"* ]] || directory=${entry#*=>}
  if [[ " $* " == *" $form "* ]]; then
    count=$(($(cat "$FAKE_GIT_COUNT" 2>/dev/null || echo 0) + 1))
    echo "$count" >"$FAKE_GIT_COUNT"
    if [ "$count" -ge "${FAKE_GIT_FAIL_NTH:-1}" ]; then
      GIT_DIR=$directory exec "$REAL_GIT" "$@"
    fi
  fi
done
exec "$REAL_GIT" "$@"
EOF
chmod +x "$SHIM/git"
export REAL_GIT
export FAKE_GIT_MISSING="$FIXTURE/no-repository.git"
NOT_A_REPOSITORY="fatal: not a git repository: "
# A repository with no commit, where HEAD names a branch that does not exist yet.
UNBORN="$FIXTURE/unborn.git"
git_quiet init --bare -b main "$UNBORN"
failing_git() {
  local forms=$1 nth=$2
  shift 2
  export FAKE_GIT_COUNT="$FIXTURE/git-shim-count"
  rm -f -- "$FAKE_GIT_COUNT"
  PATH="$SHIM:$PATH" FAKE_GIT_FAIL=$forms FAKE_GIT_FAIL_NTH=$nth run_subject "$@"
}
V71=$(git -C "$SEED" rev-parse "v7.1.0^{commit}")
[ "$(recorded_line 1)" = "$V71" ] || fail "git failures: the case starts from v7.1.0, not $(recorded_line 1)"

# A lookup of the deployed commit that fails other than with git's quiet "no such commit" is a FAILED line, never a
# commit the clone lacks.
begin_case 'deployed commit git cannot look up'
failing_git "rev-parse --verify --quiet $V71^{commit}" 1 "$CLONE" release
expect_failed "FAILED cannot look up the deployed commit $V71: $NOT_A_REPOSITORY" \
  "deployed commit git cannot look up"
expect_not_deployed "deployed commit git cannot look up"

# The major stop lists the commits from the deployed commit, and a lookup of it there that git cannot answer is an
# UNLISTED line in place of the list.
begin_case 'major stop list git cannot read'
publish "eighth major"
tag_upstream v8.0.0
failing_git "rev-parse --verify --quiet $V71^{commit}" 2 "$CLONE" release
expect_status 1 "major stop list git cannot read"
expect_first_line "MAJOR_UPDATE v7.1.0..v8.0.0" "major stop list git cannot read"
case "$(printf '%s\n' "$OUTPUT" | sed -n 2p)" in
  "UNLISTED $V71..refs/tags/v8.0.0 $NOT_A_REPOSITORY"*) ;;
  *) fail "major stop list git cannot read: no UNLISTED line in place of the list: $OUTPUT" ;;
esac
[ "$(printf '%s\n' "$OUTPUT" | wc -l | tr -d ' ')" = "2" ] || fail "major stop list git cannot read: more lines: $OUTPUT"
expect_not_deployed "major stop list git cannot read"

# A short name git cannot give, of main before or after the fast-forward or of the deployed commit, ends the update
# with a FAILED line naming it, never an UPDATED line with an empty end.
begin_case 'short names git cannot give'
for SITE in "rev-parse --short HEAD|1|main before the fast-forward" \
  "rev-parse --short HEAD|2|main after the fast-forward" \
  "rev-parse --short $V71|1|the deployed commit $V71"; do
  IFS='|' read -r FORM NTH WHAT <<<"$SITE"
  failing_git "$FORM" "$NTH" "$CLONE" release --cross-major
  expect_failed "FAILED cannot read the short name of $WHAT: $NOT_A_REPOSITORY" "short name of $WHAT"
  expect_not_deployed "short name of $WHAT"
done

# A clone whose branch or commit git cannot name stops before the checkout, on the branch it was found on, with git's
# reason. symbolic-ref --quiet says HEAD is detached only by exiting 1 with nothing on stderr, so a branch lookup that
# fails otherwise is never read as a detached HEAD; and the commit lookup of a detached HEAD, here meeting a HEAD that
# names no commit yet, which git explains only without --quiet, names git's reason.
begin_case 'branch git cannot name'
git_quiet -C "$CLONE" checkout -q -b aside
failing_git "symbolic-ref --quiet --short HEAD" 1 "$CLONE" release --cross-major
expect_failed "FAILED cannot read the branch the clone is on: $NOT_A_REPOSITORY" "branch git cannot name"
[ "$(git -C "$CLONE" branch --show-current)" = "aside" ] || fail "branch git cannot name: the clone left its branch"
expect_not_deployed "branch git cannot name"
begin_case 'detached commit git cannot name'
git_quiet -C "$CLONE" checkout -q --detach aside
failing_git "rev-parse --verify HEAD=>$UNBORN" 1 "$CLONE" release --cross-major
expect_failed "FAILED cannot read the commit the clone is on: fatal: Needed a single revision" \
  "detached commit git cannot name"
[ -z "$(git -C "$CLONE" branch --show-current)" ] || fail "detached commit git cannot name: HEAD not detached"
expect_not_deployed "detached commit git cannot name"
git_quiet -C "$CLONE" checkout -q main

# Whether main has diverged is git's no only when merge-base --is-ancestor exits 1 with nothing on stderr. One that
# warns before exiting 1, as git does over a broken ref, and a lookup of main that fails, are FAILED lines with git's
# reason, never NOT_FAST_FORWARD. A stand-in for git warns and exits 1 for the comparison alone.
begin_case 'ancestry git cannot tell'
WARNING_SHIM="$FIXTURE/warning shim"
mkdir -p "$WARNING_SHIM"
cat >"$WARNING_SHIM/git" <<'EOF'
#!/usr/bin/env bash
case " $* " in
  *" merge-base --is-ancestor "*) echo "warning: ignoring broken ref refs/heads/broken" >&2; exit 1 ;;
esac
exec "$REAL_GIT" "$@"
EOF
chmod +x "$WARNING_SHIM/git"
PATH="$WARNING_SHIM:$PATH" run_subject "$CLONE" release --cross-major
expect_failed "FAILED cannot tell whether refs/heads/main is an ancestor of refs/remotes/origin/main: warning: ignoring broken ref refs/heads/broken" \
  "ancestry git cannot tell"
expect_not_deployed "ancestry git cannot tell"
failing_git "rev-parse --verify --quiet refs/heads/main^{commit}" 1 "$CLONE" release --cross-major
expect_failed "FAILED cannot look up refs/heads/main: $NOT_A_REPOSITORY" "main git cannot look up"
expect_not_deployed "main git cannot look up"

# A clone git cannot open as a repository names git's reason.
begin_case 'repository git cannot open'
failing_git "rev-parse --git-dir" 1 "$CLONE" release
expect_failed "FAILED cannot read $CLONE as a git repository: $NOT_A_REPOSITORY" "repository git cannot open"
expect_not_deployed "repository git cannot open"

# Without a release tag on origin/main the release channel has nothing to deploy: it stops after the fetch, and the
# main channel still follows main.
begin_case 'no release'
UNTAGGED="$FIXTURE/untagged.git"
git_quiet init --bare -b main "$UNTAGGED"
git_quiet -C "$SEED" push "$UNTAGGED" main
case_number=$((case_number + 1))
CLONE="$FIXTURE/clone $case_number (untagged)"
git_quiet clone "$UNTAGGED" "$CLONE"
git -C "$CLONE" rev-parse HEAD >"$CLONE/.fake-deployed"
[ -z "$(git -C "$CLONE" tag)" ] || fail "no release: the clone holds tags"
run_subject "$CLONE" release
expect_status 1 "no release"
[ "$OUTPUT" = "NO_RELEASE" ] || fail "no release: expected only NO_RELEASE, got: $OUTPUT"
expect_no_stderr "no release"
expect_not_deployed "no release"
run_subject "$CLONE" release --channel main
expect_status 0 "no release on main"
expect_first_line "UP_TO_DATE $(head_of "$CLONE" HEAD)" "no release on main"
expect_deployed "no release on main"

echo "update.sh release channel tests passed"
