#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
SUBJECT="$SCRIPT_DIR/update.sh"
FIXTURE=$(mktemp -d "${TMPDIR:-/tmp}/update-skills-test.XXXXXX")
trap 'rm -rf -- "$FIXTURE"' EXIT

# Isolate git from the shell that started the suite, as skill-core's git_test_base.py does for the Python suites: a
# home and global configuration of its own with the fixture identity, no hooks, and git's automatic maintenance off,
# no system configuration or attributes, no prompt or editor, and none of the shell's other GIT_* variables.
while IFS= read -r name; do
  case "$name" in
    GIT_EXEC_PATH | GIT_BASH) ;;
    GIT_*) unset "$name" ;;
  esac
done < <(compgen -e)
unset XDG_CONFIG_HOME
export HOME="$FIXTURE/home"
export GIT_CONFIG_GLOBAL="$HOME/.gitconfig" GIT_CONFIG_NOSYSTEM=1 GIT_ATTR_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 GIT_EDITOR=:
mkdir -p "$HOME/no-hooks"
git config --file "$GIT_CONFIG_GLOBAL" user.name Fixture
git config --file "$GIT_CONFIG_GLOBAL" user.email fixture@example.invalid
git config --file "$GIT_CONFIG_GLOBAL" core.hooksPath "$HOME/no-hooks"
git config --file "$GIT_CONFIG_GLOBAL" maintenance.auto false
git config --file "$GIT_CONFIG_GLOBAL" maintenance.autoDetach false
git config --file "$GIT_CONFIG_GLOBAL" gc.auto 0
git config --file "$GIT_CONFIG_GLOBAL" gc.autoDetach false

# A setup command runs through git_quiet, which keeps its output off the suite's streams while it succeeds. One that fails
# prints the command and the stderr it saved, as fail does for an expectation, and the suite exits with its status.
git_quiet() {
  local status=0
  git -c user.name=Test -c user.email=test@example.invalid "$@" >/dev/null 2>"$FIXTURE/git-quiet.stderr" || status=$?
  if [ "$status" -ne 0 ]; then
    printf 'FAIL: setup command exited %s: git %s\n%s\n' "$status" "$*" "$(<"$FIXTURE/git-quiet.stderr")" >&2
    exit "$status"
  fi
}
fail() { echo "FAIL: $*" >&2; exit 1; }

