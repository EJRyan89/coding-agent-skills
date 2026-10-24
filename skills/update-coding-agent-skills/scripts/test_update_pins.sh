#!/usr/bin/env bash
set -Eeuo pipefail

# A deployment pinned with deploy.py --at, and what the update reads from deploy.py check, on the main channel.
# test_update_fixture.sh makes the fixture and the helpers every update.sh suite shares.
# shellcheck source=skills/update-coding-agent-skills/scripts/test_update_fixture.sh
source "$(dirname "$0")/test_update_fixture.sh"

# The cases start from the releases the release gate's cases publish, through v3.0.0.
publish_through v3.0.0

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

echo "update.sh pin tests passed"
