# Code-review operations

The `code-review-operations` bundle installs four public workflows and the hidden `code-review-core` dependency. Development and tests do not modify installed skills, live review archives, dashboards, or GitHub state. The promises those workflows and their archive keep are listed in the [behavior contract](code-review-operations-contract.md).

## Prerequisites

- GitHub CLI (`gh`) installed and authenticated for every configured repository.
- Python 3.11 or newer.
- A supported local agent runtime: Claude Code, Codex, or GitHub Copilot CLI 1.0.88 or newer.
- A configuration file, written once before the first skill run as described below.

Missing tools, authentication failures, incomplete pagination, and rate limits fail the affected repository without advancing its watermark.

### First configuration

No skill in the bundle works until its configuration exists. Before starting one:

1. Copy the example under [Configuration](#configuration) to a new file, such as `candidate.json`.
2. Edit it: name your repositories under `repositories` and `repository_sets`, and point `archive_root`, `summary_root`, and `dashboard_file` at absolute paths on your machine.
3. From the clone, write it to `~/.coding-agent-skills/code-review/config.json`:

```powershell
python -B skills/code-review-core/scripts/review_config.py write candidate.json
```

`write` validates the file first and changes nothing when it is invalid. [Configuration](#configuration) describes every field and how to replace the file later.

## Single-PR review and canary

Generate a durable initial review for exactly one configured pull request with:

```text
/review-prs --pull owner/repository#123
```

Re-review one whose head changed since its last review with `--re-review owner/repository#123 --scope auto|full|incremental` (see "Re-review scope" below). Repeat either selector, or mix them, to review several pull requests in one pass; `update-pr-tracker` does this for the reviews it offers.

These selectors do not enumerate the repository or advance a merged-pull watermark. It writes the validated review pair to the configured archive, using the same trusted reviewer and source-snapshot rules as a batch review.

Use an explicit initial-review canary before enabling a new repository, reviewer manifest, or routing change:

```text
/review-prs --canary owner/repository#123 --canary owner/repository#124
```

Each selector must name a configured open (including draft) or merged pull request. Repeat `--canary` to cover every case the change affects, such as a pull request of each kind a manifest routes to a different specialist, or one on each side of a routing threshold; they are prepared up to four per `prepare` call and reviewed in one pass. Each canary fetches only its pull request, materializes the exact head into an isolated hash-verified source snapshot, and writes the validated JSON/Markdown pair only under its own new temporary canary directory, which `finalize` reports as `CANARY <selector> <root>` followed by a `SHA256` line per file. One canary failing leaves the others' directories in place and reported. A canary never reads or writes configured archives, mirrors, state, dashboards, flags, watermarks, or GitHub review state. Inspect the retained output before deleting it.

## Posting findings

`review-prs` never posts to GitHub. If you then ask the agent to post findings, it should post only the ones you
choose, as one review whose payload a JSON serializer builds and `gh api --input <file>` sends, never `--field`
arguments or a shell heredoc. The payload omits `event` entirely, so no review state (approve, request changes, or
comment) is submitted, and comment bodies are plain text without internal finding IDs or labels. The skill keeps
only the rule against sending an `event`; Codex and Copilot CLI also get the rest through the runtime compatibility
contract every adapter carries.

## Review pipeline

`code-review-core/scripts/review_pipeline.py` runs every deterministic step of `review-prs`, so the orchestrating agent only runs its commands and starts reviewer subagents:

| Command | What it does |
| --- | --- |
| `enumerate [--output FILE]` | Lists each selected repository's open non-draft pull requests and those merged since its watermark, minus already-reviewed heads, into a batch file, and prints `BATCH <file>`. Without `--output` the file is `batch.json` in a new `review-prs-batch-*` temporary directory. A `--output` inside a skills directory (the one the script runs from, `~/.claude/skills`, or `~/.agents/skills`) is refused with `FAILED` before anything is read, because a file left there makes the deployer skip that skill. A repository whose listing fails is reported and keeps its watermark. |
| `prepare [--pull owner/repo#N ...] [--re-review owner/repo#N ... --scope auto\|full\|incremental] [--force] [--canary] [--host claude-code\|codex\|copilot-cli]` | Takes up to four pull requests in all (with `--canary`, only `--pull` and no `--force`), each prepared on its own: `--pull` for an initial review and `--re-review` for a re-review, so one call can mix them. `--scope` is required with `--re-review`, applies to every re-review in the call, and is refused without one. It refuses a pull request named twice, since two runs of it would race to record the same review version. For each, it fetches the pull request, skips an already-reviewed head, writes GitHub's diff and the unresolved review threads a person started, snapshots the head (from `checkout_path`, fetching `refs/pull/N/head` only when the commit is not already local, else from GitHub's tarball), loads the reviewer from its trusted commit, writes the request, and plans one prompt per reviewer role. A re-review carries the open and unverified entries of the finding ledger (see "Finding ledger") and plans its roles for its scope; a pull request with only a migrated legacy review gets an initial review. `--host` names the runtime the orchestrating session runs in, and `review-prs` always passes it (see `runtime` under configuration). |
| `dispatch --run DIR` | Starts the Copilot CLI host, for the `copilot-cli` runtime only, as a process detached from the command, and prints `STARTED <run directory>` at once, so no runtime's command time limit can stop the review. It records the host's PID and start time in `copilot-host.json` and refuses, with `FAILED <reviewer>: <reason>` on stderr and exit 2, while that host is still starting or running, so one reviewer never runs twice. Each attempt keeps the host's own output in `copilot-host-N.log`, Copilot's in `copilot-diagnostic-N.jsonl`, and the host's outcome in `copilot-outcome-N.json`. Copilot writes its result to `copilot-result-N.json`, and the host renames it to the reviewer's result file only when it is a JSON object and the role has not been set aside since `dispatch`, so nothing reads a partial result and a late host never fills a role already set aside. |
| `wait --run DIR --timeout SECONDS` | Waits at most `SECONDS` (1 to 300) for that host and prints one line: `DISPATCHED <result file>` when it finished, `RUNNING <seconds>s` with exit code 1 when it is still going after the timeout, or `FAILED <reviewer>: <reason>` on stderr with exit code 2 when it failed, ended without a result, ran past its 30-minute limit, or was never dispatched. `review-prs` repeats `wait --timeout 90` until it stops printing `RUNNING`; `check` then offers a failed reviewer its one rerun, which is another `dispatch`. |
| `workflow --run DIR [--run ...]` | Writes a Claude Code Workflow script, kept in the first run's directory, that starts every reviewer role of the given runs at once with the same task the native path uses, and prints `WORKFLOW <script> roles=<n>`. It refuses a `copilot-cli` run. |
| `wait-reviewers --run DIR [--run ...] --timeout SECONDS` | Waits at most `SECONDS` (1 to 300) until no role of the given runs is still running, and returns as soon as none is. It prints `READY <selector>` for a run whose roles all have a valid result. For each other role, it prints `RUNNING <selector> <id> <seconds>s`, which gives exit code 1, or `OVERDUE <selector> <id> <seconds>s` once the role has gone an hour since `workflow` handed it out without a valid result. The hour is counted from `workflow`, not from when the role started, so a role queued behind the Workflow tool's concurrency limit is not cut short. Like `validate-result`, it only reads: an invalid result may be one its reviewer is still fixing, so it counts as running and is left for `check`. It refuses a Copilot CLI host run, which `wait` follows. |
| `check --run DIR [--run ...]` | Validates each run's reviewer results. An invalid or missing result is renamed `*.rejected-N` and its role is offered for one fresh rerun; a second failure fails the pull request. A run whose Copilot CLI host is still starting or running prints `RUNNING <selector> <reviewer> <seconds>s` and exits 1, and its role is left as it is. |
| `validate-result --run DIR --role ID` | Run by a reviewer, not the orchestrator: prints `VALID`, or `INVALID <reason>` with exit code 1, using exactly the checks `check` applies to that role. It changes nothing, so it neither sets the result aside nor counts a retry. Every reviewer prompt a native subagent runs names this command and asks the reviewer to fix its result and rerun it, at most twice, before replying. The Copilot CLI host denies shell commands, so its reviewer relies on `check` and `dispatch` alone. |
| `finalize --run DIR [--run ...]` | For each run, assembles the result, assigns finding IDs and the verdict, commits the record pair (or a canary pair under a new temporary root), and removes the run directory. A failed run directory is kept for inspection. |
| `unfinalized --run DIR [--run ...]` | Prints `UNFINALIZED <selector> <run directory>` for each given run that still holds a prepared run, and exits 2 if there is any. Otherwise it prints `ALL_FINALIZED`. A run that `check` failed, and one that nobody finalized, both count. A directory that no longer exists counts as recorded, because `finalize` is the only step that removes a prepared run. An existing directory that is not a prepared run prints `FAILED <run> <reason>` on stderr. |
| `advance --batch FILE` | Moves each fully enumerated repository's watermark past the merged pull requests whose head now has a review. |

Every `prepare`, `check`, and `finalize` line names its pull request, and one pull request's failure never stops the others in the same call; the command exits 2 if any failed. `review-prs` works in groups of four, so each group costs three pipeline calls however many pull requests it holds, and the group size bounds how many reviewers run at once.

When the Workflow tool is available (Claude Code), every mode (a batch, `--pull` and `--re-review`, and `--canary`) instead prepares every pull request, runs one Workflow from `workflow`, then checks and finalizes every run in one call each. `workflow` prints the script between `BEGIN_WORKFLOW_SCRIPT` and `END_WORKFLOW_SCRIPT`, and the orchestrator passes it inline as `script`. The Workflow tool refuses a `scriptPath` that it didn't return itself and that isn't under the session's working directory, which includes the script `workflow` saves in the run folder under the system temp directory. The script stays compact (each role is its ID, its prompt path relative to the run folder, and its model) because the orchestrator copies it. If the tool still refuses, the roles start as native subagents. No pull request waits for another group's slowest reviewer, the Workflow tool's own concurrency limit bounds the reviewers, and their replies never reach the orchestrating session. A Workflow can only start agents, so `prepare`, `check`, retries of `RETRY` roles (as native subagents), and `finalize` stay with the orchestrator, and `check` still records every rejected result and retry. Codex and Copilot keep the grouped steps.

The Workflow tool returns as soon as it starts the reviewers and reports its completion later as a notification. `review-prs` grants its pipeline commands through `allowed-tools`, and Claude Code applies those grants only in the turn that invoked the skill. A session that ended its turn, or scheduled a wakeup, to wait for the notification had `check` and `finalize` denied in the later turn. A headless session has nobody to approve them, so nothing was recorded, and the session still exited with success. A skill cannot extend its grants, and re-invoking it in the later turn would depend on the same model following an instruction it had already ignored. So the orchestrator never leaves the invoking turn. It repeats `wait-reviewers --timeout 90` until no role is running, or until the Workflow reports it finished, and then checks and finalizes. The `OVERDUE` limit ends the wait even if that notification never reaches the session mid-turn.

In every mode, `review-prs` runs `unfinalized` on every run `prepare` started before it reports. Each `UNFINALIZED` line is reported as that pull request's failure. If any pipeline command is denied, every pull request without a `RECORDED` line is reported as failed. A skill cannot change a headless session's exit status, so the failure is in the report itself.

`prepare` works on a call's pull requests concurrently, and `enumerate` lists repositories concurrently, four at a time to stay clear of GitHub's secondary rate limits. Output keeps the order the pull requests or repositories were given in. Fetches into one checkout still run one at a time, because concurrent fetches contend for its `FETCH_HEAD` and ref locks, and a pull request whose commit another one just fetched skips its own fetch.

The generic reviewer is planned like a specialist: it gets the same fixed prompt, numbered diff, and line-anchoring checks, with no trusted repository guidance.

In Claude Code, every reviewer runs as the `code-review-reviewer` subagent, which deploys to `~/.claude/agents` with `code-review-core`. It has its own short system prompt, only the tools a role needs (`Read`, `Grep`, `Glob`, `Write`, `Edit`, `Bash`), and `omitClaudeMd: true`, so the session's `CLAUDE.md` files and their imports stay out of its context. A profiled general-purpose reviewer carried about 20,000 tokens of system prompt and tool definitions on every turn, plus about 11,000 tokens of the session's instruction files. A session started before the agent was deployed may not have the type, so `review-prs` falls back to a general-purpose subagent, and the Workflow script does the same when starting the agent fails. A `PreToolUse` hook in the agent definition runs `review_guard.py`, which enforces the run boundary that prompts alone did not: reviewers told never to read the local checkout searched it, and the whole workspace, anyway. Read, Grep, and Glob must name a path inside a `code-review-run-*` folder holding `run.json`, or `code-review-core`'s `references`. Grep and Glob must name it explicitly, since their default is the session's working directory. Write and Edit may touch only a result file, and Bash may run only the pipeline's own self-check command, exactly as written. A call the guard cannot evaluate is denied: a reviewer reading the wrong code gives a plausible but wrong review, while a failed one is reported and retried. Claude Code runs the hook in Git Bash, or in PowerShell when it finds no Git Bash, and Git Bash takes `$HOME` from `HOME`, which need not be the profile folder the guard is installed in. So the hook has Python find the guard with `os.path.expanduser`, which on Windows reads the profile folder in either shell. It runs Python in isolated mode (`-I`), so modules in the session's working directory cannot stand in for the standard library.

## Tracker pipeline

`update-pr-tracker/scripts/tracker_pipeline.py` runs every deterministic step of `update-pr-tracker`. Like the review pipeline, each command prints one fact per line, exits 0 on success, and prints `FAILED <reason>` on stderr with exit code 2 for an expected failure.

| Command | What it does |
| --- | --- |
| `collect [--output FILE] [--repository owner/repo ... \| --repository-set NAME]` | Resolves the scope with `operation_repository_sets.update-pr-tracker`, reads each repository's open pull requests with one paginated GraphQL query per page of 50 (author and display name, requested reviewers, participants, draft state, base branch, head, update time, review decision, and the configured user's latest submitted review and its commit), joins each pull request's reviewed head, coverage, verdict, counts, finding-ledger summary, and report from the archive, validates the result, and writes the tracker input. Prints `REPOSITORY <repo> pulls=<n>` or `REPOSITORY_FAILED <repo> <error>`, then `INPUT <file>`. Without `--output` the file is `input.json` in a new `update-pr-tracker-input-*` temporary directory; a `--output` inside a skills directory is refused with `FAILED` before anything is read, as for `enumerate`. |
| `update --input FILE [--remove owner/repo#N ...] [--candidates]` | Renders the owned dashboard section, taking the dashboard path, login (`github_login`, else the account `gh` is authenticated as), markers, status overrides, author names, short-link repositories (the default set), and configuration link from the configuration. Prints `UPDATED <dashboard> rows=<n>` and, with `--candidates`, `CANDIDATE missing\|stale owner/repo#N`. |

