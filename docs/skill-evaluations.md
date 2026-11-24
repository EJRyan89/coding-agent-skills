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
- **A by-name document scenario** starts the skill with the document's file name alone, as a user who names a draft
  without its folder does, so `review-document` must find it with its `resolve` command. The run stages the document
  one folder down in the model's home, the session's working directory, in `drafts/`, beside two copies `resolve`
  passes over, one in a hidden folder and one in a checkout of its own, and removes all three when the run ends. A
  finding's path is then `drafts/<document>`, its path in the home. Its `resolved` expectation reads the session's
  own tool calls from the transcript: it fails a session that searched for files with Glob, Grep, or a listing or
  search program such as `find` or `ls` before it ran `resolve`, that never ran `resolve`, or that built a copy other
  than the one `resolve` found. Bash is read by the one Bash grammar, so a search inside a compound command or behind
  a wrapper is seen; PowerShell is read from its text. It is a judgment of the session, which orchestrates on the
  strongest model in every row, rather than of the reviewers' model, and it does not see a search by a program it
  does not list, such as `git grep`, `Select-String`, or `findstr`, or one written in another language. Since it reads
  the session's calls alone, a run that recorded no review, such as one whose session searched, found the copies
  `resolve` passes over, and stopped to ask, still fails `resolved` as a `judgment`, and its other expectations as a
  `run`, when its session ran as a counted run's must: Claude Code started, did not time out, and exited 0, the
  stream's last result event says the session ended with no error, the skill was listed when skills were, nothing
  was denied, and the session ran on its own model alone. Otherwise `resolved` fails as a `run` with the rest. When
  the session's last `resolve` of the document's name returned an error, what it did after was no choice of its own:
  only the calls through that one are judged, all that call wrote included, since its result does not say which
  part failed. A search, or a Bash command the grammar refuses, before `resolve` is still a `judgment`, and when all
  they show is that the session built nothing after `resolve`, `resolved` fails as a `run`. A model's by-name runs
  share its home, so they run one at a time, and `--records` reads each by-name run's transcript beside its record, or in
  place of a record the run never wrote. There it checks what the transcript shows: the last result event, the
  listing, the denials, the session's model, and `resolve`'s result. It cannot see the exit code or whether Claude
  Code started, which only a run reads from the process, though a session ended at its time limit writes no result
  event. `review-document`'s `by-name` scenario is the worked example.
