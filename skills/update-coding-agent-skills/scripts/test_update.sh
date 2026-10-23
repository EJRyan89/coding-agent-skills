#!/usr/bin/env bash
set -Eeuo pipefail

# A shell ended by a signal it cannot trap, or by Windows rather than through its own exit, runs no EXIT trap and leaves
# only its status. So the suite runs its body in a child bash that traces every command it runs to a file, and when the
# body ends without its EXIT trap, this outer run names the exit and the last commands the body ran. A child run of the
# suite that sets UPDATE_TEST_BODY empty runs its body directly, untraced.
if [ -z "${UPDATE_TEST_BODY+set}" ]; then
  TRACE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/update-skills-trace.XXXXXX") || {
    printf 'FAIL: the suite could not make its trace folder\n' >&2
    exit 1
  }
  status=0
  UPDATE_TEST_BODY=$TRACE_DIR bash "$0" "$@" || status=$?
  if [ ! -e "$TRACE_DIR/ended" ]; then
    printf 'FAIL: the suite exited %s without its EXIT trap; the last commands it ran:\n' "$status" >&2
    tail -n 20 "$TRACE_DIR/trace" >&2 || true
  fi
  rm -rf -- "$TRACE_DIR"
  exit "$status"
fi

# The suite traces its own run on stderr before anything else runs: a START line, a CASE line as each case begins, and
# an END line from the EXIT trap on every exit, with its status and the case it ended in. A run that ends with none of
# them was never started or lost its output; one with START and no END was ended without its EXIT trap, and the outer
# run's FAIL line after them names its exit and the commands it ran last.
printf 'START test_update.sh %s\n' "$$" >&2
CASE="setup"
begin_case() {
  CASE=$1
  printf 'CASE %s\n' "$1" >&2
}
# A command that fails can print nothing, so an exit set -e makes would leave only its status. The ERR trap keeps the
# last command that failed, and an exit no FAIL line announced names its status, the case, and that command before the
# fixture is removed.
FAILED_COMMAND=""
REPORTED=""
FIXTURE=""
trap 'FAILED_COMMAND="line $LINENO: $BASH_COMMAND"' ERR
end_suite() {
  local status=$?
  if [ -n "${UPDATE_TEST_BODY:-}" ]; then
    : >"$UPDATE_TEST_BODY/ended"
  fi
  if [ "$status" -ne 0 ] && [ -z "$REPORTED" ]; then
    printf 'FAIL: the suite exited %s in case %s; the last command that failed: %s\n' "$status" "$CASE" \
      "${FAILED_COMMAND:-none}" >&2
  fi
  printf 'END test_update.sh %s: exit %s in case %s\n' "$$" "$status" "$CASE" >&2
  if [ -n "$FIXTURE" ]; then
    rm -rf -- "$FIXTURE"
  fi
}
trap end_suite EXIT
# A signal ends the suite through the EXIT trap with 128 and the signal's number, so its exit is named as well.
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
# The traced body writes each command, with its line, to the trace file the outer run reads.
if [ -n "${UPDATE_TEST_BODY:-}" ]; then
  exec {TRACE_FD}>"$UPDATE_TEST_BODY/trace"
  BASH_XTRACEFD=$TRACE_FD
  PS4='+ line $LINENO: '
  set -x
fi

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
SUBJECT="$SCRIPT_DIR/update.sh"
FIXTURE=$(mktemp -d "${TMPDIR:-/tmp}/update-skills-test.XXXXXX")

# Isolate git from the shell that started the suite, as isolated_git in the root pytest configuration does for the Python suites: a
# home and global configuration of its own with the fixture identity, no hooks, and git's automatic maintenance off,
# no system configuration or attributes, no prompt or editor, and none of the shell's other GIT_* variables. A name is
# matched in any case, as update.sh unsets it, because Windows reads a name so: git there takes git_dir as GIT_DIR.
unset_git_variables() {
  local name
  while IFS= read -r name; do
    case "${name^^}" in
      GIT_EXEC_PATH | GIT_BASH) ;;
      GIT_*) unset "$name" ;;
    esac
  done < <(compgen -e)
}
unset_git_variables
unset XDG_CONFIG_HOME
export HOME="$FIXTURE/home"
export GIT_CONFIG_GLOBAL="$HOME/.gitconfig" GIT_CONFIG_NOSYSTEM=1 GIT_ATTR_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 GIT_EDITOR=:
# A setup command runs through git_quiet, which keeps its output off the suite's streams while it succeeds. One that fails
# prints the command and the stderr it saved, as fail does for an expectation, and the suite exits with its status.
git_quiet() {
  local status=0
  git -c user.name=Test -c user.email=test@example.invalid "$@" >/dev/null 2>"$FIXTURE/git-quiet.stderr" || status=$?
  if [ "$status" -ne 0 ]; then
    printf 'FAIL: setup command exited %s: git %s\n%s\n' "$status" "$*" "$(<"$FIXTURE/git-quiet.stderr")" >&2
    REPORTED=1
    exit "$status"
  fi
}
fail() {
  echo "FAIL: $*" >&2
  REPORTED=1
  exit 1
}

# The global configuration is one plain write, as isolated_git makes it. Each git config call replaces the file by
# renaming a lock file onto it, which Windows refuses while another process holds the file open, such as a scanner
# reading what the call before wrote, and git config then exits 4. The hooks folder is named from the home, which git
# expands in its own form of the path.
mkdir -p "$HOME/no-hooks"
cat >"$GIT_CONFIG_GLOBAL" <<'EOF'
[user]
  name = Fixture
  email = fixture@example.invalid
[core]
  hooksPath = ~/no-hooks
[maintenance]
  auto = false
  autoDetach = false
[gc]
  auto = 0
  autoDetach = false
EOF

# The upstream carries a stand-in deploy.py that records its arguments, prints $FAKE_DEPLOY_STDERR to stderr when it is
# set, and exits with $FAKE_DEPLOY_EXIT. A deployment that succeeds records the commit it ran from, or the commit --at
# names and the ref as its pin, keeping a recorded pin's ref at its own commit unless --unpin is given, as the real one
# does, and check names that commit and the pin, or prints
# $FAKE_CHECK_OUTPUT when it is set, and exits with $FAKE_CHECK_EXIT. Like the real one, it refuses to replace a
# recorded pin without --unpin: with --at only a pin at another commit that is not the full name of the last release
# its commit holds, and without --at any pin.
UPSTREAM="$FIXTURE/upstream.git"
SEED="$FIXTURE/seed"
git_quiet init --bare -b main "$UPSTREAM"
git_quiet init -b main "$SEED"
cat >"$SEED/deploy.py" <<'EOF'
import os, re, subprocess, sys
here = os.path.dirname(os.path.abspath(__file__))
record = os.path.join(here, ".fake-deployed")
arguments = sys.argv[1:]
with open(os.environ["FAKE_DEPLOY_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join(arguments) + "\n")
commit, pin = None, None
if os.path.exists(record):
    with open(record, encoding="utf-8") as recorded:
        commit, _, pin = recorded.read().strip().partition("\n")
if arguments == ["check"]:
    if "FAKE_CHECK_OUTPUT" in os.environ:
        print(os.environ["FAKE_CHECK_OUTPUT"])
    elif commit:
        print("Deployed commit: " + commit + (", pinned with --at " + pin if pin else ""))
    else:
        print("Deployed commit: unknown")
    raise SystemExit(int(os.environ.get("FAKE_CHECK_EXIT", "0")))
if os.environ.get("FAKE_DEPLOY_MOVE"):
    os.rename(os.environ["FAKE_DEPLOY_MOVE"], os.environ["FAKE_DEPLOY_MOVE"] + ".bak")
print("fake deploy ran")
if "FAKE_DEPLOY_STDERR" in os.environ:
    print(os.environ["FAKE_DEPLOY_STDERR"], file=sys.stderr)
code = int(os.environ.get("FAKE_DEPLOY_EXIT", "0"))
at = arguments[arguments.index("--at") + 1] if "--at" in arguments else None
def git_lines(*command):
    return subprocess.run(["git", "-C", here, *command], capture_output=True, text=True, check=True).stdout.split()
def release_pin():
    releases = [tag for tag in git_lines("tag", "--merged", commit) if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag)]
    last = max(releases, key=lambda tag: tuple(int(part) for part in tag[1:].split(".")), default=None)
    return last is not None and pin == "refs/tags/" + last
named = git_lines("rev-parse", "--verify", (at or "HEAD") + "^{commit}")[0] if code == 0 else None
if code == 0 and pin and "--unpin" not in arguments and (at is None or (named != commit and not release_pin())):
    print(f"ERROR: Refusing to replace the deployment pinned with --at {pin} to {commit} without --unpin.", file=sys.stderr)
    code = 1
ref = pin if at and pin and named == commit and "--unpin" not in arguments else at
if code == 0:
    with open(record, "w", encoding="utf-8") as recorded:
        recorded.write(named + ("\n" + ref if ref else ""))
raise SystemExit(code)
EOF
printf '__pycache__/\n.fake-deployed\n' >"$SEED/.gitignore"
# The upstream carries ejryan89-skill-core's scripts where the repository keeps them, so a case can move them as a later
# release might. update.sh reads each release it compares with the copy deployed beside it instead.
mkdir -p "$SEED/skills/ejryan89-skill-core/scripts"
for MODULE in "$SCRIPT_DIR/../../ejryan89-skill-core/scripts/"*.py; do
  case "${MODULE##*/}" in
    test_*) ;;
    *) cp -- "$MODULE" "$SEED/skills/ejryan89-skill-core/scripts/" ;;
  esac
done
git_quiet -C "$SEED" add deploy.py .gitignore skills
git_quiet -C "$SEED" commit -m initial
git_quiet -C "$SEED" remote add origin "$UPSTREAM"
git_quiet -C "$SEED" push origin main

# A setup command that fails names itself and what git printed, and ends the suite with its exit status; one that
# succeeds prints nothing.
begin_case 'setup self-tests'
MISSING="$FIXTURE/missing repository"
set +e
(git_quiet -C "$MISSING" status) 2>"$FIXTURE/stderr"
status=$?
set -e
[ "$status" -eq 128 ] || fail "failed setup command: expected exit 128, got $status: $(<"$FIXTURE/stderr")"
[ "$(head -n 1 "$FIXTURE/stderr")" = "FAIL: setup command exited 128: git -C $MISSING status" ] ||
  fail "failed setup command: command not named: $(<"$FIXTURE/stderr")"
