---
name: evaluate-skill
description: >-
  Run a skill's fixed scenarios on each model from throwaway homes, once or several times, and judge each record by
  script: a review's, or whether a request started the skill. Use it before a pull request that changes a skill's
  prompt, description, model guidance, or reviewer instructions. Each run calls models.
argument-hint: "SKILL [--runs N] [--scenario NAME ...] [--model NAME ...]"
allowed-tools: ["Bash(python -B tools/skill_evals.py *)", "PowerShell(python -B tools/skill_evals.py *)"]
---

# Evaluate a skill

`tools/skill_evals.py` runs a skill's scenarios, the folders under `tests/fixtures/skill-evals/<skill>/`, on each model, each in a headless Claude Code session started in a throwaway home where this checkout is deployed, and checks every expectation against the record the run wrote. A review scenario varies the reviewers' model; a trigger scenario gives each of its requests, and each near miss, to a session of its own on the model and checks whether the session started the skill. The script does every deterministic step: the deployment, the runs, the checks, each failure's kind, the table, the pull request's citation, the results file, and which passing run's transcript to read. Your part is choosing what to run, saying what a failure means, and reading that transcript against its verdicts. Its docstring lists every output line and what still comes from the user's own setup.

It is a manual gate, never part of `tests/run_validation.py`. A full run is one session per scenario per model per run, and one per request of a trigger scenario, each calling models. After deploying the homes, the script runs up to `--jobs` sessions concurrently and stops each at `--timeout` seconds.

## 1. Choose the run

Run `python -B tools/skill_evals.py --help` for the accepted model names and the concurrency and timeout defaults. The script's `MODELS` constant is the authoritative list of reviewer models.

For a pull request, run every scenario on every model: only that run may record its result. One run is one sample, so for a change to a reviewer's model, effort, or guidance, where the comparison decides, add `--runs 3`. Add `--scenario <name>` or `--model <model>` (each repeatable) only to rerun a failure, and leave out `--write`, which a narrowed run refuses. A skill without a folder under `tests/fixtures/skill-evals/` has no scenarios, and the run says so with `FAILED`.

## 2. Run it

From the worktree, in the background, since it outlasts a command's time limit, and wait for it to end. For another skill with scenarios, put its name in place of `review-prs`; for a repository reviewer such as `review-repository`, put the reviewer's name, and the script starts `review-prs` on a throwaway copy of each fixture holding the reviewer's files from this checkout:

```bash
python -B tools/skill_evals.py review-prs --write
```

The first line is `HOME "<dir>"`, then a `DEPLOYED <model> <source>` line for each model's home and `RUNTIME claude <version>`, the Claude Code it runs, which the cited block repeats; none needs anything from you. A `FAILED` or `DEPLOY_FAILED` line instead means nothing ran: report what it names, such as Claude Code missing from `PATH` or the log of a deployment that failed.

## 3. Say what each failure means

A `NOT_COUNTED` line names each run whose record counts for nothing, and every `FAIL` line names its kind before its reason. Explain each before anyone changes the skill:

- **`judgment`**: the evaluation's result for that model. With `--runs`, read the scenario's `RUNS` line before calling it anything: `all` is a steady pass, `none` a steady failure, and `some` an inconsistent result, neither a regression nor a pass, which you report as such. After a single run, one failure is one sample: rerun that scenario on that model with `--runs 3` before calling it a regression.
- **`run`** or **`record`**: the run recorded no review, or its record does not show the model judged, so it is not a judgment of the model. Say what the reason shows went wrong.

For a trigger scenario, a `starts` failure means the description did not lead that model to the skill, and a `does_not_start` failure that it took a near miss: a neighbour's request, or one no skill serves, which means it takes whatever comes. Say which words of the description the request or near miss shares.

When a reason is not enough, search the run's `TRANSCRIPT` file, which is stream-json, for the failing step, for `permission_denials`, or for a reviewer's `hook_response` events, which say whether the guard ran, rather than reading it whole.

## 4. Record the result

`--write` replaced the skill's rows in `docs/skill-evaluations.md`, which the `WROTE` line names: commit it with the change. Paste the lines between `CITATION BEGIN` and `CITATION END` into the pull request's Validation section as they are, and add your reading of each `NOT_COUNTED` and `FAIL` line in it. Without a `REMOVED` line, the `HOME` directory keeps the transcripts; give the user its path to delete when they are done with it.

## 5. Read the sampled transcript

A script grader that passes a wrong run fails nothing, so every run with `--write` samples one passing run for you to read: its `SAMPLE <scenario> <model>` line, before `WROTE`, names it, with ` run=<run>` after the model when `--runs` is above 1, and the next recorded run samples the next scenario and model. `SAMPLE none` means no run passed, and there is nothing to read. Otherwise, for each `PASS` line of that run, search its `TRANSCRIPT` for what the expectation claims, such as the finding at its line, the verdict, or the Skill tool call that started the skill, and check that the transcript shows it. Then record the reading, which writes `docs/skill-evaluations.md` again and prints `WROTE <file>`; commit that edit with the run's rows:

```bash
python -B tools/skill_evals.py review-prs --sample-read agrees
```

Use `disagrees` instead when a `PASS` is not what the transcript shows: that is a defect of the script grader, its kind's check in `tools/skill_evals_scenarios.py` or the judging in `tools/skill_evals_judging.py`. Open a bug issue from the template against it, name it in the pull request, and do not change the skill to suit the grader. A `FAILED` line names a row already read or missing; report it. Validation fails while a committed row is still `unread`.