# The upstream carries a stand-in deploy.py that records its arguments and exits with $FAKE_DEPLOY_EXIT. Its check
# prints $FAKE_CHECK_OUTPUT, by default the line a deployment with no recorded commit gets, and exits with
# $FAKE_CHECK_EXIT.
UPSTREAM="$FIXTURE/upstream.git"
SEED="$FIXTURE/seed"
git_quiet init --bare -b main "$UPSTREAM"
git_quiet init -b main "$SEED"
cat >"$SEED/deploy.py" <<'EOF'
import os, sys
with open(os.environ["FAKE_DEPLOY_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join(sys.argv[1:]) + "\n")
if sys.argv[1:] == ["check"]:
    print(os.environ.get("FAKE_CHECK_OUTPUT", "Deployed commit: unknown"))
    raise SystemExit(int(os.environ.get("FAKE_CHECK_EXIT", "0")))
if os.environ.get("FAKE_DEPLOY_MOVE"):
    os.rename(os.environ["FAKE_DEPLOY_MOVE"], os.environ["FAKE_DEPLOY_MOVE"] + ".bak")
print("fake deploy ran")
raise SystemExit(int(os.environ.get("FAKE_DEPLOY_EXIT", "0")))
EOF
printf '__pycache__/\n' >"$SEED/.gitignore"
git_quiet -C "$SEED" add deploy.py .gitignore
git_quiet -C "$SEED" commit -m initial
git_quiet -C "$SEED" remote add origin "$UPSTREAM"
git_quiet -C "$SEED" push origin main

# A setup command that fails names itself and what git printed, and ends the suite with its exit status; one that
# succeeds prints nothing.
MISSING="$FIXTURE/missing repository"
set +e
(git_quiet -C "$MISSING" status) 2>"$FIXTURE/stderr"
status=$?
set -e
[ "$status" -eq 128 ] || fail "failed setup command: expected exit 128, got $status: $(<"$FIXTURE/stderr")"
[ "$(head -n 1 "$FIXTURE/stderr")" = "FAIL: setup command exited 128: git -C $MISSING status" ] ||
  fail "failed setup command: command not named: $(<"$FIXTURE/stderr")"
# Git names the folder in its own form of the path, a drive-letter one on Windows, so only the folder's name is matched.
grep -q "missing repository" <(tail -n +2 "$FIXTURE/stderr") || fail "failed setup command: git's stderr not shown: $(<"$FIXTURE/stderr")"
(git_quiet -C "$SEED" status) 2>"$FIXTURE/stderr"
[ ! -s "$FIXTURE/stderr" ] || fail "succeeding setup command: wrote to stderr: $(<"$FIXTURE/stderr")"

publish() {
  printf '%s\n' "$1" >>"$SEED/notes.txt"
  git_quiet -C "$SEED" add notes.txt
  git_quiet -C "$SEED" commit -m "$1"
  git_quiet -C "$SEED" push origin main
}

case_number=0
new_clone() {
  case_number=$((case_number + 1))
  CLONE="$FIXTURE/clone $case_number (dev)"
  git_quiet clone "$UPSTREAM" "$CLONE"
  git -C "$CLONE" config user.name Test
  git -C "$CLONE" config user.email test@example.invalid
}

run_subject() {
  export FAKE_DEPLOY_LOG="$FIXTURE/deploy-$case_number.log"
  : >"$FAKE_DEPLOY_LOG"
  set +e
  OUTPUT=$(bash "$SUBJECT" "$@" 2>"$FIXTURE/stderr")
  status=$?
  set -e
}

expect_status() { [ "$status" -eq "$1" ] || fail "$2: expected exit $1, got $status: $OUTPUT $(cat "$FIXTURE/stderr")"; }
expect_first_line() { [ "$(printf '%s\n' "$OUTPUT" | head -n 1)" = "$1" ] || fail "$2: expected '$1', got: $OUTPUT"; }
expect_no_stderr() { [ ! -s "$FIXTURE/stderr" ] || fail "$1: wrote to stderr: $(cat "$FIXTURE/stderr")"; }
# A named failure: exit 1, one stdout line starting with the given FAILED text, nothing on stderr.
expect_failed() {
  expect_status 1 "$2"
  expect_no_stderr "$2"
  [ "$(printf '%s\n' "$OUTPUT" | wc -l | tr -d ' ')" = "1" ] || fail "$2: expected one line, got: $OUTPUT"
  case "$OUTPUT" in
    "$1"*) ;;
    *) fail "$2: expected a line starting '$1', got: $OUTPUT" ;;
  esac
}
# The pin is read with check before the fetch; the deployment is the one run with --all, and it comes last. Its
# arguments are --all alone unless the second argument names them.
expect_deployed() {
  local arguments=${2:---all}
  if [ "$(grep -c -e "^--all" "$FAKE_DEPLOY_LOG")" != "1" ] || [ "$(tail -n 1 "$FAKE_DEPLOY_LOG")" != "$arguments" ]; then
    fail "$1: deploy.py was not run once with $arguments: $(cat "$FAKE_DEPLOY_LOG")"
  fi
}
expect_not_deployed() { ! grep -q -e "^--all" "$FAKE_DEPLOY_LOG" || fail "$1: deploy.py ran"; }
expect_nothing_run() { [ ! -s "$FAKE_DEPLOY_LOG" ] || fail "$1: deploy.py ran: $(cat "$FAKE_DEPLOY_LOG")"; }
head_of() { git -C "$1" rev-parse --short "$2"; }

# Usage errors: the wrong number of arguments, on stderr with exit 2.
run_subject
expect_status 2 "no argument"
[ -z "$OUTPUT" ] || fail "no argument: usage went to stdout: $OUTPUT"
grep -q "^usage: update.sh" "$FIXTURE/stderr" || fail "no argument: usage text missing"
run_subject "$FIXTURE" --cross-major extra
expect_status 2 "an extra argument"
run_subject "$FIXTURE" --unpin --unpin
expect_status 2 "a repeated option"
run_subject "$FIXTURE" --cross-major --unpin extra
expect_status 2 "four arguments"

