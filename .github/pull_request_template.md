<!-- Closes #N, when the pull request comes from an issue. Delete this line otherwise. -->
Closes #

<!-- The problem and the chosen behavior, in a paragraph. Say why, not only what. -->

## Changes

<!-- One bullet per notable change, with the files or components it touches. -->

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

## Models

<!-- Which model planned, or "no plan" with the size-gate reason, and which model implemented. -->

- Planned by:
- Implemented by:

## Validation

- [ ] `python -B tests/run_validation.py` passes in full, with no skipped prerequisites
- [ ] Regression coverage added or updated for every behavior change
- [ ] For a change to skill paths, `allowed-tools`, runtime adapters, or agents, the `runtime-canary` lines are below, with each runtime's version and any `SKIPPED` reason
- [ ] For a change to a skill's prompt, model guidance, or reviewer instructions, the `evaluate-skill` run is cited below with its table, and `docs/skill-evaluations.md` holds its result
- [ ] For a skill change, `analyze-skill-cost` audited each changed skill from the source tree with no MUST FIX left; any SUGGESTION left is named below with why
- [ ] For a contract change, an entry under `## Upgrade note` above names each changed contract item
- [ ] Documentation updated in this change
- [ ] Diff reviewed for personal paths, organization names, credentials, and generated artifacts

<!-- List anything that could not be run, and why. Run the command above for every change, documentation-only
     included: the runner reads the changed files and decides for itself which checks and suites they need. -->
