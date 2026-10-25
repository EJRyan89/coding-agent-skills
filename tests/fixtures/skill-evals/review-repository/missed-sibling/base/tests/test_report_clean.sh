#!/usr/bin/env bash
# Regression suite for tools/report_clean.sh: a new report and a new log are kept.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
source "$here/../tools/report_clean.sh"

folder=$(mktemp -d)
trap 'rm -rf "$folder"' EXIT
touch "$folder/today.csv" "$folder/today.log"

remove_old_reports "$folder"
remove_old_logs "$folder"
[[ -f "$folder/today.csv" && -f "$folder/today.log" ]] || { echo "FAIL a new report and log are kept"; exit 1; }
echo "PASS report_clean"