# Git names the folder in its own form of the path, a drive-letter one on Windows, so only the folder's name is matched.
grep -q "missing repository" <(tail -n +2 "$FIXTURE/stderr") || fail "failed setup command: git's stderr not shown: $(<"$FIXTURE/stderr")"
set +e
(git_quiet -C "$SEED" status) 2>"$FIXTURE/stderr"
status=$?
set -e
[ "$status" -eq 0 ] || fail "succeeding setup command: exited $status: $(<"$FIXTURE/stderr")"
[ ! -s "$FIXTURE/stderr" ] || fail "succeeding setup command: wrote to stderr: $(<"$FIXTURE/stderr")"

# A child run of this suite with UPDATE_TEST_SETUP_ONLY set stops here, after its setup and the self-tests above.
if [ -n "${UPDATE_TEST_SETUP_ONLY:-}" ]; then
  exit 0
fi

# The global configuration reads back as written, its hooks folder resolved from the home.
begin_case 'global configuration'
for SETTING in user.name=Fixture user.email=fixture@example.invalid maintenance.auto=false maintenance.autoDetach=false \
  gc.auto=0 gc.autoDetach=false; do
  [ "$(git config --file "$GIT_CONFIG_GLOBAL" --get "${SETTING%%=*}")" = "${SETTING#*=}" ] ||
    fail "global configuration: ${SETTING%%=*} is not ${SETTING#*=}"
done
[ "$(git config --file "$GIT_CONFIG_GLOBAL" --type=path --get core.hooksPath)" -ef "$HOME/no-hooks" ] ||
  fail "global configuration: core.hooksPath does not name $HOME/no-hooks"

# A child run of the setup meets a wrapper of the program the first argument names, which runs the third argument for
# a command whose arguments match the second and the program itself for any other. It runs its body directly, or, when
# the fourth argument is "traced", through the outer run, with its temporary folders in CHILD_TMP.
SETUP_WRAPPER="$FIXTURE/setup wrapper"
CHILD_TMP="$FIXTURE/child tmp"
run_setup_child() {
  rm -rf -- "$SETUP_WRAPPER" "$CHILD_TMP"
  mkdir -p "$SETUP_WRAPPER" "$CHILD_TMP"
  cat >"$SETUP_WRAPPER/$1" <<EOF
#!/usr/bin/env bash
case " \$* " in
  $2) $3 ;;
esac
exec "$(command -v "$1")" "\$@"
EOF
  chmod +x "$SETUP_WRAPPER/$1"
  set +e
  if [ "${4:-}" = traced ]; then
    UPDATE_TEST_SETUP_ONLY=1 TMPDIR="$CHILD_TMP" PATH="$SETUP_WRAPPER:$PATH" env -u UPDATE_TEST_BODY \
      bash "$SCRIPT_DIR/${0##*/}" >"$FIXTURE/child.out" 2>"$FIXTURE/child.err"
  else
    UPDATE_TEST_BODY='' UPDATE_TEST_SETUP_ONLY=1 PATH="$SETUP_WRAPPER:$PATH" bash "$SCRIPT_DIR/${0##*/}" \
      >"$FIXTURE/child.out" 2>"$FIXTURE/child.err"
  fi
  status=$?
  set -e
}
# The child's stderr starts with its START line and ends with its END line, which names the status and the case.
expect_traced() {
  grep -q -x -E "START test_update.sh [0-9]+" <<<"$(head -n 1 "$FIXTURE/child.err")" ||
    fail "$3: no START line first: $(<"$FIXTURE/child.err")"
  grep -q -x -F -e "END test_update.sh $(head -n 1 "$FIXTURE/child.err" | cut -d ' ' -f 3): exit $1 in case $2" \
    <<<"$(tail -n 1 "$FIXTURE/child.err")" ||
    fail "$3: no END line for exit $1 in case $2 last: $(<"$FIXTURE/child.err")"
}

# A setup command that fails names itself and git's stderr on the suite's stderr, whether it is the self-test's
# succeeding call or a repository the setup makes, and the suite exits non-zero. Git fails the matching command with 4.
expect_setup_failure() {
  run_setup_child git "$1" 'echo "error: refused by the test" >&2; exit 4'
  [ "$status" -ne 0 ] || fail "$3: the suite passed"
  grep -q -E "$2" "$FIXTURE/child.err" || fail "$3: exit $status, the command not named: $(<"$FIXTURE/child.err")"
  grep -q -x "error: refused by the test" "$FIXTURE/child.err" || fail "$3: git's stderr not shown: $(<"$FIXTURE/child.err")"
  expect_traced "$status" "$4" "$3"
}
begin_case 'failing setup commands'
expect_setup_failure '*"/seed status "*' "^FAIL: succeeding setup command: exited 4: FAIL: setup command exited 4: git -C .*/seed status$" \
  "failing self-test call" "setup self-tests"
expect_setup_failure '*" init --bare "*' "^FAIL: setup command exited 4: git init --bare -b main .*/upstream\.git$" \
  "failing upstream repository" "setup"

# The setup runs no git config, so a configuration write Git refuses with 4 cannot fail it.
begin_case 'setup without git config'
run_setup_child git '*" config "*' 'echo "error: refused by the test" >&2; exit 4'
[ "$status" -eq 0 ] || fail "setup without git config: exit $status: $(<"$FIXTURE/child.err")"
expect_traced 0 "setup self-tests" "setup without git config"

# A command that fails silently ends the suite with its status and a line naming it.
begin_case 'silent failure'
run_setup_child mkdir '*"/no-hooks "*' 'exit 4'
[ "$status" -eq 4 ] || fail "silent failure: expected exit 4, got $status: $(<"$FIXTURE/child.err")"
grep -q -x -E "FAIL: the suite exited 4 in case setup; the last command that failed: line [0-9]+: mkdir -p \"\\\$HOME/no-hooks\"" \
  "$FIXTURE/child.err" || fail "silent failure: the command not named: $(<"$FIXTURE/child.err")"
expect_traced 4 setup "silent failure"

# A failure before the fixture exists, here of mktemp, is named the same way.
begin_case 'failure before the fixture'
run_setup_child mktemp '*' 'exit 4'
[ "$status" -eq 4 ] || fail "failure before the fixture: expected exit 4, got $status: $(<"$FIXTURE/child.err")"
grep -q -x -F -e 'FAIL: the suite exited 4 in case setup; the last command that failed: line '"$(grep -n '^FIXTURE=$(mktemp' "$SCRIPT_DIR/${0##*/}" | cut -d : -f 1)"': FIXTURE=$(mktemp -d "${TMPDIR:-/tmp}/update-skills-test.XXXXXX")' \
  "$FIXTURE/child.err" || fail "failure before the fixture: the command not named: $(<"$FIXTURE/child.err")"
expect_traced 4 setup "failure before the fixture"

# A signal ends the suite through its EXIT trap, so its exit is named too. The wrapper sends the suite TERM.
begin_case 'signal'
run_setup_child mkdir '*"/no-hooks "*' 'kill -TERM "$PPID"; exit 0'
[ "$status" -eq 143 ] || fail "signal: expected exit 143, got $status: $(<"$FIXTURE/child.err")"
grep -q -x -E "FAIL: the suite exited 143 in case setup; the last command that failed: .+" "$FIXTURE/child.err" ||
  fail "signal: the exit not named: $(<"$FIXTURE/child.err")"
expect_traced 143 setup "signal"

# The outer run is silent when its body ends through the EXIT trap, and leaves no trace folder behind.
begin_case 'traced body'
run_setup_child git '*" never-run "*' 'exit 4' traced
[ "$status" -eq 0 ] || fail "traced body: exit $status: $(<"$FIXTURE/child.err")"
expect_traced 0 "setup self-tests" "traced body"
[ -z "$(ls -A "$CHILD_TMP")" ] || fail "traced body: left behind: $(ls -A "$CHILD_TMP")"

# A body ended without its EXIT trap, here by a KILL the wrapper sends it, is named by the outer run with its exit and
# the last command it ran.
begin_case 'end without the EXIT trap'
run_setup_child mkdir '*"/no-hooks "*' 'kill -KILL "$PPID"; exit 0' traced
[ "$status" -eq 137 ] || fail "end without the EXIT trap: expected exit 137, got $status: $(<"$FIXTURE/child.err")"
grep -q -x -F "FAIL: the suite exited 137 without its EXIT trap; the last commands it ran:" "$FIXTURE/child.err" ||
  fail "end without the EXIT trap: the exit not named: $(<"$FIXTURE/child.err")"
grep -q -x -E "\+ line [0-9]+: mkdir -p '?[^ ].*/no-hooks'?" <<<"$(tail -n 1 "$FIXTURE/child.err")" ||
  fail "end without the EXIT trap: the last command not named last: $(<"$FIXTURE/child.err")"
! grep -q "^END " "$FIXTURE/child.err" || fail "end without the EXIT trap: an END line: $(<"$FIXTURE/child.err")"
[ -z "$(find "$CHILD_TMP" -maxdepth 1 -name 'update-skills-trace.*')" ] ||
  fail "end without the EXIT trap: the trace folder left behind: $(ls -A "$CHILD_TMP")"

# The suite's isolation unsets a git variable named in any case, and keeps the two that locate git itself.
begin_case 'isolation'
LEFT=$(
  export git_common_dir="$MISSING/.git" Git_Work_Tree="$MISSING" GIT_OBJECT_DIRECTORY="$MISSING/objects"
  export GIT_BASH=bash GIT_EXEC_PATH=exec-path
  unset_git_variables
  compgen -e | grep -i '^git_' || true
  printf '%s %s\n' "${GIT_BASH-}" "${GIT_EXEC_PATH-}"
)
[ "$LEFT" = "GIT_BASH"$'\n'"GIT_EXEC_PATH"$'\n'"bash exec-path" ] || fail "isolation: git variables left: $LEFT"

publish() {
  printf '%s\n' "$1" >>"$SEED/notes.txt"
  git_quiet -C "$SEED" add notes.txt
  git_quiet -C "$SEED" commit -m "$1"
  git_quiet -C "$SEED" push origin main
}

# A new clone, made with any further clone arguments given, counts as deployed from the commit it checked out.
case_number=0
new_clone() {
  case_number=$((case_number + 1))
  CLONE="$FIXTURE/clone $case_number (dev)"
  git_quiet clone "$@" "$UPSTREAM" "$CLONE"
  git -C "$CLONE" rev-parse HEAD >"$CLONE/.fake-deployed"
}

run_subject() {
  export FAKE_DEPLOY_LOG="$FIXTURE/deploy-$case_number.log"
  : >"$FAKE_DEPLOY_LOG"
  set +e
  OUTPUT=$(bash "$SUBJECT" "$@" 2>"$FIXTURE/stderr")
  status=$?
  set -e
}

# A check hands grep its text in a here-string, never through a pipe: grep -q exits at its first match, and under
# pipefail the writer it leaves can die of SIGPIPE and fail a check whose pattern matched.
expect_status() { [ "$status" -eq "$1" ] || fail "$2: expected exit $1, got $status: $OUTPUT $(cat "$FIXTURE/stderr")"; }
expect_first_line() { [ "$(printf '%s\n' "$OUTPUT" | head -n 1)" = "$1" ] || fail "$2: expected '$1', got: $OUTPUT"; }
expect_no_stderr() { [ ! -s "$FIXTURE/stderr" ] || fail "$1: wrote to stderr: $(cat "$FIXTURE/stderr")"; }
# A named failure: exit 1, one stdout line starting with the given FAILED text, nothing on stderr.
expect_failed() {
  expect_status 1 "$2"
  expect_no_stderr "$2"
  [ "$(printf '%s\n' "$OUTPUT" | wc -l | tr -d ' ')" = "1" ] || fail "$2: expected one line, got: $OUTPUT"
  case "$OUTPUT" in
    "$1"*) ;;
    *) fail "$2: expected a line starting '$1', got: $OUTPUT" ;;
  esac
}
# The pin is read with check before the fetch; the deployment is the one run with --all, and it comes last. Its
# arguments are --all alone unless the second argument names them.
expect_deployed() {
  local arguments=${2:---all}
  if [ "$(grep -c -e "^--all" "$FAKE_DEPLOY_LOG")" != "1" ] || [ "$(tail -n 1 "$FAKE_DEPLOY_LOG")" != "$arguments" ]; then
    fail "$1: deploy.py was not run once with $arguments: $(cat "$FAKE_DEPLOY_LOG")"
  fi
}
expect_not_deployed() { ! grep -q -e "^--all" "$FAKE_DEPLOY_LOG" || fail "$1: deploy.py ran"; }
expect_nothing_run() { [ ! -s "$FAKE_DEPLOY_LOG" ] || fail "$1: deploy.py ran: $(cat "$FAKE_DEPLOY_LOG")"; }
head_of() { git -C "$1" rev-parse --short "$2"; }

