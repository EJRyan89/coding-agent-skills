#!/usr/bin/env bash
# Print how many days old each report in a folder is.
set -euo pipefail

days_old() {
  local path="$1" now modified
  now=$(date +%s)
  modified=$(stat -c %Y -- "$path")
  echo $(((now - modified) / 86400))
}

main() {
  local folder="${1:?usage: report_age.sh <folder>}" path
  for path in "$folder"/*.csv; do
    echo "$(days_old "$path") $path"
  done
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
