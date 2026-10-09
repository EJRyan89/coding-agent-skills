# Upgrade notes

What each release asks of a user who updates, one entry per contract change since the previous tag. The GitHub release notes open with the release's entries; [Releasing](releasing.md#versioning) defines the contracts and the levels.

Validation (`tests/run_validation.py`) compares the contract files at the last tag reachable from `origin/main` with the working tree and fails while a changed contract item is named by no entry added since that tag. New entries go under `## Unreleased`; the person tagging renames that heading to the version and opens a new empty `## Unreleased` above it.

An entry is a `###` heading saying what changed, followed by four fields:

- `Level:` starts with a level word from the Versioning section: `patch`, `minor`, or `major`. Before `1.0.0`, a change that would be major is carried in a minor, and the entry says so.
- `Contract:` the contract items the entry covers, each in backticks as the validation failure names it, or `none` for a demand no contract file records.
- `User action:` what the user must do after updating, or `none`.
- `Pull request:` the pull request as `#N`. Until it is open, the issue it closes stands in.

## Unreleased

### The reviewer guard refuses a self-check whose script or run ends in a backslash

- Level: patch. A fix: in Claude Code, the reviewer guard now holds a `code-review-reviewer`'s self-check command to the rule its source commands already followed, so no quoted script or run may end in a backslash, which would escape its closing quote. The self-check the pipeline writes is allowed as before.
- Contract: `docs/code-review-operations-contract.md`
- User action: none
- Pull request: #262

### A status override may not name a section the tracker renders, such as `My PRs`

- Level: minor. A fix that would be major from `1.0.0`: `dashboard.status_overrides` now refuses every section name `update-pr-tracker` renders, trimmed and in any case, which adds `my prs` to the refused states; `to review`, `awaiting response`, `drafts`, `my pull requests`, `missing`, `current`, and `stale` stay refused. An override named `My PRs` was accepted before, and the dashboard then showed that section twice and dropped every authored pull request not overridden; a configuration holding one now fails to load.
- Contract: `docs/code-review-operations-contract.md`
- User action: none, unless an override's status is `My PRs` in any case. Then rename or remove it in the code-review configuration file by hand, since `tracker_pipeline.py override` validates the file before changing it.
- Pull request: #292

### The review state refuses a merged_since that is not a date, and enumerate fails only that repository

- Level: patch. A fix: `validate_state` now refuses a repository's `merged_since` whose first ten characters are not a `YYYY-MM-DD` calendar date, as the review state table already said, so such a `state.json` fails with one `FAILED` line naming the repository instead of a traceback from `enumerate`. A watermark that still cannot be read when `enumerate` lists its repository ends in `REPOSITORY_FAILED` for that repository, and the command exits 1 after the batch, as for any other failed repository. Every watermark `advance` writes is such a date, so a state the scripts wrote is read as before.
- Contract: `docs/code-review-operations-contract.md`
- User action: none. A `merged_since` edited by hand to something else is now refused; correct it to a `YYYY-MM-DD` date, or remove it to start that repository again from today.
- Pull request: #295

### review-insights prints categories and analyzer names screened, and decides them as printed

- Level: patch. A fix: a `RECOMMENDATION` line's category and an `ANALYZER` line's tool and rule are printed on one line, with whitespace flattened and `?` for a double quote, backtick, `$`, backslash, or control character, so a value from a record never breaks the one-fact-per-line output or reaches a shell unquoted. `decide` takes the subject as printed or as recorded, and the skill passes every value in double quotes. A synthesized title is now refused for a control character too. Records and reports are unchanged.
- Contract: none
- User action: none
- Pull request: #293

### update-pr-tracker reports a GitHub call that failed instead of marking reviews stale

- Level: minor. A new status line and a fix: when a GitHub call a comparison needs fails for any reason but a missing commit, `tracker_pipeline.py update` no longer reads it as unknown evidence, which marked every affected review `stale`, offered it for re-review, and exited 0. Network and timeout failures now stop the run at once with `FAILED <reason>`, as authentication and rate-limit failures already did. Any other failure, such as HTTP 403, an SSO-withheld result, or a server error, prints the new line `PULL_FAILED <owner/repo#N> <error>` for each pull request whose comparison failed, then `FAILED <n> of <m> pull requests could not be compared; the dashboard keeps its previous rows`, and exits 1 without writing the dashboard or printing a `CANDIDATE`. `collect` treats the same failures alike while placing the user's review commit: a network or timeout failure stops the collection, and any other but a missing commit fails its repository with `REPOSITORY_FAILED`, where before it showed the Findings cell without what moved since the user's review. A missing commit or branch is still unknown, as before.
- Contract: none
- User action: none. An update that now fails names each pull request GitHub could not compare; rerun it once GitHub answers, or leave out a pull request that fails on every run with `--remove`.
- Pull request: #265

