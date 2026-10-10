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
  `tools/skill_evals.py`'s docstring defines the format.
- **A `repeats` expectation** names a ledger entry and the lines it was raised on, and holds when every finding the
  re-review raises there is linked to that entry, so a still-present problem counts once. It considers only findings
  at the entry's severity: restating the entry at that severity without the link fails, since that is the double
  counting the ledger exists to prevent. A less severe finding on the same line is about another defect, such as a
  suggestion beside a still-present must-fix, and passes. A more severe one is a new finding and passes too, since
  the [record contract](code-review-operations-contract.md#reviewer-behavior) lets a `repeats` link name only a
  finding at least as severe; a scenario that expects the escalation states it with its own `finding` expectation.
- **A run** is the [`evaluate-skill`](../.claude/skills/evaluate-skill/SKILL.md) repository skill, which runs
  `python -B tools/skill_evals.py <skill> --write`. It deploys the checkout into throwaway homes, one per model,
  sets that model on the home's reviewer agent, and runs each scenario through the skill in a headless Claude Code
  session on the strongest model, so only the reviewers' model varies. The session gets the home's reviewer agent
  with `--agents`, its hook pointed at the home's reviewer guard, because Claude Code runs no hook of an agent loaded
  from a folder whose workspace trust was never accepted, which a headless session's never is. A run whose reviewers
  did not all run on the model counts for nothing, and so does one whose record shows a reviewer the guard did not
  hold (`files_read` null).
- **What is recorded** is pass or fail per expectation, the model identifiers the reviewers and the session ran
  on, the Claude Code version, and the date. Nothing else: no tokens, cost, or timings.

## Latest result

One row per skill and reviewer model, replaced by each full run with `--write`. "Passed" counts expectations over
every scenario. A model is "not run" when none of its reviewers ran, with the first reason in place of the
scenarios. A pull request that changes a skill's prompt, model guidance, or reviewer instructions cites this run.

<!-- skill-evals:begin -->
| Skill | Model | Model ID | Passed | By scenario | Session model | Claude Code | Date |
| --- | --- | --- | --- | --- | --- | --- | --- |
| review-document | haiku | claude-haiku-4-5-20251001 | 9/10 | committed-edit 3/3, design-clean 2/2, design-gaps 4/5 | claude-opus-5-5 | 2.1.291 | 2026-10-10 |
| review-document | sonnet | claude-sonnet-5-5 | 10/10 | committed-edit 3/3, design-clean 2/2, design-gaps 5/5 | claude-opus-5-5 | 2.1.291 | 2026-10-10 |
| review-document | opus | claude-opus-5-5 | 10/10 | committed-edit 3/3, design-clean 2/2, design-gaps 5/5 | claude-opus-5-5 | 2.1.291 | 2026-10-10 |
| review-prs | haiku | claude-haiku-4-5-20251001 | 19/21 | clean-change 2/2, design-clean 2/2, design-gaps 4/5, planted-defects 4/5, re-review 7/7 | claude-opus-5-5 | 2.1.291 | 2026-10-10 |
| review-prs | sonnet | claude-sonnet-5-5 | 21/21 | clean-change 2/2, design-clean 2/2, design-gaps 5/5, planted-defects 5/5, re-review 7/7 | claude-opus-5-5 | 2.1.291 | 2026-10-10 |
| review-prs | opus | claude-opus-5-5 | 21/21 | clean-change 2/2, design-clean 2/2, design-gaps 5/5, planted-defects 5/5, re-review 7/7 | claude-opus-5-5 | 2.1.291 | 2026-10-10 |
<!-- skill-evals:end -->