# Usage errors: the wrong number of arguments, on stderr with exit 2.
begin_case 'usage errors'
run_subject
expect_status 2 "no argument"
[ -z "$OUTPUT" ] || fail "no argument: usage went to stdout: $OUTPUT"
grep -q "^usage: update.sh" "$FIXTURE/stderr" || fail "no argument: usage text missing"
run_subject "$FIXTURE" main --cross-major extra
expect_status 2 "an extra argument"
run_subject "$FIXTURE" main --unpin --unpin
expect_status 2 "a repeated option"
run_subject "$FIXTURE" main --cross-major --unpin extra
expect_status 2 "five arguments"
# The configured channel is required and is release or main, and --channel takes one of the two, once.
for ARGUMENTS in "" "--cross-major" "beta" "Main" "main --channel" "main --channel beta" "release --channel main --channel main" \
  "main --channel --unpin" "--channel main"; do
  read -r -a SPLIT <<<"$ARGUMENTS"
  run_subject "$FIXTURE" "${SPLIT[@]}"
  expect_status 2 "channel usage '$ARGUMENTS'"
  [ -z "$OUTPUT" ] || fail "channel usage '$ARGUMENTS': usage went to stdout: $OUTPUT"
done

# A path that is not a clone is a named failure, not a usage error.
begin_case 'not a clone'
run_subject "$FIXTURE" main
expect_failed "FAILED no deploy.py in $FIXTURE" "directory without deploy.py"
PLAIN="$FIXTURE/plain dir (no git)"
mkdir -p "$PLAIN"
cp "$SEED/deploy.py" "$PLAIN/deploy.py"
export GIT_CEILING_DIRECTORIES="$FIXTURE"
run_subject "$PLAIN" main
unset GIT_CEILING_DIRECTORIES
expect_failed "FAILED not a git repository: $PLAIN" "not a repository"
expect_not_deployed "not a repository"

# An up-to-date clone deploys without moving, even when untracked files are present.
begin_case 'up to date'
new_clone
printf 'scratch\n' >"$CLONE/untracked.txt"
run_subject "$CLONE" main
expect_status 0 "up to date"
expect_first_line "UP_TO_DATE $(head_of "$CLONE" HEAD)" "up to date"
expect_deployed "up to date"
grep -qx "fake deploy ran" <<<"$OUTPUT" || fail "up to date: deploy output was not shown"
[ "$(printf '%s\n' "$OUTPUT" | tail -n 1)" = "DEPLOYED" ] || fail "up to date: missing DEPLOYED"

# A clone behind origin is fast-forwarded, lists the pulled commits, then deploys.
begin_case 'behind'
new_clone
BEFORE=$(head_of "$CLONE" HEAD)
publish "first change"
publish "second change"
run_subject "$CLONE" main
expect_status 0 "behind"
AFTER=$(head_of "$CLONE" HEAD)
[ "$AFTER" = "$(head_of "$SEED" HEAD)" ] || fail "behind: clone was not fast-forwarded"
expect_first_line "UPDATED $BEFORE..$AFTER" "behind"
grep -q "first change" <<<"$OUTPUT" || fail "behind: pulled commit missing"
grep -q "second change" <<<"$OUTPUT" || fail "behind: pulled commit missing"
expect_deployed "behind"

# A clean clone on another branch is switched to main before the fast-forward, and stays there.
begin_case 'other branch'
new_clone
git_quiet -C "$CLONE" checkout -b topic
publish "third change"
run_subject "$CLONE" main
expect_status 0 "other branch"
[ "$(git -C "$CLONE" branch --show-current)" = "main" ] || fail "other branch: not switched to main"
[ "$(head_of "$CLONE" HEAD)" = "$(head_of "$SEED" HEAD)" ] || fail "other branch: main not fast-forwarded"
expect_deployed "other branch"

# Uncommitted tracked changes stop everything before the fetch.
begin_case 'dirty'
new_clone
BEFORE=$(git -C "$CLONE" rev-parse origin/main)
printf 'edit\n' >>"$CLONE/notes.txt"
publish "fourth change"
run_subject "$CLONE" main
expect_status 1 "dirty"
expect_first_line "DIRTY" "dirty"
expect_no_stderr "dirty"
grep -q "notes.txt" <<<"$OUTPUT" || fail "dirty: changed file not listed"
[ "$(git -C "$CLONE" rev-parse origin/main)" = "$BEFORE" ] || fail "dirty: fetched anyway"
expect_nothing_run "dirty"

# A main with its own commits is never merged, rebased, or reset.
begin_case 'diverged'
new_clone
git_quiet -C "$CLONE" commit --allow-empty -m "local work"
LOCAL=$(git -C "$CLONE" rev-parse HEAD)
publish "fifth change"
run_subject "$CLONE" main
expect_status 1 "diverged"
expect_first_line "NOT_FAST_FORWARD" "diverged"
expect_no_stderr "diverged"
[ "$(git -C "$CLONE" rev-parse HEAD)" = "$LOCAL" ] || fail "diverged: main moved"
grep -q "local work" <<<"$OUTPUT" || fail "diverged: local commit not listed: $OUTPUT"
expect_not_deployed "diverged"

# A diverged main stops before any checkout, so a clone on another branch stays on it.
begin_case 'diverged on topic'
new_clone
git_quiet -C "$CLONE" commit --allow-empty -m "local work"
LOCAL=$(git -C "$CLONE" rev-parse main)
git_quiet -C "$CLONE" checkout -b topic
publish "sixth change"
run_subject "$CLONE" main
expect_status 1 "diverged on topic"
expect_first_line "NOT_FAST_FORWARD" "diverged on topic"
expect_no_stderr "diverged on topic"
[ "$(git -C "$CLONE" branch --show-current)" = "topic" ] || fail "diverged on topic: branch switched"
[ "$(git -C "$CLONE" rev-parse main)" = "$LOCAL" ] || fail "diverged on topic: main moved"
expect_not_deployed "diverged on topic"

# A fast-forward Git refuses after the switch, here over an untracked file the update adds, switches back.
begin_case 'refused on topic'
new_clone
git_quiet -C "$CLONE" checkout -b topic
BEFORE=$(git -C "$CLONE" rev-parse main)
printf 'added\n' >"$SEED/added.txt"
git_quiet -C "$SEED" add added.txt
git_quiet -C "$SEED" commit -m "add a file"
git_quiet -C "$SEED" push origin main
printf 'untracked copy\n' >"$CLONE/added.txt"
run_subject "$CLONE" main
expect_status 1 "refused on topic"
expect_first_line "NOT_FAST_FORWARD" "refused on topic"
expect_no_stderr "refused on topic"
[ "$(git -C "$CLONE" branch --show-current)" = "topic" ] || fail "refused on topic: not switched back"
[ "$(git -C "$CLONE" rev-parse main)" = "$BEFORE" ] || fail "refused on topic: main moved"
[ "$(cat "$CLONE/added.txt")" = "untracked copy" ] || fail "refused on topic: untracked file overwritten"
expect_not_deployed "refused on topic"

# A detached HEAD is returned to the same commit.
begin_case 'refused detached'
git_quiet -C "$CLONE" checkout --detach topic
DETACHED=$(git -C "$CLONE" rev-parse HEAD)
run_subject "$CLONE" main
expect_status 1 "refused detached"
expect_first_line "NOT_FAST_FORWARD" "refused detached"
[ -z "$(git -C "$CLONE" branch --show-current)" ] || fail "refused detached: HEAD not detached"
[ "$(git -C "$CLONE" rev-parse HEAD)" = "$DETACHED" ] || fail "refused detached: not switched back"
expect_not_deployed "refused detached"

# When the switch back fails too, the output says the clone is still on main. A Git wrapper refuses that switch.
begin_case 'switch back refused'
git_quiet -C "$CLONE" checkout topic
WRAPPER="$FIXTURE/git wrapper"
mkdir -p "$WRAPPER"
cat >"$WRAPPER/git" <<EOF
#!/usr/bin/env bash
case " \$* " in
  *" checkout --quiet topic "*) echo "error: switch refused by the test" >&2; exit 1 ;;
esac
exec "$(command -v git)" "\$@"
EOF
chmod +x "$WRAPPER/git"
PATH="$WRAPPER:$PATH" run_subject "$CLONE" main
expect_status 1 "switch back refused"
expect_first_line "NOT_FAST_FORWARD" "switch back refused"
grep -qx "still on main: cannot switch back to topic: error: switch refused by the test" <<<"$OUTPUT" ||
  fail "switch back refused: no still-on-main line: $OUTPUT"
