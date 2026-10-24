#!/usr/bin/env bash
set -Eeuo pipefail

# The release gate on the main channel: a release that raises the breaking component stops the update until
# --cross-major, and the releases these cases publish are RELEASE_HISTORY's.
# test_update_fixture.sh makes the fixture and the helpers every update.sh suite shares.
# shellcheck source=skills/update-coding-agent-skills/scripts/test_update_fixture.sh
source "$(dirname "$0")/test_update_fixture.sh"

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

echo "update.sh release gate tests passed"