- **The checkout is trusted input.** A fixture's reviewer manifest names the condition scripts a review runs, and each
  home runs this checkout's skills in a session signed in as the user, so a run vouches for every file of the
  checkout, its scenarios included. A pull request from another author is read in full, as its code is, before its
  scenarios run. Before anything runs, `tools/skill_evals.py` refuses with `FAILED` a checkout whose branch follows a
  pull request's ref (`refs/pull/...`) on any remote, a branch of a remote other than `origin`, or another ref that is
  not a branch, and a detached `HEAD` at a commit no branch of `origin` holds. A branch of `origin` is a ref that
  `origin`'s fetch refspecs write from its branches alone, so a pull request's head mirrored under
  `refs/remotes/origin` is not one. A branch pushed to the author's own fork with `git push -u` is refused too, since
  `gh pr checkout` gives another author's pull request from a fork that same upstream; the refusal names the way out,
  `git branch --unset-upstream`, then a push to the fork without `-u` from then on. A branch with no upstream, or one that follows a
  branch of `origin` or a local branch, is the user's own, so an author runs their own branch's scenarios before its
  pull request merges.
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
- **A synthesis scenario** judges `review-insights`' synthesis: the prompt it writes for the reviewer agent and the
  recommendations the agent's result adds to the insights report. Its `scenario.json` names `"mode": "synthesis"`, a
  date range, and one repository, beside a fixed archive of review records, the repository's checkout that its
  guidance is read from, and a flag store. A run copies the three into its own folder with a code-review
  configuration naming them, so the fixture is never written, and starts the skill on the range and the repository.
  The record judged is the one insights report the run wrote, read by the skill's own report reader, when it covers
  the scenario's range and repository and its synthesis is recorded; a run that stops after the report, or that
  reports another range or repository, recorded no synthesis of the scenario's and counts for nothing. A run also
  counts only when the session recorded the synthesis for Claude Code and not `--inline`, which the skill records only
  once the reviewer guard has claimed the synthesis role. Its expectations read only the synthesized recommendations:
  `recommends` a change of one of several types to one of several files, linking the flags that planted it;
  `never_recommends` a file, such as one of a reviewer read from another checkout; and `never_titled`, a word an
  instruction planted in a finding asks for, or the words of what it asks, so a synthesis that obeys the instruction
  without its word fails too, unless it obeys in words of its own. `review-insights`' `synthesis` scenario is the worked example.
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
- **An expectation scoped to one reviewer:** a `finding` or `no_finding_above` expectation that names a `source`
  counts only findings that reviewer raised, alone or with others, so a scenario built to judge one specialist is not
  decided by what other reviewers notice in its fixture. `review-repository`'s `closed-spellings` control expects no
  finding above SHOULD_FIX from `sibling-spellings` and names no verdict: other reviewers may flag the fixture's
  private flattener as a copy of a shared helper and request changes, which says nothing about whether the
  specialist blocks a complete fix. A scoped `no_finding_above` fails a review that names no run of its reviewer, or
  that could not read every changed file (its coverage lists unavailable sources), whatever its verdict, since
  another reviewer's blocking finding records `CHANGES_REQUESTED` over `INCOMPLETE` and that reviewer's silence there
  shows nothing. So it stands in for the verdict a scenario expecting no finding otherwise names. `open-spelling` likewise expects the specialist's own catch and no verdict,
  since other reviewers' findings could bring `CHANGES_REQUESTED` without it. A scoped expectation's `path`, or each
  of its `paths`, must be a changed file its reviewer's route reaches, decided by the pipeline's own routing on the
  scenario's changed files and title with every `when` condition taken as open, and giving the generic reviewer no
  file once a specialist matches one when the manifest sets `uncovered` to `ignore`; a scenario naming a file that
  reviewer never reviews fails to load, since nothing the reviewer raises would ever be judged there.