[ "$(git -C "$CLONE" branch --show-current)" = "main" ] || fail "switch back refused: expected main"
expect_not_deployed "switch back refused"

# A branch that cannot switch to main, here over an untracked file main tracks, is never forced.
begin_case 'checkout failed'
new_clone
git_quiet -C "$CLONE" checkout -b topic
git_quiet -C "$CLONE" rm --quiet notes.txt
git_quiet -C "$CLONE" commit -m "drop notes"
printf 'untracked copy\n' >"$CLONE/notes.txt"
run_subject "$CLONE" main
expect_status 1 "checkout failed"
expect_first_line "CHECKOUT_FAILED" "checkout failed"
expect_no_stderr "checkout failed"
[ "$(git -C "$CLONE" branch --show-current)" = "topic" ] || fail "checkout failed: branch switched"
[ "$(cat "$CLONE/notes.txt")" = "untracked copy" ] || fail "checkout failed: untracked file overwritten"
expect_not_deployed "checkout failed"

# An unreachable origin is reported without deploying.
begin_case 'fetch failed'
new_clone
git_quiet -C "$CLONE" remote set-url origin "$FIXTURE/missing.git"
run_subject "$CLONE" main
expect_status 1 "fetch failed"
expect_first_line "FETCH_FAILED" "fetch failed"
expect_no_stderr "fetch failed"
expect_not_deployed "fetch failed"

# A Git command that fails outright is a named failure.
begin_case 'status failed'
new_clone
printf 'not an index' >"$CLONE/.git/index"
run_subject "$CLONE" main
expect_failed "FAILED git status failed: " "status failed"
expect_not_deployed "status failed"

# A script that cannot make its temporary copy stops before any Git command.
begin_case 'no temporary copy'
new_clone
export FAKE_DEPLOY_LOG="$FIXTURE/deploy-$case_number.log"
: >"$FAKE_DEPLOY_LOG"
set +e
OUTPUT=$(TMPDIR="$FIXTURE/absent temporary directory" bash "$SUBJECT" "$CLONE" main 2>"$FIXTURE/stderr")
status=$?
set -e
expect_failed "FAILED cannot create a temporary copy of update.sh: " "no temporary copy"
expect_not_deployed "no temporary copy"

# A script that cannot make the file the deployer's errors are kept in stops before any Git command too, and removes
# its temporary copy. A mktemp wrapper makes the copy and refuses every later call.
begin_case 'no file for the errors'
MKTEMP_WRAPPER="$FIXTURE/mktemp wrapper"
mkdir -p "$MKTEMP_WRAPPER" "$FIXTURE/tmp for the errors"
cat >"$MKTEMP_WRAPPER/mktemp" <<EOF
#!/usr/bin/env bash
if [ -e "$MKTEMP_WRAPPER/made" ]; then
  echo "mktemp: refused by the test" >&2
  exit 1
fi
: >"$MKTEMP_WRAPPER/made"
exec "$(command -v mktemp)" "\$@"
EOF
chmod +x "$MKTEMP_WRAPPER/mktemp"
TMPDIR="$FIXTURE/tmp for the errors" PATH="$MKTEMP_WRAPPER:$PATH" run_subject "$CLONE" main
expect_failed "FAILED cannot create a temporary file for the deployer's errors: mktemp: refused by the test" \
  "no file for the errors"
expect_nothing_run "no file for the errors"
[ -z "$(ls -A "$FIXTURE/tmp for the errors")" ] ||
  fail "no file for the errors: temporary copy left behind: $(ls -A "$FIXTURE/tmp for the errors")"

# A failing deploy is reported with its exit code.
begin_case 'deploy failed'
new_clone
export FAKE_DEPLOY_EXIT=7
run_subject "$CLONE" main
unset FAKE_DEPLOY_EXIT
expect_status 1 "deploy failed"
expect_first_line "UP_TO_DATE $(head_of "$CLONE" HEAD)" "deploy failed"
[ "$(printf '%s\n' "$OUTPUT" | tail -n 1)" = "DEPLOY_FAILED 7" ] || fail "deploy failed: missing DEPLOY_FAILED 7: $OUTPUT"
expect_deployed "deploy failed"

# With no release tag on either side there is no release to cross, so the update proceeds.
begin_case 'no tag'
new_clone
publish "untagged change"
run_subject "$CLONE" main
expect_status 0 "no tag"
grep -q "^UPDATED " <<<"$OUTPUT" || fail "no tag: expected UPDATED: $OUTPUT"
if grep -q "CROSSED" <<<"$OUTPUT"; then fail "no tag: crossing reported"; fi
expect_no_stderr "no tag"
expect_deployed "no tag"

# A release tag on local main only cannot be crossed, so the gate passes and the diverged main stops the update.
begin_case 'local tag only'
new_clone
git_quiet -C "$CLONE" commit --allow-empty -m "local release"
git_quiet -C "$CLONE" tag v0.9.0
publish "upstream after local release"
run_subject "$CLONE" main
expect_status 1 "local tag only"
expect_first_line "NOT_FAST_FORWARD" "local tag only"
expect_not_deployed "local tag only"

# Version tags are published upstream only, so the clone must fetch them to see either side's version.
tag_upstream() {
  git_quiet -C "$SEED" tag "$1"
  git_quiet -C "$SEED" push origin "$1"
}

# A release on origin/main stops a local main with no release tag, which counts as version zero.
begin_case 'untagged'
new_clone
BEFORE=$(head_of "$CLONE" HEAD)
publish "first release"
tag_upstream v0.1.0
run_subject "$CLONE" main
expect_status 1 "untagged"
expect_first_line "MAJOR_UPDATE untagged..v0.1.0" "untagged"
expect_no_stderr "untagged"
grep -q "first release" <<<"$OUTPUT" || fail "untagged: pending commit not listed"
[ "$(head_of "$CLONE" HEAD)" = "$BEFORE" ] || fail "untagged: main moved"
expect_not_deployed "untagged"
run_subject "$CLONE" main --cross-major
expect_status 0 "untagged accepted"
grep -qx "CROSSED untagged..v0.1.0" <<<"$OUTPUT" || fail "untagged accepted: crossing not named: $OUTPUT"
expect_deployed "untagged accepted"

# A release with a higher breaking component stops before main moves, and lists what it would pull.
begin_case 'major'
new_clone
BEFORE=$(head_of "$CLONE" HEAD)
publish "breaking change"
tag_upstream v0.2.0
run_subject "$CLONE" main
expect_status 1 "major"
expect_first_line "MAJOR_UPDATE v0.1.0..v0.2.0" "major"
expect_no_stderr "major"
grep -q "breaking change" <<<"$OUTPUT" || fail "major: pending commit not listed"
[ "$(head_of "$CLONE" HEAD)" = "$BEFORE" ] || fail "major: main moved"
expect_not_deployed "major"

# With --cross-major the same update proceeds, names the crossing, and deploys.
begin_case 'cross major'
run_subject "$CLONE" main --cross-major
expect_status 0 "cross major"
AFTER=$(head_of "$CLONE" HEAD)
expect_first_line "UPDATED $BEFORE..$AFTER" "cross major"
grep -qx "CROSSED v0.1.0..v0.2.0" <<<"$OUTPUT" || fail "cross major: crossing not named: $OUTPUT"
expect_deployed "cross major"

# A patch release within the same breaking component needs no acceptance.
begin_case 'patch'
new_clone
publish "patch change"
tag_upstream v0.2.3
run_subject "$CLONE" main
expect_status 0 "patch"
grep -q "^UPDATED " <<<"$OUTPUT" || fail "patch: expected UPDATED: $OUTPUT"
if grep -q "CROSSED" <<<"$OUTPUT"; then fail "patch: crossing reported"; fi
expect_deployed "patch"

# Leaving 0.x raises the major, so it stops too. A nearer tag that is not numeric is not a release.
begin_case 'to 1.0.0'
new_clone
BEFORE=$(head_of "$CLONE" HEAD)
publish "first stable"
tag_upstream v1.0.0
publish "after first stable"
tag_upstream v2x.0.0
run_subject "$CLONE" main
expect_status 1 "to 1.0.0"
expect_first_line "MAJOR_UPDATE v0.2.3..v1.0.0" "to 1.0.0"
expect_no_stderr "to 1.0.0"
expect_not_deployed "to 1.0.0"
run_subject "$CLONE" main --cross-major
expect_status 0 "to 1.0.0 accepted"
expect_deployed "to 1.0.0 accepted"

# From 1.0.0 on, only the major component counts: a minor release passes, a major one stops.
begin_case 'stable minor and major'
new_clone
publish "minor stable"
tag_upstream v1.5.0
run_subject "$CLONE" main
expect_status 0 "stable minor"
if grep -q "CROSSED" <<<"$OUTPUT"; then fail "stable minor: crossing reported"; fi
expect_deployed "stable minor"
new_clone
BEFORE=$(head_of "$CLONE" HEAD)
publish "second major"
tag_upstream v2.0.0
run_subject "$CLONE" main
expect_status 1 "stable major"
expect_first_line "MAJOR_UPDATE v1.5.0..v2.0.0" "stable major"
[ "$(head_of "$CLONE" HEAD)" = "$BEFORE" ] || fail "stable major: main moved"
expect_not_deployed "stable major"

# A pre-release is not a release, as ejryan89-skill-core's release_tags.py defines one: one of the next major does not stop the
# update, and its final release then stops it at the release before the pre-release.
begin_case 'pre-release'
run_subject "$CLONE" main --cross-major
expect_status 0 "stable major accepted"
new_clone
publish "third major candidate"
tag_upstream v3.0.0-rc.1
run_subject "$CLONE" main
expect_status 0 "pre-release"
if grep -q "CROSSED" <<<"$OUTPUT"; then fail "pre-release: crossing reported"; fi
expect_no_stderr "pre-release"
expect_deployed "pre-release"
publish "third major"
tag_upstream v3.0.0
run_subject "$CLONE" main
expect_status 1 "pre-release to release"
expect_first_line "MAJOR_UPDATE v2.0.0..v3.0.0" "pre-release to release"
expect_not_deployed "pre-release to release"
run_subject "$CLONE" main --cross-major
expect_status 0 "pre-release to release accepted"
grep -qx "CROSSED v2.0.0..v3.0.0" <<<"$OUTPUT" || fail "pre-release to release accepted: crossing not named"
expect_deployed "pre-release to release accepted"

