#!/usr/bin/env bash
set -Eeuo pipefail

# update.sh's usage, and the main channel over a clone in each state, before any release is tagged.
# test_update_fixture.sh makes the fixture and the helpers every update.sh suite shares.
# shellcheck source=skills/update-coding-agent-skills/scripts/test_update_fixture.sh
source "$(dirname "$0")/test_update_fixture.sh"

# Usage errors: the wrong number of arguments, on stderr with exit 2.
begin_case 'usage errors'
run_subject
expect_status 2 "no argument"
[ -z "$OUTPUT" ] || fail "no argument: usage went to stdout: $OUTPUT"
grep -q "^usage: update.sh" "$FIXTURE/stderr" || fail "no argument: usage text missing"
run_subject "$FIXTURE" main --cross-major extra
expect_status 2 "an extra argument"
run_subject "$FIXTURE" main --unpin --unpin
expect_status 2 "a repeated option"
run_subject "$FIXTURE" main --cross-major --unpin extra
expect_status 2 "five arguments"
# The configured channel is required and is release or main, and --channel takes one of the two, once.
for ARGUMENTS in "" "--cross-major" "beta" "Main" "main --channel" "main --channel beta" "release --channel main --channel main" \
  "main --channel --unpin" "--channel main"; do
  read -r -a SPLIT <<<"$ARGUMENTS"
  run_subject "$FIXTURE" "${SPLIT[@]}"
  expect_status 2 "channel usage '$ARGUMENTS'"
  [ -z "$OUTPUT" ] || fail "channel usage '$ARGUMENTS': usage went to stdout: $OUTPUT"
done

# A path that is not a clone is a named failure, not a usage error.
begin_case 'not a clone'
run_subject "$FIXTURE" main
expect_failed "FAILED no deploy.py in $FIXTURE" "directory without deploy.py"
PLAIN="$FIXTURE/plain dir (no git)"
mkdir -p "$PLAIN"
cp "$SEED/deploy.py" "$PLAIN/deploy.py"
export GIT_CEILING_DIRECTORIES="$FIXTURE"
run_subject "$PLAIN" main
unset GIT_CEILING_DIRECTORIES
expect_failed "FAILED not a git repository: $PLAIN" "not a repository"
expect_not_deployed "not a repository"

# An up-to-date clone deploys without moving, even when untracked files are present.
begin_case 'up to date'
new_clone
printf 'scratch\n' >"$CLONE/untracked.txt"
run_subject "$CLONE" main
expect_status 0 "up to date"
expect_first_line "UP_TO_DATE $(head_of "$CLONE" HEAD)" "up to date"
expect_deployed "up to date"
grep -qx "fake deploy ran" <<<"$OUTPUT" || fail "up to date: deploy output was not shown"
[ "$(printf '%s\n' "$OUTPUT" | tail -n 1)" = "DEPLOYED" ] || fail "up to date: missing DEPLOYED"

# A clone behind origin is fast-forwarded, lists the pulled commits, then deploys.
begin_case 'behind'
new_clone
BEFORE=$(head_of "$CLONE" HEAD)
publish "first change"
publish "second change"
run_subject "$CLONE" main
expect_status 0 "behind"
AFTER=$(head_of "$CLONE" HEAD)
[ "$AFTER" = "$(head_of "$SEED" HEAD)" ] || fail "behind: clone was not fast-forwarded"
expect_first_line "UPDATED $BEFORE..$AFTER" "behind"
grep -q "first change" <<<"$OUTPUT" || fail "behind: pulled commit missing"
grep -q "second change" <<<"$OUTPUT" || fail "behind: pulled commit missing"
expect_deployed "behind"

# A clean clone on another branch is switched to main before the fast-forward, and stays there.
begin_case 'other branch'
new_clone
git_quiet -C "$CLONE" checkout -b topic
publish "third change"
run_subject "$CLONE" main
expect_status 0 "other branch"
[ "$(git -C "$CLONE" branch --show-current)" = "main" ] || fail "other branch: not switched to main"
[ "$(head_of "$CLONE" HEAD)" = "$(head_of "$SEED" HEAD)" ] || fail "other branch: main not fast-forwarded"
expect_deployed "other branch"

# Uncommitted tracked changes stop everything before the fetch.
begin_case 'dirty'
new_clone
BEFORE=$(git -C "$CLONE" rev-parse origin/main)
printf 'edit\n' >>"$CLONE/notes.txt"
publish "fourth change"
run_subject "$CLONE" main
expect_status 1 "dirty"
expect_first_line "DIRTY" "dirty"
expect_no_stderr "dirty"
grep -q "notes.txt" <<<"$OUTPUT" || fail "dirty: changed file not listed"
[ "$(git -C "$CLONE" rev-parse origin/main)" = "$BEFORE" ] || fail "dirty: fetched anyway"
expect_nothing_run "dirty"

