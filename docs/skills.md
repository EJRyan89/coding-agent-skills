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

**Installed** says how `python deploy.py` selects the skill: `--all` installs every skill marked "by default"; an opt-in skill needs `--include <name>` or a menu choice; a bundle installs its skills together. **Needs** lists what the skill runs beyond the deployment prerequisites in the [README](../README.md#deployment-requirements), and any setting it reads from `python deploy.py configure`. `python deploy.py check` reports which of those tools are installed.

<!-- generated:summary -->
| Skill | Started by | Installed | Needs |
|---|---|---|---|
| [`analyze-skill-cost`](#analyze-skill-cost) | You or the agent | By default | Nothing extra |
| [`audit-ai-config`](#audit-ai-config) | You or the agent | By default | Nothing extra |
| [`curate-agent-memory`](#curate-agent-memory) | You or the agent | By default | Nothing extra |
| [`dotnet-format`](#dotnet-format) | You or the agent | Opt-in | `dotnet-format`, `gh` |
| [`flag-review-finding`](#flag-review-finding) | You or the agent | `code-review-operations` bundle | `copilot` (optional), `gh` |
| [`github-activity-report`](#github-activity-report) | You or the agent | By default | `gh` |
| [`init-ai-config`](#init-ai-config) | You or the agent | By default | Nothing extra |
| [`repo-cleanup`](#repo-cleanup) | You | By default | `gh`, the `REPOS_ROOT` setting |
| [`review-insights`](#review-insights) | You or the agent | `code-review-operations` bundle | `copilot` (optional), `gh` |
| [`review-prs`](#review-prs) | You or the agent | `code-review-operations` bundle | `copilot` (optional), `gh` |
| [`update-coding-agent-skills`](#update-coding-agent-skills) | You | By default | Nothing extra |
| [`update-pr-tracker`](#update-pr-tracker) | You or the agent | `code-review-operations` bundle | `copilot` (optional), `gh` |
<!-- /generated:summary -->

The summary and the first block of each section below are generated from each skill's `SKILL.md` frontmatter and `deploy-meta/<name>.json`. Change those, then run `python tools/skill_reference.py --write`; validation fails while this file is stale. The explanation after each generated block is written by hand.

## `analyze-skill-cost`

<!-- generated:analyze-skill-cost -->
Audit an agent skill for cost and efficiency: token footprint, tool calls, deterministic work left to prose, subagent overhead, runtime adapters, model, and allowed-tools. Read-only findings report. Use it when asked what a skill costs, why it is slow or expensive, or to check one before shipping it.

```text
/analyze-skill-cost <SkillName>
```

Started by you or the agent. Installed by default.
<!-- /generated:analyze-skill-cost -->

`<SkillName>` is a skill's name, not a path. The skill looks for it under `~/.claude/skills/` and, when you start it inside a Git repository, in that repository's `.agents/skills/` and `.claude/skills/`. In a checkout of this repository it also looks in `skills/` for a skill with a `deploy-meta/<name>.json`, and audits that source rather than the deployed copy under `~/.claude/skills/`, so a change is audited before it is deployed. If no skill or more than one has that name, it lists what it found and stops. It takes no flags.

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

It audits the Git repository you start it in, and asks which repository to audit when you are not in one. Ask for JSON when you want to process the findings rather than read them. It parses files statically: it runs nothing from the repository, makes no network requests, and writes nothing.

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

Started by you or the agent. Opt-in: deploy it with `--include dotnet-format`. Needs `dotnet-format` and `gh`. Takes no arguments.
<!-- /generated:dotnet-format -->

Start it inside the repository, on the branch to check. It compares with the pull request's base branch, found with `gh`, else `origin/main`, else `origin/master`, and checks every committed, uncommitted, and untracked `.cs` file that differs. It uses the nearest solution to the current directory that owns a changed file. It reports violations and asks before fixing them, and asks before adding any missing `.editorconfig` settings.

## `flag-review-finding`

<!-- generated:flag-review-finding -->
Add, list, or resolve a structured code-review improvement flag. Use it when the user says a review finding was wrong, noisy, or missed something and wants that recorded.

```text
/flag-review-finding add CATEGORY BODY [--repository owner/repo --pull N [--review-version V --finding ID]] | list | resolve ID RESOLUTION
```

Started by you or the agent. Installed with the `code-review-operations` bundle. Needs `copilot` (optional) and `gh`.
<!-- /generated:flag-review-finding -->

The first word chooses what to do:

- `add CATEGORY BODY` records a flag. `CATEGORY` is a short label of your choosing and `BODY` is the rationale; quote either when it has spaces. To tie the flag to a review finding, add `--repository owner/repo --pull N`, then `--review-version V --finding ID`. `ID` is the finding's ID in the review report, such as `F002`, and `V` is the `v<N>` in that report's **Mode** row. Finding IDs restart at `F001` in every review, so `--finding` needs `--review-version`. Only a flag that names a finding can be resolved by `review-insights`.
- `list` shows the open flags.
- `resolve ID RESOLUTION` closes the flag `ID`, such as `RF-000004`, with a non-empty explanation.

```text
/flag-review-finding add noise "Asked for a null check the caller already guarantees" --repository octo-org/widgets --pull 42 --review-version 2 --finding F003
/flag-review-finding list
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

## `init-ai-config`

<!-- generated:init-ai-config -->
Creates or upgrades AI agent configuration (Claude Code, Codex, Copilot) across runtimes from a single authoritative CLAUDE.md. May write files. Use it when asked to set up, add, or migrate a repository's agent instructions, not only to check them.

```text
/init-ai-config
```

Started by you or the agent. Installed by default. Takes no arguments.
<!-- /generated:init-ai-config -->

It configures the Git repository you start it in, and asks which repository to use when you are not in one; it never initializes Git without your approval. Instead of flags, it asks scoping questions as it goes, such as which runtimes and surfaces to support. It shows the existing configuration and any conflicts before writing, and never replaces content you wrote without your approval.

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

It performs only the actions it can prove safe, such as removing a branch whose pull request merged, and asks you about anything else, such as local branches with no remote or unmerged work.

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

Started by you or the agent. Installed with the `code-review-operations` bundle. Needs `copilot` (optional) and `gh`.
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

`--force` reviews heads that already have a review. The skill never posts to GitHub. [Code-review operations](code-review-operations.md) covers the configuration and the records it writes.

```text
/review-prs --repository-set team
/review-prs --pull octo-org/widgets#42 --re-review octo-org/widgets#37 --scope auto
/review-prs --canary octo-org/widgets#42 --canary octo-org/widgets#51
```

## `update-coding-agent-skills`

<!-- generated:update-coding-agent-skills -->
Fast-forward the coding-agent-skills clone these skills were deployed from to origin/main, then redeploy every skill with deploy.py --all.

```text
/update-coding-agent-skills
```

Started by you. Installed by default. Takes no arguments.
<!-- /generated:update-coding-agent-skills -->

It updates the clone these skills were deployed from: it fast-forwards `main` to `origin/main`, then runs `python deploy.py --all`. It stops without changing anything when tracked files have uncommitted changes, the fetch fails, or local `main` has commits that `origin/main` lacks. It never stashes, resets, or forces a deployment; those decisions stay yours.

## `update-pr-tracker`

<!-- generated:update-pr-tracker -->
Update the owned dashboard section for pull requests the configured user authors, reviews, or participates in. Use it when asked to refresh the pull request tracker or see which pull requests need attention.

```text
/update-pr-tracker [owner/repo ... | --repository-set NAME] [--no-review] [--remove owner/repo#number ...]
```

Started by you or the agent. Installed with the `code-review-operations` bundle. Needs `copilot` (optional) and `gh`.
<!-- /generated:update-pr-tracker -->

- `owner/repo ...` or `--repository-set NAME`: which repositories' pull requests to track. With neither, it uses the configured `update-pr-tracker` set. Use the same scope on every run, or the rows for the other repositories disappear.
- `--no-review`: update the dashboard without offering reviews for pull requests whose AI review is missing or out of date.
- `--remove owner/repo#number ...`: leave those pull requests out of this run's dashboard, for example one you consider approved. It removes only the row and never acts on GitHub.

Without `--no-review`, it lists the pull requests that need a review, asks whether to review them, and asks once for a re-review scope when any review is out of date.

```text
/update-pr-tracker
/update-pr-tracker --remove octo-org/widgets#42
```
