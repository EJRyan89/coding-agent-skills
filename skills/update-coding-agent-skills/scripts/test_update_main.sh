#!/usr/bin/env bash
set -Eeuo pipefail

# update.sh's usage, and the main channel over a clone in each state, before any release is tagged.
# test_update_fixture.sh makes the fixture and the helpers every update.sh suite shares.
# shellcheck source=skills/update-coding-agent-skills/scripts/test_update_fixture.sh
source "$(dirname "$0")/test_update_fixture.sh"

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
expect_failed "FAILED cannot read $PLAIN as a git repository: fatal: not a git repository" "not a repository"
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

# A clone left on a branch whose deploy.py is not main's, as another author's pull request checked out to look at it,
# never runs that deploy.py: the pin is read with main's, and the deployment runs main's after the switch.
begin_case "branch's deployer"
new_clone
checkout_branch_deployer topic
publish "change over a branch's deployer"
run_subject "$CLONE" main
expect_status 0 "branch's deployer"
expect_main_deployer "branch's deployer"
[ "$(git -C "$CLONE" branch --show-current)" = "main" ] || fail "branch's deployer: not switched to main"
[ "$(head_of "$CLONE" HEAD)" = "$(head_of "$SEED" HEAD)" ] || fail "branch's deployer: main not fast-forwarded"
expect_deployed "branch's deployer"

# The release channel reads the pin the same way; here it stops after the fetch, as no release is tagged yet.
begin_case "branch's deployer on the release channel"
new_clone
checkout_branch_deployer topic
run_subject "$CLONE" release
expect_status 1 "branch's deployer on the release channel"
expect_first_line "NO_RELEASE" "branch's deployer on the release channel"
expect_main_deployer "branch's deployer on the release channel"
[ "$(git -C "$CLONE" branch --show-current)" = "topic" ] || fail "branch's deployer on the release channel: switched"

# A clone with no local main yet reads the pin with origin/main's deploy.py as the clone last fetched it.
begin_case "branch's deployer without local main"
new_clone
checkout_branch_deployer topic
git_quiet -C "$CLONE" branch -D main
publish "change over a branch's deployer without local main"
run_subject "$CLONE" main
expect_status 0 "branch's deployer without local main"
expect_main_deployer "branch's deployer without local main"
[ "$(git -C "$CLONE" branch --show-current)" = "main" ] || fail "branch's deployer without local main: not on main"
expect_deployed "branch's deployer without local main"

# The runs that stop after the pin is read never run the branch's deploy.py either: an origin that cannot be reached
# (FETCH_FAILED), a main with commits origin/main lacks (NOT_FAST_FORWARD), and a fetch the clone cannot take (FAILED).
begin_case "branch's deployer, fetch failed"
new_clone
checkout_branch_deployer topic
git_quiet -C "$CLONE" remote set-url origin "$FIXTURE/missing.git"
run_subject "$CLONE" main
expect_status 1 "branch's deployer, fetch failed"
expect_first_line "FETCH_FAILED" "branch's deployer, fetch failed"
expect_main_deployer "branch's deployer, fetch failed"
expect_not_deployed "branch's deployer, fetch failed"

begin_case "branch's deployer, diverged"
new_clone
git_quiet -C "$CLONE" commit --allow-empty -m "local work"
checkout_branch_deployer topic
publish "change over a diverged main"
run_subject "$CLONE" main
expect_status 1 "branch's deployer, diverged"
expect_first_line "NOT_FAST_FORWARD" "branch's deployer, diverged"
expect_main_deployer "branch's deployer, diverged"
[ "$(git -C "$CLONE" branch --show-current)" = "topic" ] || fail "branch's deployer, diverged: branch switched"
expect_not_deployed "branch's deployer, diverged"

begin_case "branch's deployer, failed"
new_clone
checkout_branch_deployer topic
publish "change behind a stale lock over a branch's deployer"
mkdir -p "$CLONE/.git/refs/remotes/origin"
: >"$CLONE/.git/refs/remotes/origin/main.lock"
run_subject "$CLONE" main
expect_failed "FAILED remote_refs.py fetch failed: error: cannot lock ref 'refs/remotes/origin/main'" \
  "branch's deployer, failed"
expect_main_deployer "branch's deployer, failed"

# A clone with neither main nor origin/main has no main to read the pin with, so nothing runs.
begin_case "no main to read the pin with"
new_clone
checkout_branch_deployer topic
git_quiet -C "$CLONE" branch -D main
git_quiet -C "$CLONE" update-ref -d refs/remotes/origin/main
run_subject "$CLONE" main
expect_failed "FAILED cannot read the pin with main's deploy.py: the clone has neither refs/heads/main nor refs/remotes/origin/main" \
  "no main to read the pin with"