# A path that is not a clone is a named failure, not a usage error.
run_subject "$FIXTURE"
expect_failed "FAILED no deploy.py in $FIXTURE" "directory without deploy.py"
PLAIN="$FIXTURE/plain dir (no git)"
mkdir -p "$PLAIN"
cp "$SEED/deploy.py" "$PLAIN/deploy.py"
export GIT_CEILING_DIRECTORIES="$FIXTURE"
run_subject "$PLAIN"
unset GIT_CEILING_DIRECTORIES
expect_failed "FAILED not a git repository: $PLAIN" "not a repository"
expect_not_deployed "not a repository"

# An up-to-date clone deploys without moving, even when untracked files are present.
new_clone
printf 'scratch\n' >"$CLONE/untracked.txt"
run_subject "$CLONE"
expect_status 0 "up to date"
expect_first_line "UP_TO_DATE $(head_of "$CLONE" HEAD)" "up to date"
expect_deployed "up to date"
printf '%s\n' "$OUTPUT" | grep -qx "fake deploy ran" || fail "up to date: deploy output was not shown"
[ "$(printf '%s\n' "$OUTPUT" | tail -n 1)" = "DEPLOYED" ] || fail "up to date: missing DEPLOYED"

# A clone behind origin is fast-forwarded, lists the pulled commits, then deploys.
new_clone
BEFORE=$(head_of "$CLONE" HEAD)
publish "first change"
publish "second change"
run_subject "$CLONE"
expect_status 0 "behind"
AFTER=$(head_of "$CLONE" HEAD)
[ "$AFTER" = "$(head_of "$SEED" HEAD)" ] || fail "behind: clone was not fast-forwarded"
expect_first_line "UPDATED $BEFORE..$AFTER" "behind"
printf '%s\n' "$OUTPUT" | grep -q "first change" || fail "behind: pulled commit missing"
printf '%s\n' "$OUTPUT" | grep -q "second change" || fail "behind: pulled commit missing"
expect_deployed "behind"

# A clean clone on another branch is switched to main before the fast-forward, and stays there.
new_clone
git_quiet -C "$CLONE" checkout -b topic
publish "third change"
run_subject "$CLONE"
expect_status 0 "other branch"
[ "$(git -C "$CLONE" branch --show-current)" = "main" ] || fail "other branch: not switched to main"
[ "$(head_of "$CLONE" HEAD)" = "$(head_of "$SEED" HEAD)" ] || fail "other branch: main not fast-forwarded"
expect_deployed "other branch"

# Uncommitted tracked changes stop everything before the fetch.
new_clone
BEFORE=$(git -C "$CLONE" rev-parse origin/main)
printf 'edit\n' >>"$CLONE/notes.txt"
publish "fourth change"
run_subject "$CLONE"
expect_status 1 "dirty"
expect_first_line "DIRTY" "dirty"
expect_no_stderr "dirty"
printf '%s\n' "$OUTPUT" | grep -q "notes.txt" || fail "dirty: changed file not listed"
[ "$(git -C "$CLONE" rev-parse origin/main)" = "$BEFORE" ] || fail "dirty: fetched anyway"
expect_nothing_run "dirty"

# A main with its own commits is never merged, rebased, or reset.
new_clone
git_quiet -C "$CLONE" commit --allow-empty -m "local work"
LOCAL=$(git -C "$CLONE" rev-parse HEAD)
publish "fifth change"
run_subject "$CLONE"
expect_status 1 "diverged"
expect_first_line "NOT_FAST_FORWARD" "diverged"
expect_no_stderr "diverged"
[ "$(git -C "$CLONE" rev-parse HEAD)" = "$LOCAL" ] || fail "diverged: main moved"
printf '%s\n' "$OUTPUT" | grep -q "local work" || fail "diverged: local commit not listed: $OUTPUT"
expect_not_deployed "diverged"

# A diverged main stops before any checkout, so a clone on another branch stays on it.
new_clone
git_quiet -C "$CLONE" commit --allow-empty -m "local work"
LOCAL=$(git -C "$CLONE" rev-parse main)
git_quiet -C "$CLONE" checkout -b topic
publish "sixth change"
run_subject "$CLONE"
expect_status 1 "diverged on topic"
expect_first_line "NOT_FAST_FORWARD" "diverged on topic"
expect_no_stderr "diverged on topic"
[ "$(git -C "$CLONE" branch --show-current)" = "topic" ] || fail "diverged on topic: branch switched"
[ "$(git -C "$CLONE" rev-parse main)" = "$LOCAL" ] || fail "diverged on topic: main moved"
expect_not_deployed "diverged on topic"

