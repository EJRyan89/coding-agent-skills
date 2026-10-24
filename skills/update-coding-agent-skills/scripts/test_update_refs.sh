#!/usr/bin/env bash
set -Eeuo pipefail

# The clone's refs and git's environment: a clone pulled by hand or with no local main, release tags the update
# cannot compare, repository-selecting variables, tags named like a branch, and readings git cannot give.
# test_update_fixture.sh makes the fixture and the helpers every update.sh suite shares.
# shellcheck source=skills/update-coding-agent-skills/scripts/test_update_fixture.sh
source "$(dirname "$0")/test_update_fixture.sh"

# The cases start from the releases the release gate's cases publish, through v3.0.0.
publish_through v3.0.0
# The git wrapper the cases below write, made runnable once so each write keeps it so.
WRAPPER="$FIXTURE/git wrapper"
mkdir -p "$WRAPPER"
: >"$WRAPPER/git"
chmod +x "$WRAPPER/git"

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

echo "update.sh refs tests passed"
