#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
SUBJECT="$SCRIPT_DIR/update.sh"
FIXTURE=$(mktemp -d "${TMPDIR:-/tmp}/update-skills-test.XXXXXX")
trap 'rm -rf -- "$FIXTURE"' EXIT

git_quiet() { git -c user.name=Test -c user.email=test@example.invalid "$@" >/dev/null 2>&1; }
fail() { echo "FAIL: $*" >&2; exit 1; }

# The upstream carries a stand-in deploy.py that records its arguments and exits with $FAKE_DEPLOY_EXIT.
UPSTREAM="$FIXTURE/upstream.git"
SEED="$FIXTURE/seed"
git_quiet init --bare -b main "$UPSTREAM"
git_quiet init -b main "$SEED"
cat >"$SEED/deploy.py" <<'EOF'
import os, sys
with open(os.environ["FAKE_DEPLOY_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join(sys.argv[1:]) + "\n")
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
expect_deployed() { [ "$(cat "$FAKE_DEPLOY_LOG")" = "--all" ] || fail "$1: deploy.py was not run once with --all"; }
expect_not_deployed() { [ ! -s "$FAKE_DEPLOY_LOG" ] || fail "$1: deploy.py ran"; }
head_of() { git -C "$1" rev-parse --short "$2"; }

# Usage errors.
run_subject
expect_status 2 "no argument"
run_subject "$FIXTURE"
expect_status 2 "directory without deploy.py"

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

# A clean clone on another branch is switched to main before the fast-forward.
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
expect_status 3 "dirty"
expect_first_line "DIRTY" "dirty"
printf '%s\n' "$OUTPUT" | grep -q "notes.txt" || fail "dirty: changed file not listed"
[ "$(git -C "$CLONE" rev-parse origin/main)" = "$BEFORE" ] || fail "dirty: fetched anyway"
expect_not_deployed "dirty"

# A main with its own commits is never merged, rebased, or reset.
new_clone
git_quiet -C "$CLONE" commit --allow-empty -m "local work"
LOCAL=$(git -C "$CLONE" rev-parse HEAD)
publish "fifth change"
run_subject "$CLONE"
expect_status 5 "diverged"
expect_first_line "NOT_FAST_FORWARD" "diverged"
[ "$(git -C "$CLONE" rev-parse HEAD)" = "$LOCAL" ] || fail "diverged: main moved"
expect_not_deployed "diverged"

# An unreachable origin is reported without deploying.
new_clone
git -C "$CLONE" remote set-url origin "$FIXTURE/missing.git"
run_subject "$CLONE"
expect_status 4 "fetch failed"
expect_first_line "FETCH_FAILED" "fetch failed"
expect_not_deployed "fetch failed"

# A failing deploy is reported with its exit code.
new_clone
export FAKE_DEPLOY_EXIT=7
run_subject "$CLONE"
unset FAKE_DEPLOY_EXIT
expect_status 1 "deploy failed"
[ "$(printf '%s\n' "$OUTPUT" | tail -n 1)" = "DEPLOY_FAILED 7" ] || fail "deploy failed: missing DEPLOY_FAILED 7: $OUTPUT"
expect_deployed "deploy failed"

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