# A fast-forward Git refuses after the switch, here over an untracked file the update adds, switches back.
new_clone
git_quiet -C "$CLONE" checkout -b topic
BEFORE=$(git -C "$CLONE" rev-parse main)
printf 'added\n' >"$SEED/added.txt"
git_quiet -C "$SEED" add added.txt
git_quiet -C "$SEED" commit -m "add a file"
git_quiet -C "$SEED" push origin main
printf 'untracked copy\n' >"$CLONE/added.txt"
run_subject "$CLONE"
expect_status 1 "refused on topic"
expect_first_line "NOT_FAST_FORWARD" "refused on topic"
expect_no_stderr "refused on topic"
[ "$(git -C "$CLONE" branch --show-current)" = "topic" ] || fail "refused on topic: not switched back"
[ "$(git -C "$CLONE" rev-parse main)" = "$BEFORE" ] || fail "refused on topic: main moved"
[ "$(cat "$CLONE/added.txt")" = "untracked copy" ] || fail "refused on topic: untracked file overwritten"
expect_not_deployed "refused on topic"

# A detached HEAD is returned to the same commit.
git_quiet -C "$CLONE" checkout --detach topic
DETACHED=$(git -C "$CLONE" rev-parse HEAD)
run_subject "$CLONE"
expect_status 1 "refused detached"
expect_first_line "NOT_FAST_FORWARD" "refused detached"
[ -z "$(git -C "$CLONE" branch --show-current)" ] || fail "refused detached: HEAD not detached"
[ "$(git -C "$CLONE" rev-parse HEAD)" = "$DETACHED" ] || fail "refused detached: not switched back"
expect_not_deployed "refused detached"

# When the switch back fails too, the output says the clone is still on main. A Git wrapper refuses that switch.
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
PATH="$WRAPPER:$PATH" run_subject "$CLONE"
expect_status 1 "switch back refused"
expect_first_line "NOT_FAST_FORWARD" "switch back refused"
printf '%s\n' "$OUTPUT" | grep -qx "still on main: cannot switch back to topic: error: switch refused by the test" ||
  fail "switch back refused: no still-on-main line: $OUTPUT"
[ "$(git -C "$CLONE" branch --show-current)" = "main" ] || fail "switch back refused: expected main"
expect_not_deployed "switch back refused"

# A branch that cannot switch to main, here over an untracked file main tracks, is never forced.
new_clone
git_quiet -C "$CLONE" checkout -b topic
git_quiet -C "$CLONE" rm --quiet notes.txt
git_quiet -C "$CLONE" commit -m "drop notes"
printf 'untracked copy\n' >"$CLONE/notes.txt"
run_subject "$CLONE"
expect_status 1 "checkout failed"
expect_first_line "CHECKOUT_FAILED" "checkout failed"
expect_no_stderr "checkout failed"
[ "$(git -C "$CLONE" branch --show-current)" = "topic" ] || fail "checkout failed: branch switched"
[ "$(cat "$CLONE/notes.txt")" = "untracked copy" ] || fail "checkout failed: untracked file overwritten"
expect_not_deployed "checkout failed"

# An unreachable origin is reported without deploying.
new_clone
git -C "$CLONE" remote set-url origin "$FIXTURE/missing.git"
run_subject "$CLONE"
expect_status 1 "fetch failed"
expect_first_line "FETCH_FAILED" "fetch failed"
expect_no_stderr "fetch failed"
expect_not_deployed "fetch failed"

# A Git command that fails outright is a named failure.
new_clone
printf 'not an index' >"$CLONE/.git/index"
run_subject "$CLONE"
expect_failed "FAILED git status failed: " "status failed"
expect_not_deployed "status failed"

# A script that cannot make its temporary copy stops before any Git command.
new_clone
export FAKE_DEPLOY_LOG="$FIXTURE/deploy-$case_number.log"
: >"$FAKE_DEPLOY_LOG"
set +e
OUTPUT=$(TMPDIR="$FIXTURE/absent temporary directory" bash "$SUBJECT" "$CLONE" 2>"$FIXTURE/stderr")
status=$?
set -e
expect_failed "FAILED cannot create a temporary copy of update.sh: " "no temporary copy"
expect_not_deployed "no temporary copy"

