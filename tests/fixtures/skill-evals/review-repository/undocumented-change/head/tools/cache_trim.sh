#!/usr/bin/env bash
# Trim the report cache to its newest entries.
set -euo pipefail

keep=50
folder="${1:?usage: cache_trim.sh <folder>}"

find "$folder" -maxdepth 1 -name '*.cache' -printf '%T@ %p
' | sort -rn | tail -n "+$((keep + 1))" | cut -d' ' -f2- |
  while IFS= read -r path; do
    rm -f -- "$path"
  done
