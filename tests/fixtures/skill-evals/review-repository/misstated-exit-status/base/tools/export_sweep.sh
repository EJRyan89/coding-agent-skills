#!/usr/bin/env bash
# Delete report exports past their retention.
set -euo pipefail

retention_days=30
if [ "$#" -lt 1 ] || [ ! -d "$1" ]; then
  echo "usage: export_sweep.sh <folder> [--dry-run]" >&2
  exit 2
fi
folder="$1"
dry_run="${2:-}"

find "$folder" -maxdepth 1 -name '*.csv' -mtime "+$retention_days" -print0 |
  while IFS= read -r -d '' path; do
    if [ "$dry_run" = "--dry-run" ]; then
      echo "WOULD_DELETE $path"
    else
      rm -- "$path"
      echo "DELETED $path"
    fi
  done