# A failing deploy is reported with its exit code.
new_clone
export FAKE_DEPLOY_EXIT=7
run_subject "$CLONE"
unset FAKE_DEPLOY_EXIT
expect_status 1 "deploy failed"
expect_first_line "UP_TO_DATE $(head_of "$CLONE" HEAD)" "deploy failed"
[ "$(printf '%s\n' "$OUTPUT" | tail -n 1)" = "DEPLOY_FAILED 7" ] || fail "deploy failed: missing DEPLOY_FAILED 7: $OUTPUT"
expect_deployed "deploy failed"

# With no release tag on either side there is no release to cross, so the update proceeds.
new_clone
publish "untagged change"
run_subject "$CLONE"
expect_status 0 "no tag"
printf '%s\n' "$OUTPUT" | grep -q "^UPDATED " || fail "no tag: expected UPDATED: $OUTPUT"
if printf '%s\n' "$OUTPUT" | grep -q "CROSSED"; then fail "no tag: crossing reported"; fi
expect_no_stderr "no tag"
expect_deployed "no tag"

# A release tag on local main only cannot be crossed, so the gate passes and the diverged main stops the update.
new_clone
git_quiet -C "$CLONE" commit --allow-empty -m "local release"
git_quiet -C "$CLONE" tag v0.9.0
publish "upstream after local release"
run_subject "$CLONE"
expect_status 1 "local tag only"
expect_first_line "NOT_FAST_FORWARD" "local tag only"
expect_not_deployed "local tag only"

# Version tags are published upstream only, so the clone must fetch them to see either side's version.
tag_upstream() {
  git_quiet -C "$SEED" tag "$1"
  git_quiet -C "$SEED" push origin "$1"
}

# A release on origin/main stops a local main with no release tag, which counts as version zero.
new_clone
BEFORE=$(head_of "$CLONE" HEAD)
publish "first release"
tag_upstream v0.1.0
run_subject "$CLONE"
expect_status 1 "untagged"
expect_first_line "MAJOR_UPDATE untagged..v0.1.0" "untagged"
expect_no_stderr "untagged"
printf '%s\n' "$OUTPUT" | grep -q "first release" || fail "untagged: pending commit not listed"
[ "$(head_of "$CLONE" HEAD)" = "$BEFORE" ] || fail "untagged: main moved"
expect_not_deployed "untagged"
run_subject "$CLONE" --cross-major
expect_status 0 "untagged accepted"
printf '%s\n' "$OUTPUT" | grep -qx "CROSSED untagged..v0.1.0" || fail "untagged accepted: crossing not named: $OUTPUT"
expect_deployed "untagged accepted"

# A release with a higher breaking component stops before main moves, and lists what it would pull.
new_clone
BEFORE=$(head_of "$CLONE" HEAD)
publish "breaking change"
tag_upstream v0.2.0
run_subject "$CLONE"
expect_status 1 "major"
expect_first_line "MAJOR_UPDATE v0.1.0..v0.2.0" "major"
expect_no_stderr "major"
printf '%s\n' "$OUTPUT" | grep -q "breaking change" || fail "major: pending commit not listed"
[ "$(head_of "$CLONE" HEAD)" = "$BEFORE" ] || fail "major: main moved"
expect_not_deployed "major"

# With --cross-major the same update proceeds, names the crossing, and deploys.
run_subject "$CLONE" --cross-major
expect_status 0 "cross major"
AFTER=$(head_of "$CLONE" HEAD)
expect_first_line "UPDATED $BEFORE..$AFTER" "cross major"
printf '%s\n' "$OUTPUT" | grep -qx "CROSSED v0.1.0..v0.2.0" || fail "cross major: crossing not named: $OUTPUT"
expect_deployed "cross major"

# A patch release within the same breaking component needs no acceptance.
new_clone
publish "patch change"
tag_upstream v0.2.3
run_subject "$CLONE"
expect_status 0 "patch"
printf '%s\n' "$OUTPUT" | grep -q "^UPDATED " || fail "patch: expected UPDATED: $OUTPUT"
if printf '%s\n' "$OUTPUT" | grep -q "CROSSED"; then fail "patch: crossing reported"; fi
expect_deployed "patch"