The **Findings** cell reads the latest review's finding ledger (see "Finding ledger" below): open findings by severity, using the column's `M`, `H`, and `S` letters, with the version the oldest dates from; how many findings reviews have marked addressed; and the review version. An example is `1M 1S open since v1 · 3 addressed · v3`, or `none open · 3 addressed · v3`. A review recorded before ledgers shows only its own counts, such as `1M 2H`.

`collect` writes its input only when every repository succeeded. Rendering from a partial collection would silently drop the failed repositories' rows, so a repository failure fails the update closed and the dashboard keeps its previous rows. Repositories are collected four at a time. Missing GitHub CLI, authentication failures, and rate limits stop the collection immediately: queries not yet started are cancelled, and nothing is written. A pull request with more than 100 review requests or participants, or whose configured user's active review has no commit, fails its repository rather than being tracked from incomplete data. Team review requests are ignored, because a team never matches the configured login.

## Review insights

`review-insights/scripts/review_insights.py` reads the configured archive and summary root. A report reads only validated review records and names every record file and payload hash it analyzed.

| Command | What it does |
| --- | --- |
| `report --start DATE --end DATE [--repository owner/repo ... \| --repository-set NAME]` | Analyzes validated review records reviewed in the inclusive range, resolving the scope with `operation_repository_sets.review-insights`. Writes `<summary_root>/<set>/<start>--<end>/insights.json` and `insights.md`; an explicit repository list uses `repositories-<digest>` as its set name. Prints `REPORT <json>`, `MARKDOWN <md>`, one `RECOMMENDATION <id> <category> findings=<n> decision=<d> flags=<ids or none>` per category, and one `ANALYZER <id> coverage=<c> tool=<t> rule=<r> findings=<n> repositories=<repos> decision=<d> flags=<ids or none>` per analyzer rule, followed by up to three `EXAMPLE <id> <repo>#<pull> v<version> <finding> <title or path:line>` lines. Each recommendation line is followed by its `REVIEWER <id> <reviewer> model=<m> findings=<n> flagged=<n> addressed=<n> still_present=<n>` lines. Regenerating a report keeps each subject's ID, decision, and history; a new subject gets an unused ID. |
| `decide --report FILE <id> (--category NAME \| --analyzer COVERAGE TOOL RULE) --flags IDS\|none accepted\|rejected\|deferred [--note TEXT]` | Fails without changes unless `<id>` still names that category or analyzer rule (tool and rule match without regard to case) and links exactly the flags the user was shown, so a flag linked by a later regeneration is never resolved without reconfirmation. Appends a timestamped entry to the recommendation's `decision_history` and sets its current decision. Accepting resolves the recommendation's linked flags that are still open through the locked flag API. Prints `FLAG_RESOLVED <flag>`, `FLAG_ALREADY_RESOLVED <flag>`, and `DECIDED <id> <decision>`. |

