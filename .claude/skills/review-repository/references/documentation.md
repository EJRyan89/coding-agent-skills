# Documentation reviewer

Your files are documents. Each fact has one owner, listed in the table under "## Documentation" in TRUSTED_ROOT/docs/implementing-changes.md, and TRUSTED_ROOT/docs/design.md owns the reasons. Judge whether each changed sentence is true of the code and agrees with the other documents; do not judge prose style.

Ask of each change:

1. **True of the code.** A sentence that names a behavior, a line format, an exit code, a limit, a flag, a path, or a test says what the code does. Read the code it names (fetch it, or search for it) before accepting it, and cite the code's line in the finding.
2. **One owner.** A fact restated outside its owner agrees with the owner, and a change to the owner updates the restatements: search for the old wording.
3. **The design document.** A change that bends an invariant or revisits a decision in `docs/design.md` updates it in the same pull request, its "Held by" column included.
4. **Lists and counts.** A list, a count ("two repository skills", "the last"), or a version a change grows or shrinks is updated where it is stated.
5. **Upgrade notes.** An entry names the pull request, not the issue it closes, once the pull request exists, and describes what the code does.

The audit found this drift here: documents that gave another exit code or output than the code; an upgrade note naming the issue where the merged pull request was meant; a sentence that a setting saves a download the code still makes; two documents giving the release canary different scopes; a contract sentence about duplicates that the validator did not keep.

Categories: `Code-document drift` for a sentence the code does not keep; `Documents disagree` for two documents that state the same fact differently.

Severity: **SHOULD_FIX** for a false statement a user or an agent would act on; **SUGGESTION** for a stale count or wording no one acts on. Do not report grammar, Markdown formatting, or what validation already fails on (a stale generated block, an owner-table section going missing).
