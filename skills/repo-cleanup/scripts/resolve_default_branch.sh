#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "Usage: resolve_default_branch.sh <repository-root>" >&2
  exit 2
fi

repository_root="$1"
default_branch=$(
  git -C "$repository_root" symbolic-ref refs/remotes/origin/HEAD 2>/dev/null |
    sed 's|refs/remotes/origin/||' |
    tr -d '\r'
) || true

if [ -z "$default_branch" ]; then
  if git -C "$repository_root" show-ref --verify --quiet refs/remotes/origin/main; then
    default_branch="main"
  elif git -C "$repository_root" show-ref --verify --quiet refs/remotes/origin/master; then
    default_branch="master"
  else
    exit 1
  fi
fi

printf '%s\n' "$default_branch"