Analyzer recommendations come from the optional `analyzer` coverage reviewers give a finding (see "Analyzer coverage" below). Findings are grouped by coverage, tool, and rule, and ranked cheapest first: `available` rules (in an analyzer the repository already has, so enforcing the rule stops the finding recurring), then `known` rules (adopting an analyzer, which must clear the dependency rules on license, cost, and telemetry), then `custom-candidate` patterns (a custom rule would have to be written). Each lists the repositories its findings came from and every finding it covers as `evidence`. A finding with coverage belongs to both its category's recommendation and its rule's, so accepting either resolves its flag, and accepting the other then reports `FLAG_ALREADY_RESOLVED`. Report schema version 5 adds each recommendation's `kind`; every recommendation in an earlier report is a category one.

Each `REVIEWER` row also counts how many of its findings were later judged `addressed` and how many `still_present`, by the latest disposition of the finding's ledger entry. These are read from every review of the pull request, in range or not, so a finding raised in the range and fixed after it still counts as addressed. They measure whether findings are acted on without anyone flagging them. Report schema version 6 adds them; a row in an earlier report has `null` for both, shown as `-`, until the report is regenerated.

A flag is linked to a recommendation when it is open, names a repository, pull request, review version, and finding, and that analyzed review has that finding among the recommendation's findings. Finding IDs restart at `F001` in every review, so a flag is matched only against the review it names, never a later re-review. Links are computed when the report is written and stored in `linked_flags`, so `decide` resolves exactly the flags the report listed. Report schema version 2 adds `decision_history` and `linked_flags`, and version 3 links each flag only to the review version it names. `decide` still reads version 1 and 2 reports and upgrades them when it records a decision, but drops their links: a version 2 report linked a flag to whatever finding had its ID in the latest review, so a decision on an earlier report resolves no flag until the report is regenerated.