# A deployment made with deploy.py --at stops the update before the fetch, naming the ref and the commit check reports.
begin_case 'pinned'
PINNED_COMMIT=$(git -C "$SEED" rev-parse "v1.0.0^{commit}")
PINNED_CHECK="
Source: coding-agent-skills
Deployed commit: v1.0.0 ($PINNED_COMMIT), pinned with --at v1.0.0
Home: home"
new_clone
BEFORE=$(git -C "$CLONE" rev-parse HEAD)
FETCHED=$(git -C "$CLONE" rev-parse origin/main)
publish "after the pin"
FAKE_CHECK_OUTPUT=$PINNED_CHECK run_subject "$CLONE" main
expect_status 1 "pinned"
[ "$OUTPUT" = "PINNED v1.0.0 $PINNED_COMMIT" ] || fail "pinned: expected only the PINNED line, got: $OUTPUT"
expect_no_stderr "pinned"
[ "$(git -C "$CLONE" rev-parse origin/main)" = "$FETCHED" ] || fail "pinned: fetched anyway"
[ "$(git -C "$CLONE" rev-parse HEAD)" = "$BEFORE" ] || fail "pinned: main moved"
[ "$(cat "$FAKE_DEPLOY_LOG")" = "check" ] || fail "pinned: expected only check to run: $(cat "$FAKE_DEPLOY_LOG")"

# A pin check names by its commit alone, when no tag describes it, is read the same way.
begin_case 'pinned untagged'
FAKE_CHECK_OUTPUT="Deployed commit: $PINNED_COMMIT, pinned with --at main~2" run_subject "$CLONE" main
expect_status 1 "pinned untagged"
[ "$OUTPUT" = "PINNED main~2 $PINNED_COMMIT" ] || fail "pinned untagged: got: $OUTPUT"
expect_not_deployed "pinned untagged"

# With --unpin the update moves past the pin, but the installed version is the pinned commit's, so a release that
# raises its major stops it until --cross-major too.
begin_case 'unpin across a major'
FAKE_CHECK_OUTPUT=$PINNED_CHECK run_subject "$CLONE" main --unpin
expect_status 1 "unpin across a major"
expect_first_line "MAJOR_UPDATE v1.0.0..v3.0.0" "unpin across a major"
grep -q "second major" <<<"$OUTPUT" || fail "unpin across a major: commit since the pin not listed: $OUTPUT"
[ "$(git -C "$CLONE" rev-parse HEAD)" = "$BEFORE" ] || fail "unpin across a major: main moved"
expect_not_deployed "unpin across a major"

# With --unpin, before or after --cross-major, the update moves past the pin and deploys main, passing --unpin to the
# deployer, which refuses to replace a pin without it.
begin_case 'unpin with cross-major'
FAKE_CHECK_OUTPUT=$PINNED_CHECK run_subject "$CLONE" main --cross-major --unpin
expect_status 0 "unpin after cross-major"
expect_first_line "UPDATED $(git -C "$CLONE" rev-parse --short "$BEFORE")..$(head_of "$CLONE" HEAD)" "unpin after cross-major"
grep -qx "CROSSED v1.0.0..v3.0.0" <<<"$OUTPUT" || fail "unpin after cross-major: crossing not named: $OUTPUT"
expect_deployed "unpin after cross-major" "--all --unpin"
publish "unpin first"
FAKE_CHECK_OUTPUT=$PINNED_CHECK run_subject "$CLONE" main --unpin --cross-major
expect_status 0 "unpin before cross-major"
expect_deployed "unpin before cross-major" "--all --unpin"

# A deployment check names without a pin does not stop the update, and its commit is read from inside the parentheses.
begin_case 'not pinned'
publish "unpinned change"
FAKE_CHECK_OUTPUT="Deployed commit: v3.0.0-4-g0000000 ($(git -C "$CLONE" rev-parse HEAD))" run_subject "$CLONE" main
expect_status 0 "not pinned"
grep -q "^UPDATED " <<<"$OUTPUT" || fail "not pinned: expected UPDATED: $OUTPUT"
expect_deployed "not pinned"

# A pin check does not name, here a check that names the commit alone, still stops at the deployer, which refuses to
# replace it without --unpin. The clone was fetched and fast-forwarded, the deployer's refusal is shown, and the last
# line names the ref and the commit from it, whether a tag describes the commit or not.
begin_case 'pin refused'
for DESCRIBED in "v1.0.0 ($PINNED_COMMIT)" "$PINNED_COMMIT"; do
  publish "change over a pin check does not name: $DESCRIBED"
  BEFORE=$(head_of "$CLONE" HEAD)
  REFUSAL="ERROR: Refusing to replace the deployment pinned with --at v1.0.0 to $DESCRIBED without --unpin."
  FAKE_CHECK_OUTPUT="Deployed commit: $(git -C "$CLONE" rev-parse HEAD)" FAKE_DEPLOY_EXIT=1 \
    FAKE_DEPLOY_STDERR=$'\n'"$REFUSAL"$'\nTo move it to this checkout\'s HEAD, rerun with --unpin.\n' \
    run_subject "$CLONE" main
  expect_status 1 "pin refused '$DESCRIBED'"
  expect_first_line "UPDATED $BEFORE..$(head_of "$SEED" HEAD)" "pin refused '$DESCRIBED'"
  [ "$(head_of "$CLONE" HEAD)" = "$(head_of "$SEED" HEAD)" ] || fail "pin refused '$DESCRIBED': main not fast-forwarded"
  [ "$(printf '%s\n' "$OUTPUT" | tail -n 1)" = "DEPLOY_PINNED v1.0.0 $PINNED_COMMIT" ] ||
    fail "pin refused '$DESCRIBED': expected DEPLOY_PINNED v1.0.0 $PINNED_COMMIT last, got: $OUTPUT"
  if grep -q "DEPLOY_FAILED" <<<"$OUTPUT"; then fail "pin refused '$DESCRIBED': DEPLOY_FAILED printed"; fi
  grep -qxF -e "$REFUSAL" <<<"$(tr -d '\r' <"$FIXTURE/stderr")" ||
    fail "pin refused '$DESCRIBED': the deployer's refusal not shown: $(cat "$FIXTURE/stderr")"
  expect_deployed "pin refused '$DESCRIBED'"
done

# Any other failure is DEPLOY_FAILED with the deployer's error shown: another error, or the refusal's line with an exit
# the refusal never has.
begin_case 'deploy failed with an error'
for FAILURE in "1 ERROR: Could not write the manifest." \
  "2 ERROR: Refusing to replace the deployment pinned with --at v1.0.0 to $PINNED_COMMIT without --unpin."; do
  FAKE_CHECK_OUTPUT="Deployed commit: $(git -C "$CLONE" rev-parse HEAD)" FAKE_DEPLOY_EXIT=${FAILURE%% *} \
    FAKE_DEPLOY_STDERR=${FAILURE#* } run_subject "$CLONE" main
  expect_status 1 "deploy failed '$FAILURE'"
  [ "$(printf '%s\n' "$OUTPUT" | tail -n 1)" = "DEPLOY_FAILED ${FAILURE%% *}" ] ||
    fail "deploy failed '$FAILURE': expected DEPLOY_FAILED ${FAILURE%% *} last, got: $OUTPUT"
  grep -qxF -e "${FAILURE#* }" <<<"$(tr -d '\r' <"$FIXTURE/stderr")" ||
    fail "deploy failed '$FAILURE': the deployer's error not shown: $(cat "$FIXTURE/stderr")"
done

# A check that fails still names the deployed commit when it prints the line, and the update reads it.
begin_case 'check failed'
FAKE_CHECK_OUTPUT="Deployed commit: $(git -C "$CLONE" rev-parse HEAD)" FAKE_CHECK_EXIT=1 run_subject "$CLONE" main
expect_status 0 "check failed"
expect_first_line "UP_TO_DATE $(head_of "$CLONE" HEAD)" "check failed"
expect_no_stderr "check failed"
expect_deployed "check failed"

# A check that names no deployed commit and pin the update can read leaves whether the installation is pinned unknown,
# so the update stops on either channel before anything is fetched, with or without --unpin or --cross-major, rather
# than deploy over a pin it could not see: a check that prints nothing, one that prints only its error, and a line
# that names neither a full commit nor unknown, or a pin without a ref or with one no ref could be.
begin_case 'pin unknown'
FETCHED=$(git -C "$CLONE" rev-parse origin/main)
for CHECK in "1|" "0|" "1|ERROR: Could not read the manifest." "0|Deployed commit: v3.0.0" \
  "0|Deployed commit: v3.0.0 (abc1234)" "0|Deployed commit: $PINNED_COMMIT, pinned with --at " \
  "0|Deployed commit: unknown, pinned with --at v1.0.0 extra"; do
  CODE=${CHECK%%|*}
  CHECK_OUTPUT=${CHECK#*|}
  case "$CHECK_OUTPUT" in
    "") REASON="deploy.py check exited $CODE and printed nothing" ;;
    "Deployed commit: "*) REASON="deploy.py check printed: $CHECK_OUTPUT" ;;
    *) REASON="deploy.py check exited $CODE without naming the deployed commit: $CHECK_OUTPUT" ;;
  esac
  for ARGUMENTS in "main" "release" "release --cross-major" "main --cross-major --unpin"; do
    read -r -a SPLIT <<<"$ARGUMENTS"
    FAKE_CHECK_OUTPUT=$CHECK_OUTPUT FAKE_CHECK_EXIT=$CODE run_subject "$CLONE" "${SPLIT[@]}"
    [ "$OUTPUT" = "FAILED cannot read whether the installation is pinned: $REASON" ] ||
      fail "pin unknown '$CHECK' $ARGUMENTS: got: $OUTPUT"
    expect_failed "FAILED" "pin unknown '$CHECK' $ARGUMENTS"
    [ "$(cat "$FAKE_DEPLOY_LOG")" = "check" ] || fail "pin unknown '$CHECK' $ARGUMENTS: ran: $(cat "$FAKE_DEPLOY_LOG")"
    [ "$(git -C "$CLONE" rev-parse origin/main)" = "$FETCHED" ] || fail "pin unknown '$CHECK' $ARGUMENTS: fetched"
  done
done

