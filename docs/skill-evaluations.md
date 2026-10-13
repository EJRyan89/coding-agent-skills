# Skill evaluations

A skill evaluation runs a skill on fixed scenarios, once per reviewer model, and checks what each run recorded
against the scenario's expectations, by script and never from the report. It answers whether a change to a skill's
prompt, model guidance, or reviewer instructions still does the task, and on which models. It calls models, so it
is a manual gate like the [runtime canary](../.claude/skills/runtime-canary/SKILL.md), never part of validation or CI.

- **Scenarios** live under `tests/fixtures/skill-evals/<skill>/<scenario>/`, which never ships. Each is a
  `scenario.json` of expectations beside a code-review fixture ("Fixture canaries" in
  [code-review-operations.md](code-review-operations.md#fixture-canaries)), or, for a skill started on one document's
  path such as `review-document`, a `scenario.json` that names the document. The run writes that document into a
  folder of its own and starts the skill on its path; with a base, the folder is a throwaway git checkout where the
  base is committed and the document is an uncommitted edit of it, so only the edit is reviewed. A document may be
  another scenario's file, so `review-document` reviews the `review-prs` design documents without copies.
  The checkout's Git commands point at a temporary empty hook directory, so neither global nor template-installed
  user hooks run; the base commit also disables signing, without changing the user's Git configuration.
  `tools/skill_evals.py`'s docstring defines the format.
- **A repository reviewer's scenarios**, such as `review-repository`'s, sit under the reviewer's name, and each
  `scenario.json` names `review-prs` as the skill beside the reviewer, since `review-prs` runs a reviewer and no
  session starts one. `--canary-home` ships none of this repository's own skills, so each run copies the fixture into a
  throwaway folder and places the reviewer's files there from the checkout, in both trees: its `SKILL.md`,
  `references/`, `scripts/` but its regression suites, and every file its manifest declares. The fixtures hold no copy
  that could drift from the skill under test, the copy is removed when the run ends, and the rows are the reviewer's.
- **A `repeats` expectation** names a ledger entry and the lines it was raised on, and holds when every finding the
  re-review raises there is linked to that entry, so a still-present problem counts once. It considers only findings
  at the entry's severity: restating the entry at that severity without the link fails, since that is the double
  counting the ledger exists to prevent. A less severe finding on the same line is about another defect, such as a
  suggestion beside a still-present must-fix, and passes. A more severe one is a new finding and passes too, since
  the [record contract](code-review-operations-contract.md#reviewer-behavior) lets a `repeats` link name only a
  finding at least as severe; a scenario that expects the escalation states it with its own `finding` expectation.
- **A run** is the [`evaluate-skill`](../.claude/skills/evaluate-skill/SKILL.md) repository skill, which runs
  `python -B tools/skill_evals.py <skill> --write`. It deploys the checkout into throwaway homes, one per model,
  sets that model's ID on the home's reviewer agent, and runs each scenario through the skill in a headless Claude
  Code session on the strongest model's ID, so only the reviewers' model varies. It sets IDs rather than Claude
  Code's aliases, since an alias can resolve to an older generation of the family. The session gets the home's
  reviewer agent with `--agents`, its hook pointed at the home's reviewer guard, because Claude Code runs no hook of
  an agent loaded from a folder whose workspace trust was never accepted, which a headless session's never is. A run
  whose reviewers did not all run on that ID, a dated suffix allowed, or whose session ran on another, counts for
  nothing, and so does one whose record shows a reviewer the guard did not hold (`files_read` null). A narrowed run
  can repeat `--scenario` and `--model`; each distinct name counts once, in first-occurrence order, both when
  running scenarios and when judging existing records with `--records`. Without `--scenario`, scenarios are selected
  in name order.
- **What is recorded** is pass or fail per expectation, the model identifiers the reviewers and the session ran
  on, the Claude Code version, and the date. Nothing else: no tokens, cost, or timings.

## Latest result

One row per skill and reviewer model, replaced by each full run with `--write`. "Passed" counts expectations over
every scenario a reviewer ran on. A scenario is "not run" for a model when none of its reviewers started, such as
when the session refused before reviewing, since it holds no judgment of the model; the notes below say why. A model
is "not run" when none of its reviewers ran, with the first reason in place of the scenarios. A pull request that
changes a skill's prompt, model guidance, or reviewer instructions cites this run.

<!-- skill-evals:begin -->
| Skill | Model | Model ID | Passed | By scenario | Session model | Claude Code | Date |
| --- | --- | --- | --- | --- | --- | --- | --- |
| review-document | haiku | claude-haiku-5-5 | 14/15 | committed-edit 3/3, design-clean 1/2, design-gaps 5/5, pdf-gaps not run, word-gaps 5/5 | claude-opus-5-5 | 2.1.291 | 2026-10-12 |
| review-document | sonnet | claude-sonnet-5-5 | 15/15 | committed-edit 3/3, design-clean 2/2, design-gaps 5/5, pdf-gaps not run, word-gaps 5/5 | claude-opus-5-5 | 2.1.291 | 2026-10-12 |
| review-document | opus | claude-opus-5-5 | 15/15 | committed-edit 3/3, design-clean 2/2, design-gaps 5/5, pdf-gaps not run, word-gaps 5/5 | claude-opus-5-5 | 2.1.291 | 2026-10-12 |
| review-prs | haiku | claude-haiku-5-5 | 19/21 | clean-change 2/2, design-clean 1/2, design-gaps 5/5, planted-defects 4/5, re-review 7/7 | claude-opus-5-5 | 2.1.291 | 2026-10-12 |
| review-prs | sonnet | claude-sonnet-5-5 | 21/21 | clean-change 2/2, design-clean 2/2, design-gaps 5/5, planted-defects 5/5, re-review 7/7 | claude-opus-5-5 | 2.1.291 | 2026-10-12 |
| review-prs | opus | claude-opus-5-5 | 21/21 | clean-change 2/2, design-clean 2/2, design-gaps 5/5, planted-defects 5/5, re-review 7/7 | claude-opus-5-5 | 2.1.291 | 2026-10-12 |
| review-repository | haiku | claude-haiku-5-5 | 5/5 | drifted-guide 2/2, tested-change 1/1, undocumented-change 1/1, untested-change 1/1 | claude-opus-5-5 | 2.1.291 | 2026-10-13 |
| review-repository | sonnet | claude-sonnet-5-5 | 5/5 | drifted-guide 2/2, tested-change 1/1, undocumented-change 1/1, untested-change 1/1 | claude-opus-5-5 | 2.1.291 | 2026-10-13 |
| review-repository | opus | claude-opus-5-5 | 5/5 | drifted-guide 2/2, tested-change 1/1, undocumented-change 1/1, untested-change 1/1 | claude-opus-5-5 | 2.1.291 | 2026-10-13 |
<!-- skill-evals:end -->

### Notes

- **`review-repository`, `tested-change`:** Sonnet's should-fix finding, raised again on a rerun, is correct: the
  fixture's `main` prints `days_old`'s output through `echo "$(...)"`, which drops the refusal's exit status, so the
  change the scenario calls clean holds a defect the other models did not report. The fixture needs the fix, not the
  reviewer.
- **`review-document`, `pdf-gaps`:** not run on any model, because the `pdftotext` first on the session's `PATH`
  was the one Git for Windows bundles, below the extraction floor, so the PDF's text was refused before a reviewer
  started. Putting a supported parser ahead of Git's lets it run.
- **Haiku as a reviewer:** its misses are judgment misses that recur on a rerun and with the earlier wording of the
  shared reviewer guidance, so they are the model's, not the guidance's: it raises should-fix findings on a clean
  design document, and misses a wrong value in an error message that the other models report. The suite supports it
  for finding the planted must-fix and should-fix defects and for re-review, not for approving a clean design or
  for suggestion-level detail.