expect_nothing_run "no main to read the pin with"

# Main's tree that cannot be written out, here as git refuses the archive or mktemp the folder, stops before anything
# runs, and leaves no folder behind.
begin_case "main's tree not written out"
new_clone
checkout_branch_deployer topic
ARCHIVE_WRAPPER="$FIXTURE/archive wrapper"
mkdir -p "$ARCHIVE_WRAPPER" "$FIXTURE/tmp for the archive"
cat >"$ARCHIVE_WRAPPER/git" <<EOF
#!/usr/bin/env bash
case " \$* " in
  *" archive "*) echo "error: archive refused by the test" >&2; exit 1 ;;
esac
exec "$(command -v git)" "\$@"
EOF
chmod +x "$ARCHIVE_WRAPPER/git"
TMPDIR="$FIXTURE/tmp for the archive" PATH="$ARCHIVE_WRAPPER:$PATH" run_subject "$CLONE" main
expect_failed "FAILED cannot write main's tree out to read the pin with its deploy.py: error: archive refused by the test" \
  "main's tree not written out"
expect_nothing_run "main's tree not written out"
[ -z "$(ls -A "$FIXTURE/tmp for the archive")" ] ||
  fail "main's tree not written out: left behind: $(ls -A "$FIXTURE/tmp for the archive")"
FOLDER_WRAPPER="$FIXTURE/folder mktemp wrapper"
mkdir -p "$FOLDER_WRAPPER"
cat >"$FOLDER_WRAPPER/mktemp" <<EOF
#!/usr/bin/env bash
if [ "\${1:-}" = -d ]; then
  echo "mktemp: folder refused by the test" >&2
  exit 1
fi
exec "$(command -v mktemp)" "\$@"
EOF
chmod +x "$FOLDER_WRAPPER/mktemp"
TMPDIR="$FIXTURE/tmp for the archive" PATH="$FOLDER_WRAPPER:$PATH" run_subject "$CLONE" main
expect_failed "FAILED cannot create a temporary folder for main's tree: mktemp: folder refused by the test" \
  "main's folder not made"
expect_nothing_run "main's folder not made"
[ -z "$(ls -A "$FIXTURE/tmp for the archive")" ] ||
  fail "main's folder not made: left behind: $(ls -A "$FIXTURE/tmp for the archive")"

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

# The check for uncommitted changes never writes the clone's index: a tracked file whose timestamp moved but whose
# content did not is one a status with optional locks refreshes in the index and writes back under its lock. The
# edit stops the run at its DIRTY line, before the checkout and fast-forward that write the index on purpose.
begin_case 'status leaves the index'
new_clone
touch -t 200001010000 "$CLONE/deploy.py"
printf 'edit\n' >>"$CLONE/notes.txt"
INDEX_BEFORE=$(cksum <"$CLONE/.git/index")
run_subject "$CLONE" main
expect_status 1 "status leaves the index"
expect_first_line "DIRTY" "status leaves the index"
[ "$(cksum <"$CLONE/.git/index")" = "$INDEX_BEFORE" ] || fail "status leaves the index: the index was rewritten"

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

# An origin that wants a credential fails the update at once, as FETCH_FAILED, and starts none of the askpass helpers
# GIT_ASKPASS, SSH_ASKPASS, or core.askPass name, which would otherwise answer git or wait for a person. The origin is a
# local HTTP server that answers every request with 401 and a Basic challenge; it stops once its stop file appears.
begin_case 'origin wants a credential'
case "${OSTYPE:-}" in
  msys* | cygwin*) TEST_PYTHON=python ;;
  *) TEST_PYTHON=python3 ;;
