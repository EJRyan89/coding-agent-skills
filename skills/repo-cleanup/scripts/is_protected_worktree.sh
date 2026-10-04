#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "Usage: is_protected_worktree.sh <branch> <worktree-path>" >&2
  exit 2
fi

branch="$1"
worktree_path="${2//\\//}"

case "$branch" in
  release/*)
    exit 0
    ;;
esac

case "/${worktree_path#/}/" in
  */release/*)
    exit 0
    ;;
  *)
    exit 1
    ;;
esac