# Leaving 0.x raises the major, so it stops too. A nearer tag that is not numeric is not a release.
new_clone
BEFORE=$(head_of "$CLONE" HEAD)
publish "first stable"
tag_upstream v1.0.0
publish "after first stable"
tag_upstream v2x.0.0
run_subject "$CLONE"
expect_status 1 "to 1.0.0"
expect_first_line "MAJOR_UPDATE v0.2.3..v1.0.0" "to 1.0.0"
expect_no_stderr "to 1.0.0"
expect_not_deployed "to 1.0.0"
run_subject "$CLONE" --cross-major
expect_status 0 "to 1.0.0 accepted"
expect_deployed "to 1.0.0 accepted"

# From 1.0.0 on, only the major component counts: a minor release passes, a major one stops.
new_clone
publish "minor stable"
tag_upstream v1.5.0
run_subject "$CLONE"
expect_status 0 "stable minor"
if printf '%s\n' "$OUTPUT" | grep -q "CROSSED"; then fail "stable minor: crossing reported"; fi
expect_deployed "stable minor"
new_clone
BEFORE=$(head_of "$CLONE" HEAD)
publish "second major"
tag_upstream v2.0.0
run_subject "$CLONE"
expect_status 1 "stable major"
expect_first_line "MAJOR_UPDATE v1.5.0..v2.0.0" "stable major"
[ "$(head_of "$CLONE" HEAD)" = "$BEFORE" ] || fail "stable major: main moved"
expect_not_deployed "stable major"

# A pre-release counts as its version: one of the next major stops, and its final release then passes.
run_subject "$CLONE" --cross-major
expect_status 0 "stable major accepted"
new_clone
publish "third major candidate"
tag_upstream v3.0.0-rc.1
run_subject "$CLONE"
expect_status 1 "pre-release"
expect_first_line "MAJOR_UPDATE v2.0.0..v3.0.0-rc.1" "pre-release"
expect_not_deployed "pre-release"
run_subject "$CLONE" --cross-major
expect_status 0 "pre-release accepted"
publish "third major"
tag_upstream v3.0.0
run_subject "$CLONE"
expect_status 0 "pre-release to release"
if printf '%s\n' "$OUTPUT" | grep -q "CROSSED"; then fail "pre-release to release: crossing reported"; fi
expect_deployed "pre-release to release"

# A deployment made with deploy.py --at stops the update before the fetch, naming the ref and the commit check reports.
PINNED_COMMIT=0123456789abcdef0123456789abcdef01234567
PINNED_CHECK="
Source: coding-agent-skills
Deployed commit: v1.0.0 ($PINNED_COMMIT), pinned with --at v1.0.0
Home: home"
new_clone
BEFORE=$(git -C "$CLONE" rev-parse HEAD)
FETCHED=$(git -C "$CLONE" rev-parse origin/main)
publish "after the pin"
FAKE_CHECK_OUTPUT=$PINNED_CHECK run_subject "$CLONE"
expect_status 1 "pinned"
[ "$OUTPUT" = "PINNED v1.0.0 $PINNED_COMMIT" ] || fail "pinned: expected only the PINNED line, got: $OUTPUT"
expect_no_stderr "pinned"
[ "$(git -C "$CLONE" rev-parse origin/main)" = "$FETCHED" ] || fail "pinned: fetched anyway"
[ "$(git -C "$CLONE" rev-parse HEAD)" = "$BEFORE" ] || fail "pinned: main moved"
[ "$(cat "$FAKE_DEPLOY_LOG")" = "check" ] || fail "pinned: expected only check to run: $(cat "$FAKE_DEPLOY_LOG")"

# A pin check names by its commit alone, when no tag describes it, is read the same way.
FAKE_CHECK_OUTPUT="Deployed commit: $PINNED_COMMIT, pinned with --at main~2" run_subject "$CLONE"
expect_status 1 "pinned untagged"
[ "$OUTPUT" = "PINNED main~2 $PINNED_COMMIT" ] || fail "pinned untagged: got: $OUTPUT"
expect_not_deployed "pinned untagged"