# A main with its own commits is never merged, rebased, or reset.
begin_case 'diverged'
new_clone
git_quiet -C "$CLONE" commit --allow-empty -m "local work"
LOCAL=$(git -C "$CLONE" rev-parse HEAD)
publish "fifth change"
run_subject "$CLONE" main
expect_status 1 "diverged"
expect_first_line "NOT_FAST_FORWARD" "diverged"
expect_no_stderr "diverged"
[ "$(git -C "$CLONE" rev-parse HEAD)" = "$LOCAL" ] || fail "diverged: main moved"
grep -q "local work" <<<"$OUTPUT" || fail "diverged: local commit not listed: $OUTPUT"
expect_not_deployed "diverged"

# A diverged main stops before any checkout, so a clone on another branch stays on it.
begin_case 'diverged on topic'
new_clone
git_quiet -C "$CLONE" commit --allow-empty -m "local work"
LOCAL=$(git -C "$CLONE" rev-parse main)
git_quiet -C "$CLONE" checkout -b topic
publish "sixth change"
run_subject "$CLONE" main
expect_status 1 "diverged on topic"
expect_first_line "NOT_FAST_FORWARD" "diverged on topic"
expect_no_stderr "diverged on topic"
[ "$(git -C "$CLONE" branch --show-current)" = "topic" ] || fail "diverged on topic: branch switched"
[ "$(git -C "$CLONE" rev-parse main)" = "$LOCAL" ] || fail "diverged on topic: main moved"
expect_not_deployed "diverged on topic"

# A fast-forward Git refuses after the switch, here over an untracked file the update adds, switches back.
begin_case 'refused on topic'
new_clone
git_quiet -C "$CLONE" checkout -b topic
BEFORE=$(git -C "$CLONE" rev-parse main)
printf 'added\n' >"$SEED/added.txt"
git_quiet -C "$SEED" add added.txt
git_quiet -C "$SEED" commit -m "add a file"
git_quiet -C "$SEED" push origin main
printf 'untracked copy\n' >"$CLONE/added.txt"
run_subject "$CLONE" main
expect_status 1 "refused on topic"
expect_first_line "NOT_FAST_FORWARD" "refused on topic"
expect_no_stderr "refused on topic"
[ "$(git -C "$CLONE" branch --show-current)" = "topic" ] || fail "refused on topic: not switched back"
[ "$(git -C "$CLONE" rev-parse main)" = "$BEFORE" ] || fail "refused on topic: main moved"
[ "$(cat "$CLONE/added.txt")" = "untracked copy" ] || fail "refused on topic: untracked file overwritten"
expect_not_deployed "refused on topic"

# A detached HEAD is returned to the same commit.
begin_case 'refused detached'
git_quiet -C "$CLONE" checkout --detach topic
DETACHED=$(git -C "$CLONE" rev-parse HEAD)
run_subject "$CLONE" main
expect_status 1 "refused detached"
expect_first_line "NOT_FAST_FORWARD" "refused detached"
[ -z "$(git -C "$CLONE" branch --show-current)" ] || fail "refused detached: HEAD not detached"
[ "$(git -C "$CLONE" rev-parse HEAD)" = "$DETACHED" ] || fail "refused detached: not switched back"
expect_not_deployed "refused detached"

# When the switch back fails too, the output says the clone is still on main. A Git wrapper refuses that switch.
begin_case 'switch back refused'
git_quiet -C "$CLONE" checkout topic
WRAPPER="$FIXTURE/git wrapper"
mkdir -p "$WRAPPER"
cat >"$WRAPPER/git" <<EOF
#!/usr/bin/env bash
case " \$* " in
  *" checkout --quiet topic "*) echo "error: switch refused by the test" >&2; exit 1 ;;
esac
exec "$(command -v git)" "\$@"
EOF
chmod +x "$WRAPPER/git"
PATH="$WRAPPER:$PATH" run_subject "$CLONE" main
expect_status 1 "switch back refused"
expect_first_line "NOT_FAST_FORWARD" "switch back refused"
grep -qx "still on main: cannot switch back to topic: error: switch refused by the test" <<<"$OUTPUT" ||
  fail "switch back refused: no still-on-main line: $OUTPUT"
[ "$(git -C "$CLONE" branch --show-current)" = "main" ] || fail "switch back refused: expected main"
expect_not_deployed "switch back refused"

