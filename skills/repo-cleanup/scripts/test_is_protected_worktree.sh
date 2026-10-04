#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
SUBJECT="$SCRIPT_DIR/is_protected_worktree.sh"

assert_protected() {
  local branch="$1" path="$2"
  if ! bash "$SUBJECT" "$branch" "$path"; then
    echo "FAIL: Expected protected worktree: branch=$branch path=$path" >&2
    return 1
  fi
}

assert_unprotected() {
  local branch="$1" path="$2" status
  set +e
  bash "$SUBJECT" "$branch" "$path"
  status=$?
  set -e
  if [ "$status" -ne 1 ]; then
    echo "FAIL: Expected unprotected worktree (status 1): branch=$branch path=$path status=$status" >&2
    return 1
  fi
}

assert_protected "release/4.10" "C:/GitHub/Worktrees/example/topic"
assert_protected "topic/fix" "C:/GitHub/Worktrees/example/release/4.10"
assert_protected "topic/fix" 'C:\GitHub\Worktrees\example\release\4.10'
assert_protected "topic/fix" "C:/release/example"
assert_unprotected "topic/release-notes" "C:/GitHub/Worktrees/example/topic"
assert_unprotected "topic/fix" "C:/GitHub/Worktrees/example/pre-release/topic"

set +e
bash "$SUBJECT" "topic/fix" >/dev/null 2>&1
usage_status=$?
set -e
if [ "$usage_status" -ne 2 ]; then
  echo "FAIL: Expected usage error 2, got $usage_status" >&2
  exit 1
fi

echo "is_protected_worktree.sh tests passed"
