---
name: review-prs
description: "Review eligible pull requests in explicitly configured repositories, review or re-review explicit pull requests, or run isolated initial-review canaries, and produce validated structured reports. Use it when asked to review or re-review pull requests."
argument-hint: "[owner/repo ... | --repository-set NAME | --pull owner/repo#number ... --re-review owner/repo#number ... --scope auto|full|incremental] [--force] | --canary owner/repo#number ..."
allowed-tools: ["Bash(python -B \"${CLAUDE_SKILL_DIR}/../code-review-core/scripts/*)", "PowerShell(python -B \"${CLAUDE_SKILL_DIR}/../code-review-core/scripts/*)", "Read", "Agent", "AskUserQuestion", "Workflow"]
---

# Review pull requests

Every step below is one command of the `code-review-core` pipeline script, run exactly as shown; do not import the core modules, write your own glue code, or read the core scripts to work out what to do. Commands print one fact per line and exit 0 on success; `FAILED <reason>` on stderr with exit code 2 is an expected failure to report, not a reason to improvise. Never infer an organization-wide scope.

`--pull` (review) and `--re-review` (review again after the head changed) each take one `owner/repo#number`. Repeat either, or both, to cover several pull requests in one run, naming each pull request once; they cannot be combined with batch selectors. `--canary` takes one `owner/repo#number` too and is repeated the same way; it cannot be combined with any other selector or with `--force`.

Every `--re-review` needs `--scope`, which applies to all of them: `full` reviews the whole pull request again, `incremental` reviews in full only the files whose changes differ from the last review (the rest only get dispositions), and `auto` picks one of those for each pull request from how much changed since its last review. If `--re-review` was given without `--scope`, ask the user once with AskUserQuestion, offering `auto`, `full`, and `incremental` in that order; never choose one yourself. Each re-review's `NOTE <selector> Scope ...` line says which scope ran and why; include it in the report.

1. **Batch mode only.** Enumerate, passing the user's `--repository owner/repo` (repeatable) or `--repository-set NAME`, or neither for the configured set, plus `--force` if given, and a new batch file path:
   ```bash
   python -B "${CLAUDE_SKILL_DIR}/../code-review-core/scripts/review_pipeline.py" enumerate --output "<batch file>"
   ```
   Review each printed `PULL <owner/repo#number>`. Report each `REPOSITORY_FAILED <repository> <error>`.
2. **Prepare** the pull requests in groups of up to four, one `--pull` per pull request in one command (or `--re-review` for one given with `--re-review`), adding `--scope` when the command has a `--re-review`, and `--force` when given. Canaries are prepared the same way, as one `--canary` followed by a `--pull` for each of the group's pull requests. `--host` names the runtime this session is running in: `claude-code`, `codex`, or `copilot-cli`. Give it a timeout of at least 10 minutes:
   ```bash
   python -B "${CLAUDE_SKILL_DIR}/../code-review-core/scripts/review_pipeline.py" prepare --host "<runtime>" --pull "<owner/repo#number>" --pull "<owner/repo#number>"
   ```
   `SKIP <selector> <reason>` means it is already reviewed at this head; report it. `FAILED <selector> <reason>` on stderr means that pull request failed (for a re-review, `has no review yet` means it needs `--pull` instead); report it and keep going with the rest of the group. Every other pull request gets a block: `RUN <selector> <run directory>`, any `NOTE <selector> <text>` lines, and either `ROLE <id> <prompt file>` lines, each possibly followed by `MODEL <selector> <id> <model>`, or one `HOST copilot-cli <run directory>` line. A re-review's `NOTE` that the review supersedes a migrated legacy review means there were no structured findings to carry forward, so it runs as an initial review that becomes version 1; say so in the report.
