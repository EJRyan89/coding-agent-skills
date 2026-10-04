<!-- Closes #N, when the pull request comes from an issue. Delete this line otherwise. -->
Closes #

<!-- The problem and the chosen behavior, in a paragraph. Say why, not only what. -->

## Changes

<!-- One bullet per notable change, with the files or components it touches. -->

-

## Implications

<!-- User-visible, compatibility, or security implications, such as a new configuration value, a changed
     command, a manifest change, or a trust boundary that moved. Write "None" when there are none. -->

## Validation

- [ ] `python -B tests/run_validation.py` passes in full, with no skipped prerequisites
- [ ] Regression coverage added or updated for every behavior change
- [ ] For a change to skill paths, `allowed-tools`, runtime adapters, or agents, the `runtime-canary` lines are below, with each runtime's version and any `SKIPPED` reason
- [ ] For a skill change, `analyze-skill-cost` audited each changed skill from the source tree with no MUST FIX left; any SUGGESTION left is named below with why
- [ ] Documentation updated in this change
- [ ] Diff reviewed for personal paths, organization names, credentials, and generated artifacts

<!-- List anything that could not be run, and why. A documentation-only change that does not alter commands,
     workflow definitions, template contracts, or safety expectations may skip the full suite: say so here. -->