# With --unpin, before or after --cross-major, the update moves past the pin and deploys main, passing --unpin to the
# deployer, which refuses to replace a pin without it.
FAKE_CHECK_OUTPUT=$PINNED_CHECK run_subject "$CLONE" --unpin
expect_status 0 "unpin"
expect_first_line "UPDATED $(git -C "$CLONE" rev-parse --short "$BEFORE")..$(head_of "$CLONE" HEAD)" "unpin"
expect_deployed "unpin" "--all --unpin"
publish "after the unpin"
FAKE_CHECK_OUTPUT=$PINNED_CHECK run_subject "$CLONE" --cross-major --unpin
expect_status 0 "unpin after cross-major"
expect_deployed "unpin after cross-major" "--all --unpin"
publish "unpin first"
FAKE_CHECK_OUTPUT=$PINNED_CHECK run_subject "$CLONE" --unpin --cross-major
expect_status 0 "unpin before cross-major"
expect_deployed "unpin before cross-major" "--all --unpin"

# A deployment check names without a pin, or a check that fails, does not stop the update.
publish "unpinned change"
FAKE_CHECK_OUTPUT="Deployed commit: v1.0.0 ($PINNED_COMMIT)" run_subject "$CLONE"
expect_status 0 "not pinned"
printf '%s\n' "$OUTPUT" | grep -q "^UPDATED " || fail "not pinned: expected UPDATED: $OUTPUT"
expect_deployed "not pinned"
FAKE_CHECK_OUTPUT="" FAKE_CHECK_EXIT=1 run_subject "$CLONE"
expect_status 0 "check failed"
expect_first_line "UP_TO_DATE $(head_of "$CLONE" HEAD)" "check failed"
expect_no_stderr "check failed"
expect_deployed "check failed"

# An unknown option is a usage error.
run_subject "$CLONE" --force
expect_status 2 "unknown option"
expect_not_deployed "unknown option"

# A repository-selecting variable a shell or a git hook exported does not point the update at another clone.
new_clone
OTHER=$CLONE
OTHER_HEAD=$(head_of "$OTHER" HEAD)
new_clone
publish "change while GIT_DIR names another clone"
export GIT_DIR="$OTHER/.git" GIT_INDEX_FILE="$OTHER/.git/index" GIT_NAMESPACE="elsewhere"
run_subject "$CLONE"
unset GIT_DIR GIT_INDEX_FILE GIT_NAMESPACE
expect_status 0 "repository variables"
[ "$(head_of "$CLONE" HEAD)" = "$(head_of "$SEED" HEAD)" ] || fail "repository variables: the named clone did not move"
[ "$(head_of "$OTHER" HEAD)" = "$OTHER_HEAD" ] || fail "repository variables: the other clone moved"
expect_deployed "repository variables"

# Windows reads an environment name in any case, so git there takes git_dir as GIT_DIR; the update unsets each spelling.
new_clone
OTHER=$CLONE
OTHER_HEAD=$(head_of "$OTHER" HEAD)
new_clone
publish "change while git_dir names another clone"
export git_dir="$OTHER/.git" Git_Index_File="$OTHER/.git/index" Git_Namespace="elsewhere"
run_subject "$CLONE"
unset git_dir Git_Index_File Git_Namespace
expect_status 0 "repository variables in any case"
[ "$(head_of "$CLONE" HEAD)" = "$(head_of "$SEED" HEAD)" ] || fail "repository variables in any case: the named clone did not move"
[ "$(head_of "$OTHER" HEAD)" = "$OTHER_HEAD" ] || fail "repository variables in any case: the other clone moved"
expect_deployed "repository variables in any case"

# The deploy can back up the installed skill directory the script runs from, and the temporary copy is removed.
new_clone
INSTALLED="$FIXTURE/installed skill (copy)"
mkdir -p "$INSTALLED/scripts" "$FIXTURE/tmp"
cp "$SUBJECT" "$INSTALLED/scripts/update.sh"
export FAKE_DEPLOY_LOG="$FIXTURE/deploy-$case_number.log"
: >"$FAKE_DEPLOY_LOG"
set +e
OUTPUT=$(TMPDIR="$FIXTURE/tmp" FAKE_DEPLOY_MOVE="$INSTALLED" bash "$INSTALLED/scripts/update.sh" "$CLONE" 2>"$FIXTURE/stderr")
status=$?
set -e
expect_status 0 "self backup"
[ -d "$INSTALLED.bak" ] || fail "self backup: installed directory was not moved"
[ -z "$(ls -A "$FIXTURE/tmp")" ] || fail "self backup: temporary copy left behind: $(ls -A "$FIXTURE/tmp")"
expect_deployed "self backup"

echo "update.sh tests passed"
