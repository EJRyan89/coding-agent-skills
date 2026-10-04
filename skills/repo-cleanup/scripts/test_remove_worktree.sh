#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
SUBJECT="$SCRIPT_DIR/remove_worktree.sh"
FIXTURE=$(mktemp -d)
trap 'rm -rf "$FIXTURE"' EXIT

git_quiet() { git -c user.name=Test -c user.email=test@example.invalid "$@" >/dev/null 2>&1; }

REPO="$FIXTURE/repo"
git_quiet init -b main "$REPO"
git_quiet -C "$REPO" commit --allow-empty -m init

run_subject() {
  set +e
  bash "$SUBJECT" "$@" 2>"$FIXTURE/stderr"
  status=$?
  set -e
}

fail() { echo "FAIL: $*" >&2; exit 1; }

# A clean, merged worktree is removed with its branch.
git_quiet -C "$REPO" worktree add -b merged "$FIXTURE/merged"
run_subject "$REPO" "$FIXTURE/merged" merged
[ "$status" -eq 0 ] || fail "merged worktree: expected 0, got $status"
[ ! -e "$FIXTURE/merged" ] || fail "merged worktree directory remained"
! git -C "$REPO" rev-parse --verify --quiet refs/heads/merged >/dev/null || fail "merged branch remained"

# A locked worktree is preserved: no fallback deletion, branch untouched.
git_quiet -C "$REPO" worktree add -b locked "$FIXTURE/locked"
printf 'keep\n' > "$FIXTURE/locked/sentinel.txt"
git_quiet -C "$FIXTURE/locked" add sentinel.txt
git_quiet -C "$FIXTURE/locked" commit -m sentinel
git_quiet -C "$REPO" worktree lock "$FIXTURE/locked"
run_subject "$REPO" "$FIXTURE/locked" locked
[ "$status" -eq 1 ] || fail "locked worktree: expected 1, got $status"
[ -f "$FIXTURE/locked/sentinel.txt" ] || fail "locked worktree content was deleted"
git -C "$REPO" rev-parse --verify --quiet refs/heads/locked >/dev/null || fail "locked branch was deleted"
grep -q "Git refused to remove worktree" "$FIXTURE/stderr" || fail "locked refusal was not reported"

# A worktree with untracked files is preserved.
git_quiet -C "$REPO" worktree add -b untracked "$FIXTURE/untracked"
printf 'draft\n' > "$FIXTURE/untracked/draft.txt"
run_subject "$REPO" "$FIXTURE/untracked" untracked
[ "$status" -eq 1 ] || fail "untracked worktree: expected 1, got $status"
[ -f "$FIXTURE/untracked/draft.txt" ] || fail "untracked file was deleted"

# An unmerged branch keeps its branch after a clean removal.
git_quiet -C "$REPO" worktree add -b unmerged "$FIXTURE/unmerged"
git_quiet -C "$FIXTURE/unmerged" commit --allow-empty -m work
run_subject "$REPO" "$FIXTURE/unmerged" unmerged
[ "$status" -eq 3 ] || fail "unmerged worktree: expected 3, got $status"
[ ! -e "$FIXTURE/unmerged" ] || fail "unmerged worktree directory remained"
git -C "$REPO" rev-parse --verify --quiet refs/heads/unmerged >/dev/null || fail "unmerged branch was deleted"

run_subject "$REPO" "$FIXTURE/merged"
[ "$status" -eq 2 ] || fail "usage: expected 2, got $status"

echo "remove_worktree.sh tests passed"
