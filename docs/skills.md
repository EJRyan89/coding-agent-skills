# Skills

Every skill this repository deploys: how to start it, what its arguments mean, and what it needs installed.

## Starting a skill

In Claude Code, type `/<skill>` followed by its arguments, or ask for the skill by name. A skill started by "you or the agent" also runs when a request matches its description. One started only by "you" changes things you should decide on, such as deleting branches or redeploying, so it runs only when you type it.

Codex and GitHub Copilot CLI load each skill through a generated adapter under `~/.agents/skills` that carries the skill's description, so a skill started by "you or the agent" also runs there when a request matches it. Start one explicitly with `$<skill>` in Codex or `/<skill>` in Copilot, followed by its arguments. A skill started only by "you" runs only when you start it that way.

When a required argument is missing, a skill asks for it rather than guessing.

## Reading the argument syntax

| Notation | Meaning |
|---|---|
| `[--force]` | Optional |
| `a \| b` | One of the alternatives |
| `owner/repo ...` | Repeatable |
| `ORG`, `<SkillName>` | A value you supply |
| `owner/repo#number` | One pull request, such as `octo-org/widgets#42` |

## Summary

**Installed** says how `python deploy.py` selects the skill: `--all` installs every skill marked "by default"; an opt-in skill needs `--include <name>` or a menu choice; a bundle installs its skills together. **Needs** lists what the skill runs beyond the deployment prerequisites in [Installation](installation.md#requirements), and any setting it reads from `python deploy.py configure`. `python deploy.py check` reports which of those tools are installed.

<!-- generated:summary -->
| Skill | Started by | Installed | Needs |
|---|---|---|---|
| [`analyze-skill-cost`](#analyze-skill-cost) | You or the agent | By default | Nothing extra |
| [`audit-ai-config`](#audit-ai-config) | You or the agent | By default | Nothing extra |
| [`curate-agent-memory`](#curate-agent-memory) | You or the agent | By default | Nothing extra |
| [`dotnet-format`](#dotnet-format) | You or the agent | Opt-in | `dotnet-format`, `gh` (optional) |
| [`flag-review-finding`](#flag-review-finding) | You or the agent | `code-review-operations` bundle | Nothing extra |
| [`github-activity-report`](#github-activity-report) | You or the agent | By default | `gh` |
| [`repo-cleanup`](#repo-cleanup) | You | By default | `gh`, the `REPOS_ROOT` setting |
| [`review-insights`](#review-insights) | You or the agent | `code-review-operations` bundle | Nothing extra |
| [`review-prs`](#review-prs) | You or the agent | `code-review-operations` bundle | `copilot` (optional), `gh` |
| [`update-coding-agent-skills`](#update-coding-agent-skills) | You | By default | Nothing extra |
| [`update-pr-tracker`](#update-pr-tracker) | You or the agent | `code-review-operations` bundle | `gh` |
<!-- /generated:summary -->

The summary and the first block of each section below are generated from each skill's `SKILL.md` frontmatter and `deploy-meta/<name>.json`. Change those, then run `python tools/skill_reference.py --write`; validation fails while this file is stale. The explanation after each generated block is written by hand.

## Runtime support

Which runtimes run each skill. **Full** means every step runs there as it does in Claude Code. **Partial** means the skill runs but loses what the note under the table names, because the runtime lacks a capability the skill uses: `agent-delegation` (starting a subagent), `workflow` (Claude Code's Workflow tool), or `user-only-start` (starting a user-only skill from a headless `copilot -p` session as well as an interactive one). **None** means the runtime does not run it. Each skill declares its support in `deploy-meta/<name>.json`; validation holds the declaration to the capabilities its frontmatter shows it uses, and the [runtime canary](../tools/runtime_canary.py) fails when a runtime ran a skill otherwise than the table says.

<!-- generated:runtime-support -->
| Skill | Claude Code | Codex CLI | Copilot CLI |
|---|---|---|---|
| [`analyze-skill-cost`](#analyze-skill-cost) | Full | Full | Full |
| [`audit-ai-config`](#audit-ai-config) | Full | Full | Full |
| [`curate-agent-memory`](#curate-agent-memory) | Full | Full | Full |
| [`dotnet-format`](#dotnet-format) | Full | Full | Full |
| [`flag-review-finding`](#flag-review-finding) | Full | Full | Full |
| [`github-activity-report`](#github-activity-report) | Full | Full | Full |
| [`repo-cleanup`](#repo-cleanup) | Full | Full | Partial |
| [`review-insights`](#review-insights) | Full | Full | Full |
| [`review-prs`](#review-prs) | Full | Partial | Partial |
| [`update-coding-agent-skills`](#update-coding-agent-skills) | Full | Full | Partial |
| [`update-pr-tracker`](#update-pr-tracker) | Full | Partial | Partial |

- `repo-cleanup` on Copilot CLI: partial, lacking `user-only-start`. A headless copilot -p session cannot start it; start it from an interactive session.
- `review-prs` on Codex CLI: partial, lacking `workflow`. Reviewers start as native subagents, so a reviewer effort setting has no effect.
- `review-prs` on Copilot CLI: partial, lacking `agent-delegation` and `workflow`. The generic reviewer and delegation-free specialists run inline; a specialists manifest that keeps agent-delegation fails.
- `update-coding-agent-skills` on Copilot CLI: partial, lacking `user-only-start`. A headless copilot -p session cannot start it; start it from an interactive session.
- `update-pr-tracker` on Codex CLI: partial, lacking `workflow`. The reviews it starts run through review-prs, which loses reviewer effort settings here.
- `update-pr-tracker` on Copilot CLI: partial, lacking `agent-delegation` and `workflow`. The reviews it starts run through review-prs, which here runs the generic reviewer and delegation-free specialists inline and fails a specialists manifest that keeps agent-delegation.
<!-- /generated:runtime-support -->

## `analyze-skill-cost`

<!-- generated:analyze-skill-cost -->
Audit an agent skill for cost and efficiency: token footprint, tool calls, deterministic work left to prose, subagent overhead, runtime adapters, model, and allowed-tools. Read-only findings report. Use it when asked what a skill costs, why it is slow or expensive, or to check one before shipping it.

```text
/analyze-skill-cost <SkillName>
```

Started by you or the agent. Installed by default.
<!-- /generated:analyze-skill-cost -->

`<SkillName>` is a skill's name, not a path. The skill looks for it under `~/.claude/skills/` and, when you start it inside a Git repository, in that repository's `.agents/skills/` and `.claude/skills/`. In a checkout of this repository it also looks in `skills/` for a skill with a `deploy-meta/<name>.json`, and audits that source rather than the deployed copy under `~/.claude/skills/`, so a change is audited before it is deployed. If no skill or more than one has that name, it lists what it found and stops. It takes no flags.

It judges a skill's text by content, not size: duplicated code blocks, prose that restates another step, and documentation the agent never acts on are findings at any length. Sizes and token estimates are reported as data, never as a threshold. The only structure limits it checks are the ones in Anthropic's [skill authoring best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices): a body under 500 lines, a table of contents in a reference file over 100 lines, and references linked directly from `SKILL.md`. Each is a suggestion. It never suggests a cheaper `model` for a skill usually followed in the same turn by work that starts subagents, because those subagents inherit the skill's model: a code review started in the same turn as a Haiku-pinned skill ran every reviewer on Haiku, and they missed a must-fix finding.

```text
/analyze-skill-cost repo-cleanup
```

## `audit-ai-config`

<!-- generated:audit-ai-config -->
Read-only assessment of a repository's AI agent configuration for Claude Code, Codex, and Copilot that reports findings and never writes files. Use it when asked to audit or check a repository's agent instructions or setup, not to create or change them.

```text
/audit-ai-config
```

Started by you or the agent. Installed by default. Takes no arguments.
<!-- /generated:audit-ai-config -->

It audits the Git repository you start it in, and asks which repository to audit when you are not in one. It reports the repository's authority and scope, every error and warning, and every documented limitation that applies, and summarizes the informational inventory as a count per check; ask for the full inventory when you want each file it found. Ask for JSON when you want to process the findings rather than read them. It parses files statically: it runs nothing from the repository, makes no network requests, and writes nothing.

It never fixes what it finds; findings are fixed by hand. This source used to ship `init-ai-config`, which generated a cross-runtime layout from `CLAUDE.md`. That skill was retired in `v0.2.0`, because every supported runtime reads `CLAUDE.md` directly or through a thin `AGENTS.md` redirect. The audit still checks the generated layout in repositories the generator configured before, and its `references/generated-layout.md` describes that layout.

## `curate-agent-memory`

<!-- generated:curate-agent-memory -->
Audit and clean up Claude Code auto-memory for a project: find stale, duplicated, or misplaced memories, move durable rules to their proper home, and apply only the changes the user approves. Use it when asked to review, tidy, or prune a project's memories.

```text
/curate-agent-memory [repository path] [--memory-dir DIR]
```

Started by you or the agent. Installed by default.
<!-- /generated:curate-agent-memory -->

- `repository path`: the repository whose memory to curate. It defaults to the current Git repository's root. Every worktree of a repository shares one memory store, so any of them gives the same result.
- `--memory-dir DIR`: the memory directory to use. Without it, the skill finds the directory Claude Code uses, from settings first, and lists candidates for you to choose from when it finds none.

Either way it shows the directory and asks you to confirm before reading further, and it changes nothing you have not approved.

It looks for the directory in Claude Code's documented order: `autoMemoryDirectory` from managed, local, project, then user settings; then `CLAUDE_CODE_PROJECT_DIR_NAME` under the configuration directory; then a directory derived from the repository's main worktree. It compares each memory with the repository's `CLAUDE.md`, `CLAUDE.local.md`, `.claude/rules/`, `AGENTS.md`, README, contributor and `docs/` guidance, Copilot instructions, and skill files, plus your own `CLAUDE.md` and `rules/`. When it rebuilds `MEMORY.md`, it writes one `- [Title](file.md) — hook` line per memory file and never deletes or changes a memory: entries keep their order, title, and surrounding headings, entries for missing files or repeated links are dropped, and unindexed memories are appended. The hook is the memory's `description`, else the entry's existing hook, else the body's first line. It applies the approved changes in order: destination files first, then it deletes the memories they cover through its own script, then it rebuilds `MEMORY.md`. The script deletes only `.md` files named directly inside the memory directory; it refuses `MEMORY.md`, any path with a separator, drive, or `..`, a directory, and a symbolic link or junction, and when it refuses any name it deletes nothing.

```text
/curate-agent-memory
/curate-agent-memory C:\GitHub\widgets
```

## `dotnet-format`

<!-- generated:dotnet-format -->
Run dotnet format (whitespace + style + analyzers) and region layout checks on C# files changed on the current branch and optionally auto-fix violations. Use it when asked to format or style-check C# changes before a commit or pull request.

```text
/dotnet-format
```

Started by you or the agent. Opt-in: deploy it with `--include dotnet-format`. Needs `dotnet-format` and `gh` (optional). Takes no arguments.
<!-- /generated:dotnet-format -->

Start it inside the repository, on the branch to check. It compares with the pull request's base branch, found with `gh`, else the remote's default branch (`origin/HEAD`), else `origin/main`, else `origin/master`, and checks every committed, uncommitted, and untracked `.cs` file that differs. It uses the nearest solution to the current directory that owns a changed file, else the repository's solution whose projects own the most, and stops rather than pick one that owns none. Files outside that solution, such as scripts or `.cs` files no project compiles, get only the layout checks. It reports violations and asks before fixing them, and asks before adding any missing `.editorconfig` settings. It runs the `dotnet-format` global tool (`dotnet tool install -g dotnet-format`), not the SDK's built-in `dotnet format`, which can fail with `TypeInitializationException` against .NET Framework solutions on newer SDKs, and stops a formatter run after 570 seconds.

It checks these layout rules:

| Rule | Enforced by |
|---|---|
| `else if` on one line | Roslynator `RCS0041` |
| Exactly one blank line between members | `RCS0010` and `RCS0012` (at least one), `RCS0063` (no more than one) |
| Exactly one blank line after `#region` and before `#endregion` | `RCS0002`, `RCS0005`, and `RCS0063` |
| No blank line between a type's `{` and its first `#region` | `RCS0063` |
| Newline at end of file | `insert_final_newline = true`, applied by the formatter's whitespace pass |
| `#endregion` in the brace scope of its `#region`; no `#endregion` description; exactly one blank line between `#endregion` and a following `#region`; no blank line between `#endregion` and a following `}` | `csharp_layout.py check` |

The Roslynator rules need the `.editorconfig` settings the skill proposes and the `Roslynator.Formatting.Analyzers` package, which it never adds itself.

## `flag-review-finding`

<!-- generated:flag-review-finding -->
Add, list, or resolve a structured code-review improvement flag. Use it when the user says a review finding was wrong, noisy, or missed something and wants that recorded.

```text
/flag-review-finding add CATEGORY BODY [--repository owner/repo --pull N [--review-version V --finding ID]] | list | findings --repository owner/repo --pull N | resolve ID RESOLUTION
```

Started by you or the agent. Installed with the `code-review-operations` bundle.
<!-- /generated:flag-review-finding -->

The first word chooses what to do:

- `add CATEGORY BODY` records a flag. `CATEGORY` is a short label of your choosing and `BODY` is the rationale; quote either when it has spaces. To tie the flag to a review finding, add `--repository owner/repo --pull N`, then `--review-version V --finding ID`. Both come from the finding's label in the review report: `v2 F003` is `--review-version 2 --finding F003`. A re-review report shows a finding carried from an earlier review under that review's label, so the report's **Mode** row is not the version to use. Finding IDs restart at `F001` in every review, so `--finding` needs `--review-version`. The flag is refused unless `--finding` has that form and that review of the pull request is in the configured archive with that finding, because a flag on a finding that does not exist would never reach a reviewer. Only a flag that names a finding can be resolved by `review-insights`. A flag on a finding doesn't close it or change the verdict: the next re-review of that pull request gives the flag's category and rationale to the reviewer, which weighs it and marks the finding `superseded`, citing the flag, when the flag holds. "Finding ledger" in [code-review-operations.md](code-review-operations.md#finding-ledger) has the details.
- `list` shows the open flags.
- `findings --repository owner/repo --pull N` shows the pull request's open and unverified findings, each under the label its review report shows, so the agent can find the one you mean without reading the report.
- `resolve ID RESOLUTION` closes the flag `ID`, such as `RF-000004`, with a non-empty explanation.

```text
/flag-review-finding add noise "Asked for a null check the caller already guarantees" --repository octo-org/widgets --pull 42 --review-version 2 --finding F003
/flag-review-finding list
/flag-review-finding findings --repository octo-org/widgets --pull 42
/flag-review-finding resolve RF-000004 "The reviewer prompt now checks the caller's contract"
```

## `github-activity-report`

<!-- generated:github-activity-report -->
Report one user's GitHub contributions in one organization month by month: pull requests authored and merged, commits, pull requests reviewed, and reviews submitted. Use it when asked what someone contributed or reviewed over recent months.

```text
/github-activity-report ORG USER [--months N]
```

Started by you or the agent. Installed by default. Needs `gh`.
<!-- /generated:github-activity-report -->

- `ORG`: the GitHub organization. Required.
- `USER`: the GitHub login to report on. Required.
- `--months N`: how many calendar months to cover, ending with the current, partial month. The default is 12 and the maximum 36.

A 12-month report takes a few minutes, because searches are spaced to stay under GitHub's rate limits. `gh` must be signed in with a token that can read the organization's repositories, including SSO authorization where the organization enforces it.

```text
/github-activity-report octo-org octocat --months 6
```

## `repo-cleanup`

<!-- generated:repo-cleanup -->
Clean up GitHub repos: switch to default branch, prune gone branches, remove stale worktrees, and fast-forward remaining branches. Sweeps all repos under `REPOS_ROOT` or targets a single repo by name/path.

```text
/repo-cleanup [<repo-name-or-path>]
```

Started by you. Installed by default. Needs `gh` and the `REPOS_ROOT` setting.
<!-- /generated:repo-cleanup -->

- With no argument, it sweeps every repository under the `REPOS_ROOT` you set with `python deploy.py configure`.
- `<repo-name-or-path>` cleans one repository: a name under `REPOS_ROOT`, or an absolute path to a directory that contains `.git`.

It performs only the actions it can prove safe, such as removing a branch whose pull request merged, and asks you about anything else, such as local branches with no remote or unmerged work. Its plan files go in a new temporary directory, never in a skill directory.

For each repository it switches to the default branch, runs `git fetch --all --prune`, prunes worktree records, fast-forwards the default branch, classifies branches and worktrees, and performs the safe actions, re-checking every recorded branch tip first. Its summary of a repository covers the default branch, deleted branches, removed worktrees, fast-forwarded and diverged branches, branches not fast-forwarded because their worktree has changes, skipped dirty worktrees, branches kept because their pull request status was unverified or unmatched, protected release worktrees, and any unmerged, local-only, preserved, or gone-with-open-PR branches and removed empty directories.

The commands enforce these rules, whatever the agent is asked:

- `release/*` branches are never deleted. A worktree is never removed when its branch matches `release/*` or its path contains a `/release/` segment.
- A branch is deleted automatically only when its remote branch is gone and its pull request status is `MERGED`, `CLOSED`, or `NONE`, and only when the default branch, local or fetched, contains its tip. `git branch -d` alone would judge it against `HEAD`, which a skipped or failed switch leaves elsewhere, so such a branch is deleted with `git branch -d`, or with `git branch -D` when `-d` refuses only because `HEAD` lacks it. A squash- or rebase-merged branch the default branch does not contain is deleted with `git branch -D` when its status is `MERGED` and its tip has not moved. A merged or closed pull request counts only when its head is in the same repository, there are no newer upstream commits, and it ended at the branch's exact local tip. A merged pull request also counts when the local tip is one of its commits and every later commit is a merge whose non-first parents are reachable from the fetched default branch, as GitHub's "Update branch" makes; a later commit with any other changes leaves the branch `UNMATCHED`. Branches whose status is `OPEN`, `UNMATCHED`, or `UNKNOWN` (any failed or possibly truncated `gh` query) are kept and reported, never deleted or offered for deletion.
- Otherwise `git branch -D` runs only after you confirm, and only for a branch reported `UNMERGED` whose tip has not moved. A `CLOSED` or `NONE` branch the default branch does not contain is always reported `UNMERGED`, never forced.
- Worktrees with uncommitted or untracked changes are never removed, only reported. Worktrees are removed only by `git worktree remove`; when Git refuses, the worktree and its branch are preserved and reported.
- Empty directories are removed only with `rmdir`, only for parents of worktrees removed in this run, and never at or above `REPOS_ROOT/Worktrees` or the repository root.
- Fast-forwards are fast-forward only, and never move a branch checked out in a worktree with uncommitted or untracked changes, the main worktree included; the summary lists each such branch under "Fast-forward skipped". Diverged branches are reported, never rebased, merged, or reset. The main worktree's files are never stashed, reset, or cleaned.
- An unexpected failure in one repository is reported as that repository's `ERROR`, and the sweep still reports every other repository.
- When a repository's fetch fails, nothing after the fetch runs for it. Earlier steps may already have switched its checkout, and a multi-remote fetch may have updated some remote-tracking refs before failing.

In Copilot CLI, start it from an interactive session: a headless `copilot -p` session cannot start a user-only skill ([Headless sessions](copilot-support.md#headless-sessions)). It runs fully in Claude Code and Codex.

```text
/repo-cleanup
/repo-cleanup widgets
```

## `review-insights`

<!-- generated:review-insights -->
Analyze structured code-review findings for an explicit date range and repository set. Use it when asked which review findings were accepted or rejected, or what reviews keep flagging.

```text
/review-insights START_DATE END_DATE [owner/repo ... | --repository-set NAME]
```

Started by you or the agent. Installed with the `code-review-operations` bundle.
<!-- /generated:review-insights -->

- `START_DATE END_DATE`: the inclusive range, as `YYYY-MM-DD`. Both are required; the skill never guesses a range.
- `owner/repo ...` or `--repository-set NAME`: whose reviews to analyze. With neither, it uses the configured `review-insights` set. Repository sets are defined in the [code-review configuration](code-review-operations.md#configuration).

It then asks you to accept, reject, or defer each recommendation. Accepting one resolves the flags linked to it.

```text
/review-insights 2026-09-01 2026-09-30 --repository-set team
```

## `review-prs`

<!-- generated:review-prs -->
Review eligible pull requests in explicitly configured repositories, review or re-review explicit pull requests, or run isolated initial-review canaries, and produce validated structured reports. Use it when asked to review or re-review pull requests.

```text
/review-prs [owner/repo ... | --repository-set NAME | --pull owner/repo#number ... --re-review owner/repo#number ... --scope auto|full|incremental] [--force] | --canary owner/repo#number ...
```

Started by you or the agent. Installed with the `code-review-operations` bundle. Needs `copilot` (optional) and `gh`.
<!-- /generated:review-prs -->

It runs in one of three modes, chosen by its arguments:

- **Batch**: `owner/repo ...`, `--repository-set NAME`, or nothing for the configured set. It reviews each repository's open, non-draft pull requests and those merged since its last batch, skipping heads already reviewed.
- **Explicit**: `--pull owner/repo#number` for a first review and `--re-review owner/repo#number` for one whose head changed. Repeat either, or both, in one run, naming each pull request once. They cannot be combined with batch selectors. A re-review needs an earlier review, gives each of its findings a disposition, and records the next review version without overwriting the last.
- **Canary**: `--canary owner/repo#number` runs a first review in isolation. It writes the result under a new temporary directory and reads or writes nothing configured. Repeat it to check a configuration change against several pull requests in one run, such as one of each kind a reviewer manifest routes differently; each gets its own temporary directory. It takes no other selector and no `--force`.

`--scope` sets how much every `--re-review` in the run reviews again: `full` reviews the whole pull request; `incremental` reviews in full only the files whose changes differ from the last review, and only records dispositions for the earlier findings in the rest; `auto` chooses between them from how much changed. Without it, the skill asks once; it never picks one itself.

Each reviewer role runs as a subagent. Where the session cannot start one (Copilot CLI, or a review started from inside a subagent), or when you ask for an inline review, the session works each role itself, one at a time; [Inline reviews](code-review-operations.md#inline-reviews) says what that costs in isolation.

It runs fully only in Claude Code. In Codex, reviewers start as native subagents rather than through the Workflow tool, so a reviewer `effort` setting, in the configuration or a specialists manifest, has no effect. In Copilot CLI, the generic reviewer and the specialists of a manifest that leaves `agent-delegation` out of `required_capabilities` run inline, and a repository entrypoint reviewer runs on the bounded Copilot host; a specialists manifest that lists `agent-delegation` fails there.

`--force` reviews heads that already have a review. The skill never posts to GitHub, and keeps its working files, such as a batch's list of pull requests, in new temporary directories, never in a skill directory. [Code-review operations](code-review-operations.md) covers the configuration and the records it writes.

```text
/review-prs --repository-set team
/review-prs --pull octo-org/widgets#42 --re-review octo-org/widgets#37 --scope auto
/review-prs --canary octo-org/widgets#42 --canary octo-org/widgets#51
```

## `update-coding-agent-skills`

<!-- generated:update-coding-agent-skills -->
Fast-forward the coding-agent-skills clone these skills were deployed from to origin/main, then redeploy every skill with deploy.py --all. Stops before a release that raises the major version until the user passes --cross-major.

```text
/update-coding-agent-skills [--cross-major]
```

Started by you. Installed by default.
<!-- /generated:update-coding-agent-skills -->

It updates the clone these skills were deployed from: it fast-forwards `main` to `origin/main`, then runs `python deploy.py --all`. It stops without changing anything when tracked files have uncommitted changes, the fetch fails, or local `main` has commits that `origin/main` lacks. It never stashes, resets, or forces a deployment; those decisions stay yours.

Without arguments it also stops, reporting `MAJOR_UPDATE <current>..<target>`, when the nearest release tag on `origin/main` raises the major version above the one on local `main` (or the minor version, while the major is 0), because such a release may ask you to act, as [Versioning](releasing.md#versioning) explains; read the release notes for `<target>`, then rerun it with the flag to apply the update. A clone with no release tag on either side is updated without the check.

- `--cross-major`: apply an update across such a release boundary. The output names the crossing as `CROSSED <current>..<target>`.

In Copilot CLI, start it from an interactive session: a headless `copilot -p` session cannot start a user-only skill ([Headless sessions](copilot-support.md#headless-sessions)). It runs fully in Claude Code and Codex.

```text
/update-coding-agent-skills --cross-major
```

## `update-pr-tracker`

<!-- generated:update-pr-tracker -->
Update the owned dashboard section for pull requests the configured user authors, reviews, or participates in. Use it when asked to refresh the pull request tracker or see which pull requests need attention.

```text
/update-pr-tracker [owner/repo ... | --repository-set NAME] [--no-review] [--remove owner/repo#number ...]
```

Started by you or the agent. Installed with the `code-review-operations` bundle. Needs `gh`.
<!-- /generated:update-pr-tracker -->

- `owner/repo ...` or `--repository-set NAME`: which repositories' pull requests to track. With neither, it uses the configured `update-pr-tracker` set. Use the same scope on every run, or the rows for the other repositories disappear.
- `--no-review`: update the dashboard without offering reviews for pull requests whose AI review is missing or out of date.
- `--remove owner/repo#number ...`: leave those pull requests out of this run's dashboard, for example one you consider approved. It removes only the row and never acts on GitHub.

Without `--no-review`, it lists the pull requests that need a review, asks whether to review them, and asks once for a re-review scope when any review is out of date. The pull requests it collects are kept in a new temporary directory, never in a skill directory.

The reviews it offers run through `review-prs`, so in Codex and Copilot CLI they have the limits that skill's section states. The dashboard itself works the same in every runtime.

Each row's Findings cell first shows the work that remains, then the progress. The remaining work is the latest AI review's open findings by severity, including those carried from earlier reviews, and how many of them you have flagged with `flag-review-finding`, for example `1M 1S open (1 flagged)` or `none open`. A legend line above the first section spells out the letters: `M` must fix, `H` should fix, `S` suggestion. The progress is what moved since your own last review of that pull request, for example `· 1 new, 1 addressed since your review`, or `· unchanged since your review`. "New" counts findings first raised after the AI review of the commit you reviewed and still open; "addressed" counts findings raised by then that a later review found fixed. Before you have reviewed, or when GitHub can't place your review's commit, it counts every finding the reviews found fixed instead, such as `1H 3S open · 2 addressed`, and shows the open findings alone when none has been fixed yet. A review recorded before the finding ledger reads the same way, and a migrated legacy review shows only its open findings, because it can't say what was addressed. The AI Review link opens the latest report, whose file name carries the review version, and which lists every open finding in full.

```text
/update-pr-tracker
/update-pr-tracker --remove octo-org/widgets#42
```
