#!/usr/bin/env bash
set -Eeuo pipefail

# update.sh's `# Exit:` line held to the codes the script exits with and the codes its SKILL.md names, and update.sh run
# under the faults the fault corpus applies to a Python entry point: every git command exiting 128, a folder in
# deploy.py's place, and a clone that is missing. Each run exits with a code the line names, and a run that exits 1
# prints a status line the script's header names first.
# shellcheck source=skills/update-coding-agent-skills/scripts/test_update_fixture.sh
source "$(dirname "$0")/test_update_fixture.sh"

# The codes the `# Exit:` line names: the number that starts each of its `;`-separated entries.
begin_case 'exit line'
EXIT_LINE=$(grep -m 1 '^# Exit: ' "$SUBJECT") || fail "exit line: update.sh has no '# Exit:' line"
TABLE=()
IFS=';' read -r -a ENTRIES <<<"${EXIT_LINE#\# Exit: }"
for ENTRY in "${ENTRIES[@]}"; do
  read -r CODE _ <<<"$ENTRY"
  [[ "$CODE" =~ ^[0-9]+$ ]] || fail "exit line: an entry does not start with a code: '$ENTRY'"
  TABLE+=("$CODE")
done
TABLE_CODES=$(printf '%s\n' "${TABLE[@]}" | sort -u)
in_table() { grep -qx -e "$1" <<<"$TABLE_CODES"; }

# Every exit the script takes is a literal code, and the script exits with each code the line names: 0 by falling off
# its end after DEPLOYED, every other code through an `exit` command, one where a command starts on a line that is no
# comment. A bare `exit` reads as an empty code, which fails.
EXITS=$(grep -v -E '^[[:space:]]*#' "$SUBJECT" |
  grep -o -E '(^|[;{(]|&&|\|\||then|else|do)[[:space:]]*exit([[:space:]]+[^[:space:];}]+)?([[:space:];}]|$)' |
  sed -E 's/^.*exit[[:space:]]*//; s/[[:space:];}]*$//' | sort -u)
while IFS= read -r CODE; do
  [[ "$CODE" =~ ^[0-9]+$ ]] || fail "exit line: update.sh exits with '$CODE', which no table code can name"
  in_table "$CODE" || fail "exit line: update.sh exits $CODE, which its '# Exit:' line does not name: $EXIT_LINE"
done <<<"$EXITS"
[ "$(tail -n 1 "$SUBJECT")" = 'echo "DEPLOYED"' ] || fail "exit line: update.sh no longer ends at DEPLOYED, its exit 0"
for CODE in "${TABLE[@]}"; do
  [ "$CODE" = 0 ] || grep -qx -e "$CODE" <<<"$EXITS" ||
    fail "exit line: the '# Exit:' line names $CODE, which update.sh never exits with"
done

# SKILL.md names only codes the line names.
SKILL_MD="$SCRIPT_DIR/../SKILL.md"
NAMED=$(grep -o -E 'exit (code |status )?[0-9]+' "$SKILL_MD" | grep -o -E '[0-9]+$' | sort -u || true)
[ -n "$NAMED" ] || fail "exit line: SKILL.md names no exit code, so this check reads nothing"
while IFS= read -r CODE; do
  in_table "$CODE" || fail "exit line: SKILL.md names exit $CODE, which update.sh's '# Exit:' line does not"
done <<<"$NAMED"

# The status words update.sh's header names, each at the start of an entry of its status list.
STATUSES=$(sed -n '/^# Prints one status line first/,/^# Exit: /p' "$SUBJECT" |
  grep -o -E '^#   ([A-Z][A-Z_]+|UP_TO_DATE|UPDATED)' | sed -E 's/^#   //' | sort -u)
grep -qx FAILED <<<"$STATUSES" || fail "exit line: the header's status list was not read: $STATUSES"

# A run under a fault exits with a code the line names, never 2, which is usage alone, and its first line is a status
# the header names; nothing Bash itself reports, such as an unbound variable, reaches stderr.
expect_named_exit() {
  in_table "$status" || fail "$1: exited $status, which the '# Exit:' line does not name: $OUTPUT"
  [ "$status" -ne 2 ] || fail "$1: a fault exited 2, the usage code: $(cat "$FIXTURE/stderr")"
  [ "$status" -ne 0 ] || fail "$1: a fault exited 0: $OUTPUT"
  local first=${OUTPUT%%$'\n'*}
  grep -qx -e "${first%% *}" <<<"$STATUSES" || fail "$1: first line '$first' is no status the header names"
  if grep -q -E 'unbound variable|command not found|syntax error' "$FIXTURE/stderr"; then
    fail "$1: Bash reported an error of the script's own: $(cat "$FIXTURE/stderr")"
  fi
}

# Every git command exits 128 with the reason git gives for a broken checkout.
begin_case 'git exits 128'
new_clone
BROKEN_GIT="$FIXTURE/broken git"
mkdir -p "$BROKEN_GIT"
cat >"$BROKEN_GIT/git" <<'EOF'
#!/usr/bin/env bash
echo "fatal: not a git repository (or any of the parent directories): .git" >&2
exit 128
EOF
chmod +x "$BROKEN_GIT/git"
for CHANNEL in main release; do
  PATH="$BROKEN_GIT:$PATH" run_subject "$CLONE" "$CHANNEL"
  expect_named_exit "git exits 128 on $CHANNEL"
  expect_not_deployed "git exits 128 on $CHANNEL"
done

# A folder in the place of deploy.py, the one file update.sh reads from the clone before git.
begin_case 'directory in place of deploy.py'
new_clone
rm -f -- "$CLONE/deploy.py"
mkdir -p "$CLONE/deploy.py"
for CHANNEL in main release; do
  run_subject "$CLONE" "$CHANNEL"
  expect_named_exit "directory in place of deploy.py on $CHANNEL"
  expect_not_deployed "directory in place of deploy.py on $CHANNEL"
done

# A clone that is not there.
begin_case 'missing clone'
for CHANNEL in main release; do
  run_subject "$FIXTURE/no such clone" "$CHANNEL"
  expect_named_exit "missing clone on $CHANNEL"
done

echo "update.sh exit table and fault tests passed"
