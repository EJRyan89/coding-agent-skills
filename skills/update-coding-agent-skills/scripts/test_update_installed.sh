#!/usr/bin/env bash
set -Eeuo pipefail

# The script as the deployer installs it: what it runs from the clone, and the ejryan89-skill-core beside it.
# test_update_fixture.sh makes the fixture and the helpers every update.sh suite shares.
# shellcheck source=skills/update-coding-agent-skills/scripts/test_update_fixture.sh
source "$(dirname "$0")/test_update_fixture.sh"

# The cases start from a release the next patch release does not cross, v6.0.0, with a commit after it.
publish_through v4.0.0
publish "sixth major"
tag_upstream v6.0.0
publish "after the sixth major"

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

echo "update.sh installed script tests passed"
