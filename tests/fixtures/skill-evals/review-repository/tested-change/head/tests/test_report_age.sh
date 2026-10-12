#!/usr/bin/env bash
# Regression suite for tools/report_age.sh: a new report is a day old at most, and a missing one is refused.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
source "$here/../tools/report_age.sh"

folder=$(mktemp -d)
trap 'rm -rf "$folder"' EXIT
touch "$folder/today.csv"

[[ "$(days_old "$folder/today.csv")" == 0 ]] || { echo "FAIL a new report is 0 days old"; exit 1; }
status=0
days_old "$folder/absent.csv" 2>/dev/null || status=$?
[[ "$status" == 2 ]] || { echo "FAIL a missing report exits 2, not $status"; exit 1; }
echo "PASS report_age"
