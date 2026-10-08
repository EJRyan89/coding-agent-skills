# Upgrade notes

What each release asks of a user who updates, one entry per contract change since the previous tag. The GitHub release notes open with the release's entries; [Releasing](releasing.md#versioning) defines the contracts and the levels.

Validation (`tests/run_validation.py`) compares the contract files at the last tag reachable from `origin/main` with the working tree and fails while a changed contract item is named by no entry added since that tag. New entries go under `## Unreleased`; the person tagging renames that heading to the version and opens a new empty `## Unreleased` above it.

An entry is a `###` heading saying what changed, followed by four fields:

- `Level:` starts with a level word from the Versioning section: `patch`, `minor`, or `major`. Before `1.0.0`, a change that would be major is carried in a minor, and the entry says so.
- `Contract:` the contract items the entry covers, each in backticks as the validation failure names it, or `none` for a demand no contract file records.
- `User action:` what the user must do after updating, or `none`.
- `Pull request:` the pull request as `#N`. Until it is open, the issue it closes stands in.

## Unreleased

### review-insights synthesizes findings and flags into targeted recommendations

- Level: minor. Additions: `report` writes a sealed synthesis input, context, and prompt beside `insights.json` and prints `SYNTHESIS_PROMPT`, `SYNTHESIS_RESULT`, `SYNTHESIS recorded`, or `SYNTHESIS skipped` before its other lines; the new `synthesize` command and its `VALID`, `PROBLEM`, `SYNTHESIZED`, `TITLE`, `TARGET`, `CHANGE`, `RATIONALE`, and `SYNTHESIZED_COUNT` lines; the `TOPIC`, `ASSESSMENT`, `ADDRESSED_BY`, and `FINDING` lines after each category recommendation once a synthesis is recorded; `decide-custom`; and, replacing the custom-candidate `ANALYZER` and `EXAMPLE` lines, which `report` no longer prints, one `CUSTOM_CANDIDATES` line with its `PATTERN`, `PATTERN_ASSESSMENT`, and `PATTERN_ADDRESSED_BY` lines; `decide --synthesized TITLE`; and report schema version 7, whose `synthesis` field and `synthesized` recommendations an earlier release refuses to read. The skill now starts one subagent per report to write the synthesis. The new `scope` command prints `CURRENT_REPOSITORY` or `NO_CURRENT_REPOSITORY` and `DEFAULT_SET`, and the skill, given no repository or set inside a configured checkout or its worktree, asks whether to analyze that repository or the default set.
- Contract: `docs/code-review-operations-contract.md`
- User action: none. Run `review-insights` again for a range to get its synthesis; a report written before this release has none until it is regenerated.
- Pull request: #182

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
