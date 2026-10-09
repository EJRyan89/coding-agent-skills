# Skill evaluations

A skill evaluation runs a skill on fixed scenarios, once per reviewer model, and checks what each run recorded
against the scenario's expectations, by script and never from the report. It answers whether a change to a skill's
prompt, model guidance, or reviewer instructions still does the task, and on which models. It calls models, so it
is a manual gate like the [runtime canary](../.claude/skills/runtime-canary/SKILL.md), never part of validation or CI.

- **Scenarios** live under `tests/fixtures/skill-evals/<skill>/<scenario>/`, which never ships. Each is a code-review
  fixture ("Fixture canaries" in [code-review-operations.md](code-review-operations.md#fixture-canaries)) and a
  `scenario.json` of expectations; `tools/skill_evals.py`'s docstring defines the format.
- **A run** is the [`evaluate-skill`](../.claude/skills/evaluate-skill/SKILL.md) repository skill, which runs
  `python -B tools/skill_evals.py <skill> --write`. It deploys the checkout into throwaway homes, one per model,
  sets that model on the home's reviewer agent, and runs each scenario through the skill in a headless Claude Code
  session on the strongest model, so only the reviewers' model varies. A run whose reviewers did not all run on
  the model counts for nothing.
- **What is recorded** is pass or fail per expectation, the model identifiers the reviewers and the session ran
  on, the Claude Code version, and the date. Nothing else: no tokens, cost, or timings.

## Latest result

One row per skill and reviewer model, replaced by each full run with `--write`. "Passed" counts expectations over
every scenario. A model is "not run" when none of its reviewers ran, with the first reason in place of the
scenarios. A pull request that changes a skill's prompt, model guidance, or reviewer instructions cites this run.

<!-- skill-evals:begin -->
| Skill | Model | Model ID | Passed | By scenario | Session model | Claude Code | Date |
| --- | --- | --- | --- | --- | --- | --- | --- |
| review-prs | haiku | claude-haiku-4-5-20251001 | 13/14 | clean-change 2/2, planted-defects 4/5, re-review 7/7 | claude-opus-5-5 | 2.1.291 | 2026-10-08 |
| review-prs | sonnet | claude-sonnet-5-5 | 14/14 | clean-change 2/2, planted-defects 5/5, re-review 7/7 | claude-opus-5-5 | 2.1.291 | 2026-10-08 |
| review-prs | opus | claude-opus-5-5 | 14/14 | clean-change 2/2, planted-defects 5/5, re-review 7/7 | claude-opus-5-5 | 2.1.291 | 2026-10-08 |
<!-- skill-evals:end -->