The flag store is `CODE_REVIEW_FLAGS`, else `~/.coding-agent-skills/code-review/flags.json`, for both `review-insights` and `flag-review-finding`. Flag store schema version 2 adds `review_version`, which a flag naming a finding must set. A version 1 store is read as version 2 with no review versions and is written as version 2 on its next change; its flags that name a finding are never linked by `review-insights`, because their finding IDs cannot be tied to one review.

## Configuration

Set `CODE_REVIEW_CONFIG` to an alternate file for fixtures or profiles. Otherwise the suite reads `~/.coding-agent-skills/code-review/config.json`.

```json
{
  "schema_version": 1,
  "default_repository_set": "primary",
  "repository_sets": { "primary": ["example/example-repository"] },
  "repositories": {
    "example/example-repository": {
      "reviewer": {
        "id": "generic",
        "protocol_version": 1,
        "trusted_ref": null,
        "scope": "generic",
        "manifest_path": null
      },
      "checkout_path": null
    }
  },
  "archive_root": "D:\\AgentData\\Reviews",
  "local_mirror_root": null,
  "summary_root": "D:\\AgentData\\ReviewSummaries",
  "dashboard_file": "D:\\AgentData\\PullRequests.md",
  "github_login": null,
  "runtime": "auto",
  "verdict_policy": {
    "request_changes_for": ["MUST_FIX"],
    "should_fix_threshold": 3
  },
  "dashboard": {
    "start_marker": "<!-- code-review-pr-tracker:start -->",
    "end_marker": "<!-- code-review-pr-tracker:end -->",
    "status_overrides": {},
    "author_names": {}
  }
}
```

`runtime` is the runtime that starts reviewers, which decides how they are dispatched: `ROLE` subagents under `claude-code` and `codex`, the Copilot CLI host under `copilot-cli`. Naming one always wins. `auto`, the default, uses the runtime `prepare --host` states, which `review-prs` passes from the session it runs in, and only without a host falls back to the first of `claude`, `codex`, and `copilot` found on `PATH`. `PATH` says which CLIs are installed, not which one is orchestrating, so a Codex or Copilot session on a machine that also has `claude` would otherwise dispatch as Claude Code. Each run's `run.json` records the stated `host` (`null` without one) beside the resolved `runtime`.

`reviewer_effort` optionally sets the reasoning effort (`low`, `medium`, `high`, `xhigh`, or `max`) of every reviewer the Workflow tool starts; `null`, the default, keeps the session's effort. A specialist's own `effort` in its manifest wins over it. It exists to measure whether reviewers think more than a review needs, because thinking was about 80% of a profiled reviewer's output tokens and most of its wall time. Change it only alongside a before-and-after comparison of real reviews: lower effort can miss findings the way a smaller model did. An ordinary subagent cannot take a per-call effort, so it applies only on the Workflow path.

`re_review_scope` optionally sets when an `auto` re-review reviews the whole pull request: `full_share` (default `0.5`) is the share of the pull request's changed lines that changed since the last review, and `full_lines` (default `1000`) is a number of such lines; reaching either means a full pass. Tune them from the scope each record keeps, against what later full passes found.

`operation_repository_sets` optionally gives an operation its own default set, used when no repositories or set are named: for example `{"review-prs": "primary", "update-pr-tracker": "tracked"}`. Valid operations are `review-prs`, `update-pr-tracker`, and `review-insights`; without an entry the operation uses `default_repository_set`. A repository with no merged-pull watermark yet starts at the first run's date, so its first batch reviews only open pull requests.

