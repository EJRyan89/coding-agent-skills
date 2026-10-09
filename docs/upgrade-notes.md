# Upgrade notes

What each release asks of a user who updates, one entry per contract change since the previous tag. The GitHub release notes open with the release's entries; [Releasing](releasing.md#versioning) defines the contracts and the levels.

Validation (`tests/run_validation.py`) compares the contract files at the last tag reachable from `origin/main` with the working tree and fails while a changed contract item is named by no entry added since that tag. New entries go under `## Unreleased`; the person tagging renames that heading to the version and opens a new empty `## Unreleased` above it.

An entry is a `###` heading saying what changed, followed by four fields:

- `Level:` starts with a level word from the Versioning section: `patch`, `minor`, or `major`. Before `1.0.0`, a change that would be major is carried in a minor, and the entry says so.
- `Contract:` the contract items the entry covers, each in backticks as the validation failure names it, or `none` for a demand no contract file records.
- `User action:` what the user must do after updating, or `none`.
- `Pull request:` the pull request as `#N`. Until it is open, the issue it closes stands in.

## Unreleased

### The code-review contract states the specialist result and the review state, and enumerate exits 1 after a failed repository

- Level: minor. Before `1.0.0` this carries what would later be major. The contract's Formats section gains tables for the specialist result each reviewer writes and for the review state `review-prs` keeps (`state.json`), and its opening paragraph names its one exception, `review-insights`' own report. A specialist result now fails its check, and its reviewer is asked to fix it, when it holds a field its prompt's output contract does not list, when it gives its findings as `comments` instead of `findings`, or when a finding's `category` is not a string; before, these were accepted, and the extra fields were dropped. `review_pipeline.py enumerate` exits 1 after printing any `REPOSITORY_FAILED` line, as the tracker's `collect` does; it still lists the other repositories and writes the batch. The generic reviewer's instructions now follow its prompt's output contract and no longer name the adapter schema. The contract now says that the configuration file takes no lock, and why. The unused `references/review-output-template.md` is removed.
- Contract: `docs/code-review-operations-contract.md`
- User action: none. Nothing in `review-prs` acts on `enumerate`'s exit code. A repository specialist profile that tells its reviewer to add fields of its own needs no change: the prompt's output contract replaces any format a profile gives, and the reviewer's self-check names a field that does not belong.
- Pull request: #197

### update-coding-agent-skills stops at a release even when local main has no release tag

- Level: patch. A fix: the `MAJOR_UPDATE` stop now applies when no release tag reaches local `main`, which counts as version 0.0.0 and prints as `MAJOR_UPDATE untagged..<target>` (and `CROSSED untagged..<target>` with `--cross-major`); before, such an installation updated across any release unchecked. A release tag is `vMAJOR.MINOR.PATCH` with an optional pre-release suffix, the highest one reachable counts, and a tag of another shape, such as `v2x.0.0`, is ignored instead of disabling the check. With no release tag on `origin/main` the update proceeds as before.
- Contract: none
- User action: none. An installation whose clone has no release tag on local `main` stops once at the next release and asks for `--cross-major`, like any installation behind a breaking release.
- Pull request: #234

### analyze-skill-cost reads each plain-scalar grant on its own and fails on a file that is not UTF-8

- Level: patch. Fixes: a plain-scalar `allowed-tools` such as `Bash(p), PowerShell(p)` is read as two grants, where it was one Bash grant that printed false `UNPAIRED_ALLOWED` and `UNGRANTED` lines. `skill_inventory.py tools` and `scan` print `FAILED cannot read <path>: not UTF-8 text` and exit 1 for a file that is binary or not UTF-8, instead of an empty result. `scan` also marks a prose line naming a subagent as an `agent` cue. The delegation and model rules moved to a reference the skill reads only when they apply.
- Contract: none
- User action: none
- Pull request: #201

### audit-ai-config reports unreadable files as findings, accepts block scalars, and prints each finding's line

- Level: minor. Before `1.0.0` this carries what would later be major: the JSON report's findings no longer carry `detail`, which was always empty, and the Markdown findings table gains a `Line` column between `Path` and `Message`. An undecodable skill, agent, or `.codex/config.toml` is now an `ERROR` finding where the audit used to stop with a traceback, a `|` or `>` block scalar in skill or agent frontmatter is read as YAML reads it instead of being an `ERROR` per line, `--root .` names the directory, and nested-file checks skip version-control and dependency directories.
- Contract: none
- User action: none, unless a script reads the JSON `detail` field or the Markdown table by column position; drop the field and count the new column.
- Pull request: #230

### dotnet-format formats .slnx solutions with the .NET SDK, reports a failed fetch, and splits a long file list