3. **Review.** For each `ROLE` in the group, start one fresh native subagent of type `code-review-reviewer`, all before waiting, with exactly this prompt and nothing else: `Read <prompt file> and follow it exactly. It is your complete task.` When a `MODEL` line names that role, start its subagent on that model (in Claude Code, the Agent tool's `model`); otherwise name no model. If this session has no `code-review-reviewer` type (it is deployed with these skills, so a session started before the deployment may lack it), use a general-purpose subagent instead. For `HOST copilot-cli`, run the pipeline's `dispatch --run <run directory>` command instead.
4. **Check** the group's runs together when they finish, one `--run` per run directory:
   ```bash
   python -B "${CLAUDE_SKILL_DIR}/../code-review-core/scripts/review_pipeline.py" check --run "<run directory>" --run "<run directory>"
   ```
   `ALL_VALID <selector>` means that pull request is ready for step 5. For each `RETRY <selector> <id> <prompt file> <reason>`, start one fresh subagent of the same type with the same prompt as before, on the model of any `MODEL` line that follows it (or rerun `dispatch`), then check those runs again. `FAILED <selector> <id> <reason>` means that pull request failed: report the reason, archive nothing, and keep it eligible for the next run.
5. **Finalize** every `ALL_VALID` run in one command, which assembles each result, assigns finding IDs and the verdict, and commits each JSON/Markdown pair:
   ```bash
   python -B "${CLAUDE_SKILL_DIR}/../code-review-core/scripts/review_pipeline.py" finalize --run "<run directory>" --run "<run directory>"
   ```
   Report each `RECORDED <selector> verdict=<verdict> findings=<count> <report>` line, and each `FAILED <run directory> <reason>` on stderr as that pull request's failure. A version race or archive failure is that pull request's failure, never permission to guess the next version. Each canary also prints `CANARY <selector> <root>` and `SHA256 <hash> <path>` lines: its pair is written only under its own new temporary root and nothing configured is read or written; report every root and its hashes and leave them for inspection. Then start the next group at step 2.
6. **Batch mode only.** After every pull request has finished or failed, run the pipeline's `advance --batch <batch file>` command and report its `WATERMARK` lines. It moves a repository's watermark only past merged pull requests that now have a review, so failures stay eligible. `--pull`, `--re-review`, and `--canary` never advance a watermark.

## With the Workflow tool

When the Workflow tool is available (Claude Code), start reviewers through it in every mode: a batch, `--pull` and `--re-review`, and `--canary`. A batch is then reviewed in one pass instead of in groups, so no pull request waits for another group's slowest reviewer, reviewers' replies stay out of this session, and a configured `reviewer_effort` reaches every reviewer (an ordinary subagent cannot take one). Use the steps above with these changes:

1. Prepare every pull request first (step 2, still up to four per command, one command after another).
2. Write one Workflow script for every prepared run, one `--run` per run directory:
   ```bash
   python -B "${CLAUDE_SKILL_DIR}/../code-review-core/scripts/review_pipeline.py" workflow --run "<run directory>" --run "<run directory>"
   ```
   It prints `WORKFLOW <script> roles=<count>`, then the script between a `BEGIN_WORKFLOW_SCRIPT` line and an `END_WORKFLOW_SCRIPT` line. If any run printed `HOST copilot-cli`, use the groups instead.
3. Call the Workflow tool with `script` set to exactly the lines between those two markers, copied verbatim, and no other input; do not pass `scriptPath`, which the tool refuses for a file in the system temp directory. Wait for it to finish. It starts every role with the same prompt as step 3, up to the tool's own concurrency limit. If the Workflow tool refuses the script or cannot start it, start every role as a native subagent instead (step 3) and continue.
4. Check every run in one command (step 4), starting a fresh native subagent for each `RETRY`, then finalize every `ALL_VALID` run in one command (step 5) and run step 6.

Never write, repair, or edit a reviewer result, a prompt, or a run file yourself, and never parse a reviewer's prose as its result. Review only GitHub state: never post comments or submit reviews.

If the user later asks you to post findings, post only what they choose: build the review payload with a JSON serializer, omit `event` entirely so no review state (approve, request changes, or comment) is submitted, and pass it with `gh api --input <file>`, never with `--field` arguments or a shell heredoc. Keep comment bodies plain text, without internal finding IDs or labels.

Groups of four bound the reviewers running at once, and the Workflow tool bounds them on its path; do not start the next group early. Report per-repository partial failures explicitly; success in one repository must not conceal failure in another.
