#!/usr/bin/env bash
# Remove the exported reports or their logs older than a week: report_clean.sh reports|logs <folder>.
set -euo pipefail

remove_old_reports() {
  local folder="$1"
  if [[ ! -d "$folder" ]]; then
    echo "MISSING $folder" >&2
    return 2
  fi
  find "$folder" -name '*.csv' -mtime +7 -delete
}

remove_old_logs() {
  local folder="$1"
  find "$folder" -name '*.log' -mtime +7 -delete
}

main() {
  local usage="usage: report_clean.sh reports|logs <folder>"
  local kind="${1:?$usage}" folder="${2:?$usage}"
  case "$kind" in
    reports) remove_old_reports "$folder" ;;
    logs) remove_old_logs "$folder" ;;
    *)
      echo "UNKNOWN $kind" >&2
      return 2
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