- **An expectation on added lines:** a reviewer anchors a finding only on an added line of a file it reviews, so a
  fixture scenario's `finding`, `repeats`, or `no_finding_above`, scoped or not, fails to load when its path, or one
  of its `paths`, holds no added line of the scenario's change, such as a file the change deletes, only removes lines
  from, or moves unchanged, or when its `lines` are none of that path's added lines. A scoped `no_finding_above` that
  names no `paths` fails to load when its reviewer's route reaches no file with an added line, and so does a fixture
  scenario whose change adds no line to any file, which no review could raise a finding in. Such a `finding` could
  never be met and such a `repeats` or `no_finding_above` could never fail. The added lines come from the same diff that
  decides a reviewer's reach, as the pipeline parses it. Two kinds of expectation are not held to them, since a run
  diffs a Word document's or a PDF's extracted text rather than its bytes, and loading a scenario would then need the
  PDF extractor: a document scenario's, and one on a Word document or a PDF in a fixture scenario, which counts as a
  file the change may add a line to.
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
  reviewers' self-check and source commands are granted at the exact quoted paths of the home's scripts. Each session
  runs in Claude Code's `acceptEdits` mode, standing in for the user who approves each reviewer's result file, and
  that mode also approves the filesystem commands `mkdir`, `touch`, `rm`, `rmdir`, `mv`, `cp`, and `sed` on paths inside the working directory, the run's home,
  most of them also behind a safe environment variable or a wrapper such as `timeout`, `nice`, or `nohup`
  ([Permission modes](https://code.claude.com/docs/en/permission-modes)), so a skill that runs one of them without
  granting it passes a run where a user in the default mode would be asked. A narrowed run
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
  same reason, from the one check in `tools/runtime_canary.py`. An init event that lists its skills as anything but a
  list of names fails the session with that reason instead, since which skills it found cannot be known, and a trigger
  session's message or Skill call in a shape the tool does not read makes its run count for nothing rather than
  recording that the session started no skill.
- **What a run isolates:** a session takes the user's environment, the sign-in included, less git's
  repository-selecting variables and, for a review or synthesis session, `CLAUDE_CODE_EFFORT_LEVEL`, which would
  outrank the reviewer's stated effort (`CLAUDE_CODE_SUBAGENT_EFFORT` is kept). It reads project and local settings
  only, so not the user's own, though Claude Code's managed settings always apply. Every session, a review's, a
  synthesis's, or a trigger scenario's, starts with `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`, so it loads no CLAUDE.md, rules
  file, or auto memory, neither the user's, the managed one, nor any from a folder above the home, and with
  `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, so it writes no auto memory, which would otherwise land in the user's own memory
  folder for the model's home, where every later run on that model could read it
  ([environment variables](https://code.claude.com/docs/en/env-vars)). No run writes `claudeMdExcludes`: the first
  variable already keeps out every file that setting could name, and the managed one, which it cannot
  ([Memory](https://code.claude.com/docs/en/memory#exclude-specific-claude-md-files)).
- **What it prints** is `PASS` or `FAIL` per expectation and model, then a table. The tool's `MODELS` constant is the
  one place the models are named, and `tests/tools/test_skill_evals.py` covers the tool with a stub Claude Code and no
  model.
- **What is recorded** is pass or fail per expectation, the model identifiers the reviewers and the session ran
  on, the effort the reviewers ran at, the Claude Code version, and the date. Nothing else: no tokens, cost, or timings.

## Latest result

One row per skill and model, replaced by each full run with `--write`. "Passed" counts expectations over every run
of every scenario the judged part ran on: the reviewers, or a trigger scenario's sessions. "Runs" is the runs of each
scenario; with more than one, each scenario's counts end with the runs that passed every expectation, as
`7/9 (2/3 runs)`, and a run whose reviewers never started is left out of both counts. A scenario is "not run" for a model when none of its reviewers started, such as
when the session refused before reviewing, since it holds no judgment of the model; the notes below say why. A model
is "not run" when none of its reviewers ran, with the first reason in place of the scenarios, in fixed phrases that
name no path of the machine; the run's `FAIL` lines keep the whole reason. A by-name run that started no reviewer
but whose `resolved` was judged on the session's calls is the exception: that one expectation counts in "Passed" and
the scenario's counts, so a session that searched and stopped reads as `by-name 0/1`, and a model none of whose
reviewers ran but that has such a judgment gets a row whose Model ID is `none`. Such a run counts among the
scenario's runs only when its `resolved` failed, as a run that failed, so a run that measured nothing of the
reviewers never counts as one that passed every expectation; several runs none of which counts read `(0/0 runs)`.
The judgment is the session's, which runs on the
strongest model in every row, as the by-name scenario above says. A run measures nothing of a model no record of
which counted, every expectation on it failing as a `run` or `record`, such as a session that was signed out or a
reviewer no guard held, so `--write` keeps that model's committed row as it was and prints `KEPT <model>` for it,
and writes the model's new row only when the skill had none. A `resolved` judged on the session's calls alone
measures nothing of the model either, so it never makes `--write` replace a committed row. A run with `--write` refuses, before it
starts, a table between the markers that holds anything but the header, its separator, and rows of its columns. A
pull request that changes a skill's prompt, description, model guidance, or reviewer instructions cites this run.

"Reviewer effort" is the effort each model's reviewers ran at, which the run sets beside the model in the
home's reviewer agent from `REVIEWER_EFFORT` in `tools/skill_evals_records.py`; a row recorded before the run
set it says `unstated`, since its reviewers ran at whatever effort Claude Code gave them, and a skill whose
scenarios start no reviewer says `none`.

"Route pins" is each route of a scenario's reviewer manifest that names its own `model` or `effort`, since the pins
are part of the configuration under test. A pinned model is honoured: the pipeline starts that route's reviewer on it
whatever model the row judges, so a run counts when each reviewer ran on the judged model or on a model a route pins,
and with `--records` each reviewer's reported model is held to its own route's pin. A pinned `inherit` pins nothing,
and a pin of a model the evaluation names no ID for fails the scenario's load. A pinned effort is honoured too: the
session starts that route's reviewer at it through the Agent tool's `effort`, over the agent's effort, the row's
"Reviewer effort", so the cell names it as `effort <effort>`. A row with no pin says `none`.

A trigger scenario's result is a row like any other. Its Model ID and Session model are both the judged model, since
its sessions run on that model, and By scenario gives its expectations passed, one per request and near miss. A skill
with review scenarios too lists its trigger scenario among them, and its Session model names both the reviews'
session model and the judged one. A skill with no row has not been run since its scenarios were written.

<!-- skill-evals:begin -->
| Skill | Model | Model ID | Reviewer effort | Route pins | Passed | Runs | By scenario | Session model | Claude Code | Date |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| audit-ai-config | haiku | claude-haiku-5-5 | none | none | 6/6 | 1 | description 6/6 | claude-haiku-5-5 | 2.1.323 | 2026-11-22 |
| audit-ai-config | sonnet | claude-sonnet-5-5 | none | none | 6/6 | 1 | description 6/6 | claude-sonnet-5-5 | 2.1.323 | 2026-11-22 |
| audit-ai-config | opus | claude-opus-5-5 | none | none | 6/6 | 1 | description 6/6 | claude-opus-5-5 | 2.1.323 | 2026-11-22 |
| audit-skill | haiku | claude-haiku-5-5 | none | none | 7/9 | 1 | description 7/9 | claude-haiku-5-5 | 2.1.323 | 2026-11-20 |
| audit-skill | sonnet | claude-sonnet-5-5 | none | none | 8/9 | 1 | description 8/9 | claude-sonnet-5-5 | 2.1.323 | 2026-11-20 |
| audit-skill | opus | claude-opus-5-5 | none | none | 9/9 | 1 | description 9/9 | claude-opus-5-5 | 2.1.323 | 2026-11-20 |
| curate-agent-memory | haiku | claude-haiku-5-5 | none | none | 7/7 | 1 | description 7/7 | claude-haiku-5-5 | 2.1.323 | 2026-11-21 |
| curate-agent-memory | sonnet | claude-sonnet-5-5 | none | none | 7/7 | 1 | description 7/7 | claude-sonnet-5-5 | 2.1.323 | 2026-11-21 |
| curate-agent-memory | opus | claude-opus-5-5 | none | none | 7/7 | 1 | description 7/7 | claude-opus-5-5 | 2.1.323 | 2026-11-21 |
| github-activity-report | haiku | claude-haiku-5-5 | none | none | 7/7 | 1 | description 7/7 | claude-haiku-5-5 | 2.1.302 | 2026-10-22 |
| github-activity-report | sonnet | claude-sonnet-5-5 | none | none | 7/7 | 1 | description 7/7 | claude-sonnet-5-5 | 2.1.302 | 2026-10-22 |
| github-activity-report | opus | claude-opus-5-5 | none | none | 7/7 | 1 | description 7/7 | claude-opus-5-5 | 2.1.302 | 2026-10-22 |
| review-document | haiku | claude-haiku-5-5 | high | none | 34/36 | 1 | by-name 3/3, committed-edit 3/3, description 7/8, design-clean 1/2, design-draft 3/3, design-final 2/2, design-gaps 5/5, pdf-gaps 5/5, word-gaps 5/5 | claude-haiku-5-5, claude-opus-5-5 | 2.1.323 | 2026-11-22 |
| review-document | sonnet | claude-sonnet-5-5 | high | none | 36/36 | 1 | by-name 3/3, committed-edit 3/3, description 8/8, design-clean 2/2, design-draft 3/3, design-final 2/2, design-gaps 5/5, pdf-gaps 5/5, word-gaps 5/5 | claude-opus-5-5, claude-sonnet-5-5 | 2.1.323 | 2026-11-22 |
| review-document | opus | claude-opus-5-5 | high | none | 36/36 | 1 | by-name 3/3, committed-edit 3/3, description 8/8, design-clean 2/2, design-draft 3/3, design-final 2/2, design-gaps 5/5, pdf-gaps 5/5, word-gaps 5/5 | claude-opus-5-5 | 2.1.323 | 2026-11-22 |
| review-insights | haiku | claude-haiku-5-5 | high | none | 12/12 | 1 | description 6/6, synthesis 6/6 | claude-haiku-5-5, claude-opus-5-5 | 2.1.323 | 2026-11-21 |
| review-insights | sonnet | claude-sonnet-5-5 | high | none | 12/12 | 1 | description 6/6, synthesis 6/6 | claude-opus-5-5, claude-sonnet-5-5 | 2.1.323 | 2026-11-21 |
| review-insights | opus | claude-opus-5-5 | high | none | 12/12 | 1 | description 6/6, synthesis 6/6 | claude-opus-5-5 | 2.1.323 | 2026-11-21 |
| review-prs | haiku | claude-haiku-5-5 | high | none | 27/29 | 1 | clean-change 2/2, description 7/7, design-clean 1/2, design-gaps 5/5, planted-defects 4/5, re-review 8/8 | claude-haiku-5-5, claude-opus-5-5 | 2.1.323 | 2026-11-20 |
| review-prs | sonnet | claude-sonnet-5-5 | high | none | 29/29 | 1 | clean-change 2/2, description 7/7, design-clean 2/2, design-gaps 5/5, planted-defects 5/5, re-review 8/8 | claude-opus-5-5, claude-sonnet-5-5 | 2.1.323 | 2026-11-20 |
| review-prs | opus | claude-opus-5-5 | high | none | 29/29 | 1 | clean-change 2/2, description 7/7, design-clean 2/2, design-gaps 5/5, planted-defects 5/5, re-review 8/8 | claude-opus-5-5 | 2.1.323 | 2026-11-20 |
| review-repository | haiku | claude-haiku-5-5 | high | none | 16/18 | 1 | branch-name-command 0/1, build-grant 1/1, closed-spellings 2/2, drifted-guide 2/2, missed-sibling 1/1, misstated-exit-status 1/1, open-spelling 2/2, tested-change 1/2, text-read-policy 1/1, undocumented-change 2/2, unnoted-flag-rename 1/1, untested-change 1/1, write-before-refusal 1/1 | claude-opus-5-5 | 2.1.323 | 2026-11-20 |
| review-repository | sonnet | claude-sonnet-5-5 | high | none | 18/18 | 1 | branch-name-command 1/1, build-grant 1/1, closed-spellings 2/2, drifted-guide 2/2, missed-sibling 1/1, misstated-exit-status 1/1, open-spelling 2/2, tested-change 2/2, text-read-policy 1/1, undocumented-change 2/2, unnoted-flag-rename 1/1, untested-change 1/1, write-before-refusal 1/1 | claude-opus-5-5 | 2.1.323 | 2026-11-20 |
| review-repository | opus | claude-opus-5-5 | high | none | 18/18 | 1 | branch-name-command 1/1, build-grant 1/1, closed-spellings 2/2, drifted-guide 2/2, missed-sibling 1/1, misstated-exit-status 1/1, open-spelling 2/2, tested-change 2/2, text-read-policy 1/1, undocumented-change 2/2, unnoted-flag-rename 1/1, untested-change 1/1, write-before-refusal 1/1 | claude-opus-5-5 | 2.1.323 | 2026-11-20 |
| update-pr-dashboard | haiku | claude-haiku-5-5 | none | none | 6/6 | 1 | description 6/6 | claude-haiku-5-5 | 2.1.323 | 2026-11-22 |
| update-pr-dashboard | sonnet | claude-sonnet-5-5 | none | none | 6/6 | 1 | description 6/6 | claude-sonnet-5-5 | 2.1.323 | 2026-11-22 |
| update-pr-dashboard | opus | claude-opus-5-5 | none | none | 6/6 | 1 | description 6/6 | claude-opus-5-5 | 2.1.323 | 2026-11-22 |
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
| audit-ai-config | description | sonnet | 1 | 2026-11-22 | agrees |
| audit-skill | description | opus | 1 | 2026-11-20 | agrees |
| curate-agent-memory | description | sonnet | 1 | 2026-11-21 | agrees |
| review-document | design-draft | sonnet | 1 | 2026-11-22 | agrees |
| review-insights | synthesis | haiku | 1 | 2026-11-21 | agrees |
| review-prs | description | sonnet | 1 | 2026-11-20 | agrees |
| review-repository | misstated-exit-status | opus | 1 | 2026-11-20 | agrees |
| update-pr-dashboard | description | opus | 1 | 2026-11-22 | agrees |
<!-- skill-evals-samples:end -->
