#!/usr/bin/env bash
set -Eeuo pipefail

# The fixture and the helpers every update.sh suite shares. Each test_update_*.sh suite sources this file first, and
# it is a suite itself: run directly, it goes on to test its own harness and isolation. Its name makes it a suite, so the
# deployer leaves it out of what it deploys, as it leaves every suite.

# A shell ended by a signal it cannot trap, or by Windows rather than through its own exit, runs no EXIT trap and leaves
# only its status. So the suite runs its body in a child bash that traces every command it runs to a file, and when the
# body ends without its EXIT trap, this outer run names the exit and the last commands the body ran. A child run of the
# suite that sets UPDATE_TEST_BODY empty runs its body directly, untraced.
#
# The status this run reads for the body is not evidence on Windows: Git Bash reads a child bash that Windows ended, as
# it ends one with 0x80000004 on a CI runner, as exit 0. So the body's EXIT trap records its own status in the ended file, this run
# exits with that, and a body that recorded none fails the suite whatever status it was read as.
if [ -z "${UPDATE_TEST_BODY+set}" ]; then
  TRACE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/update-skills-trace.XXXXXX") || {
    printf 'FAIL: the suite could not make its trace folder\n' >&2
    exit 1
  }
  status=0
  UPDATE_TEST_BODY=$TRACE_DIR bash "$0" "$@" || status=$?
  if [ -s "$TRACE_DIR/ended" ]; then
    status=$(<"$TRACE_DIR/ended")
  else
    printf 'FAIL: the suite exited %s without its EXIT trap; the last commands it ran:\n' "$status" >&2
    tail -n 20 "$TRACE_DIR/trace" >&2 || true
    [ "$status" -ne 0 ] || status=1
  fi
  rm -rf -- "$TRACE_DIR"
  exit "$status"
fi