- Level: minor. An addition: `dotnet-format` finds `.slnx` solutions as well as `.sln` and formats a `.slnx` with the .NET SDK's `dotnet format`, because the deprecated `dotnet-format` global tool, still used for a `.sln`, cannot open one; `dotnet` is a new optional tool with a floor of 9.0.200, the first SDK that reads `.slnx`, which `python deploy.py check` reports. `dotnet_format_targets.py resolve` stops with a `STOP` naming the SDK when the chosen solution is a `.slnx` and `dotnet` is not installed, prefers a `.sln` over a `.slnx` that owns as many changed files, prints `FETCH_FAILED <reason>` after `REPO_ROOT` when `git fetch origin` fails or does not finish and goes on with origin as last fetched, and reports a missing or stalled git, or a repository git refuses, with git's own reason instead of "not inside a Git repository". `run_dotnet_format.py` formats a file list too long for one Windows command line in several runs within its one time limit, where it used to fail with a raw operating-system error.
- Contract: `deployer/tools.py`
- User action: none for a `.sln`. To format a `.slnx` solution, install the .NET SDK 9.0.200 or newer.
- Pull request: #202

### curate-agent-memory changes only a memory store and is granted no Write

- Level: patch. Fixes: `memory_audit.py delete` and `reindex` refuse a directory that holds no `MEMORY.md` or lies inside a skills directory, printing one `FAILED <dir> ...` line and changing nothing, where they used to act on any directory. A `>` or `|` block-scalar `description` is read whole in the audit report and the rebuilt `MEMORY.md`, instead of as the `>-` header. The skill no longer grants `Write`; it edits with `Edit`, so creating a new destination file asks first.
- Contract: none
- User action: none. A memory directory Claude Code writes holds `MEMORY.md`; one that has lost it needs an empty `MEMORY.md` before `reindex` rebuilds it.
- Pull request: #200

### A review's snapshot from a checkout holds only the changed files, and reviewers fetch the rest

- Level: minor. An addition: on the checkout route `prepare` writes only the changed files and the analyzer settings into the source snapshot and lists every other file of the head with its blob id; each reviewer prompt names two commands of `code-review-core/scripts/review_source.py`, `source-file`, which writes a file from the commit by its blob id, and `source-search`, which runs `git grep` at the commit, and the reviewer guard allows both for the reviewer's own role. `review.snapshot.source` gains the value `checkout-lazy`, which an earlier release refuses to read, and `files_read` counts fetched files. The Copilot CLI host, and a specialists manifest whose routed specialist has a `when` condition, still get the whole snapshot, recorded as `checkout`. A fixture canary's repository now lives in its run until `finalize`.
- Contract: `docs/code-review-operations-contract.md`
- User action: none. The pipeline, the commands, and the reviewer guard deploy together with `code-review-core`.
- Pull request: #227

### deploy.py check also reports Git and Claude Code with their versions

- Level: minor. An addition: `check` lists `Git` (used by every skill; `MISSING` when it is not on `PATH`, which leaves deploying possible) and `Claude Code` (optional) with their versions, so its output is all the bug template asks for besides the model. Neither has a floor, and no other line changes.
- Contract: `deployer/tools.py`
- User action: none.
- Pull request: #231

### --force-item names an agent without its .md and refuses a name the run does not install

- Level: minor. A deployer flag changes: `--force-item` now accepts an agent's own name, such as `code-review-reviewer`, as well as its file name, and a run given a `--force-item` that matches no item it installs stops before anything changes, listing the names it accepts, where it used to ignore the name and deploy without replacing anything.
- Contract: none
- User action: none, unless a script passes `--force-item` a name that the run does not install; correct the name or drop it.
- Pull request: #224

### A batch review lists only the pull requests its watermark can still select

- Level: minor. An addition: `enumerate` prints `LISTED <repository> scan=<full|watermark> pages=<n> pulls=<n> read=<n> candidates=<n>` before each listed repository's `PULL` lines. A repository with a watermark now lists its open pull requests and its closed ones from the most recently updated back to the watermark, instead of its whole history, and only the pull requests that can still be selected are looked up in the archive. Which pull requests are selected does not change: a merged pull request is judged by its merge date, as the contract now states.
- Contract: none
- User action: none. A repository without a watermark lists its whole history once, as before, and `advance` then records one.
- Pull request: #216

### update-pr-tracker compares commits four at a time and prints how many GitHub calls it made

- Level: minor. An addition: `tracker_pipeline.py update` prints `GITHUB_CALLS <n>` after `UPDATED`. It now compares commits four at a time and reads a commit's file tree only when the two comparisons list the same files, so a pull request whose files differ is a change even where GitHub cannot list the repository's tree in full, which was unknown before.
- Contract: none
- User action: none.
- Pull request: #215

### Review records measure the source snapshot and what each reviewer read