# A branch that cannot switch to main, here over an untracked file main tracks, is never forced.
begin_case 'checkout failed'
new_clone
git_quiet -C "$CLONE" checkout -b topic
git_quiet -C "$CLONE" rm --quiet notes.txt
git_quiet -C "$CLONE" commit -m "drop notes"
printf 'untracked copy\n' >"$CLONE/notes.txt"
run_subject "$CLONE" main
expect_status 1 "checkout failed"
expect_first_line "CHECKOUT_FAILED" "checkout failed"
expect_no_stderr "checkout failed"
[ "$(git -C "$CLONE" branch --show-current)" = "topic" ] || fail "checkout failed: branch switched"
[ "$(cat "$CLONE/notes.txt")" = "untracked copy" ] || fail "checkout failed: untracked file overwritten"
expect_not_deployed "checkout failed"

# An unreachable origin is reported without deploying.
begin_case 'fetch failed'
new_clone
git_quiet -C "$CLONE" remote set-url origin "$FIXTURE/missing.git"
run_subject "$CLONE" main
expect_status 1 "fetch failed"
expect_first_line "FETCH_FAILED" "fetch failed"
expect_no_stderr "fetch failed"
expect_not_deployed "fetch failed"

# A Git command that fails outright is a named failure.
begin_case 'status failed'
new_clone
printf 'not an index' >"$CLONE/.git/index"
run_subject "$CLONE" main
expect_failed "FAILED git status failed: " "status failed"
expect_not_deployed "status failed"

# A script that cannot make its temporary copy stops before any Git command.
begin_case 'no temporary copy'
new_clone
export FAKE_DEPLOY_LOG="$FIXTURE/deploy-$case_number.log"
: >"$FAKE_DEPLOY_LOG"
set +e
OUTPUT=$(TMPDIR="$FIXTURE/absent temporary directory" bash "$SUBJECT" "$CLONE" main 2>"$FIXTURE/stderr")
status=$?
set -e
expect_failed "FAILED cannot create a temporary copy of update.sh: " "no temporary copy"
expect_not_deployed "no temporary copy"

# A script that cannot make the file the deployer's errors are kept in stops before any Git command too, and removes
# its temporary copy. A mktemp wrapper makes the copy and refuses every later call.
begin_case 'no file for the errors'
MKTEMP_WRAPPER="$FIXTURE/mktemp wrapper"
mkdir -p "$MKTEMP_WRAPPER" "$FIXTURE/tmp for the errors"
cat >"$MKTEMP_WRAPPER/mktemp" <<EOF
#!/usr/bin/env bash
if [ -e "$MKTEMP_WRAPPER/made" ]; then
  echo "mktemp: refused by the test" >&2
  exit 1
fi
: >"$MKTEMP_WRAPPER/made"
exec "$(command -v mktemp)" "\$@"
EOF
chmod +x "$MKTEMP_WRAPPER/mktemp"
TMPDIR="$FIXTURE/tmp for the errors" PATH="$MKTEMP_WRAPPER:$PATH" run_subject "$CLONE" main
expect_failed "FAILED cannot create a temporary file for the deployer's errors: mktemp: refused by the test" \
  "no file for the errors"
expect_nothing_run "no file for the errors"
[ -z "$(ls -A "$FIXTURE/tmp for the errors")" ] ||
  fail "no file for the errors: temporary copy left behind: $(ls -A "$FIXTURE/tmp for the errors")"

# A failing deploy is reported with its exit code.
begin_case 'deploy failed'
new_clone
export FAKE_DEPLOY_EXIT=7
run_subject "$CLONE" main
unset FAKE_DEPLOY_EXIT
expect_status 1 "deploy failed"
expect_first_line "UP_TO_DATE $(head_of "$CLONE" HEAD)" "deploy failed"
[ "$(printf '%s\n' "$OUTPUT" | tail -n 1)" = "DEPLOY_FAILED 7" ] || fail "deploy failed: missing DEPLOY_FAILED 7: $OUTPUT"
expect_deployed "deploy failed"

# With no release tag on either side there is no release to cross, so the update proceeds.
begin_case 'no tag'
new_clone
publish "untagged change"
run_subject "$CLONE" main
expect_status 0 "no tag"
grep -q "^UPDATED " <<<"$OUTPUT" || fail "no tag: expected UPDATED: $OUTPUT"
if grep -q "CROSSED" <<<"$OUTPUT"; then fail "no tag: crossing reported"; fi
expect_no_stderr "no tag"
expect_deployed "no tag"

# A release tag on local main only cannot be crossed, so the gate passes and the diverged main stops the update.
begin_case 'local tag only'
new_clone
git_quiet -C "$CLONE" commit --allow-empty -m "local release"
git_quiet -C "$CLONE" tag v0.9.0
publish "upstream after local release"
run_subject "$CLONE" main
expect_status 1 "local tag only"
expect_first_line "NOT_FAST_FORWARD" "local tag only"
expect_not_deployed "local tag only"

echo "update.sh main channel tests passed"
