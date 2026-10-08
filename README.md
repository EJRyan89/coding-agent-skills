# Agent Skills

Portable agent skills for Claude Code, Codex, and GitHub Copilot CLI, with a guarded deployer that renders machine-specific configuration into immutable templates. Skill instructions are authoritative in Claude format; Codex and Copilot read them through generated adapters that map tools and models with the shared `runtime-compatibility.md`.

## Included skills

| Skill | What it does |
|---|---|
| [`analyze-skill-cost`](docs/skills.md#analyze-skill-cost) | Audits an agent skill for token, tool-call, delegation, adapter, and model-selection efficiency. |
| [`audit-ai-config`](docs/skills.md#audit-ai-config) | Audits a repository's AI-agent configuration, read-only and deterministically. |
| [`curate-agent-memory`](docs/skills.md#curate-agent-memory) | Audits a project's Claude Code auto-memory and, with approval, moves durable rules to where they belong. |
| [`dotnet-format`](docs/skills.md#dotnet-format) | Runs formatting and analyzer checks against changed C# files. Opt-in. |
| [`github-activity-report`](docs/skills.md#github-activity-report) | Reports one user's pull requests, commits, and reviews in one GitHub organization, month by month. |
| [`repo-cleanup`](docs/skills.md#repo-cleanup) | Performs guarded Git repository housekeeping. |
| [`update-coding-agent-skills`](docs/skills.md#update-coding-agent-skills) | Fast-forwards the clone the skills were deployed from to `origin/main` and redeploys them. |
| [`code-review-operations`](docs/code-review-operations.md) | A bundle installed together: [`review-prs`](docs/skills.md#review-prs), [`update-pr-tracker`](docs/skills.md#update-pr-tracker), [`review-insights`](docs/skills.md#review-insights), and [`flag-review-finding`](docs/skills.md#flag-review-finding). |

[Skills](docs/skills.md) explains how to start each skill, its arguments with examples, and what it needs installed.

## Supported platforms and runtimes

Version 0.2.0 supports Windows only; macOS and Linux support is planned. On those systems, and in WSL, the deployer stops before changing anything.

Install the runtimes you use. None of them is needed to deploy, so you can use the skills from Codex or Copilot without installing Claude Code.

| Runtime | Maintainer's machines | Fresh Windows runner | Start a skill with |
|---|---|---|---|
| Claude Code | 2.1.291 | 2.1.289 | `/<skill>` |
| Codex CLI | 0.160.0 | 0.160.0 | `$<skill>` |
| GitHub Copilot CLI | 1.0.92 | 1.0.91 | `/<skill>` |

The maintainer's machines ran the [runtime canary](tools/runtime_canary.py) and real skill and review runs. The fresh runner is the manual [`deployable` workflow](.github/workflows/deployable.yml), last passed on 2026-10-07: on a clean GitHub-hosted Windows runner it installs the prerequisites and each CLI, deploys, checks that Codex and Copilot find every skill, uninstalls, and deploys again. Claude Code reads the deployed files directly and cannot list its skills without a session, so for it the runner checks the installed version and that every deployed skill and agent is in place. It starts no model, so it does not show a skill running; that stays a manual check through the canary. See [Releasing](docs/releasing.md#before-tagging).

## Quick start

You need Python 3.11 or newer, Git for Windows, ShellCheck, and PowerShell; several skills also use the GitHub CLI. [Installation](docs/installation.md) gives the install commands and explains each step below.

```bash
git clone https://github.com/EJRyan89/coding-agent-skills.git
```

Then, from the `coding-agent-skills` folder the clone created:

```bash
python deploy.py check
python deploy.py configure
python deploy.py --all --dry-run
python deploy.py --all
```

`check` reports missing tools, `configure` asks for the directory holding your local repositories, and `--all` deploys every skill except opt-in ones such as `dotnet-format`, which you add with `--include dotnet-format`.

If you use Codex CLI, make its two [Windows settings](docs/installation.md#codex-cli-settings) before starting a skill. If you use Codex or Copilot CLI, `python deploy.py verify` checks that each finds the deployed skills.

## Updating and uninstalling

Start `update-coding-agent-skills` from any runtime, or update by hand with `git pull --ff-only` and the same dry run and deployment as above. To uninstall, run `python deploy.py` and answer `none` at the selection prompt; locally modified copies are kept. [Installation](docs/installation.md#updating) covers both.

## Safety model

The deployer:

- renders copies in memory and never modifies template sources; a dry run writes nothing;
- parses configuration without executing it and substitutes only the variables each skill declares, escaping or rejecting values according to the file type;
- validates names, paths, metadata, unresolved tokens, Bash, PowerShell, and ShellCheck findings before applying changes;
- tracks source-scoped ownership in a manifest and rejects cross-source collisions;
- journals mutations, so the next run recovers or rolls back an interrupted deployment;
- preserves modified destinations unless replacement is explicitly authorized.

When it stops because it needs you to decide, its message names a section of [Recovery](docs/recovery.md).

## Documentation

Using the skills:

| Page | What it covers |
|---|---|
| [Skills](docs/skills.md) | Starting each skill, its arguments, and what it needs |
| [Installation](docs/installation.md) | Requirements, install commands, deploying, updating, and uninstalling |
| [Code-review operations](docs/code-review-operations.md) | Configuring and running the code-review bundle |
| [Codex support](docs/codex-support.md) | Discovery and the Windows settings Codex CLI needs |
| [Copilot support](docs/copilot-support.md) | Discovery precedence, headless sessions, inline reviews, and the bounded review host |
| [Recovery](docs/recovery.md) | Interrupted deployments, backups, locks, and ownership conflicts |

Working on this repository:

| Page | What it covers |
|---|---|
| [Contributing](CONTRIBUTING.md) | Development environment, validation, and pull request expectations |
| [Adding a skill](docs/adding-a-skill.md) | The skill template and metadata contract |
| [Code-review operations contract](docs/code-review-operations-contract.md) | Behavior the code-review bundle must keep |
| [Parallel sessions](docs/parallel-sessions.md) | Worktrees for concurrent agent sessions |
| [Dependency updates](docs/dependency-updates.md) | Every pinned or floor-checked dependency, and the steps after a Dependabot pull request |
| [Releasing](docs/releasing.md) | How a release is cut |

Report suspected vulnerabilities according to the [Security policy](SECURITY.md), not through a public issue.

## Supporting the project

The skills and the deployer are free and open source, and will stay that way. If they've saved you time, you can [buy me a coffee on Ko-fi](https://ko-fi.com/ejryan89).

## License

This project is available under the [MIT License](LICENSE).