- Level: minor. An addition: every new record carries the optional `review.snapshot` (its source, `checkout` or `tarball`, its files and bytes, and the seconds `prepare` spent fetching, materializing, and writing prompts) and, per reviewer, `files_read` and `bytes_read`, null when no guard counted its reads, as for an inline reviewer. An earlier release refuses to read a record that has them. The report gains a **Snapshot** row and a **Files read** column, `run.json` gains `snapshot` and `reads`, and a canary's `finalize` prints `STATS` lines. The reviewer guard now writes each role's read log in the run's `work` folder.
- Contract: `docs/code-review-operations-contract.md`
- User action: none. Records written before this release have neither field and read as before.
- Pull request: #206

## v0.3.0

### review-insights synthesizes findings and flags into targeted recommendations

- Level: minor. Additions: `report` writes a sealed synthesis input, context, and prompt beside `insights.json` and prints `SYNTHESIS_PROMPT`, `SYNTHESIS_RESULT`, `SYNTHESIS recorded`, or `SYNTHESIS skipped` before its other lines; the new `synthesize` command and its `VALID`, `PROBLEM`, `SYNTHESIZED`, `TITLE`, `TARGET`, `CHANGE`, `RATIONALE`, and `SYNTHESIZED_COUNT` lines; the `TOPIC`, `ASSESSMENT`, `ADDRESSED_BY`, and `FINDING` lines after each category recommendation once a synthesis is recorded; `decide-custom`; and, replacing the custom-candidate `ANALYZER` and `EXAMPLE` lines, which `report` no longer prints, one `CUSTOM_CANDIDATES` line with its `PATTERN`, `PATTERN_ASSESSMENT`, and `PATTERN_ADDRESSED_BY` lines; `decide --synthesized TITLE`; and report schema version 7, whose `synthesis` field and `synthesized` recommendations an earlier release refuses to read. The skill now starts one subagent per report to write the synthesis. The new `scope` command prints `CURRENT_REPOSITORY` or `NO_CURRENT_REPOSITORY` and `DEFAULT_SET`, and the skill, given no repository or set inside a configured checkout or its worktree, asks whether to analyze that repository or the default set.
- Contract: `docs/code-review-operations-contract.md`
- User action: none. Run `review-insights` again for a range to get its synthesis; a report written before this release has none until it is regenerated.
- Pull request: #190

### A specialists manifest can have findings name the kind of problem instead of the reviewer

- Level: minor. An addition: the optional top-level `finding_categories` in a specialists manifest, and `fallback_finding_category`, the one of them reviewers use when no other fits. With it, every reviewer gives each finding a `category` from the list, `check` refuses a finding without one, and records keep it; without it, nothing changes. An earlier release refuses a manifest that sets it.
- Contract: `docs/code-review-operations-contract.md`
- User action: none. To group findings by kind in records and in `review-insights`, add `finding_categories` to the manifest; reviews before then keep their reviewer-named categories.
- Pull request: #189

### Each skill declares which runtimes run it, and the runtime canary checks the declaration

- Level: minor. An addition: every `deploy-meta` file declares `runtime_support` (`full`, `partial`, or `none` for Claude Code, Codex, and Copilot CLI), `docs/skills.md` prints it as "Runtime support", and the runtime canary prints `NOT_ATTEMPTED` and `MATRIX` lines and exits 1 when a run disagrees with it. The deployer now refuses a source whose `runtime_support` is malformed; a source without one deploys as before.
- Contract: none
- User action: none. Before relying on a skill from Codex or Copilot CLI, check its row: the code reviews and the user-only skills run there in part.
- Pull request: #184

### review-prs runs canaries of local fixtures, as initial reviews and re-reviews

- Level: minor. Additions: `review-prs --canary --fixture DIR [--re-review --prior RECORD]` and the pipeline's `prepare --canary --fixture DIR [--re-review --prior RECORD]`, whose failure prints `FAILED <directory> <reason>`; the fixture's `pull.json` format; and the `fixture` field of `run.json`. A canary's `finalize` no longer reads the flag store, as a canary was documented never to.
- Contract: `docs/code-review-operations-contract.md`
- User action: none.
- Pull request: #179

### Reviews run inline where subagents cannot start, and specialists manifests no longer need agent-delegation

- Level: minor. Additions: `prepare --inline`, the `next-role` command and its `INLINE`, `INLINE_ROLE`, and `INLINE_DONE` lines, and the optional `review.dispatch` field every new record carries (`subagents`, `copilot-host`, or `inline`), which an earlier release refuses to read. A relaxed rule: a specialists manifest may leave `agent-delegation` out of `required_capabilities`, and then its specialists also run inline. On Copilot CLI, a review by the generic reviewer or by a specialists manifest without `agent-delegation` now runs inline instead of failing.
- Contract: `docs/code-review-operations-contract.md`
- User action: none. To let a specialists manifest run on Copilot CLI, remove `agent-delegation` from its `required_capabilities`; a manifest that keeps it runs only as subagents, as before.
- Pull request: #3