esac
# The origin binds without HTTPServer's reverse lookup of its address, which can stall on a CI runner, and prints
# where it is stuck if it has not written its port file within 8 seconds, so the case's failure names the line.
cat >"$FIXTURE/asking_origin.py" <<'EOF'
import faulthandler, sys
faulthandler.dump_traceback_later(8, file=sys.stderr)
import http.server, os, socketserver, time
class Origin(http.server.HTTPServer):
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
class Unauthorized(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="test"')
        self.send_header("Content-Length", "0")
        self.end_headers()
    def log_message(self, *args):
        pass
server = Origin(("127.0.0.1", 0), Unauthorized)
server.timeout = 0.2
with open(sys.argv[1] + ".tmp", "w") as port:
    port.write(str(server.server_address[1]))
os.replace(sys.argv[1] + ".tmp", sys.argv[1])
faulthandler.cancel_dump_traceback_later()
ends = time.monotonic() + 300
while not os.path.exists(sys.argv[2]) and time.monotonic() < ends:
    server.handle_request()
EOF
"$TEST_PYTHON" "$FIXTURE/asking_origin.py" "$FIXTURE/origin port" "$FIXTURE/origin stop" 2>"$FIXTURE/origin stderr" &
SERVER_PID=$!
for _ in $(seq 1 100); do
  [ ! -s "$FIXTURE/origin port" ] || break
  sleep 0.1
done
if [ ! -s "$FIXTURE/origin port" ]; then
  # Say which Python ran, whether it was still starting or had ended, and what it wrote, so a CI leg's failure names
  # its cause.
  if kill -0 "$SERVER_PID" 2>/dev/null; then
    ORIGIN_STATE="was still starting after 10 seconds"
    kill "$SERVER_PID" 2>/dev/null || true
  else
    ORIGIN_STATUS=0
    wait "$SERVER_PID" || ORIGIN_STATUS=$?
    ORIGIN_STATE="exited with status $ORIGIN_STATUS"
  fi
  fail "origin wants a credential: the test origin did not start: $(command -v "$TEST_PYTHON") $ORIGIN_STATE;" \
    "its stderr: $(tr '\n' ' ' <"$FIXTURE/origin stderr")"
fi
cat >"$FIXTURE/askpass helper" <<EOF
#!/bin/sh
echo started >>"$FIXTURE/askpass ran"
echo answer
EOF
chmod +x "$FIXTURE/askpass helper"
new_clone
git_quiet -C "$CLONE" remote set-url origin "http://127.0.0.1:$(<"$FIXTURE/origin port")/repository.git"
git_quiet -C "$CLONE" config core.askPass "$FIXTURE/askpass helper"
GIT_ASKPASS="$FIXTURE/askpass helper" SSH_ASKPASS="$FIXTURE/askpass helper" SSH_ASKPASS_REQUIRE=force \
  NO_PROXY=127.0.0.1 no_proxy=127.0.0.1 run_subject "$CLONE" main
: >"$FIXTURE/origin stop"
wait "$SERVER_PID" || true
expect_status 1 "origin wants a credential"
expect_first_line "FETCH_FAILED" "origin wants a credential"
expect_no_stderr "origin wants a credential"
[ ! -e "$FIXTURE/askpass ran" ] || fail "origin wants a credential: an askpass helper ran"
expect_not_deployed "origin wants a credential"

# A fetch that origin answered but the clone could not take, here a stale lock on the ref it writes, is the clone's
# failure, named with git's reason, never FETCH_FAILED, which sends the user to sign in to origin.
begin_case 'ref lock in the clone'
new_clone
publish "change behind a stale lock"
mkdir -p "$CLONE/.git/refs/remotes/origin"
: >"$CLONE/.git/refs/remotes/origin/main.lock"
run_subject "$CLONE" main
expect_failed "FAILED remote_refs.py fetch failed: error: cannot lock ref 'refs/remotes/origin/main'" "ref lock"
expect_not_deployed "ref lock"

# A Git command that fails outright is a named failure.
begin_case 'status failed'
new_clone
printf 'not an index' >"$CLONE/.git/index"
run_subject "$CLONE" main
expect_failed "FAILED git status failed: " "status failed"
expect_not_deployed "status failed"

# A script that cannot make its temporary copy stops before any Git command. TMPDIR names a folder no file can be made
# in, and a mktemp wrapper does what macOS's mktemp does: given no template, it makes the file in a folder of its own,
# the user's temporary folder there, ahead of TMPDIR. So the copy fails on every platform only while update.sh names its
# files from a template under TMPDIR. Where the platform has user ids, that is a folder of the user's own that the user
# cannot write, which private_folder.py accepts, since a folder that does not exist is refused before mktemp runs; on
# Windows, where a folder's mode bits do not stop a write, it is a folder that does not exist.
begin_case 'no temporary copy'
new_clone
NO_COPY_TMPDIR="$FIXTURE/absent temporary directory"
case "${OSTYPE:-}" in
  msys* | cygwin*) ;;
  *)
    NO_COPY_TMPDIR="$FIXTURE/read-only temporary directory"
    mkdir -p "$NO_COPY_TMPDIR"
    chmod 555 "$NO_COPY_TMPDIR"
    ;;
