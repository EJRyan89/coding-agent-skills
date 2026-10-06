# Installation

What the deployer needs, how to install it, and how to deploy, update, and uninstall the skills. The [README](../README.md#quick-start) has the short version.

## Requirements

Every deployment needs:

| Tool | Why |
|---|---|
| Python 3.11 or newer | Runs the deployer. The deployer and the `audit-ai-config` engine use only the standard library. |
| Git for Windows | Its Bash checks the syntax of rendered Bash scripts and Bash blocks in Markdown; several skills run Bash commands. |
| ShellCheck | Lints the same rendered Bash content. Deployment stops if it is missing. CI uses 0.9.0 and checks the latest release weekly; newer releases also work. |
| PowerShell | Parses rendered `.ps1` files. Windows PowerShell 5.1 is enough for deployment; the validation suite needs PowerShell 7 (`pwsh`). |

Some skills need more when you use them, not when you deploy them:

| Tool | Needed by |
|---|---|
| GitHub CLI (`gh`) 2.48.0 or newer, signed in with `gh auth login` | The code-review operations bundle, `github-activity-report`, and `repo-cleanup`; `dotnet-format` uses it, when present, to find a pull request's base branch |
| Claude Code (optional) | Running the skills from Claude Code; the deployer itself does not need it |
| Codex CLI 0.88.0 or newer (optional) | Verifying Codex skill discovery |
| GitHub Copilot CLI 1.0.88 or newer (optional) | Verifying Copilot skill discovery, and the bounded Copilot code-review host |
| .NET SDK and the `dotnet-format` global tool | `dotnet-format`, which checks for them and reports the install command |

None of the agent runtimes is needed to deploy. The deployer keeps its files under `~/.claude` even when Claude Code is not installed, because the skills' authoritative copies live there and the Codex and Copilot adapters point to them.

## Installing the tools

Use whichever installer your machine allows. [winget](https://learn.microsoft.com/windows/package-manager/winget/) comes with Windows 10 and 11; [Chocolatey](https://chocolatey.org/install) and [Scoop](https://scoop.sh/) are separate installs, and Chocolatey needs an administrator shell. Every tool can also be downloaded from its official site.

| Tool | `winget install --id` | `choco install` | `scoop install` | Official download |
|---|---|---|---|---|
| Python | `Python.Python.3.13` | `python` | `python` | [python.org](https://www.python.org/downloads/windows/) |
| Git for Windows | `Git.Git` | `git` | `git` | [git-scm.com](https://git-scm.com/downloads/win) |
| ShellCheck | `koalaman.shellcheck` | `shellcheck` | `shellcheck` | [GitHub releases](https://github.com/koalaman/shellcheck/releases) |
| PowerShell 7 | `Microsoft.PowerShell` | `pwsh` | `pwsh` | [GitHub releases](https://github.com/PowerShell/PowerShell/releases) |
| GitHub CLI | `GitHub.cli` | `gh` | `gh` | [cli.github.com](https://cli.github.com/) |
| Claude Code | none | none | none | `npm install -g @anthropic-ai/claude-code` with Node.js |
| Codex CLI | none | none | none | `npm install -g @openai/codex` with Node.js, or [GitHub releases](https://github.com/openai/codex/releases) |
| GitHub Copilot CLI | `GitHub.Copilot` | `github-copilot-cli` (community-maintained) | `copilot-cli` | `npm install -g @github/copilot` with Node.js 22 or newer, or [GitHub releases](https://github.com/github/copilot-cli/releases) |

The deployer finds Git Bash in `C:\Program Files\Git`, or next to the `git` on your `PATH`, which covers Scoop and per-user Git installs. It never uses another `bash`, such as WSL's. If Git isn't on your `PATH`, set `GIT_BASH` to its `bin\bash.exe`.

**After installing a tool, open a new terminal.** Windows only updates `PATH` for programs started after the installation. An editor or agent application that was already running, including one minimized to the system tray, must be fully quit and restarted before its terminals can find the new tool. Run `python deploy.py check` to see what is installed. The validation suite checks for its tools before running any test, and the deployer checks before changing any file; both list every missing tool with its install command. Deploying also warns when a selected skill uses a tool that is not installed.

## Codex CLI settings

Codex CLI on Windows needs two machine settings before it can run a skill's scripts. Until both are in place, every skill script fails in Codex, with `blocked by policy` or `The file cannot be accessed by the system`:

- add a `[windows]` sandbox setting to `~/.codex/config.toml`, as [Windows sandbox mode](codex-support.md#windows-sandbox-mode) shows; and
- turn off the `python.exe` and `python3.exe` app execution aliases.

Windows registers `python.exe` and `python3.exe` as app execution aliases in `%LOCALAPPDATA%\Microsoft\WindowsApps`: every Windows install has them as Microsoft Store stubs, and the Python install manager registers its own. Codex CLI's sandbox cannot start those aliases, so turn them off under **Settings > Apps > Advanced app settings > App execution aliases** whichever Python installer you used, and keep `py` and `pymanager` on if they are listed. Reordering your user `PATH` is not always enough, because `WindowsApps` can also be on the system `PATH`, which Windows searches first. See [Python app execution aliases](codex-support.md#python-app-execution-aliases).

## Deploying

Clone the repository and run every command from its directory, in PowerShell or Git Bash:

```bash
python deploy.py check
python deploy.py configure
python deploy.py --all --dry-run
python deploy.py --all
```

`check` lists every tool the deployer and the skills need, with its version and which skills use it, and changes nothing. `deploy.py` is the only script you run to deploy; run `python deploy.py --help` for every option.

`configure` prompts for the root directory containing your local repositories; rerun it to change the value, or add `--reset` to start from an empty configuration. At any prompt, Enter keeps the current value and Ctrl+C cancels without saving. Configured paths must be absolute drive-letter paths of directories that already exist. `configure` accepts them as you would type or paste them, such as `C:\GitHub` or `"C:\GitHub\"`, and stores them with forward slashes, such as `C:/GitHub`.

`--all` deploys every skill except opt-in ones, which only some users need, such as `dotnet-format` for C#; add one with `--include`, as in `python deploy.py --all --include dotnet-format`, or choose it from the menu. Once installed, an opt-in skill stays installed on later `--all` runs.

Deployer configuration is stored under `~/.claude/deployer/config/`. Deployed authoritative skills and the ownership manifest are stored under `~/.claude/skills/`; generated thin runtime adapters for Codex and GitHub Copilot CLI are transactionally maintained under `~/.agents/skills/`. The code-review suite keeps its runtime-neutral configuration and state under `~/.coding-agent-skills/code-review/` by default. [Recovery](recovery.md#where-the-deployer-keeps-its-state) lists every path the deployer uses.

### Checking runtime discovery

If you use Codex CLI or Copilot CLI, check that each one finds the deployed skills. This starts no AI session and changes nothing:

```bash
python deploy.py verify
```

It reports each adapter as `FOUND`, `NOT FOUND`, `DISABLED`, or `SHADOWED` for every installed runtime, and skips a runtime that is not installed. A runtime may spell a directory differently from the deployer, such as by its 8.3 short name (`RUNNER~1` for `runneradmin`); `verify` compares the directories the paths resolve to, not their text. A same-named skill can shadow an adapter: Copilot CLI gives personal skills under `~/.copilot/skills/` precedence over the generated adapters, and Codex offers both copies. The deployer reports Copilot shadows when it deploys but never modifies the user-managed Copilot directory. See [Copilot support](copilot-support.md) and [Codex support](codex-support.md).

### Skipped items

If an unmanaged or locally modified destination differs from the rendered template, deployment skips it and lists it under `SKIPPED`, with a diff for unmanaged skills. Review the difference before explicitly replacing only that item:

```bash
python deploy.py --all --force-item dotnet-format
```

Forced replacements are retained under `~/.claude/skills/.backups/<run-id>/`.

## Updating

From any runtime, start `update-coding-agent-skills`: `/update-coding-agent-skills` in Claude Code or Copilot CLI, `$update-coding-agent-skills` in Codex. It fast-forwards this clone to `origin/main` and redeploys, and it stops without changing anything if the clone has uncommitted changes or local commits. It also stops before a release that raises the major version (or the minor version, while the major is 0), since that release may ask you to act; read its notes, then start the skill again with `--cross-major` to apply it. See [Versioning](releasing.md#versioning) for what each level means.

To update by hand, update the clone, preview the deployment, and then apply it:

```bash
git pull --ff-only
python deploy.py --all --dry-run
python deploy.py --all
```

The dry run groups items by planned action, with anything that needs your attention, such as conflicts, first, and lists each group alphabetically. Installed items whose rendered content would not change are grouped under `UNCHANGED`, so the `UPDATE` group shows exactly what the update changes. A runtime adapter gets its own line, marked `(runtime adapter)`, only when it needs attention or does something its skill does not. The deployment itself ends with a report in the same layout, in the past tense: `SKIPPED`, `UPDATED`, `INSTALLED`, `UNCHANGED`, and so on, followed by the run ID and manifest path.

Deploy from the same clone each time: the manifest records it, and a deployment from another checkout refuses. If you move or re-clone the repository, deploy once from the new checkout with `--take-over-source`; see [Deploying from another checkout](recovery.md#deploying-from-another-checkout).

If an updated skill requires a new configuration value, the deployer stops and asks you to run `python deploy.py configure` first.

## Uninstalling

Run `python deploy.py --dry-run`, answer `none` at the selection prompt to preview the removal, then run `python deploy.py` and answer `none` again to remove every unmodified skill and shared asset deployed from this repository.

Locally modified deployment outputs are preserved and reported for manual review. A shared asset that a preserved skill or another installed source still needs is kept and reported as `KEEP` in the preview and `KEPT` in the deployment report; resolve the preserved skill or uninstall that source, then rerun this uninstall to remove it. Configuration and retained backups are not deleted, and content owned by other sources is not affected.