### flag-review-finding refuses a finding the archive does not have and lists a pull request's findings

- Level: minor. An addition and a refusal, carried in the pre-1.0 minor: the new `findings` subcommand prints `FINDING` lines; `add` now fails on a finding ID that is not of the form `F001` or that the named review does not have, which it used to save as a flag that never linked; and a flag store whose `finding_id` is not a string or null fails to load instead of crashing later. `review-insights` counts a finding and its repeats once and reports an outcome only when a later review judged it, so a regenerated report can show smaller counts.
- Contract: `docs/code-review-operations-contract.md`
- User action: none, unless a hand-edited flag store holds a `finding_id` that is neither a string nor null. Then every command that reads it prints `FAILED Flag finding ID is invalid`; set that value to null or the finding ID.
- Pull request: #165

### A review without checkout_path fails when GitHub's tarball is not the commit's exact tree

- Level: patch. A fix: the snapshot could leave out a changed file or hold bytes the commit does not hold.
- Contract: none
- User action: none, unless `prepare` prints `FAILED` saying GitHub's tarball is not the commit's exact tree. Then set `checkout_path` for that repository.
- Pull request: #141

### Item names the file system treats as one are one item, and the deployer's own suffixes are reserved

- Level: minor. A deployment that was accepted can now be refused, carried in the pre-1.0 minor. A source whose shared asset is a case variant of another source's item, such as `guide.md` beside `Guide.md`, is refused as a collision, and a name ending in `.deploying-bak` or containing `.tmp.` is refused for every kind of item.
- Contract: none
- User action: none for the skills this repository ships. Before updating, rename in its source any shared asset of your own whose name differs only in case from an item another source deploys, ends in `.deploying-bak`, or contains `.tmp.`, and deploy; the manifest reader refuses an entry with a reserved name.
- Pull request: #157

### A changed path no reviewer prompt can carry safely is an unavailable source

- Level: patch. The field's format is unchanged; it now also lists a changed path with a control character, a backslash, or an absolute, empty, `.`, or `..` segment, which no reviewer is given, so the review is `INCOMPLETE` instead of failing or reaching a prompt.
- Contract: `docs/code-review-operations-contract.md`
- User action: none.
- Pull request: #142

## v0.2.0

### init-ai-config is removed on update

- Level: minor. Major-level, a removed skill, carried in the pre-1.0 minor.
- Contract: `skills/init-ai-config`
- User action: none. Updating removes it; `audit-ai-config` stays.
- Pull request: #83

### update-coding-agent-skills stops at MAJOR_UPDATE on every v0.1.x installation

- Level: minor. While the major version is 0, a raised minor is the breaking component, so `v0.2.0` stops every `v0.1.x` installation.
- Contract: none
- User action: start `update-coding-agent-skills` again once with `--cross-major` after reading these notes.
- Pull request: #32

### deploy.py verify requires Codex CLI 0.88.0 or newer

- Level: minor. Major-level, a raised tool floor, carried in the pre-1.0 minor. Deployment is unaffected; `verify` reports an older Codex CLI as `OUTDATED` and fails.
- Contract: `deployer/tools.py`
- User action: update Codex CLI to 0.88.0 or newer before running `python deploy.py verify`.
- Pull request: #72

### The hidden skill-core skill is added

- Level: minor. A new skill, deployed as a dependency of the skills whose scripts share its modules; it is not offered for selection.
- Contract: `skills/skill-core`
- User action: none.
- Pull request: #104

### The code-review formats are stated as tables, and the unreferenced schemas are removed

- Level: patch. No script read the removed schemas, and the tables state the formats the code already enforced.
- Contract: `docs/code-review-operations-contract.md`, `skills/code-review-core/references/flag-record.schema.json`, `skills/code-review-core/references/legacy-review-index.schema.json`, `skills/code-review-core/references/review-config.schema.json`, `skills/code-review-core/references/review-record.schema.json`, `skills/code-review-core/references/review-request.schema.json`, `skills/code-review-core/references/reviewer-manifest.schema.json`
- User action: none.
- Pull request: #60

### The code-review formats gain optional fields

- Level: minor. Additive: a review record's `schema_version` stays 1, and records without a finding ledger stay valid and read as having no history. A reviewer's output no longer has to carry `prior_dispositions` when the request has no prior findings and may mark a finding as a `repeats` of another; the configuration gains the optional `model_names`; a re-review request's carried findings gain `flags`; trusted reviewer files are hashed as their exact committed bytes.
- Contract: `skills/code-review-core/references/review-adapter.schema.json`, `docs/code-review-operations-contract.md`
- User action: none. A `review-insights` report written before shows `-` in its new columns until it is regenerated.
- Pull request: #47, #67, #80, #82
