---
name: evaluate-skill
description: >-
  Run a skill's fixed scenarios once per reviewer model from throwaway homes and judge each review record by script.
  Use it before a pull request that changes a skill's prompt, model guidance, or reviewer instructions. Each run
  calls models.
argument-hint: "SKILL [--scenario NAME ...] [--model haiku|sonnet|opus ...]"
allowed-tools: ["Bash(python -B tools/skill_evals.py*)", "PowerShell(python -B tools/skill_evals.py*)"]
---

# Evaluate a skill

`tools/skill_evals.py` runs a skill's scenarios, the folders under `tests/fixtures/skill-evals/<skill>/`, once per reviewer model, each in a headless Claude Code session started in a throwaway home where this checkout is deployed, and checks every expectation against the record the run wrote. The script does every deterministic step: the deployment, the runs, the checks, the table, and the results file. Your part is choosing what to run and saying what a failure means. Its docstring lists every output line and what still comes from the user's own setup.

It is a manual gate, never part of `tests/run_validation.py`. A full run of `review-prs`, today the only skill with scenarios, is fifteen sessions (five scenarios on three models), each calling models. Two full runs of the first three scenarios on 2026-10-09 took about four minutes each, every session one to two; after deploying the homes, the script bounds the sessions at two and a half hours, since it runs three at once and stops each at 1,800 seconds.

## 1. Choose the run

For a pull request, run every scenario on every model: only that run may record its result. Add `--scenario <name>` or `--model <model>` (each repeatable) only to rerun a failure, and leave out `--write`, which a narrowed run refuses. A skill without a folder under `tests/fixtures/skill-evals/` has no scenarios, and the run says so with `FAILED`.

## 2. Run it

From the worktree, in the background, since it outlasts a command's time limit, and wait for it to end. For another skill with scenarios, put its name in place of `review-prs`:

```bash
python -B tools/skill_evals.py review-prs --write
```

The first line is `HOME "<dir>"`. A `FAILED` line instead means nothing ran: report what it names, such as Claude Code missing from `PATH`.

## 3. Say what each failure means

Every `FAIL` line ends with its reason, from the record or from the run. Explain each before anyone changes the skill:

- **A judgment the record shows**, such as `no finding at least MUST_FIX on those lines`, `found SHOULD_FIX at ...`, `verdict is APPROVED`, or a ledger count or disposition: this is the evaluation's result for that model. One run is one sample, so rerun that scenario on that model once before calling it a regression, and report both runs.
- **`the run recorded no review; ...`**: the session ended before `finalize`. The reason says whether it timed out, exited with an error, did not list the skill, or had a command denied, naming the tool. That is a run problem, not a judgment of the model.
- **`no reviewer subagent ran ...` or `reviewers ran on <id>, not <model>`**: the reviewers did not run on the model, for example because the session worked the roles itself, so the record does not count.
- **`no reviewer guard held <role>: its files_read is null`**: that reviewer ran without the reviewer guard, so the run never exercised the boundary real reviews have, and the record does not count. Search the `TRANSCRIPT` for its `hook_response` events, which say whether the guard ran, rather than judging the model.
- **`DEPLOY_FAILED <model> "<reason>"`**: the throwaway home could not be prepared, or its reviewer agent cannot be passed to the session with `--agents`; its log or reason says why.

When a reason is not enough, search the run's `TRANSCRIPT` file, which is stream-json, for the failing step or for `permission_denials`, rather than reading it whole.

## 4. Record the result

`--write` replaced the skill's rows in `docs/skill-evaluations.md`, which the `WROTE` line names: commit it with the change. A model whose reviewers never ran stays recorded as `not run`, with the reason. In the pull request's Validation section, cite the run with its table and the `REVIEWER`, `GUARDED`, and `RUNTIME` lines, and your reading of each `FAIL`. Without a `REMOVED` line, the `HOME` directory keeps the transcripts; give the user its path to delete when they are done with it.