# With no deployed commit to read, from a check that names none, the installed version is unknown, which any release
# on origin/main raises, until --cross-major accepts it.
begin_case 'no deployed commit'
FAKE_CHECK_OUTPUT="Deployed commit: unknown" FAKE_CHECK_EXIT=1 run_subject "$CLONE" main
expect_status 1 "no deployed commit"
expect_first_line "MAJOR_UPDATE unknown..v3.0.0" "no deployed commit"
expect_no_stderr "no deployed commit"
expect_not_deployed "no deployed commit"
FAKE_CHECK_OUTPUT="Deployed commit: unknown" run_subject "$CLONE" main --cross-major
expect_status 0 "no deployed commit accepted"
grep -qx "CROSSED unknown..v3.0.0" <<<"$OUTPUT" || fail "no deployed commit accepted: crossing not named: $OUTPUT"
expect_deployed "no deployed commit accepted"

# A deployed commit the clone does not hold is a named failure, not version zero.
begin_case 'deployed commit missing'
MISSING_COMMIT=0123456789abcdef0123456789abcdef01234567
FAKE_CHECK_OUTPUT="Deployed commit: $MISSING_COMMIT" run_subject "$CLONE" main
expect_failed "FAILED cannot read the release tags reaching the deployed commit $MISSING_COMMIT: " "deployed commit missing"
expect_not_deployed "deployed commit missing"

# An unknown option is a usage error.
begin_case 'unknown option'
run_subject "$CLONE" main --force
expect_status 2 "unknown option"
expect_not_deployed "unknown option"

# A clone pulled by hand past a release that raises the major, and not redeployed, stops at the deployed commit's
# release, not at local main's, and lists the commits the update would deploy.
begin_case 'pulled by hand'
new_clone
publish "fourth major"
tag_upstream v4.0.0
git_quiet -C "$CLONE" pull --ff-only
run_subject "$CLONE" main
expect_status 1 "pulled by hand"
expect_first_line "MAJOR_UPDATE v3.0.0..v4.0.0" "pulled by hand"
expect_no_stderr "pulled by hand"
grep -q "fourth major" <<<"$OUTPUT" || fail "pulled by hand: pending commit not listed: $OUTPUT"
expect_not_deployed "pulled by hand"
run_subject "$CLONE" main --cross-major
expect_status 0 "pulled by hand accepted"
expect_first_line "UP_TO_DATE $(head_of "$CLONE" HEAD)" "pulled by hand accepted"
grep -qx "CROSSED v3.0.0..v4.0.0" <<<"$OUTPUT" || fail "pulled by hand accepted: crossing not named: $OUTPUT"
expect_deployed "pulled by hand accepted"

# A clone with no local main, detached at the release origin/main is on, is at that release and updates.
begin_case 'no local main'
new_clone --branch v4.0.0
! git -C "$CLONE" show-ref --verify --quiet refs/heads/main || fail "no local main: the clone has a main"
run_subject "$CLONE" main
expect_status 0 "no local main"
expect_first_line "UP_TO_DATE $(head_of "$SEED" HEAD)" "no local main"
expect_no_stderr "no local main"
expect_deployed "no local main"

# A release whose major or minor is longer than Bash's integers hold is refused, even with --cross-major, and the tag
# is then removed so the cases below follow the releases above.
begin_case 'oversized tag'
new_clone
publish "oversized release"
for TAG in v99999999999999999999.0.0 v5.99999999999999999999.0; do
  tag_upstream "$TAG"
  for OPTION in "" --cross-major; do
    run_subject "$CLONE" main ${OPTION:+"$OPTION"}
    expect_failed "FAILED cannot compare release tag $TAG: " "oversized tag $TAG $OPTION"
    expect_not_deployed "oversized tag $TAG $OPTION"
  done
  git_quiet -C "$SEED" push origin ":refs/tags/$TAG"
  git_quiet -C "$SEED" tag -d "$TAG"
  git_quiet -C "$CLONE" tag -d "$TAG"
done

# A component's leading zeros do not count toward its length: a deployed commit whose only release is a zero-padded
# 3.0.0 is compared as 3.0.0.
begin_case 'leading zeros'
PADDED=v00000000000000000000003.0.0
PADDED_COMMIT=$(git -C "$CLONE" commit-tree -m "padded release" "HEAD^{tree}")
git_quiet -C "$CLONE" tag "$PADDED" "$PADDED_COMMIT"
FAKE_CHECK_OUTPUT="Deployed commit: $PADDED_COMMIT" run_subject "$CLONE" main
expect_status 1 "leading zeros"
expect_first_line "MAJOR_UPDATE $PADDED..v4.0.0" "leading zeros"
expect_no_stderr "leading zeros"
expect_not_deployed "leading zeros"
git_quiet -C "$CLONE" tag -d "$PADDED"

# A repository-selecting variable a shell or a git hook exported does not point the update at another clone.
begin_case 'repository variables'
new_clone
OTHER=$CLONE
OTHER_HEAD=$(head_of "$OTHER" HEAD)
new_clone
publish "change while GIT_DIR names another clone"
export GIT_DIR="$OTHER/.git" GIT_INDEX_FILE="$OTHER/.git/index" GIT_NAMESPACE="elsewhere"
run_subject "$CLONE" main
unset GIT_DIR GIT_INDEX_FILE GIT_NAMESPACE
expect_status 0 "repository variables"
[ "$(head_of "$CLONE" HEAD)" = "$(head_of "$SEED" HEAD)" ] || fail "repository variables: the named clone did not move"
[ "$(head_of "$OTHER" HEAD)" = "$OTHER_HEAD" ] || fail "repository variables: the other clone moved"
expect_deployed "repository variables"

# Windows reads an environment name in any case, so git there takes git_dir as GIT_DIR; the update unsets each spelling.
begin_case 'repository variables in any case'
new_clone
OTHER=$CLONE
OTHER_HEAD=$(head_of "$OTHER" HEAD)
new_clone
publish "change while git_dir names another clone"
export git_dir="$OTHER/.git" Git_Index_File="$OTHER/.git/index" Git_Namespace="elsewhere"
run_subject "$CLONE" main
unset git_dir Git_Index_File Git_Namespace
expect_status 0 "repository variables in any case"
[ "$(head_of "$CLONE" HEAD)" = "$(head_of "$SEED" HEAD)" ] || fail "repository variables in any case: the named clone did not move"
[ "$(head_of "$OTHER" HEAD)" = "$OTHER_HEAD" ] || fail "repository variables in any case: the other clone moved"
expect_deployed "repository variables in any case"

# A tag named like a branch the update reads stops it before the fetch, whether origin publishes it or the clone holds
# it, and never moves main. The tagged commit is not on origin's main, so a tag that stood in for the branch would
# fast-forward main to it.
begin_case 'tag shadows a branch'
SIDE=$(git -C "$SEED" commit-tree -p HEAD -m "not on main" "HEAD^{tree}")
expect_shadowed() {
  expect_status 1 "$2"
  [ "$OUTPUT" = "TAG_SHADOWS_BRANCH"$'\n'"$1" ] || fail "$2: expected TAG_SHADOWS_BRANCH and '$1', got: $OUTPUT"
  expect_no_stderr "$2"
  [ "$(git -C "$CLONE" rev-parse refs/heads/main)" = "$BEFORE" ] || fail "$2: main moved"
  [ "$(git -C "$CLONE" rev-parse refs/remotes/origin/main)" = "$FETCHED" ] || fail "$2: fetched anyway"
  expect_not_deployed "$2"
}
for NAME in origin/main main; do
  new_clone
  BEFORE=$(git -C "$CLONE" rev-parse refs/heads/main)
  FETCHED=$(git -C "$CLONE" rev-parse refs/remotes/origin/main)
  publish "change while origin has a tag $NAME"
  git_quiet -C "$SEED" push origin "$SIDE:refs/tags/$NAME"
  run_subject "$CLONE" main
  expect_shadowed "refs/tags/$NAME on origin" "remote tag $NAME"
  ! git -C "$CLONE" show-ref --verify --quiet "refs/tags/$NAME" || fail "remote tag $NAME: the tag was fetched"
  git_quiet -C "$SEED" push origin ":refs/tags/$NAME"
done
new_clone
BEFORE=$(git -C "$CLONE" rev-parse refs/heads/main)
FETCHED=$(git -C "$CLONE" rev-parse refs/remotes/origin/main)
git_quiet -C "$CLONE" tag main "$SIDE"
publish "change while the clone has a tag main"
run_subject "$CLONE" main
expect_shadowed "refs/tags/main in the clone" "local tag main"

# A shadowing tag origin publishes after the check is not fetched, and the update follows origin's main. A Git wrapper
# hides the tag from the check.
begin_case 'tag after the check'
new_clone
publish "change while a tag appears after the check"
git_quiet -C "$SEED" push origin "$SIDE:refs/tags/origin/main" "$SIDE:refs/tags/main"
cat >"$WRAPPER/git" <<EOF
#!/usr/bin/env bash
case " \$* " in
  *" ls-remote "*) exit 0 ;;
esac
exec "$(command -v git)" "\$@"
EOF
PATH="$WRAPPER:$PATH" run_subject "$CLONE" main
git_quiet -C "$SEED" push origin ":refs/tags/origin/main" ":refs/tags/main"
expect_status 0 "tag after the check"
[ "$(head_of "$CLONE" refs/heads/main)" = "$(head_of "$SEED" HEAD)" ] || fail "tag after the check: main is not origin's main"
for TAG in refs/tags/main refs/tags/origin/main; do
  ! git -C "$CLONE" show-ref --verify --quiet "$TAG" || fail "tag after the check: $TAG was fetched"
done
expect_deployed "tag after the check"

# A reading Git cannot give is a named failure, never an empty answer: the clone's tags, which would otherwise read as
# no shadowing tag, and the list of repository-selecting variables, which would otherwise leave every one in place.
begin_case 'readings git cannot give'
for READING in "for-each-ref --format=%(refname) refs/tags/main|FAILED cannot read the clone's tags: " \
  "rev-parse --local-env-vars|FAILED cannot list git's repository-selecting variables: "; do
  new_clone
  FETCHED=$(git -C "$CLONE" rev-parse origin/main)
  publish "change while git cannot give: ${READING%%|*}"
  cat >"$WRAPPER/git" <<EOF
#!/usr/bin/env bash
case " \$* " in
  *" ${READING%%|*} "*) echo "fatal: refused by the test" >&2; exit 128 ;;
esac
exec "$(command -v git)" "\$@"
EOF
  PATH="$WRAPPER:$PATH" run_subject "$CLONE" main
  expect_failed "${READING#*|}fatal: refused by the test" "git cannot give ${READING%%|*}"
  [ "$(git -C "$CLONE" rev-parse origin/main)" = "$FETCHED" ] || fail "git cannot give ${READING%%|*}: fetched"
  expect_not_deployed "git cannot give ${READING%%|*}"
done