A review's verdict is `CHANGES_REQUESTED` when its findings require it, otherwise `INCOMPLETE` when a changed file's source was unavailable to reviewers (excluded from the snapshot for size or an unsafe name; listed in the record's `review.coverage.unavailable_sources` and in the report), otherwise `APPROVED`. A coverage gap never hides a blocking finding. Changed files a reviewer manifest deliberately leaves unreviewed (`"uncovered": "ignore"`) are listed in `review.coverage.uncovered_files` and in the report's **Not reviewed** note, and do not change the verdict. A specialist or reviewer that produces no valid result is different: the result is `failed`, nothing is archived, and the pull request is retried on the next run.

A migrated legacy review (`legacy-review.json`, written by the completed legacy migration) counts as a reviewed head for skip detection and the tracker's `current`/`stale` column. It has no structured findings, so `review-prs --re-review` of a legacy-only pull request runs an initial review that becomes version 1.

Validate or atomically replace configuration with:

```powershell
python -B skills/code-review-core/scripts/review_config.py validate D:\AgentData\config.json
python -B skills/code-review-core/scripts/review_config.py write candidate.json --output D:\AgentData\config.json
```

For a repository-provided reviewer, set `scope` to `repository`, give it a symbolic `id`, and configure an absolute checkout path. Then name the reviewer one of three ways:

| Fields | Reviewer | Use when |
| --- | --- | --- |
| `manifest_path` | A manifest committed to the repository, read from the trusted commit. | The repository maintains its own manifest. |
| `skill` | The repository's own review skill or agent file, run as one entrypoint reviewer with the repository files it names. | The skill does not start subagents of its own. |
| `skill` + `manifest` | The same skill's specialists, routed by a manifest kept outside the repository. | The skill starts specialist subagents and the repository should not change. |

`skill` is a safe repository-relative path. `manifest` is an absolute path, or `true` for `reviewers/<owner>/<repo>/manifest.json` beside the configuration file; it cannot be combined with `manifest_path`. `trusted_ref` may override the PR base commit, but pull refs and a ref resolving to the reviewed head are rejected.

### Choosing and checking a repository reviewer

A review skill that starts its own subagents fails when it runs as a reviewer, because a subagent cannot start subagents. Two read-only commands tell you whether a skill needs a manifest, and whether a manifest works:

```powershell
python -B skills/code-review-core/scripts/review_pipeline.py inspect-reviewer --repository owner/repo
python -B skills/code-review-core/scripts/review_pipeline.py validate-reviewer --repository owner/repo --pull 123
```

`inspect-reviewer` reads the configured `skill` at the trusted ref (or `--ref`) and prints its `TOOLS`, `DELEGATES yes|no|unknown` with the reason and `EVIDENCE <line> <text>`, the repository files it names as `REFERENCES`, and a `VERDICT`:

- `entrypoint-ok`: its tool list rules out Agent and Task, or its text never asks for a subagent. `skill` alone is enough.
- `manifest-required`: it may start subagents and its text says it does, or it names another agent file. `skill` alone is refused at `prepare`; write a specialists manifest that routes those agents directly, pointing its profiles at the existing agent files.
- `undetermined`: its tool list grants Agent or Task but its text never says why. It runs as an entrypoint with a note; add a manifest if its review fails.
- `manifest-configured`: a manifest is set.

`validate-reviewer` proves a reviewer setup without starting a reviewer or writing anything. It prints the reviewer and its source, `FILES <n> found` after reading every file the manifest names (profiles and resources at the trusted commit, condition scripts beside a local manifest), and `UNMATCHED <specialist> <pattern>` for an include pattern that matches no file in the repository. It measures the source snapshot `prepare` would write, with the same exclusions, and prints `SNAPSHOT <commit> files=<n> bytes=<n> limit=<n> excluded=<reason>:<n>,...`: for the reviewer's commit without `--pull`, and for each pull's head (where a changed file keeps its 16 MiB allowance) with it. A snapshot over the file-count or size limit prints `FAILED` with the largest top-level directories, because `prepare` would refuse it. For each `--pull`, it prints the pull's changed files and which specialists would start (`ROUTE`), each condition script's real result against the pull's head (`CONDITION open|closed`), or `GENERIC` when none matches. When some specialists route, it prints `UNCOVERED <path>` for each changed file no specialist matches, then `GENERIC files=<n>` when the generic reviewer will review them or `UNREVIEWED files=<n>` when the manifest sets `uncovered` to `ignore`. A file matched only by a specialist whose condition is closed is skipped on purpose and is not listed. It ends with `VALID`; any missing file, invalid manifest, or failing condition script prints `FAILED`. Rerun it when the repository's own routing changes, since a local manifest is a hand-kept copy of that routing.

A local manifest supplies only its condition scripts, from its own folder. Every profile and resource it names is read from the repository's trusted commit, never from the pull-request head, and a condition script may not share a path with one of them.

## Re-review scope

Every review records a fingerprint of each changed file's patch in `review.patches`. A fingerprint hashes only the added and removed lines and the file's header lines (a binary file uses its new blob ID), never hunk positions, context, or the base side's blob, so a base merge or rebase that leaves a file's change alone leaves its fingerprint alone. A re-review compares the fingerprints at the new head with the latest record's; a file whose fingerprint differs, or that the record did not have, has changed since that review.

A re-review always names its scope; nothing picks one silently. `update-pr-tracker` asks once per run for its stale reviews, and `review-prs --re-review` asks when no `--scope` was given:

- `full` reviews the whole pull request again, as an initial review would, and gives every prior finding a disposition.
- `incremental` reviews in full only the files that changed. A specialist none of whose files changed only gives dispositions for its prior findings and comments, and is left out when it has none. A pass in which nothing changed still records the new head with a dispositions-only generic reviewer.
- `auto` runs `full` when the changed files' lines reach `re_review_scope.full_share` of the pull request's changed lines or `re_review_scope.full_lines` lines (counting files instead when nothing has changed lines), and `incremental` otherwise.

`incremental` and `auto` fall back to `full` when the latest record has no fingerprints (it was written before they were recorded) and for a repository entrypoint reviewer, which always reviews everything. `prepare` prints the scope used as `NOTE <selector> Scope <scope>, <files> of <total> files and <lines> of <total> changed lines differ from v<n> (requested <scope>: <reason>)`, and the record keeps the same facts in `review.scope`, which the report shows.

## Finding ledger

Finding IDs restart at `F001` in every review, so a problem's identity across versions is the chain of dispositions, which every record now makes explicit in its `ledger`. The ledger has one entry for every problem raised since the pull request's latest initial review. An entry is identified by the version and ID where it first appeared, written `v1:F001`, and records its severity and category, its state, the version that last judged it (`judged_in`), each later review's disposition, and the findings linked to it as repeats. `finalize` computes it, and the verdict, the report, `update-pr-tracker`, and `review-insights` read it.

- **State.** An entry is `open` when its latest judgment raised or repeated it, or found it `still_present` or `partially_addressed`. It is `closed` when the latest is `addressed` or `superseded`, and `unverified` when `unable_to_verify`.
- **Verdict.** `calculate_verdict` counts open entries, not the latest record's findings. A must-fix carried from an earlier review therefore requests changes until a review closes it, even when the review that judged it reported no findings, such as a dispositions-only pass of an incremental re-review. `request_changes_for` and `should_fix_threshold` apply to open entries as they did to findings. An `unverified` entry does not count, but is offered again.
- **What a re-review is given.** A re-review is given every open and unverified entry as a prior finding, with ID `v<n>:F<nnn>`, at the place it was last reported. That includes an entry the previous review only judged, so nothing drops out of the history unjudged. A record with a ledger names its prior dispositions by those IDs.
- **Repeats.** A finding that reports the same problem as another one sets `repeats` and is counted with it, never twice. In a reviewer's result, `repeats` names another finding in the same result (a specialist gives its 0-based index; the adapter protocol a `candidate_key`) or a prior finding ID the reviewer judged `still_present` or `partially_addressed`. The record stores it as `{"version": <n>, "id": "F<nnn>"}`. The target must be at least as severe, must not be a repeat itself, and must exist; validation rejects anything else, and a specialist's invalid link is a retryable invalid result. `assemble` follows a link through merged findings to the finding at its end, and drops it when that finding is now less severe than the linking one, so the finding counts on its own. A `full` re-review that reports a still-present problem again links it to its entry instead of opening a second one. The report shows a repeat inside the finding it repeats, or with a **Repeats** line for a prior finding, and lists the open entries from earlier reviews under **Open Findings**.
- **Initial reviews.** An initial review starts a fresh ledger. A batch or `--pull` review of a pull request whose head changed is an initial review, given no prior findings. Earlier entries would never be judged, and carrying them would count every problem the new review reports again a second time.
- **Older records.** A record written before ledgers stays valid and has no `ledger`; the tracker shows its counts alone. Its ledger is computed from the records' findings and dispositions when the next re-review is prepared and finalized. Their bare dispositions name findings of the version they compared with. Those records have no links, so a problem an earlier `full` re-review reported again is two entries; the next re-review is given both and can mark one `superseded`.

## Reviewer manifest

The manifest declares every trusted reviewer file. Directories are not recursively discovered. A repository's existing human-oriented review skill may be a declared resource, but the manifest entrypoint must also enforce the normalized adapter-result protocol; terminal prose is not accepted as a result.

```json
{
  "schema_version": 1,
  "id": "repository-review",
  "protocol_version": 1,
  "supports": ["initial", "re-review"],
  "required_capabilities": ["agent-delegation", "read-diff", "write-result"],
  "entrypoint": ".claude/skills/repository-review/SKILL.md",
  "resources": [".claude/skills/repository-review/references/rules.md"],
  "agent_profiles": [".claude/agents/repository-review.md"]
}
```

The core verifies the checkout's GitHub origin, resolves the trusted ref to a commit, rejects symlink entries and unsafe paths, materializes only declared blobs, and records the source commit and SHA-256 hash of every loaded file.

### Specialist reviewers

A repository whose review is split across specialist agents declares them instead of writing an orchestrating entrypoint. The suite routes changed files, starts one agent per matched specialist, and assembles their results; the repository supplies only data and trusted instruction files.

```json
{
  "schema_version": 2,
  "id": "repository-specialists",
  "protocol_version": 1,
  "kind": "specialists",
  "supports": ["initial", "re-review"],
  "required_capabilities": ["agent-delegation", "read-diff", "write-result"],
  "resources": ["docs/review/conventions.md"],
  "specialists": [
    {
      "id": "database-review",
      "category": "Database",
      "profile": ".claude/agents/database-review.md",
      "include": ["^db/.*\\.sql$"],
      "exclude": [],
      "resources": ["docs/review/database.md"],
      "when": null
    }
  ],
  "conditions": {
    "compatibility-window-open": { "script": "tools/review/compatibility_window.py" }
  }
}
```

- `include` and `exclude` are Python regular expressions searched against repository-relative paths. A specialist runs when at least one changed file matches an `include` pattern and no `exclude` pattern.
- `when` names a condition. Its script, materialized from the trusted commit, runs as `python <script> --source-root <snapshot>`; exit code 0 enables the specialist, 1 skips it, and anything else fails the review. Conditions are evaluated only for specialists that matched files.
- `resources` are trusted files any specialist may read. Each specialist's own `resources` are materialized too: these are its guideline documents, so they are read from the pull request's base commit when that commit contains them (a pull request into a release branch is reviewed against that branch's rules) and from the trusted ref otherwise. Profiles, shared `resources`, and condition scripts always come from the trusted ref, which must hold the manifest. `materialization.json` records each guideline's source commit in `guideline_sources`. Specialist ids are slugs; `generic-review` is reserved.
- A specialist may set `model` (`inherit`, `sonnet`, `opus`, `haiku`, or `fable`) and `effort` (`low`, `medium`, `high`, `xhigh`, or `max`) in the manifest. Both are optional and are validated with the manifest. The manifest's `model` wins over the profile's (`inherit` means the session's model, whatever the profile says), and its `effort` wins over the configuration's `reviewer_effort`. A setting for one specialist beats a general one. Effort reaches only reviewers the Workflow tool starts: an ordinary subagent cannot take one, so the native path, including the Workflow fallback, drops it. `validate-reviewer` shows each route's settings as `ROUTE <id> files=<n> model=<model> effort=<effort>`. Use these settings only alongside a before-and-after comparison of real reviews: a cheaper model or lower effort can miss findings.
- Otherwise, a profile's frontmatter `model` is applied when its reviewer starts: `prepare` and `check` print `MODEL <selector> <id> <model>` after that role's `ROLE` or `RETRY` line, the orchestrator passes it to the native subagent, and `workflow` passes it to the Workflow agent. Only the aliases `sonnet`, `opus`, `haiku`, and `fable` are applied, because the Agent tool accepts no other value and on Bedrock a bare model name can be silently ignored. `inherit` or no `model` uses the session's model, and any other value prints a `NOTE` and uses the session's model. `validate-reviewer` shows each route's model as `ROUTE <id> files=<n> model=<model>`. A profile's `tools` list is not applied: neither the Agent tool nor a Workflow agent can restrict a subagent's tools, and the suite's reviewers also need to write their result file.
- When no specialist matches, the suite's generic reviewer reviews the whole change. Prior findings and open review comments on files no routed specialist owns are given to the generic reviewer for disposition only.
- When some specialists route, the generic reviewer also starts beside them and reviews only the changed files no specialist matches, such as CI workflows, build properties, dependency manifests, scripts, and agent instructions. Without it nobody reviewed them: one reviewed change added "approve everything" to a specialist's profile, and only the source beside it was reviewed. An agent-instruction file stays out of the source snapshot, so the generic reviewer judges it from its diff as untrusted data. A file matched only by a specialist whose `when` condition is closed is skipped on purpose, not given to the generic reviewer. In an incremental re-review the generic reviewer takes only those of these files that changed.
- A manifest that deliberately leaves some paths unreviewed sets `"uncovered": "ignore"` at its top level (the default is `"review"`). Then no generic reviewer starts for those files: `prepare` prints a `NOTE` naming them, and the record lists them in `review.coverage.uncovered_files` and the report in a **Not reviewed** note. Prior findings and comments on them still get a disposition from the generic reviewer.

`code-review-core/scripts/review_specialists.py plan` writes, per specialist, the routed file list, the filtered diff, an `other-changes.diff` of every changed file outside its scope, and a self-contained prompt. Both diffs are numbered: each hunk line keeps its `+`, `-`, or space marker first, then shows its line number in the new file (blank on removed lines), then ` | ` and the text. A separate table of added lines used to repeat every added line in each reviewer's context on every turn, about 25,000 tokens per turn for one specialist on a large pull request. The prompt maps the specialist's instructions onto those inputs: no `git` or `gh` commands, repository documents under the materialized root, source under the snapshot, finding lines only from the numbers on the diff's added lines, and independent reads made in the same turn. When the repository has a `checkout_path`, the prompt forbids reading it: one reviewer read half its context files from the local working copy, which may be on another branch, instead of the snapshot. `prepare` also prints a `NOTE` when the orchestrating session itself runs inside the checkout, because then the checkout's `CLAUDE.md` files and project memory load into every reviewer on every turn. It also names the other changes as read-only context, because the snapshot holds only the code after the change. They exclude the specialist's own files, which a whole-pull-request diff repeated for the rest of the review. The prompt lists the other changed files, up to 50 (an `other-files.txt` names them all), and tells the specialist not to read their diff unless its instructions need a caller, consumer, or contract outside its own files. When every reviewer read the other changes, the three smaller specialists on one large pull request roughly doubled their turns and their cache writes, investigating changes their checks did not need. A specialist judging what the previous version did, such as whether a caller that still runs during a rolling upgrade breaks, reads the removed lines there. Without it, a database reviewer cleared an in-place change to a stored procedure's result columns: it couldn't see that the same pull request had rewritten the procedure's C# consumer, and judged that consumer from the head source alone. Findings still come only from the specialist's own added lines. Each specialist writes `{model, summary, findings, prior_dispositions, comment_dispositions}`, where `model` is the model ID its system prompt says it runs on (or `unknown`), and every finding carries a one-line `title` of at most 120 characters that the report uses as its headline. A finding may also carry `repeats` (see "Finding ledger"). `check` validates every result; `assemble` drops findings that are not on an added line of that specialist's own files, merges two findings at the same path and line when one description contains the other, or when they come from different specialists and name mostly the same code (identifier overlap of at least half, three or more in common), keeping the most severe finding's title, wording, severity, and category, requires exactly one disposition for each prior finding and each open review comment, and writes the adapter result. Any missing or invalid specialist result yields status `failed`, which is never archived.

### Analyzer coverage

A reviewer can mark a finding a diagnostic analyzer could catch with `analyzer: {coverage, tool, rule}`, and leaves it out when finding the issue needs judgment about intent or behavior. `coverage` is `available` (a rule in an analyzer the repository already has, left unenforced by its settings), `known` (a rule in an established analyzer the repository does not use), or `custom-candidate` (no existing rule; `rule` is a lowercase kebab-case pattern name of at most 60 characters, reused for every occurrence of the pattern). `tool` and `rule` are single tokens of at most 100 characters. The field is optional in the adapter protocol and in records, so records written before it existed stay valid, and the report adds an **Analyzer** line to the finding.

`plan` builds `analyzers.json` from the source snapshot with `code-review-core/scripts/review_analyzers.py` and names it in every prompt as `ANALYZERS_FILE`, so a reviewer needs no turns to find the repository's analyzers. It lists analyzer packages and `Analyzer` items referenced from MSBuild files, the SDK's own analyzers and code-style rules for SDK-style projects, and analyzers identified by their configuration files (ruff, flake8, pylint, mypy, pyright, ShellCheck, PSScriptAnalyzer, ESLint and its plugins, and others), plus the settings that decide which rules run and how severely: MSBuild analysis properties, `.editorconfig` and `.globalconfig` diagnostic severities, ruff and flake8 selections, and ShellCheck directives. It only parses files; it never runs an analyzer or repository code. `check` rejects `available` for a tool the inventory does not list and `known` for one it does, so "the repository already has it" rests on the repository's files. A repository entrypoint reviewer gets no inventory, so only the field's shape is checked.

### Review comments and the Reviewers table

`prepare` lists the pull request's unresolved review threads that a person started (a bot's thread, such as an AI reviewer's, is excluded; a deleted account counts as a person) as `C1`, `C2`, ..., using each thread's first comment. Every reviewer the suite plans must give each comment it is routed exactly one disposition from the prior-finding vocabulary, judged by whether the current code addresses the request. A repository entrypoint reviewer that predates comment dispositions may omit them all; one that gives any must cover every comment. The record keeps the comments and their dispositions together, and the report shows them under **Prior Findings Status → From GitHub PR comments**.

`finalize` also records which reviewers ran in `review.reviewers`: each reviewer's ID, focus, how many changed files it covered, how many final findings it raised (a finding merged from several specialists counts for each), how many times it was rerun after an invalid result, whether it only gave dispositions, the model it reported running on, and how long it took. The report shows them as a **Reviewers** table. The model comes from the reviewer's own accepted result, because the scripts cannot see which model a subagent got: a reviewer started without a model inherits a default the runtime resolves, and on one Bedrock setup that silently became a smaller model, which missed a must-fix finding. A repository entrypoint reviewer's result protocol has no model field, so its model shows as `-`. Records written before these fields existed stay valid. Token usage and cost are deliberately not recorded.

A reviewer's time (`seconds`, shown as **Time**) runs from when its role was handed out to the last write of its accepted result, so a rerun counts toward it. A role is handed out when `prepare` prints its `ROLE` line, when `workflow` writes the script that starts it, or when `dispatch` first starts the Copilot CLI host. The time also covers the orchestrator's turn to start the reviewer and any wait for the Workflow tool's concurrency limit, so it shows which reviewer the pull request waited on, not the model's working time. A run prepared before timing existed records no time and shows `-`.

## Source snapshots

Every adapter request includes a source snapshot materialized from the exact PR head commit. Snapshot files are hashed as they are written. Within `prepare`, nothing untrusted runs between the write and the request, so the later steps check only the snapshot's structure: its manifest, its exact file set, the size limits, and that no path is a reparse point or escapes its root. A step that runs in a separate process, such as the Copilot CLI host or `review_specialists.py plan`, also re-reads and re-hashes every file. Under real-time antivirus each file read costs milliseconds, so re-reading a large repository's snapshot three times took minutes. Files are written from a small thread pool for the same reason. Agent configuration and instruction paths—including `CLAUDE.md`, `AGENTS.md`, `.claude/`, `.agents/`, `.codex/`, and GitHub agent/skill/instruction paths—are excluded so a pull request cannot replace its own reviewer instructions. Repository-specific rules needed by a reviewer must instead be declared in its manifest and are materialized separately from the trusted base commit.

The snapshot is read-only by capability: runtime hosts may read it, but only the normalized result path is writable. It is extracted from the configured checkout's object store (`git archive` of the head commit; no worktree or branch change) or, for a repository without `checkout_path`, from GitHub's tarball of that commit.

Binary files (a NUL byte within the first 8,000 bytes, the same test Git uses) and files over 1 MiB are recorded as explicit `binary` or `file-size-limit` exclusions in `source-snapshot.json` and do not count toward the 256 MiB snapshot total; reviewers still see their changes in the diff. Files the pull request changes are kept up to 16 MiB. Paths Windows cannot represent safely (a `:` or reserved character in any segment) are recorded as `unsafe-path` exclusions and never written. Two paths that differ only by case, or by trailing dots or spaces, fail the snapshot, because a case-insensitive filesystem would merge them. Exceeding the file-count or total-size limit, and non-regular entries, also fail closed.

## Dashboard markers

Create exactly one marker pair in the configured dashboard before running `update-pr-tracker`:

```markdown
<!-- code-review-pr-tracker:start -->
<!-- code-review-pr-tracker:end -->
```

Missing or duplicate markers fail without modifying the file.

The tracker renders one section per state, each headed with its row count: `To Review`, `Awaiting Response`, `Drafts`, one section per pinned override, and `My PRs`. Each section is a collapsible `<details>` block whose table groups rows by requestor (`Requestor | PR | AI Result | Findings | AI Review`; `My PRs` shows `PR | Status | ...` with GitHub's review decision). AI Result is the latest review's verdict, Findings its counts (`2M 1H 3S`), and AI Review a `vscode://file/` link to its report, marked `(stale)` or `(incomplete)` when applicable. Empty sections are omitted. Section placement depends only on the configured user's own latest GitHub review:

| User's latest review | PR changed since that review | Section |
|---|---|---|
| None or dismissed | — | `To review` |
| `CHANGES_REQUESTED` or `COMMENTED` | changed | `To review` |
| `CHANGES_REQUESTED` or `COMMENTED` | unchanged or unknown | `Awaiting response` |
| `APPROVED` | changed or unknown | `To review` |
| `APPROVED` | unchanged | omitted |

"Changed" is computed by the tracker, not judged by the agent, from GitHub's comparison of each commit with the current base branch (`base...sha`). A pull request is unchanged only when both commits change the same set of files, with the same status and previous name, and leave every one of them identical in both content and Git file mode (executable, symlink, or submodule), so a review of the earlier commit applies unchanged to the later one. Any difference, including a merge commit that resolves a conflict or brings in other work, is a change. Evidence it cannot obtain or that proves nothing, such as an unreachable reviewed commit, a comparison listing GitHub's maximum of 300 files, a truncated file tree, or commits that have already reached the base branch, is unknown, so uncertainty never moves a requested-changes review into `To review`. Missing GitHub CLI, authentication, and rate-limit failures stop the run instead.

The `AI review` column is independent of section placement. It reports `missing`, `current`, `stale`, or `incomplete`, using the same change detection against the latest validated AI review's head commit, so base-branch merges do not make a review stale. `incomplete` is an unchanged head whose review could not read every changed file in full; it is not offered for review again until the head changes. Draft pull requests are offered like any other, so their AI review is ready before they leave draft.

Dashboard status overrides are for exceptional pinned states the normal classifier cannot derive, such as `on hold` or `delegated`. Do not add an override merely because a review finished or a pull request merged. Computed states are rejected as overrides.

The Requestor column shows each author's GitHub profile name, or their login when the profile has none. `dashboard.author_names` optionally maps a login to the name to show instead, for example `{"octocat": "Mona Lisa"}`, for authors with no profile name or one you would rather not display. Logins match ignoring case, so two entries that differ only in case are rejected; names must be non-empty single lines. Rows within a section sort by the name shown. It applies only to rendering, so an edit takes effect on the next `update` without recollecting.

When the user directly says a pull request is approved, looks good, or can be removed, pass its full `owner/repo#number` key with `--remove`. This removes only its dashboard row and updates the generated counts; it never submits a GitHub review or records a persistent override.

After updating the tracker, `update-pr-tracker` lists pull requests whose AI review is missing or stale and asks whether to generate or refresh them. The prompt contains no cost estimate. Use `--no-review` to update the dashboard without that prompt. When any is stale, the same prompt asks once how to re-review them (`auto`, `full`, or `incremental`; see "Re-review scope"). It then makes one `review-prs` call for everything confirmed, with `--pull` for each missing review and `--re-review` for each stale one with that `--scope`, so the pull requests are reviewed in one pass rather than one after another.

The bundled generic reviewer prompt and result schema are loaded from the installed `code-review-core/references` directory. They do not depend on a repository checkout, its current branch, or a hard-coded local path.

## Legacy reviews

The one-time migration from the legacy local skills is complete, and its tools have been retired. Migrated pull requests keep a `legacy-review.json` index of the legacy review's head, verdict, and time beside the copied `legacy-review.md`; the findings and prior-finding statuses were never converted and stay readable only in that Markdown. When comparing a legacy report with a new one, read the legacy statuses as these dispositions:

| Legacy status | Disposition |
| --- | --- |
| ADDRESSED | `addressed` |
| PARTIALLY ADDRESSED | `partially_addressed` |
| NOT ADDRESSED | `still_present` |
| NO LONGER APPLICABLE | `superseded` |

The new `unable_to_verify` has no legacy equivalent. Legacy per-severity finding IDs (`M1`, `H1`, `S1`) are not carried over; new reports number findings `F001`, `F002`, ... so an ID survives a change of severity between reviews.
