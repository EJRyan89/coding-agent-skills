---
name: evaluate-skill
description: >-
  Run a skill's fixed scenarios once per reviewer model from throwaway homes and judge each review record by script.
  Use it before a pull request that changes a skill's prompt, model guidance, or reviewer instructions. Each run
  calls models.
argument-hint: "SKILL [--scenario NAME ...] [--model NAME ...]"
allowed-tools: ["Bash(python -B tools/skill_evals.py *)", "PowerShell(python -B tools/skill_evals.py *)"]
---

# Evaluate a skill

`tools/skill_evals.py` runs a skill's scenarios, the folders under `tests/fixtures/skill-evals/<skill>/`, once per reviewer model, each in a headless Claude Code session started in a throwaway home where this checkout is deployed, and checks every expectation against the record the run wrote. The script does every deterministic step: the deployment, the runs, the checks, each failure's kind, the table, the pull request's citation, and the results file. Your part is choosing what to run and saying what a failure means. Its docstring lists every output line and what still comes from the user's own setup.

It is a manual gate, never part of `tests/run_validation.py`. A full run is one session per scenario per model, each calling models. After deploying the homes, the script runs up to `--jobs` sessions concurrently and stops each at `--timeout` seconds.

## 1. Choose the run

Run `python -B tools/skill_evals.py --help` for the accepted model names and the concurrency and timeout defaults. The script's `MODELS` constant is the authoritative list of reviewer models.

For a pull request, run every scenario on every model: only that run may record its result. Add `--scenario <name>` or `--model <model>` (each repeatable) only to rerun a failure, and leave out `--write`, which a narrowed run refuses. A skill without a folder under `tests/fixtures/skill-evals/` has no scenarios, and the run says so with `FAILED`.

## 2. Run it

From the worktree, in the background, since it outlasts a command's time limit, and wait for it to end. For another skill with scenarios, put its name in place of `review-prs`; for a repository reviewer such as `review-repository`, put the reviewer's name, and the script starts `review-prs` on a throwaway copy of each fixture holding the reviewer's files from this checkout:

```bash
python -B tools/skill_evals.py review-prs --write
```

The first line is `HOME "<dir>"`. A `FAILED` or `DEPLOY_FAILED` line instead means nothing ran: report what it names, such as Claude Code missing from `PATH` or the log of a deployment that failed.

## 3. Say what each failure means

A `NOT_COUNTED` line names each run whose record counts for nothing, and every `FAIL` line names its kind before its reason. Explain each before anyone changes the skill:

- **`judgment`**: the evaluation's result for that model. One run is one sample, so rerun that scenario on that model once before calling it a regression, and report both runs.
- **`run`** or **`record`**: the run recorded no review, or its record does not show the model judged, so it is not a judgment of the model. Say what the reason shows went wrong.

When a reason is not enough, search the run's `TRANSCRIPT` file, which is stream-json, for the failing step, for `permission_denials`, or for a reviewer's `hook_response` events, which say whether the guard ran, rather than reading it whole.

## 4. Record the result

`--write` replaced the skill's rows in `docs/skill-evaluations.md`, which the `WROTE` line names: commit it with the change. Paste the lines between `CITATION BEGIN` and `CITATION END` into the pull request's Validation section as they are, and add your reading of each `NOT_COUNTED` and `FAIL` line in it. Without a `REMOVED` line, the `HOME` directory keeps the transcripts; give the user its path to delete when they are done with it.