## v0.4.0

### A specialist condition may declare the snapshot paths it reads, so its review keeps the lazy snapshot

- Level: minor. Additive: a condition in a specialists manifest may list `reads`, glob patterns of the snapshot paths its script opens besides the changed files, matched as `snapshot_exclude` is. When every condition a review runs declares them, `prepare` keeps the checkout route's snapshot lazy, recorded as `checkout-lazy`, and writes the paths they match beside the changed files and the analyzer settings; the script sees no other path. A condition without `reads` keeps the whole snapshot, recorded as `checkout`, as before. A path `snapshot_exclude` matches is never written, declared or not. `validate-reviewer` runs each condition on the snapshot a review gives it, so its `CONDITION` lines match the review's. A previous release refuses a manifest that has `reads`.
- Contract: `docs/code-review-operations-contract.md`
- User action: none. To use it, add `reads` to each condition of a repository's manifest and check the conditions with `validate-reviewer`; the "Specialist reviewers" section of `docs/code-review-operations.md` gives an example.
- Pull request: #251

### A review skill the base predates is read from the default branch's tip, or the generic reviewer runs, and the record names the reviewer's source

- Level: minor. Additive: a pull request whose base commit predates the repository's configured review `skill`, with no `trusted_ref` configured, no longer fails `prepare` with `does not exist at`. The skill is read from the default branch's tip as origin reports it; when the tip lacks it too, or is the pull request's head, the suite's generic reviewer reviews that pull request. `prepare` prints a `NOTE` for either fallback. Each record's `review.adapter` gains `source`: `generic`, `trusted-ref`, `base`, `default-branch`, or `generic-fallback`, which the report shows as **Reviewer source**. A reviewer with a `trusted_ref` or a `manifest_path`, and a pull request whose base has the skill, behave as before. A previous release refuses a record that has `source`.
- Contract: `docs/code-review-operations-contract.md`
- User action: none
- Pull request: #252

### update-pr-tracker sets and clears status overrides with a validated command

- Level: minor. An addition: `tracker_pipeline.py override` lists the configuration's `dashboard.status_overrides` (`OVERRIDE <owner/repo#N> <status>`, then `OVERRIDES <n>`), and with `--set owner/repo#N=STATUS` or `--clear owner/repo#N` changes them in one validated write, printing `SET`, `CLEARED`, and `WROTE <configuration>`. It changes nothing else in the file and fills in no default. It refuses a computed state, a malformed key, or clearing an override that is not set, and clears an override whose pull request has closed. `update-pr-tracker` now tells the agent to use it when you ask for a pull request to be shown under a status of your own.
- Contract: none
- User action: none
- Pull request: #250

### A repository's snapshot_exclude leaves files out of its review snapshot, and a changed one still makes the review INCOMPLETE

- Level: minor. Additive: a repository's entry in the code-review configuration may list `snapshot_exclude`, glob patterns of files to leave out of its source snapshot, such as resources, designer files, and generated reports; without it nothing changes. A matching file is recorded in the snapshot's manifest under `excluded_paths` with the new reason `configured`, on the lazy, whole, and tarball routes alike: it is never written, `source-file` prints `EXCLUDED <path> configured` for it, and `source-search` leaves it out. A changed file it matches is an unavailable source, so that review is `INCOMPLETE`. `validate-reviewer` fails on a pattern that matches a file the reviewer declares, and its `SNAPSHOT` line counts `configured` exclusions. Each record's `review.snapshot` gains `excluded`, the head's paths left out by reason, which the report's Snapshot row and a canary's `STATS` snapshot line show. A previous release refuses a record that has it.
- Contract: `docs/code-review-operations-contract.md`
- User action: none. To use it, add `snapshot_exclude` to a repository's entry and run `validate-reviewer` for a repository reviewer; "Configuration" in `docs/code-review-operations.md` gives an example.
- Pull request: #209

### The Copilot CLI host re-reads the source snapshot only as far as it changed since prepare

- Level: patch. Faster with the same guarantee: for a run the Copilot CLI host reviews, `prepare` stamps the source snapshot in `run.json` (one SHA-256 over the manifest's bytes and each written file's path, size, and modification time), and the host, before it starts Copilot, checks the structure and the stamp and re-hashes only the files a time cannot vouch for and any fetched file. A stamp that no longer matches, or a run prepared before this release, has every file re-hashed as before. At 25,000 files the host's check went from minutes to about a second, and `prepare` spends about a second taking the stamp.
- Contract: none
- User action: none
- Pull request: #241

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
