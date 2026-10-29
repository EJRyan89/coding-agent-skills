# Skill evaluations

A skill evaluation runs a skill on fixed scenarios on each model, once or several times, and checks what each run
recorded against the scenario's expectations, by script and never from the report. It answers whether a change to a
skill's prompt, description, model guidance, or reviewer instructions still does the task, or still starts the skill
when it should, and on which models. It calls models, so it
is a manual gate like the [runtime canary](../.claude/skills/runtime-canary/SKILL.md), never part of validation or CI.

- **Scenarios** live under `tests/fixtures/skill-evals/<skill>/<scenario>/`, which never ships. Each is a
  `scenario.json` of expectations beside a code-review fixture ("Fixture canaries" in
  [code-review-operations.md](code-review-operations.md#fixture-canaries)), or, for a skill started on one document's
  path such as `review-document`, a `scenario.json` that names the document. The run writes that document into a
  folder of its own and starts the skill on its path; with a base, the folder is a throwaway git checkout where the
  base is committed and the document is an uncommitted edit of it, so only the edit is reviewed. A scenario may name
  the document's stage, which the skill is started with as `--stage`. A document may be
  another scenario's file, so `review-document` reviews the `review-prs` design documents without copies.
  The checkout's Git commands point at a temporary empty hook directory, so neither global nor template-installed
  user hooks run; the base commit also disables signing, without changing the user's Git configuration.
  `tools/skill_evals.py`'s docstring defines the format.
- **The checkout is trusted input.** A fixture's reviewer manifest names the condition scripts a review runs, and each
  home runs this checkout's skills in a session signed in as the user, so a run vouches for every file of the
  checkout, its scenarios included. A pull request from another author is read in full, as its code is, before its
  scenarios run. Before anything runs, `tools/skill_evals.py` refuses with `FAILED` a checkout whose branch follows a
  pull request's ref (`refs/pull/...`) or a remote other than `origin`, as `gh pr checkout` sets for another
  repository's pull request, and a detached `HEAD` at a commit no branch of `origin` holds. A branch with no upstream,
  or one that follows `origin` or a local branch, is the user's own, so an author runs their own branch's scenarios
  before its pull request merges.
- **A repository reviewer's scenarios**, such as `review-repository`'s, sit under the reviewer's name, and each
  `scenario.json` names `review-prs` as the skill beside the reviewer, since `review-prs` runs a reviewer and no
  session starts one. `--canary-home` ships none of this repository's own skills, so each run copies the fixture into a
  throwaway folder and places the reviewer's files there from the checkout, in both trees: its `SKILL.md`,
  `references/`, `scripts/` but its regression suites, and every file its manifest declares. The fixtures hold no copy
  that could drift from the skill under test, the copy is removed when the run ends, and the rows are the reviewer's.
- **A trigger scenario** judges a skill's description rather than its work. Its `scenario.json` names a shipped
  skill the model may start, with `"mode": "trigger"`, a list of `requests` a user might type that should start it,
  drawn from its description's "Use it when" sentence, and two lists of near misses that should not: `near_misses`,
  a neighbouring skill's requests, and `unserved`, requests no skill serves. Each request and near miss is one
  expectation. A run gives each to a headless session
  of its own in the model's home, on that model, since the session's model decides whether to start a skill, with no
  grant and a short turn limit, so a session that starts the skill stops soon after. The session started the skill
  when one of its own messages calls the Skill tool naming it. The run writes a trigger record of the skills each
  session started; a session that could not start, timed out, did not list the skill, or ran on another model makes
  the run count for nothing. `audit-skill`'s `description` scenario is the worked example.
- **Every shipped skill the model may start** has a trigger scenario, its `description` scenario, and validation
  fails without one ("Validation" in [adding-a-skill.md](adding-a-skill.md#validation)). Its requests are several
  ways a user would ask for what the "Use it when" sentence names, at least one of them without the skill's name, so
  the description rather than the name is what is judged. Its near misses are requests a neighbouring skill's
  sentence names, such as a pull request review beside a document review, which show the description beats its
  neighbour's, and at least one request no skill serves, which shows it does not take whatever comes. The
  `change-skill` repository skill owes the run whenever a description is new or changed. Repository skills have none,
  since `--canary-home` deploys none of them for a session to start.
- **Several runs:** one run is one sample of what a model does, so `--runs N` runs each scenario N times on each
  model and judges every run. Each line about one run names it as `run=<run>`, and a `RUNS` line per scenario and
  model gives the runs that passed every expectation, of the runs, and whether that is all of them (the pass^k of k
  runs), some, which is an inconsistent result, or none. `--records` judges `<model>-<run>.json` records the same way.
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
  nothing, and so does one whose record shows a reviewer the guard did not hold (`files_read` null). The record is
  the pull request's latest in the run's canary root, read and validated by ejryan89-code-review-core's archive, and must be
  the version the run writes: 2 for a re-review, whose canary root first holds the prior record as version 1, and 1
  otherwise, so a run whose own record is missing or unreadable fails rather than being judged by the prior. The
  reviewers' self-check and source commands are granted at the exact quoted paths of the home's scripts. A narrowed run
  can repeat `--scenario` and `--model`; each distinct name counts once, in first-occurrence order, both when
  running scenarios and when judging existing records with `--records`. Without `--scenario`, scenarios are selected
  in name order. `--records` holds each record to what a run's is held to: ejryan89-code-review-core's record contract, the
  version the run writes, the reviewer guard, and each reviewer's model, as the reviewer's result reports it.
- **What a run depends on:** each session is a `claude -p` session without `--bare`, so it discovers the home's
  skills and agents and signs in as the user does. Claude Code's
  [headless page](https://code.claude.com/docs/en/headless) says `--bare`, which skips that discovery and the
  subscription sign-in, will become the default for `-p`, and no option yet keeps discovery. A session whose init
  event lists no skill therefore makes its run count for nothing with the reason that it discovered no skill, as a
  bare session does, rather than being judged; the runtime canary and the plugin canary fail such a session with the
  same reason, from the one check in `tools/runtime_canary.py`.
- **What it prints** is `PASS` or `FAIL` per expectation and model, then a table. The tool's `MODELS` constant is the
  one place the models are named, and `tests/tools/test_skill_evals.py` covers the tool with a stub Claude Code and no
  model.
- **What is recorded** is pass or fail per expectation, the model identifiers the reviewers and the session ran
  on, the Claude Code version, and the date. Nothing else: no tokens, cost, or timings.

## Latest result

One row per skill and model, replaced by each full run with `--write`. "Passed" counts expectations over every run
of every scenario the judged part ran on: the reviewers, or a trigger scenario's sessions. "Runs" is the runs of each
scenario; with more than one, each scenario's counts end with the runs that passed every expectation, as
`7/9 (2/3 runs)`, and a run whose reviewers never started is left out of both counts. A scenario is "not run" for a model when none of its reviewers started, such as
when the session refused before reviewing, since it holds no judgment of the model; the notes below say why. A model
is "not run" when none of its reviewers ran, with the first reason in place of the scenarios, in fixed phrases that
name no path of the machine; the run's `FAIL` lines keep the whole reason. A run with `--write` refuses, before it
starts, a table between the markers that holds anything but the header, its separator, and rows of its columns. A
pull request that changes a skill's prompt, description, model guidance, or reviewer instructions cites this run.

A trigger scenario's result is a row like any other. Its Model ID and Session model are both the judged model, since
its sessions run on that model, and By scenario gives its expectations passed, one per request and near miss. A skill
with review scenarios too lists its trigger scenario among them, and its Session model names both the reviews'
session model and the judged one. A skill with no row has not been run since its scenarios were written.

<!-- skill-evals:begin -->
| Skill | Model | Model ID | Passed | Runs | By scenario | Session model | Claude Code | Date |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| audit-skill | haiku | claude-haiku-5-5 | 6/8 | 1 | description 6/8 | claude-haiku-5-5 | 2.1.302 | 2026-10-22 |
| audit-skill | sonnet | claude-sonnet-5-5 | 8/8 | 1 | description 8/8 | claude-sonnet-5-5 | 2.1.302 | 2026-10-22 |
| audit-skill | opus | claude-opus-5-5 | 8/8 | 1 | description 8/8 | claude-opus-5-5 | 2.1.302 | 2026-10-22 |
| github-activity-report | haiku | claude-haiku-5-5 | 7/7 | 1 | description 7/7 | claude-haiku-5-5 | 2.1.302 | 2026-10-22 |
| github-activity-report | sonnet | claude-sonnet-5-5 | 7/7 | 1 | description 7/7 | claude-sonnet-5-5 | 2.1.302 | 2026-10-22 |
| github-activity-report | opus | claude-opus-5-5 | 7/7 | 1 | description 7/7 | claude-opus-5-5 | 2.1.302 | 2026-10-22 |
| review-document | haiku | claude-haiku-5-5 | 29/32 | 1 | committed-edit 3/3, description 5/7, design-clean 1/2, design-draft 3/3, design-final 2/2, design-gaps 5/5, pdf-gaps 5/5, word-gaps 5/5 | claude-haiku-5-5, claude-opus-5-5 | 2.1.302 | 2026-10-25 |
| review-document | sonnet | claude-sonnet-5-5 | 31/32 | 1 | committed-edit 3/3, description 6/7, design-clean 2/2, design-draft 3/3, design-final 2/2, design-gaps 5/5, pdf-gaps 5/5, word-gaps 5/5 | claude-opus-5-5, claude-sonnet-5-5 | 2.1.302 | 2026-10-25 |
| review-document | opus | claude-opus-5-5 | 32/32 | 1 | committed-edit 3/3, description 7/7, design-clean 2/2, design-draft 3/3, design-final 2/2, design-gaps 5/5, pdf-gaps 5/5, word-gaps 5/5 | claude-opus-5-5 | 2.1.302 | 2026-10-25 |
| review-prs | haiku | claude-haiku-5-5 | 24/27 | 1 | clean-change 2/2, description 5/6, design-clean 1/2, design-gaps 5/5, planted-defects 4/5, re-review 7/7 | claude-haiku-5-5, claude-opus-5-5 | 2.1.302 | 2026-10-24 |
| review-prs | sonnet | claude-sonnet-5-5 | 27/27 | 1 | clean-change 2/2, description 6/6, design-clean 2/2, design-gaps 5/5, planted-defects 5/5, re-review 7/7 | claude-opus-5-5, claude-sonnet-5-5 | 2.1.302 | 2026-10-24 |
| review-prs | opus | claude-opus-5-5 | 27/27 | 1 | clean-change 2/2, description 6/6, design-clean 2/2, design-gaps 5/5, planted-defects 5/5, re-review 7/7 | claude-opus-5-5 | 2.1.302 | 2026-10-24 |
| review-repository | haiku | claude-haiku-5-5 | 13/14 | 1 | branch-name-command 1/1, build-grant 1/1, drifted-guide 2/2, missed-sibling 1/1, misstated-exit-status 1/1, tested-change 2/2, text-read-policy 0/1, undocumented-change 2/2, unnoted-flag-rename 1/1, untested-change 1/1, write-before-refusal 1/1 | claude-opus-5-5 | 2.1.302 | 2026-10-28 |
| review-repository | sonnet | claude-sonnet-5-5 | 13/14 | 1 | branch-name-command 1/1, build-grant 1/1, drifted-guide 2/2, missed-sibling 1/1, misstated-exit-status 1/1, tested-change 2/2, text-read-policy 0/1, undocumented-change 2/2, unnoted-flag-rename 1/1, untested-change 1/1, write-before-refusal 1/1 | claude-opus-5-5 | 2.1.302 | 2026-10-28 |
| review-repository | opus | claude-opus-5-5 | 14/14 | 1 | branch-name-command 1/1, build-grant 1/1, drifted-guide 2/2, missed-sibling 1/1, misstated-exit-status 1/1, tested-change 2/2, text-read-policy 1/1, undocumented-change 2/2, unnoted-flag-rename 1/1, untested-change 1/1, write-before-refusal 1/1 | claude-opus-5-5 | 2.1.302 | 2026-10-28 |
<!-- skill-evals:end -->

### Notes

A note names the skill and the scenario it explains in its bold label, as `` `<skill>`, `<scenario>` ``, and one that
says the scenario was not run explains a `not run` in that skill's rows: `tests/tools/test_skill_evals.py` fails while
a scenario a row shows `not run` has no such note, or such a note names a scenario every row of its skill ran.

- **Haiku as a reviewer:** its misses are judgment misses that recur on a rerun and with the earlier wording of the
  shared reviewer guidance, so they are the model's, not the guidance's: it raises should-fix findings on a clean
  design document, and misses a wrong value in an error message that the other models report. The suite supports it
  for finding the planted must-fix and should-fix defects and for re-review, not for approving a clean design or
  for suggestion-level detail.

## Transcript samples

A script grader that passes a wrong run fails nothing, so a failing run's transcript is the only one a reader would
otherwise open, and a grader's own defect stays unseen. Each run with `--write` therefore samples one run that passed
every expectation, prints it as `SAMPLE <scenario> <model>`, and replaces the skill's row below with it, unread.
Successive recorded runs take the next scenario and model after the last sampled, so each scenario on each model is
read in turn. The reader searches that run's `TRANSCRIPT` for what each expectation claims, such as the finding at its
line or the Skill tool call that started the skill, and records the reading with
`python -B tools/skill_evals.py <skill> --sample-read agrees` or `--sample-read disagrees`. A run that disagrees is a
defect of the grader: its kind's check in `tools/skill_evals_scenarios.py`, or the judging in
`tools/skill_evals_judging.py`, and it is filed as a bug before the row is committed. `tests/tools/test_skill_evals.py`
fails while a committed row is still `unread`, so the reading travels with the run it samples.

<!-- skill-evals-samples:begin -->
| Skill | Scenario | Model | Run | Date | Transcript |
| --- | --- | --- | --- | --- | --- |
| review-document | committed-edit | haiku | 1 | 2026-10-25 | agrees |
| review-repository | build-grant | sonnet | 1 | 2026-10-28 | agrees |
<!-- skill-evals-samples:end -->
