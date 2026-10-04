# Agent Skills

Portable agent skills with a guarded deployment workflow for rendering machine-specific configuration into immutable templates. Skill instructions are authoritative in Claude format and are designed for Claude Code, Codex, and GitHub Copilot CLI compatibility.

## Included skills

- `analyze-skill-cost` — audits an agent skill for token, tool-call, delegation, adapter, and model-selection efficiency.
- `audit-ai-config` — performs a deterministic, read-only audit of repository AI-agent configuration.
- `curate-agent-memory` — audits Claude Code auto-memory for a project and, with approval, moves durable rules to where they belong.
- `dotnet-format` (opt-in) — runs formatting and analyzer checks against changed C# files.
- `github-activity-report` — reports one user's pull requests, commits, and reviews in one GitHub organization, month by month.
- `init-ai-config` — creates or upgrades cross-runtime AI-agent configuration from an authoritative `CLAUDE.md`.
- `repo-cleanup` — performs guarded Git repository housekeeping.
- `update-coding-agent-skills` — fast-forwards the clone the skills were deployed from to `origin/main` and redeploys them.
- `code-review-operations` — an atomic bundle containing `review-prs`, `update-pr-tracker`, `review-insights`, and `flag-review-finding`, backed by the hidden `code-review-core` dependency.

See [Skills](docs/skills.md) for how to start each skill, its arguments with examples, and what it needs installed.

Codex and Copilot read the shared `runtime-compatibility.md` tool and model mapping through their generated adapters; Claude Code runs the skills directly without it.

## Supported platforms and runtimes

The first release, 0.1.0, supports Windows only. macOS and Linux support is planned for a later release. On those systems, and in WSL, the deployer stops before changing anything.

The skills run in any of these runtimes. Install the ones you use; none of them is needed to deploy, so you can use the skills from Codex or Copilot without installing Claude Code.

| Runtime | Tested with | Start a skill with | Check discovery with |
|---|---|---|---|
| Claude Code | 2.1.289 | `/<skill>` | starting a skill |
| Codex CLI | 0.160.0 | `$<skill>` | `python deploy.py verify` |
| GitHub Copilot CLI | 1.0.91 | `/<skill>` | `python deploy.py verify` |

These versions were tested on the maintainer's own Windows machines, through the [runtime canary](tools/runtime_canary.py) and real skill and review runs, not on a clean Windows installation; installing from a fresh clone on a clean Windows environment, one runtime at a time, has not been done by hand and is deferred for 0.1.0. [Issue #12](https://github.com/EJRyan89/coding-agent-skills/issues/12) adds a manual workflow that covers the deployer and runtime-discovery half of that check on a fresh GitHub-hosted Windows runner; a model running a skill, and the Codex Windows sandbox and Python alias settings, stay tested by hand on the maintainer's machines.