esac
DARWIN_MKTEMP="$FIXTURE/darwin mktemp wrapper"
mkdir -p "$DARWIN_MKTEMP" "$FIXTURE/darwin user temp"
cat >"$DARWIN_MKTEMP/mktemp" <<EOF
#!/usr/bin/env bash
[ \$# -ne 0 ] || set -- "$FIXTURE/darwin user temp/tmp.XXXXXXXXXX"
exec "$(command -v mktemp)" "\$@"
EOF
chmod +x "$DARWIN_MKTEMP/mktemp"
export FAKE_DEPLOY_LOG="$FIXTURE/deploy-$case_number.log"
: >"$FAKE_DEPLOY_LOG"
set +e
OUTPUT=$(TMPDIR="$NO_COPY_TMPDIR" PATH="$DARWIN_MKTEMP:$PATH" bash "$SUBJECT" "$CLONE" main 2>"$FIXTURE/stderr")
status=$?
set -e
expect_failed "FAILED cannot create a temporary copy of update.sh: " "no temporary copy"
expect_not_deployed "no temporary copy"
case "${OSTYPE:-}" in
  msys* | cygwin*) ;;
  *)
    chmod 755 "$NO_COPY_TMPDIR"
    # A TMPDIR that does not exist is one another account could make first, so it is refused before mktemp.
    begin_case 'temporary directory that does not exist'
    TMPDIR="$FIXTURE/absent temporary directory" run_subject "$CLONE" main
    expect_failed "FAILED cannot make update.sh's temporary files: the temporary directory $FIXTURE/absent temporary directory cannot be examined: " \
      "temporary directory that does not exist"
    expect_nothing_run "temporary directory that does not exist"
    ;;
esac

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

# The same holds for the file that keeps git's errors, which update.sh makes after the deployer's: a mktemp wrapper
# makes the copy and the deployer's file and refuses the third call, and both files made are removed.
begin_case 'no file for git errors'
GIT_MKTEMP_WRAPPER="$FIXTURE/git mktemp wrapper"
mkdir -p "$GIT_MKTEMP_WRAPPER" "$FIXTURE/tmp for git errors"
cat >"$GIT_MKTEMP_WRAPPER/mktemp" <<EOF
#!/usr/bin/env bash
made=\$(cat "$GIT_MKTEMP_WRAPPER/made" 2>/dev/null || echo 0)
if [ "\$made" -ge 2 ]; then
  echo "mktemp: refused by the test" >&2
  exit 1
fi
echo \$((made + 1)) >"$GIT_MKTEMP_WRAPPER/made"
exec "$(command -v mktemp)" "\$@"
EOF
chmod +x "$GIT_MKTEMP_WRAPPER/mktemp"
TMPDIR="$FIXTURE/tmp for git errors" PATH="$GIT_MKTEMP_WRAPPER:$PATH" run_subject "$CLONE" main
expect_failed "FAILED cannot create a temporary file for git's errors: mktemp: refused by the test" \
  "no file for git errors"
expect_nothing_run "no file for git errors"
[ "$(cat "$GIT_MKTEMP_WRAPPER/made")" = "2" ] || fail "no file for git errors: the first two files were not made"
[ -z "$(ls -A "$FIXTURE/tmp for git errors")" ] ||
  fail "no file for git errors: temporary files left behind: $(ls -A "$FIXTURE/tmp for git errors")"

# Every temporary file, the copy, the deployer's errors, git's errors, and the folder main's tree is written to for a
# clone on another branch, is named from a template under TMPDIR: a mktemp wrapper refuses any call that names none
# there, as a bare mktemp on macOS would make its file elsewhere, so a full update from such a branch deploys only while
# each of the four names its template. Each is removed when the update ends. Where the platform has user ids, the
# template names TMPDIR resolved through its links, as private_folder.py judged it.
begin_case 'every file under TMPDIR'
new_clone
checkout_branch_deployer topic
UNDER_TMPDIR="$FIXTURE/tmp for every file"
TEMPLATE_MKTEMP="$FIXTURE/template mktemp wrapper"
mkdir -p "$TEMPLATE_MKTEMP" "$UNDER_TMPDIR"
UNDER_TMPDIR_RESOLVED=$(cd -- "$UNDER_TMPDIR" && pwd -P)
cat >"$TEMPLATE_MKTEMP/mktemp" <<EOF
#!/usr/bin/env bash
NAMED=\${1:-}
[ "\$NAMED" != -d ] || NAMED=\${2:-}
case "\$NAMED" in
  "$UNDER_TMPDIR/"* | "$UNDER_TMPDIR_RESOLVED/"*) exec "$(command -v mktemp)" "\$@" ;;
esac
echo "mktemp: no template under TMPDIR: \$*" >&2
exit 1
EOF
chmod +x "$TEMPLATE_MKTEMP/mktemp"
TMPDIR="$UNDER_TMPDIR" PATH="$TEMPLATE_MKTEMP:$PATH" run_subject "$CLONE" main
expect_status 0 "every file under TMPDIR"
expect_deployed "every file under TMPDIR"
expect_main_deployer "every file under TMPDIR"
[ -z "$(ls -A "$UNDER_TMPDIR")" ] ||
  fail "every file under TMPDIR: temporary files left behind: $(ls -A "$UNDER_TMPDIR")"

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

echo "update.sh main channel tests passed"