# The suite traces its own run on stderr before anything else runs: a START line, a CASE line as each case begins, and
# an END line from the EXIT trap on every exit, with its status and the case it ended in. A run that ends with none of
# them was never started or lost its output; one with START and no END was ended without its EXIT trap, and the outer
# run's FAIL line after them names its exit and the commands it ran last.
SUITE=${0##*/}
printf 'START %s %s\n' "$SUITE" "$$" >&2
CASE="setup"
begin_case() {
  CASE=$1
  printf 'CASE %s\n' "$1" >&2
}
# A command that fails can print nothing, so an exit set -e makes would leave only its status. The ERR trap keeps the
# last command that failed, with the file it is in, and an exit no FAIL line announced names its status, the case, and
# that command before the fixture is removed.
FAILED_COMMAND=""
REPORTED=""
FIXTURE=""
trap 'FAILED_COMMAND="${BASH_SOURCE[0]##*/} line $LINENO: $BASH_COMMAND"' ERR
end_suite() {
  local status=$?
  if [ -n "${UPDATE_TEST_BODY:-}" ]; then
    printf '%s\n' "$status" >"$UPDATE_TEST_BODY/ended"
  fi
  if [ "$status" -ne 0 ] && [ -z "$REPORTED" ]; then
    printf 'FAIL: the suite exited %s in case %s; the last command that failed: %s\n' "$status" "$CASE" \
      "${FAILED_COMMAND:-none}" >&2
  fi
  printf 'END %s %s: exit %s in case %s\n' "$SUITE" "$$" "$status" "$CASE" >&2
  if [ -n "$FIXTURE" ]; then
    rm -rf -- "$FIXTURE"
  fi
}
trap end_suite EXIT
# A signal ends the suite through the EXIT trap with 128 and the signal's number, so its exit is named as well.
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
# The traced body writes each command, with its file and line, to the trace file the outer run reads.
if [ -n "${UPDATE_TEST_BODY:-}" ]; then
  exec {TRACE_FD}>"$UPDATE_TEST_BODY/trace"
  BASH_XTRACEFD=$TRACE_FD
  PS4='+ ${BASH_SOURCE[0]##*/} line $LINENO: '
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
# names and the ref as its pin, keeping a user's recorded pin's ref at its own commit unless --unpin is given, as the
# real one does, and check names that commit and the pin, or prints
# $FAKE_CHECK_OUTPUT when it is set, prints $FAKE_CHECK_STDERR to stderr, as the real one prints its errors, when it is
# set, and exits with $FAKE_CHECK_EXIT. Like the real one, it refuses to replace a
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
    if "FAKE_CHECK_STDERR" in os.environ:
        for line in ("", os.environ["FAKE_CHECK_STDERR"], ""):
            print(line, file=sys.stderr)
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
ref = pin if at and pin and named == commit and "--unpin" not in arguments and not release_pin() else at
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

# Version tags are published upstream only, so the clone must fetch them to see either side's version.
tag_upstream() {
  git_quiet -C "$SEED" tag "$1"
  git_quiet -C "$SEED" push origin "$1"
}

# The releases test_update_majors.sh's cases publish, in order, each a commit's message and, after a bar, its tag. A
# suite whose cases start from a later release publishes them through that release first, with one push.
RELEASE_HISTORY=("first release|v0.1.0" "breaking change|v0.2.0" "patch change|v0.2.3" "first stable|v1.0.0"
  "after first stable|v2x.0.0" "minor stable|v1.5.0" "second major|v2.0.0" "third major candidate|v3.0.0-rc.1"
  "third major|v3.0.0" "fourth major|v4.0.0")
publish_through() {
  local entry message
  for entry in "${RELEASE_HISTORY[@]}"; do
    message=${entry%%|*}
    printf '%s\n' "$message" >>"$SEED/notes.txt"
    git_quiet -C "$SEED" add notes.txt
    git_quiet -C "$SEED" commit -m "$message"
    git_quiet -C "$SEED" tag "${entry#*|}"
    [ "${entry#*|}" != "$1" ] || break
  done
  [ "${entry#*|}" = "$1" ] || fail "publish_through: no release $1 in the history"
  git_quiet -C "$SEED" push origin main --tags
}

# A suite that sources this file goes on to its own cases; run directly, it tests its harness and isolation.
if [ "${BASH_SOURCE[0]}" != "$0" ]; then
  return 0
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
  grep -q -x -E "START test_update_fixture\.sh [0-9]+" <<<"$(head -n 1 "$FIXTURE/child.err")" ||
    fail "$3: no START line first: $(<"$FIXTURE/child.err")"
  grep -q -x -F -e "END test_update_fixture.sh $(head -n 1 "$FIXTURE/child.err" | cut -d ' ' -f 3): exit $1 in case $2" \
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
grep -q -x -E "FAIL: the suite exited 4 in case setup; the last command that failed: test_update_fixture\\.sh line [0-9]+: mkdir -p \"\\\$HOME/no-hooks\"" \
  "$FIXTURE/child.err" || fail "silent failure: the command not named: $(<"$FIXTURE/child.err")"
expect_traced 4 setup "silent failure"

# A failure before the fixture exists, here of mktemp, is named the same way.
begin_case 'failure before the fixture'
run_setup_child mktemp '*' 'exit 4'
[ "$status" -eq 4 ] || fail "failure before the fixture: expected exit 4, got $status: $(<"$FIXTURE/child.err")"
grep -q -x -F -e 'FAIL: the suite exited 4 in case setup; the last command that failed: test_update_fixture.sh line '"$(grep -n '^FIXTURE=$(mktemp' "$SCRIPT_DIR/${0##*/}" | cut -d : -f 1)"': FIXTURE=$(mktemp -d "${TMPDIR:-/tmp}/update-skills-test.XXXXXX")' \
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
grep -q -x -E "\+ test_update_fixture\.sh line [0-9]+: mkdir -p '?[^ ].*/no-hooks'?" <<<"$(tail -n 1 "$FIXTURE/child.err")" ||
  fail "end without the EXIT trap: the last command not named last: $(<"$FIXTURE/child.err")"
! grep -q "^END " "$FIXTURE/child.err" || fail "end without the EXIT trap: an END line: $(<"$FIXTURE/child.err")"
[ -z "$(find "$CHILD_TMP" -maxdepth 1 -name 'update-skills-trace.*')" ] ||
  fail "end without the EXIT trap: the trace folder left behind: $(ls -A "$CHILD_TMP")"

# Git Bash reads a body that Windows ended as exit 0, so a body that recorded no exit fails the suite whatever status it
# was read as. The wrapper ends the body through Windows with the status a CI runner ended one with, 0x80000004.
case "${OSTYPE:-}" in
  msys* | cygwin*)
    begin_case 'ended by Windows'
    run_setup_child mkdir '*"/no-hooks "*' 'python -c "import ctypes, sys
k = ctypes.windll.kernel32
k.OpenProcess.restype = ctypes.c_void_p
k.TerminateProcess(ctypes.c_void_p(k.OpenProcess(1, False, int(sys.argv[1]))), ctypes.c_uint(0x80000004))" \
  "$(<"/proc/$PPID/winpid")"; exit 0' traced
    [ "$status" -ne 0 ] || fail "ended by Windows: the suite passed: $(<"$FIXTURE/child.err")"
    grep -q -x -E "FAIL: the suite exited [0-9]+ without its EXIT trap; the last commands it ran:" "$FIXTURE/child.err" ||
      fail "ended by Windows: the exit not named: $(<"$FIXTURE/child.err")"
    ! grep -q "^END " "$FIXTURE/child.err" || fail "ended by Windows: an END line: $(<"$FIXTURE/child.err")"
    ;;
esac

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

echo "update.sh fixture tests passed"
