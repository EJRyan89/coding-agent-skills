#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
TEST_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/repo-cleanup-test.XXXXXX")
trap 'rm -rf -- "$TEST_ROOT"' EXIT

make_repository() {
  local name="$1" branch="$2" repository
  repository="$TEST_ROOT/$name"
  git init --quiet --initial-branch="$branch" "$repository"
  git -C "$repository" config user.email "tests@example.invalid"
  git -C "$repository" config user.name "Repository Tests"
  git -C "$repository" commit --quiet --allow-empty -m initial
  printf '%s\n' "$repository"
}

assert_branch() {
  local expected="$1" repository="$2" actual
  actual=$(bash "$SCRIPT_DIR/resolve_default_branch.sh" "$repository")
  if [ "$actual" != "$expected" ]; then
    echo "FAIL: Expected branch '$expected', got '$actual'" >&2
    return 1
  fi
}

head_repo=$(make_repository head trunk)
git -C "$head_repo" update-ref refs/remotes/origin/trunk HEAD
git -C "$head_repo" symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/trunk
assert_branch trunk "$head_repo"

main_repo=$(make_repository main topic)
git -C "$main_repo" update-ref refs/remotes/origin/main HEAD
assert_branch main "$main_repo"

master_repo=$(make_repository master topic)
git -C "$master_repo" update-ref refs/remotes/origin/master HEAD
assert_branch master "$master_repo"

missing_repo=$(make_repository missing topic)
if bash "$SCRIPT_DIR/resolve_default_branch.sh" "$missing_repo" >/dev/null 2>&1; then
  echo "FAIL: Expected missing default branch to return 1" >&2
  exit 1
fi

set +e
bash "$SCRIPT_DIR/resolve_default_branch.sh" >/dev/null 2>&1
usage_status=$?
set -e
if [ "$usage_status" -ne 2 ]; then
  echo "FAIL: Expected usage error 2, got $usage_status" >&2
  exit 1
fi

echo "resolve_default_branch.sh tests passed"
