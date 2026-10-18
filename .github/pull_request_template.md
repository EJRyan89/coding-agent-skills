<!-- The title opens with its type, an optional scope, a colon, and a space, as in "fix(deployer): ...", with no "!";
     "Pull requests" in docs/implementing-changes.md lists the types. Name each issue this pull request resolves in a
     Closes #N line of its own, and delete the line when it resolves none. -->
Closes #

<!-- The problem and the chosen behavior, in a paragraph. Say why, not only what. -->

## Changes

<!-- One bullet per notable change, with the files or components it touches. A file this pull request returns to
     its state before a commit on main is a revert, which validation fails unless a line of its own declares it:
     Reverts `<path>` to its state before <commit>
     with seven or more hexadecimal digits of the commit validation names. -->

-

## Implications

<!-- User-visible, compatibility, or security implications, such as a new configuration value, a changed
     command, a manifest change, or a trust boundary that moved. Write "None" when there are none. -->

## Upgrade note

<!-- What this change asks of a user who updates, written here and never in docs/upgrade-notes.md: the squash
     commit carries this body into main, and tools/release_notes.py writes each release's notes from it. Give one
     entry per change a user meets, in this shape, and keep "None" only when there is none. Validation fails while a
     changed contract item is named by no entry; docs/upgrade-notes.md describes each field.

### What changed, as a user meets it

- Level: patch, minor, or major, from the Versioning section of docs/releasing.md, and why.
- Contract: each changed contract item in backticks, as validation names it, or none.
- User action: what the user must do after updating, or none.
-->

None

## Validation

- [ ] The local checks "Validation" in `docs/implementing-changes.md` names for the touched files pass, with no skipped prerequisites; the `Validation` check runs every suite, or the subset the runner selects when the change is documentation-only
- [ ] Regression coverage added or updated for every behavior change
- [ ] For a change to skill paths, `allowed-tools`, runtime adapters, or agents, the `runtime-canary` lines are below, with each runtime's version and any `SKIPPED` or `NOTHING_CHECKED` reason
- [ ] For a change to a skill's prompt, model guidance, or reviewer instructions, the `evaluate-skill` run is cited below with its table, and `docs/skill-evaluations.md` holds its result
- [ ] For a skill change, `analyze-skill-cost` audited each changed skill from the source tree with no MUST FIX left; any SUGGESTION left is named below with why
- [ ] For a contract change, an entry under `## Upgrade note` above names each changed contract item
- [ ] The `Mutation gate` job's summary is cited below with each changed module's kill ratio, and a surviving mutant on a changed line that alters behavior a caller relies on is killed by a new test
- [ ] Documentation updated in this change
- [ ] Diff reviewed for personal paths, organization names, credentials, and generated artifacts

<!-- List anything that could not be run, and why. Continuous integration runs every suite on every push, the
     merge of the base branch included, and on a pull request that is not documentation-only, so bringing the branch
     current needs no local rerun. -->