# The release channel deploys the highest release tag origin/main holds through deploy.py --at with the tag's full
# name, lists the commits it adds to the deployed one, and leaves the clone on main at origin/main: the commits on main
# since, a pre-release tag, and a tag that is not a version do not move it.
begin_case 'release'
recorded_line() { sed -n "${1}p" "$CLONE/.fake-deployed" | tr -d '\r'; }
new_clone
BEFORE=$(head_of "$CLONE" HEAD)
publish "release four one"
tag_upstream v4.1.0
publish "release four two"
tag_upstream v4.2.0
RELEASE_COMMIT=$(git -C "$SEED" rev-parse "v4.2.0^{commit}")
publish "after the last release"
tag_upstream v4.3.0-rc.1
tag_upstream latest
LATEST_COMMIT=$(git -C "$SEED" rev-parse "latest^{commit}")
run_subject "$CLONE" release
expect_status 0 "release"
expect_first_line "UPDATED $BEFORE..v4.2.0" "release"
expect_no_stderr "release"
for PULLED in "release four one" "release four two"; do
  grep -q " $PULLED$" <<<"$OUTPUT" || fail "release: commit '$PULLED' not listed: $OUTPUT"
done
if grep -q -e "after the last release" -e "NOT_IN_RELEASE" <<<"$OUTPUT"; then
  fail "release: listed a commit the release does not carry: $OUTPUT"
fi
expect_deployed "release" "--all --at refs/tags/v4.2.0"
[ "$(recorded_line 1)" = "$RELEASE_COMMIT" ] || fail "release: deployed $(recorded_line 1), not v4.2.0"
[ "$(git -C "$CLONE" branch --show-current)" = "main" ] || fail "release: the clone is not on main"
[ "$(head_of "$CLONE" HEAD)" = "$(head_of "$SEED" HEAD)" ] || fail "release: main not fast-forwarded to origin/main"

# A later run is up to date with the release, whatever main gained since, and deploys it again.
begin_case 'release again'
publish "later on main"
run_subject "$CLONE" release
expect_status 0 "release again"
expect_first_line "UP_TO_DATE v4.2.0" "release again"
expect_deployed "release again" "--all --at refs/tags/v4.2.0"
[ "$(head_of "$CLONE" HEAD)" = "$(head_of "$SEED" HEAD)" ] || fail "release again: main not fast-forwarded"

# For one run, --channel main follows main as the main channel always has, and replaces the release channel's own
# pin, which the deployer refuses without --unpin.
begin_case 'main for one run'
run_subject "$CLONE" release --channel main
expect_status 0 "main for one run"
expect_first_line "UP_TO_DATE $(head_of "$CLONE" HEAD)" "main for one run"
expect_deployed "main for one run" "--all --unpin"
[ "$(recorded_line 1)" = "$(git -C "$CLONE" rev-parse HEAD)" ] || fail "main for one run: main's head not deployed"

# Back on the release channel, an installation that followed main past the release steps back to it, and the commits
# the release does not carry are named after NOT_IN_RELEASE.
begin_case 'step back'
FOLLOWED=$(head_of "$CLONE" HEAD)
run_subject "$CLONE" release
expect_status 0 "step back"
expect_first_line "UPDATED $FOLLOWED..v4.2.0" "step back"
grep -qx "NOT_IN_RELEASE v4.2.0..$FOLLOWED" <<<"$OUTPUT" || fail "step back: no NOT_IN_RELEASE line: $OUTPUT"
for LEFT in "after the last release" "later on main"; do
  grep -q " $LEFT$" <<<"$OUTPUT" || fail "step back: commit '$LEFT' not listed: $OUTPUT"
done
expect_deployed "step back" "--all --at refs/tags/v4.2.0"

# The configured main channel follows main as before, replacing the release channel's own pin, and --channel release
# deploys the release for one run.
begin_case 'configured main'
BEFORE=$(head_of "$CLONE" HEAD)
publish "main channel change"
run_subject "$CLONE" main
expect_status 0 "configured main"
expect_first_line "UPDATED $BEFORE..$(head_of "$SEED" HEAD)" "configured main"
grep -q " main channel change$" <<<"$OUTPUT" || fail "configured main: pulled commit not listed: $OUTPUT"
expect_deployed "configured main" "--all --unpin"
FOLLOWED=$(head_of "$CLONE" HEAD)
run_subject "$CLONE" main --channel release
expect_status 0 "release for one run"
expect_first_line "UPDATED $FOLLOWED..v4.2.0" "release for one run"
expect_deployed "release for one run" "--all --at refs/tags/v4.2.0"
[ "$(recorded_line 2)" = "refs/tags/v4.2.0" ] || fail "release for one run: the pin is $(recorded_line 2)"

# A pin the user made, at another commit than the release, stops the release channel after the fetch, before main
# moves or anything is deployed: one made by hand with a tag's short name, or with a full name that is not the last
# release its commit holds.
begin_case 'user pin'
V41=$(git -C "$SEED" rev-parse "v4.1.0^{commit}")
for PIN in "$V41 v4.1.0" "$LATEST_COMMIT refs/tags/latest" "$V41 main~5"; do
  BEFORE=$(git -C "$CLONE" rev-parse HEAD)
  FETCHED=$(git -C "$CLONE" rev-parse origin/main)
  publish "change over a pin at ${PIN#* }"
  printf '%s\n%s\n' "${PIN%% *}" "${PIN#* }" >"$CLONE/.fake-deployed"
  run_subject "$CLONE" release
  expect_status 1 "user pin ${PIN#* }"
  [ "$OUTPUT" = "PINNED ${PIN#* } ${PIN%% *}" ] || fail "user pin ${PIN#* }: expected only the PINNED line, got: $OUTPUT"
  expect_no_stderr "user pin ${PIN#* }"
  [ "$(git -C "$CLONE" rev-parse origin/main)" != "$FETCHED" ] || fail "user pin ${PIN#* }: not fetched"
  [ "$(git -C "$CLONE" rev-parse HEAD)" = "$BEFORE" ] || fail "user pin ${PIN#* }: main moved"
  expect_not_deployed "user pin ${PIN#* }"
done

# A user's pin at the release's own commit is the deployment the update makes, so it does not stop it, and the pin
# stays the user's, under the name the user gave it.
begin_case 'user pin at the release'
printf '%s\nv4.2.0\n' "$RELEASE_COMMIT" >"$CLONE/.fake-deployed"
run_subject "$CLONE" release
expect_status 0 "user pin at the release"
expect_first_line "UP_TO_DATE v4.2.0" "user pin at the release"
expect_deployed "user pin at the release" "--all --at refs/tags/v4.2.0"
[ "$(recorded_line 2)" = "v4.2.0" ] || fail "user pin at the release: the pin is $(recorded_line 2)"

# A user's pin check cannot read stops the release channel too, before the fetch, and --cross-major does not move it:
# without a deployed commit the update would otherwise stop only at the major release, and once accepted, deploy.
begin_case 'user pin check cannot read'
printf '%s\nv4.1.0\n' "$V41" >"$CLONE/.fake-deployed"
FETCHED=$(git -C "$CLONE" rev-parse origin/main)
for ARGUMENTS in "release" "release --cross-major"; do
  read -r -a SPLIT <<<"$ARGUMENTS"
  FAKE_CHECK_OUTPUT="" FAKE_CHECK_EXIT=1 run_subject "$CLONE" "${SPLIT[@]}"
  expect_failed "FAILED cannot read whether the installation is pinned: deploy.py check exited 1 and printed nothing" \
    "user pin check cannot read $ARGUMENTS"
  expect_not_deployed "user pin check cannot read $ARGUMENTS"
  [ "$(git -C "$CLONE" rev-parse origin/main)" = "$FETCHED" ] || fail "user pin check cannot read $ARGUMENTS: fetched"
  [ "$(recorded_line 2)" = "v4.1.0" ] || fail "user pin check cannot read $ARGUMENTS: the pin is $(recorded_line 2)"
done

# A user's pin check does not name, as when the pin was made after check ran, still stops the release channel: the
# deployer refuses to replace it through --at without --unpin, and the update ends with DEPLOY_PINNED after the
# fast-forward, deploying nothing.
begin_case 'user pin the release channel deploys over'
FAKE_CHECK_OUTPUT="Deployed commit: v4.1.0 ($V41)" run_subject "$CLONE" release
expect_status 1 "user pin the release channel deploys over"
expect_first_line "UPDATED $(git -C "$CLONE" rev-parse --short "$V41")..v4.2.0" "user pin the release channel deploys over"
[ "$(printf '%s\n' "$OUTPUT" | tail -n 1)" = "DEPLOY_PINNED v4.1.0 $V41" ] ||
  fail "user pin the release channel deploys over: expected DEPLOY_PINNED v4.1.0 $V41 last, got: $OUTPUT"
expect_deployed "user pin the release channel deploys over" "--all --at refs/tags/v4.2.0"
[ "$(recorded_line 2)" = "v4.1.0" ] || fail "user pin the release channel deploys over: the pin is $(recorded_line 2)"

# With --unpin the release channel moves a user's pin to the release through --at, passing --unpin to the deployer,
# which refuses to replace a user's pin without it.
begin_case 'user pin unpinned'
run_subject "$CLONE" release --unpin
expect_status 0 "user pin unpinned"
expect_first_line "UPDATED $(git -C "$CLONE" rev-parse --short "$V41")..v4.2.0" "user pin unpinned"
grep -q " release four two$" <<<"$OUTPUT" || fail "user pin unpinned: commit since the pin not listed: $OUTPUT"
expect_deployed "user pin unpinned" "--all --at refs/tags/v4.2.0 --unpin"
[ "$(recorded_line 2)" = "refs/tags/v4.2.0" ] || fail "user pin unpinned: the pin is $(recorded_line 2)"

# A release that raises the breaking component stops the release channel at that tag, listing the commits up to it
# and none after, until --cross-major.
begin_case 'release major'
BEFORE=$(git -C "$CLONE" rev-parse HEAD)
publish "fifth major"
tag_upstream v5.0.0
publish "after the fifth major"
run_subject "$CLONE" release
expect_status 1 "release major"
expect_first_line "MAJOR_UPDATE v4.2.0..v5.0.0" "release major"
expect_no_stderr "release major"
grep -q " fifth major$" <<<"$OUTPUT" || fail "release major: the release's commit not listed: $OUTPUT"
if grep -q "after the fifth major" <<<"$OUTPUT"; then fail "release major: listed a commit after the tag"; fi
[ "$(git -C "$CLONE" rev-parse HEAD)" = "$BEFORE" ] || fail "release major: main moved"
expect_not_deployed "release major"
run_subject "$CLONE" release --cross-major
expect_status 0 "release major accepted"
expect_first_line "UPDATED $(git -C "$CLONE" rev-parse --short "$RELEASE_COMMIT")..v5.0.0" "release major accepted"
grep -qx "CROSSED v4.2.0..v5.0.0" <<<"$OUTPUT" || fail "release major accepted: crossing not named: $OUTPUT"
expect_deployed "release major accepted" "--all --at refs/tags/v5.0.0"

