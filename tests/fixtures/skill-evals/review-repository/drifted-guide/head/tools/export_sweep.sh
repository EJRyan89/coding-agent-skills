#!/usr/bin/env bash
# Delete report exports past their retention.
set -euo pipefail

retention_days=7
folder="${1:?usage: export_sweep.sh <folder> [--dry-run]}"
dry_run="${2:-}"

find "$folder" -maxdepth 1 -name '*.csv' -mtime "+$retention_days" -print0 |
  while IFS= read -r -d '' path; do
    if [ "$dry_run" = "--dry-run" ]; then
      echo "SKIPPED $path (dry run)"
    else
      rm -- "$path"
      echo "DELETED $path"
    fi
  done
