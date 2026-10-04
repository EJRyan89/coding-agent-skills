#!/usr/bin/env bash
set -uo pipefail

# Remove one worktree only through Git, then delete its branch if fully merged.
# Exit 0: worktree and branch removed. Exit 3: worktree removed, branch kept
# because it is not fully merged. Exit 1: Git refused to remove the worktree
# (locked, dirty, or otherwise protected); nothing was deleted. Exit 2: usage.

if [ "$#" -ne 3 ]; then
  echo "Usage: remove_worktree.sh <repository-root> <worktree-path> <branch>" >&2
  exit 2
fi

repository_root="$1"
worktree_path="$2"
branch="$3"

if ! output=$(git -C "$repository_root" worktree remove "$worktree_path" 2>&1); then
  printf 'Git refused to remove worktree %s: %s\n' "$worktree_path" "$output" >&2
  exit 1
fi

if ! git -C "$repository_root" branch -d "$branch" >/dev/null 2>&1; then
  exit 3
fi
