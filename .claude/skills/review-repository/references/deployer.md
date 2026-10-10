# Deployer reviewer

Your files are the deployer, which writes into the folders every runtime loads skills from, so a wrong assumption removes or replaces skills a user has. Judge the change against the invariants in TRUSTED_ROOT/docs/design.md: "One write point", "One platform seam", "Refusals before mutation, and journaled rollback", and the deployer's row of "Trust model". Read only those sections (Grep its headings, then Read from one to the next).

Ask of each change:

1. **One write point.** Every filesystem mutation goes through `deployer/fsops.py`. A new `FSOPS_ALLOWED` entry names a throwaway path under the system temporary directory.
2. **One platform seam.** Operating-system behavior lives in `deployer/platform_support.py`, including behavior that names no platform token: case-folded path comparison, a rename that refuses to replace on Windows only, case-insensitive environment names.
3. **Refuse first.** Each new reason to stop is found before the first write, and its test asserts the home is unchanged, not only the message.
4. **Journal, ownership, lock.** A new change is journaled and put right by the next run; manifest ownership and permanent backups survive it; an interrupt leaves no lock held and prints no traceback.
5. **The kinds table.** A pass over skills, shared assets, runtime adapters, and agents iterates `deployer/kinds.py`, or says beside it why not. What a preserved skill keeps holds for every kind it depends on.
6. **What the documents promise.** Exit codes, messages, and option combinations match `docs/installation.md`, `docs/recovery.md`, and the upgrade notes; an option a run ignores is refused or documented.

The audit found this drift here: a preserved skill that kept its shared assets but lost its agent; refusal tests that asserted only the message; option errors that exited 1 where the document says 2; an unreadable file reported as malformed; a hand-ordered table beside the kinds table; a no-replace rename outside the seam.

Categories: `Unheld invariant` for an invariant the change breaks or leaves without a test that fails when it breaks; `Code-document drift` where code and a document disagree; `Correctness`; `Test coverage`.

Severity: **MUST_FIX** for a write outside `fsops`, a write before a refusal, or a lost journal, ownership, or backup record; **SHOULD_FIX** for an invariant left untested, platform behavior outside the seam, or a document the change makes false; **SUGGESTION** otherwise. Do not report what validation already fails on: a direct write, a platform token, format, lint, or types.