# A user's pin the update deployed its release over is still the user's at the next release: the release channel
# stops at it, the deployer's refusal ends an update that could not read it with DEPLOY_PINNED, and only --unpin moves
# it to the release.
begin_case 'user pin at the release, then the next release'
V5=$(git -C "$SEED" rev-parse "v5.0.0^{commit}")
printf '%s\nv5.0.0\n' "$V5" >"$CLONE/.fake-deployed"
run_subject "$CLONE" release
expect_status 0 "user pin at the release before the next"
expect_deployed "user pin at the release before the next" "--all --at refs/tags/v5.0.0"
[ "$(recorded_line 2)" = "v5.0.0" ] || fail "user pin at the release before the next: the pin is $(recorded_line 2)"
publish "five one"
tag_upstream v5.1.0
run_subject "$CLONE" release
expect_status 1 "user pin at the next release"
[ "$OUTPUT" = "PINNED v5.0.0 $V5" ] || fail "user pin at the next release: expected only the PINNED line, got: $OUTPUT"
expect_not_deployed "user pin at the next release"
FAKE_CHECK_OUTPUT="Deployed commit: v5.0.0 ($V5)" run_subject "$CLONE" release
expect_status 1 "user pin at the next release unread"
[ "$(printf '%s\n' "$OUTPUT" | tail -n 1)" = "DEPLOY_PINNED v5.0.0 $V5" ] ||
  fail "user pin at the next release unread: expected DEPLOY_PINNED v5.0.0 $V5 last, got: $OUTPUT"
[ "$(recorded_line 2)" = "v5.0.0" ] || fail "user pin at the next release unread: the pin is $(recorded_line 2)"
run_subject "$CLONE" release --unpin
expect_status 0 "user pin at the next release unpinned"
expect_deployed "user pin at the next release unpinned" "--all --at refs/tags/v5.1.0 --unpin"
[ "$(recorded_line 2)" = "refs/tags/v5.1.0" ] ||
  fail "user pin at the next release unpinned: the pin is $(recorded_line 2)"

# With no deployed commit to read, the release channel stops at any release, and once --cross-major accepts it the
# update names the deployed commit unknown and lists the commits the release adds to local main, as the stop did.
begin_case 'release from no deployed commit'
BEFORE=$(git -C "$CLONE" rev-parse HEAD)
publish "sixth major"
tag_upstream v6.0.0
publish "after the sixth major"
FAKE_CHECK_OUTPUT="Deployed commit: unknown" run_subject "$CLONE" release
expect_status 1 "release from no deployed commit"
expect_first_line "MAJOR_UPDATE unknown..v6.0.0" "release from no deployed commit"
grep -q " sixth major$" <<<"$OUTPUT" || fail "release from no deployed commit: commit not listed: $OUTPUT"
[ "$(git -C "$CLONE" rev-parse HEAD)" = "$BEFORE" ] || fail "release from no deployed commit: main moved"
expect_not_deployed "release from no deployed commit"
FAKE_CHECK_OUTPUT="Deployed commit: unknown" run_subject "$CLONE" release --cross-major
expect_status 0 "release from no deployed commit accepted"
expect_first_line "UPDATED unknown..v6.0.0" "release from no deployed commit accepted"
expect_no_stderr "release from no deployed commit accepted"
[ "$(printf '%s\n' "$OUTPUT" | sed -n 2p)" = "$(git -C "$CLONE" log --oneline --no-decorate -1 v6.0.0)" ] ||
  fail "release from no deployed commit accepted: the release's commit does not follow UPDATED: $OUTPUT"
if grep -q -e "after the sixth major" -e " fifth major$" -e "NOT_IN_RELEASE" <<<"$OUTPUT"; then
  fail "release from no deployed commit accepted: listed a commit the release does not add to local main: $OUTPUT"
fi
grep -qx "CROSSED unknown..v6.0.0" <<<"$OUTPUT" ||
  fail "release from no deployed commit accepted: crossing not named: $OUTPUT"
expect_deployed "release from no deployed commit accepted" "--all --at refs/tags/v6.0.0"

# Without a release tag on origin/main the release channel has nothing to deploy: it stops after the fetch, and the
# main channel still follows main.
begin_case 'no release'
UNTAGGED="$FIXTURE/untagged.git"
git_quiet init --bare -b main "$UNTAGGED"
git_quiet -C "$SEED" push "$UNTAGGED" main
case_number=$((case_number + 1))
CLONE="$FIXTURE/clone $case_number (untagged)"
git_quiet clone "$UNTAGGED" "$CLONE"
git -C "$CLONE" rev-parse HEAD >"$CLONE/.fake-deployed"
[ -z "$(git -C "$CLONE" tag)" ] || fail "no release: the clone holds tags"
run_subject "$CLONE" release
expect_status 1 "no release"
[ "$OUTPUT" = "NO_RELEASE" ] || fail "no release: expected only NO_RELEASE, got: $OUTPUT"
expect_no_stderr "no release"
expect_not_deployed "no release"
run_subject "$CLONE" release --channel main
expect_status 0 "no release on main"
expect_first_line "UP_TO_DATE $(head_of "$CLONE" HEAD)" "no release on main"
expect_deployed "no release on main"

# An installation as the deployer lays it out under the skills folder the first argument names: the update skill's
# script, and ejryan89-skill-core's scripts beside it unless the second argument is "without the core".
install_update() {
  mkdir -p "$1/update-coding-agent-skills/scripts"
  cp -- "$SUBJECT" "$1/update-coding-agent-skills/scripts/update.sh"
  if [ "${2:-}" != "without the core" ]; then
    mkdir -p "$1/ejryan89-skill-core/scripts"
    for MODULE in "$SCRIPT_DIR/../../ejryan89-skill-core/scripts/"*.py; do
      case "${MODULE##*/}" in
        test_*) ;;
        *) cp -- "$MODULE" "$1/ejryan89-skill-core/scripts/" ;;
      esac
    done
  fi
}

# update.sh runs nothing from the clone but deploy.py at its root, so a later release may move any other file.
begin_case 'clone paths'
FROM_CLONE=$(grep -o -E '\$\{?CLONE\}?/[^" ]*' "$SUBJECT" | sort -u)
[ "$FROM_CLONE" = "\$CLONE/deploy.py" ] || fail "clone paths: update.sh names other paths in the clone: $FROM_CLONE"

# An installed script reads releases with the ejryan89-skill-core deployed beside it, so a clone already holding a
# release that moved ejryan89-skill-core, pulled by hand before the update, still updates to it.
begin_case 'helper moved in the clone'
new_clone
BEFORE=$(head_of "$CLONE" HEAD)
git_quiet -C "$SEED" mv skills/ejryan89-skill-core skills/moved-core
git_quiet -C "$SEED" commit -m "move the core"
git_quiet -C "$SEED" push origin main
tag_upstream v6.0.1
git_quiet -C "$CLONE" pull --ff-only
[ ! -e "$CLONE/skills/ejryan89-skill-core" ] || fail "helper moved in the clone: the clone still holds the core"
INSTALLED_SKILLS="$FIXTURE/installed (moved core)/skills"
install_update "$INSTALLED_SKILLS"
SUBJECT="$INSTALLED_SKILLS/update-coding-agent-skills/scripts/update.sh" run_subject "$CLONE" release
expect_status 0 "helper moved in the clone"
expect_first_line "UPDATED $BEFORE..v6.0.1" "helper moved in the clone"
expect_no_stderr "helper moved in the clone"
expect_deployed "helper moved in the clone" "--all --at refs/tags/v6.0.1"

# An installation without ejryan89-skill-core beside it is a named failure before the clone is read.
begin_case 'no core beside the script'
new_clone
INSTALLED_SKILLS="$FIXTURE/installed (no core)/skills"
install_update "$INSTALLED_SKILLS" "without the core"
SUBJECT="$INSTALLED_SKILLS/update-coding-agent-skills/scripts/update.sh" run_subject "$CLONE" release
expect_failed "FAILED no ejryan89-skill-core release_tags.py deployed beside update.sh: " "no core beside the script"
expect_nothing_run "no core beside the script"

# A release tag that cannot be read after the tags named it, here deleted by a git wrapper just before update.sh reads
# its commit, is a named failure that ends with git's reason.
begin_case 'release commit unreadable'
new_clone
TAG_WRAPPER="$FIXTURE/tag wrapper"
mkdir -p "$TAG_WRAPPER"
cat >"$TAG_WRAPPER/git" <<EOF
#!/usr/bin/env bash
case " \$* " in
  *" rev-parse --verify refs/tags/v6.0.1^{commit} "*) "$(command -v git)" -C "$CLONE" tag -d v6.0.1 >/dev/null ;;
esac
exec "$(command -v git)" "\$@"
EOF
chmod +x "$TAG_WRAPPER/git"
PATH="$TAG_WRAPPER:$PATH" run_subject "$CLONE" release
expect_failed "FAILED cannot read the commit of refs/tags/v6.0.1: fatal: Needed a single revision"   "release commit unreadable"
expect_not_deployed "release commit unreadable"

# The deploy can back up the installed skill directory the script runs from, and the temporary copy is removed.
begin_case 'self backup'
new_clone
install_update "$FIXTURE/installed (self backup)/skills"
INSTALLED="$FIXTURE/installed (self backup)/skills/update-coding-agent-skills"
mkdir -p "$FIXTURE/tmp"
export FAKE_DEPLOY_LOG="$FIXTURE/deploy-$case_number.log"
: >"$FAKE_DEPLOY_LOG"
set +e
OUTPUT=$(TMPDIR="$FIXTURE/tmp" FAKE_DEPLOY_MOVE="$INSTALLED" bash "$INSTALLED/scripts/update.sh" "$CLONE" main 2>"$FIXTURE/stderr")
status=$?
set -e
expect_status 0 "self backup"
[ -d "$INSTALLED.bak" ] || fail "self backup: installed directory was not moved"
[ -z "$(ls -A "$FIXTURE/tmp")" ] || fail "self backup: temporary copy left behind: $(ls -A "$FIXTURE/tmp")"
expect_deployed "self backup"

echo "update.sh tests passed"
