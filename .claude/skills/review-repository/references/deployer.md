# Deployer reviewer

Your files are the deployer, which writes into the folders every runtime loads skills from, so a wrong assumption removes or replaces skills a user has. Judge the change against the invariants in TRUSTED_ROOT/docs/design.md: "One write point", "One platform seam", "Refusals before mutation, and journaled rollback", and the deployer's row of "Trust model". Read only those sections (Grep its headings, then Read from one to the next).

Ask of each change:

1. **One write point.** Every filesystem mutation goes through `deployer/fsops.py`. A new `FSOPS_ALLOWED` entry names a throwaway path under the system temporary directory.
2. **One platform seam.** Operating-system behavior lives in `deployer/platform_support.py`, including behavior that names no platform token: case-folded path comparison, a rename that refuses to replace on Windows only, case-insensitive environment names.
3. **Refuse first.** Each new reason to stop is found before the first write, and its test asserts the home is unchanged, not only the message.
4. **Journal, ownership, lock.** A new change is journaled and put right by the next run; manifest ownership and permanent backups survive it; an interrupt leaves no lock held and prints no traceback.
5. **The kinds table.** A pass over skills, shared assets, runtime adapters, and agents iterates `deployer/kinds.py`, or says beside it why not. What a preserved skill keeps holds for every kind it depends on.
6. **What the documents promise.** Exit codes, messages, and option combinations match `docs/installation.md`, `docs/recovery.md`, and the upgrade notes; an option a run ignores is refused or documented.
7. **Successive runs.** For each record the change writes or reinterprets (the manifest's pin, owned items, and each skill's `skill_deps`; the journal), follow it through two runs: after run N, who owns each field, the user or the tool, and does run N+1 read it as the documents promise, including when run N was given the value in another form (a short name, another case, an equivalent path)? A test that asserts the state after one run without the next run's behavior is a finding. Example: a pin at the release's own commit, recorded as `refs/tags/<tag>` after one update, which the next release's update took for its own and moved without `--unpin` (#1689).
8. **The previous release's installed copy.** For a change to a refusal, flag, status line, or a path an installed copy reads: how does the previous release's installed `update-coding-agent-skills`, which runs this deployer from the fast-forwarded clone, behave with it, and does each way out the documents name work for that first update? A route only the new copy can take is a finding, and the upgrade note must name the way past it. Example: an installed copy that built the release channel's `--at` without `--unpin` met the new refusal over a user's pin, and its advice to rerun with `--unpin` repeated the stop. The test #1607 proposes, the latest release's installed update run against a newer clone, is what holds the answer.

The audit found this drift here: a preserved skill that kept its shared assets but lost its agent; refusal tests that asserted only the message; option errors that exited 1 where the document says 2; an unreadable file reported as malformed; a hand-ordered table beside the kinds table; a no-replace rename outside the seam.

A finding question 7 or 8 found opens its title with `Successive runs:` or `Installed copy:`, so later audits can count them.

Categories: `Unheld invariant` for an invariant the change breaks or leaves without a test that fails when it breaks; `Code-document drift` where code and a document disagree; `Correctness`; `Test coverage`.

Severity: **MUST_FIX** for a write outside `fsops`, a write before a refusal, or a lost journal, ownership, or backup record; **SHOULD_FIX** for an invariant left untested, platform behavior outside the seam, or a document the change makes false; **SUGGESTION** otherwise. Do not report what validation already fails on: a direct write, a platform token, format, lint, or types.