Codex CLI on Windows needs two machine settings before it can run a skill's scripts: a sandbox setting in `~/.codex/config.toml`, and Python's app execution aliases turned off. [First install](#first-install) says when to make them, and [Codex support](docs/codex-support.md) explains both. The deployer keeps its files under `~/.claude` even when Claude Code is not installed, because the skills' authoritative copies live there and the Codex and Copilot adapters point to them.

## Deployment requirements

Every deployment needs:

| Tool | Why |
|---|---|
| Python 3.11 or newer | Runs the deployer. The deployer, the `audit-ai-config` engine, and the `init-ai-config` generator use only the standard library. |
| Git for Windows | Its Bash checks the syntax of rendered Bash scripts and Bash blocks in Markdown; several skills run Bash commands. |
| ShellCheck | Lints the same rendered Bash content. Deployment stops if it is missing. CI uses 0.9.0; newer releases also work. |
| PowerShell | Parses rendered `.ps1` files. Windows PowerShell 5.1 is enough for deployment; the validation suite needs PowerShell 7 (`pwsh`). |

Some skills need more when you use them, not when you deploy them:

| Tool | Needed by |
|---|---|
| GitHub CLI (`gh`) 2.48.0 or newer, signed in with `gh auth login` | The code-review operations bundle, `github-activity-report`, and `repo-cleanup`; `dotnet-format` uses it, when present, to find a pull request's base branch |
| GitHub Copilot CLI 1.0.88 or newer (optional) | Verifying Copilot skill discovery, and the bounded Copilot code-review host |
| .NET SDK and the `dotnet-format` global tool | `dotnet-format`, which checks for them and reports the install command |

### Installing the tools

Use whichever installer your machine allows. [winget](https://learn.microsoft.com/windows/package-manager/winget/) comes with Windows 10 and 11; [Chocolatey](https://chocolatey.org/install) and [Scoop](https://scoop.sh/) are separate installs, and Chocolatey needs an administrator shell. Every tool can also be downloaded from its official site.

| Tool | `winget install --id` | `choco install` | `scoop install` | Official download |
|---|---|---|---|---|
| Python | `Python.Python.3.13` | `python` | `python` | [python.org](https://www.python.org/downloads/windows/) |
| Git for Windows | `Git.Git` | `git` | `git` | [git-scm.com](https://git-scm.com/downloads/win) |
| ShellCheck | `koalaman.shellcheck` | `shellcheck` | `shellcheck` | [GitHub releases](https://github.com/koalaman/shellcheck/releases) |
| PowerShell 7 | `Microsoft.PowerShell` | `pwsh` | `pwsh` | [GitHub releases](https://github.com/PowerShell/PowerShell/releases) |
| GitHub CLI | `GitHub.cli` | `gh` | `gh` | [cli.github.com](https://cli.github.com/) |
| GitHub Copilot CLI | `GitHub.Copilot` | `github-copilot-cli` (community-maintained) | `copilot-cli` | `npm install -g @github/copilot` with Node.js 22 or newer, or [GitHub releases](https://github.com/github/copilot-cli/releases) |

The deployer finds Git Bash in `C:\Program Files\Git`, or next to the `git` on your `PATH`, which covers Scoop and per-user Git installs. It never uses another `bash`, such as WSL's. If Git isn't on your `PATH`, set `GIT_BASH` to its `bin\bash.exe`.

Windows registers `python.exe` and `python3.exe` as app execution aliases in `%LOCALAPPDATA%\Microsoft\WindowsApps`: every Windows install has them as Microsoft Store stubs, and the Python install manager registers its own. Codex CLI's sandbox cannot start those aliases, so if you use Codex, turn them off under **Settings > Apps > Advanced app settings > App execution aliases** whichever Python installer you used, and keep `py` and `pymanager` on if they are listed. Reordering your user `PATH` is not always enough, because `WindowsApps` can also be on the system `PATH`, which Windows searches first. See [Codex support](docs/codex-support.md#python-app-execution-aliases).

**After installing a tool, open a new terminal.** Windows only updates `PATH` for programs started after the installation. An editor or agent application that was already running, including one minimized to the system tray, must be fully quit and restarted before its terminals can find the new tool. Run `python deploy.py check` to see what is installed. The validation suite checks for its tools before running any test, and the deployer checks before changing any file; both list every missing tool with its install command. Deploying also warns when a selected skill uses a tool that is not installed.

Configured filesystem paths must be absolute drive-letter paths. `python deploy.py configure` accepts them as you would type or paste them, such as `C:\GitHub` or `"C:\GitHub\"`, and stores them with forward slashes, such as `C:/GitHub`. The directory must already exist.

## Clone

```bash
git clone https://github.com/EJRyan89/coding-agent-skills.git
cd coding-agent-skills
```

## First install

Run all commands from this directory, in PowerShell or Git Bash:

```bash
python deploy.py check
python deploy.py configure
python deploy.py --all --dry-run
python deploy.py --all
```

`check` lists every tool the deployer and the skills need, with its version and which skills use it, and changes nothing. `--all` deploys every skill except opt-in ones, which only some users need, such as `dotnet-format` for C#; add one with `--include`, as in `python deploy.py --all --include dotnet-format`, or choose it from the menu. Once installed, an opt-in skill stays installed on later `--all` runs. `deploy.py` is the only script you run to deploy. Its `configure` command prompts for the root directory containing your local repositories; rerun it to change the value, or add `--reset` to start from an empty configuration. At any prompt, Enter keeps the current value and Ctrl+C cancels without saving. Run `python deploy.py --help` for every option.

If you use Codex CLI, make its two Windows settings before you start a skill. Until both are in place, every skill script fails in Codex, with `blocked by policy` or `The file cannot be accessed by the system`:

- add a `[windows]` sandbox setting to `~/.codex/config.toml`, as [Windows sandbox mode](docs/codex-support.md#windows-sandbox-mode) shows; and
- turn off the `python.exe` and `python3.exe` app execution aliases, as [Installing the tools](#installing-the-tools) describes.

Deployer configuration is stored under `~/.claude/deployer/config/`. Deployed authoritative skills and the ownership manifest are stored under `~/.claude/skills/`; generated thin runtime adapters for Codex and GitHub Copilot CLI are transactionally maintained under `~/.agents/skills/`. The code-review suite keeps its runtime-neutral configuration and state under `~/.coding-agent-skills/code-review/` by default.

If you use Codex CLI or Copilot CLI, check that each one finds the deployed skills. This starts no AI session and changes nothing:

```bash
python deploy.py verify
```

It reports each adapter as `FOUND`, `NOT FOUND`, `DISABLED`, or `SHADOWED` for every installed runtime, and skips a runtime that is not installed. A same-named skill can shadow an adapter: Copilot CLI gives personal skills under `~/.copilot/skills/` precedence over the generated adapters, and Codex offers both copies. The deployer reports Copilot shadows when it deploys but never modifies the user-managed Copilot directory. See [Copilot support](docs/copilot-support.md) and [Codex support](docs/codex-support.md).

If an unmanaged or locally modified destination differs from the rendered template, deployment skips it and lists it under `SKIPPED`, with a diff for unmanaged skills. Review the difference before explicitly replacing only that item:

```bash
python deploy.py --all --force-item dotnet-format
```

Forced replacements are retained under `~/.claude/skills/.backups/<run-id>/`.

## Update

From any runtime, start `update-coding-agent-skills`: `/update-coding-agent-skills` in Claude Code or Copilot CLI, `$update-coding-agent-skills` in Codex. It fast-forwards this clone to `origin/main` and redeploys, and it stops without changing anything if the clone has uncommitted changes or local commits.

To update by hand, update the clone, preview the deployment, and then apply it:

```bash
git pull --ff-only
python deploy.py --all --dry-run
python deploy.py --all
```

The dry run groups items by planned action, with anything that needs your attention, such as conflicts, first, and lists each group alphabetically. Installed items whose rendered content would not change are grouped under `UNCHANGED`, so the `UPDATE` group shows exactly what the update changes. A runtime adapter gets its own line, marked `(runtime adapter)`, only when it needs attention or does something its skill does not. The deployment itself ends with a report in the same layout, in the past tense: `SKIPPED`, `UPDATED`, `INSTALLED`, `UNCHANGED`, and so on, followed by the run ID and manifest path.

If an updated skill requires a new configuration value, the deployer stops and asks you to run `python deploy.py configure` first.

## Uninstall

Run `python deploy.py --dry-run`, answer `none` at the selection prompt to preview the removal, then run `python deploy.py` and answer `none` again to remove every unmodified skill and shared asset deployed from this repository.

Locally modified deployment outputs are preserved and reported for manual review. A shared asset that a preserved skill or another installed source still needs is kept and reported as `KEEP` in the preview and `KEPT` in the deployment report; resolve the preserved skill or uninstall that source, then rerun this uninstall to remove it. Configuration and retained backups are not deleted, and content owned by other sources is not affected.

## Recovery

An interrupted or failed deployment is reconciled from its journal by the next run, so you normally need do nothing. When the deployer stops instead, because recovery found something it cannot decide, a lock it cannot prove stale, an item another source owns, or a configuration it cannot read, its message ends with the step to take and names a section of [Recovery](docs/recovery.md). That page also says where the journal, the lock, and the permanent backups live, and how to restore a backup.

## Safety model

The deployer:

- renders copies in memory and never modifies template sources; a dry run writes nothing;
- parses configuration without executing it and substitutes only the variables each skill declares, escaping or rejecting values according to the file type;
- validates names, paths, metadata, unresolved tokens, Bash, PowerShell, and ShellCheck findings before applying changes;
- tracks source-scoped ownership in a manifest and rejects cross-source collisions;
- journals mutations and recovers or rolls back interrupted deployments (see [Recovery](docs/recovery.md));
- preserves modified destinations unless replacement is explicitly authorized.

## Development

The templates, metadata, and deployment logic in this project are the authoritative source. Files installed under `~/.claude/skills/` are deployment outputs and must not be treated as upstream source changes.

Run the complete validation suite, the same command CI runs, from a terminal:

```powershell
python -B tests/run_validation.py
```

Pass `-k <pattern>` to run the policies and suites whose name or path matches, for example `-k deployer`. Suites run in parallel, with large ones split into shards; set `VALIDATION_JOBS` to change the worker count.

Run one deployer test module directly, for example:

```bash
python -B tests/deployer/test_recovery_migration.py
```

See [Code-review operations](docs/code-review-operations.md) for suite configuration, [Copilot support](docs/copilot-support.md) for discovery and host boundaries, [Codex support](docs/codex-support.md) for the Windows settings Codex CLI needs, [Adding a skill](docs/adding-a-skill.md) for the template contract, [Dependency updates](docs/dependency-updates.md) for the pin-review process, and [Release checklist](docs/release-checklist.md) for the remaining release steps.

## Contributing and security

See [Contributing](CONTRIBUTING.md) for development and validation expectations. Report suspected vulnerabilities according to the [Security policy](SECURITY.md), not through a public issue.

## Support

These skills and the deployer are free and open source, and stay that way. If they've been useful to you, a coffee is welcome: [ko-fi.com/ejryan89](https://ko-fi.com/ejryan89).

## License

This project is available under the [MIT License](LICENSE).
