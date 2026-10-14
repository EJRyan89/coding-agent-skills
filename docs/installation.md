# Installation

What the deployer needs, how to install it, and how to deploy, update, and uninstall the skills. The [README](../README.md#quick-start) has the short version.

## Requirements

Every deployment needs:

| Tool | Why |
|---|---|
| Python 3.11.4 or newer | Runs the deployer. The deployer and the `audit-ai-config` engine use only the standard library. |
| Git for Windows, or on Linux Git and Bash | The Bash, Git for Windows' on Windows and the system's on Linux, checks the syntax of rendered Bash scripts and Bash blocks in Markdown; several skills run Bash commands. |
| ShellCheck | Lints the same rendered Bash content. Deployment stops if it is missing. CI uses 0.9.0 and checks the latest release weekly; newer releases also work. |
| PowerShell | Parses rendered `.ps1` files regardless of extension case. Windows PowerShell 5.1 is enough for deployment on Windows; Linux and the validation suite need PowerShell 7 (`pwsh`). |

Some skills need more when you use them, not when you deploy them:

| Tool | Needed by |
|---|---|
| GitHub CLI (`gh`) 2.48.0 or newer, signed in with `gh auth login` | The code-review operations bundle, `github-activity-report`, and `repo-cleanup`; `dotnet-format` uses it, when present, to find a pull request's base branch |
| Claude Code (optional) | Running the skills from Claude Code; the deployer itself does not need it |
| Codex CLI 0.88.0 or newer (optional) | Verifying Codex skill discovery |
| GitHub Copilot CLI 1.0.88 or newer (optional) | Verifying Copilot skill discovery, and the bounded Copilot code-review host |
| .NET SDK and the `dotnet-format` global tool; .NET SDK 9.0.200 or newer for a `.slnx` solution | `dotnet-format`, which checks for them and reports the install command; `dotnet-dependencies` uses the SDK, when present, for `dotnet nuget why` |
| `pdftotext` from Xpdf or Poppler, meeting its [provider-specific support floor](dependency-updates.md#runtime-tool-floors) (optional) | The code-review skills, to review a PDF a pull request changes; a missing, unsupported, or unidentifiable parser leaves that review `INCOMPLETE` |

`python deploy.py check` and the skills find each of these tools the same way: the first on your `PATH`, else the one in WinGet's Links folder (`%LOCALAPPDATA%\Microsoft\WinGet\Links`), where WinGet puts a portable install that may not be on `PATH`; on Linux, `PATH` alone. A skill runs the tool at the path it found, so a tool `check` reports is the one the skill starts.

For PDF review, an old `pdftotext` bundled with Git for Windows may no longer meet its provider's floor. Install a supported parser from a source you trust and put its directory on the reviewing process's `PATH`, then run `python deploy.py check` in that environment. The [Xpdf command-line tools](https://www.xpdfreader.com/download.html) and [Poppler project](https://poppler.freedesktop.org/) remain supported providers; the project does not install or update them for you. `check` and every extraction look at each `pdftotext` on `PATH`, then in WinGet's Links folder, take the first one that meets its floor, and name each one they passed over and where it was found, so Git's bundled copy, which Git Bash puts ahead of the Windows `PATH`, is skipped rather than blocking a supported one further down. Runtime extraction checks again, so a later `PATH` change or downgrade is checked rather than trusted. A standalone `review-document` refuses before creating its fixture; a pull request records an explicit PDF coverage gap.

On Linux, a distribution's `pdftotext`, from its `poppler-utils` package, keeps a version below Poppler's floor as the distribution backports fixes, so it is refused like any other parser below its floor, and `check` and every refusal there name the Xpdf command-line tools. Download the Linux build of the [Xpdf command-line tools](https://www.xpdfreader.com/download.html), unpack it, and put its `bin64` folder on `PATH`; since `check` and every extraction take the first parser that meets its floor, the Xpdf tools are used wherever they stand on `PATH`, and the distribution's copy is named as skipped.

None of the agent runtimes is needed to deploy. The deployer keeps its files under `~/.claude` even when Claude Code is not installed, because the skills' authoritative copies live there and the Codex and Copilot adapters point to them.

## Installing the tools

### On Windows

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
| GitHub Copilot CLI | `GitHub.Copilot` | `github-copilot-cli` (community-maintained) | `copilot-cli` | `npm install -g @github/copilot` with Node.js 22 or newer, or [GitHub releases](https://github.com/github/copilot-cli/releases); the code-review host needs WinGet's or the release's program ([Copilot support](copilot-support.md#bounded-code-review-host)) |

The deployer finds Git Bash in `C:\Program Files\Git`, or next to the `git` on your `PATH`, which covers Scoop and per-user Git installs. It never uses another `bash`, such as WSL's. If Git isn't on your `PATH`, set `GIT_BASH` to its `bin\bash.exe`.

### On Linux

The deployer and the whole validation suite run on Linux, and the [`deployable` workflow](../.github/workflows/deployable.yml) deploys onto a fresh Ubuntu runner; the [README](../README.md#supported-platforms-and-runtimes) says where Linux support stands. Install the tools from your distribution's archive where it has them, or from the vendor's own package repository where it does not:

| Tool | Debian and Ubuntu (`sudo apt install`) | Fedora (`sudo dnf install`) | Official download |
|---|---|---|---|
| Python | `python3` | `python3` | [python.org](https://www.python.org/downloads/source/) |
| Git and Bash | `git`; Bash comes with the system | `git`; Bash comes with the system | [git-scm.com](https://git-scm.com/downloads/linux) |
| ShellCheck | `shellcheck` | `ShellCheck` | [GitHub releases](https://github.com/koalaman/shellcheck/releases) |
| PowerShell 7 | from [Microsoft's package repository](https://learn.microsoft.com/powershell/scripting/install/install-ubuntu), or `sudo snap install powershell --classic` | from [Microsoft's package repository](https://learn.microsoft.com/powershell/scripting/install/install-rhel) | [GitHub releases](https://github.com/PowerShell/PowerShell/releases) |
| GitHub CLI | from [GitHub's package repository](https://github.com/cli/cli/blob/trunk/docs/install_linux.md), whose release meets the floor where an archive's may not | `gh` | [cli.github.com](https://cli.github.com/) |
| Node.js, for the npm installs below | from [NodeSource](https://github.com/nodesource/distributions) or a version manager when the archive's `nodejs` is older than Copilot CLI needs | `nodejs` | [nodejs.org](https://nodejs.org/) |
| Claude Code | none | none | `npm install -g @anthropic-ai/claude-code` with Node.js |
| Codex CLI | none | none | `npm install -g @openai/codex` with Node.js, or [GitHub releases](https://github.com/openai/codex/releases) |
| GitHub Copilot CLI | none | none | `npm install -g @github/copilot` with Node.js 22 or newer, or [GitHub releases](https://github.com/github/copilot-cli/releases) |
| `pdftotext` | none: the archive's `poppler-utils` is below Poppler's floor | none, for the same reason | the [Xpdf command-line tools](https://www.xpdfreader.com/download.html), as [Requirements](#requirements) describes |

On a release whose `python3` is older than the deployer's floor, `check` says so; install a newer Python from python.org or with a version manager. On Linux the deployer uses the first `bash` and `pwsh` on your `PATH`, your home is `HOME`, and a configured directory is an absolute path such as `/home/you/GitHub`. Linux distributions name Python `python3`, so run every command on this page as `python3`, as in `python3 deploy.py check`; the skills' own commands already name it so there.

### After installing

**After installing a tool, open a new terminal.** Windows only updates `PATH` for programs started after the installation. An editor or agent application that was already running, including one minimized to the system tray, must be fully quit and restarted before its terminals can find the new tool. Run `python deploy.py check` to see what is installed. The validation suite checks for its tools before running any test, and the deployer checks before changing any file; both list every missing tool with its install command. Deploying also warns when a selected skill uses a tool that is not installed.

## Codex CLI settings

Codex CLI on Windows needs two machine settings before it can run a skill's scripts; on Linux it needs neither. Until both are in place, every skill script fails in Codex, with `blocked by policy` or `The file cannot be accessed by the system`:

- add a `[windows]` sandbox setting to `~/.codex/config.toml`, as [Windows sandbox mode](codex-support.md#windows-sandbox-mode) shows; and
- turn off the `python.exe` and `python3.exe` app execution aliases.

Windows registers `python.exe` and `python3.exe` as app execution aliases in `%LOCALAPPDATA%\Microsoft\WindowsApps`: every Windows install has them as Microsoft Store stubs, and the Python install manager registers its own. Codex CLI's sandbox cannot start those aliases, so turn them off under **Settings > Apps > Advanced app settings > App execution aliases** whichever Python installer you used, and keep `py` and `pymanager` on if they are listed. Reordering your user `PATH` is not always enough, because `WindowsApps` can also be on the system `PATH`, which Windows searches first. See [Python app execution aliases](codex-support.md#python-app-execution-aliases).

## Deploying

Clone the repository and run every command from its directory, in PowerShell or Git Bash on Windows, or in any shell on Linux with `python3` in place of `python`:

```bash
python deploy.py check
python deploy.py configure
python deploy.py --all --dry-run
python deploy.py --all
```

`check` lists every tool the deployer and the skills need, with its version and which skills use it, and changes nothing. It also lists Git and Claude Code with their versions, so its output is what a bug report asks for. Its `Deployed commit` line names the commit the skills were last deployed from, with the nearest tag when Git can describe it, such as `v0.1.0-14-g71ad815 (<full hash>)`, or `unknown` before the first deployment that records one. `deploy.py` is the only script you run to deploy; run `python deploy.py --help` for every option.

`configure` prompts for the root directory containing your local repositories; rerun it to change the value, or add `--reset` to start from an empty configuration. At any prompt, Enter keeps the current value and Ctrl+C cancels without saving. Configured paths must be absolute paths of directories that already exist: drive-letter paths on Windows, and paths from the root, such as `/home/you/GitHub`, on Linux. On Windows `configure` accepts them as you would type or paste them, such as `C:\GitHub` or `"C:\GitHub\"`, and stores them with forward slashes, such as `C:/GitHub`.

Every prompt, `configure`'s and the deployment's selection menu alike, goes to stderr, and results go to stdout, so a script that reads `deploy.py`'s output never receives a question.

`--all` deploys every skill except opt-in ones, which only some users need, such as the `dotnet` bundle for C#; add one with `--include`, as in `python deploy.py --all --include dotnet`, or choose it from the menu. Once chosen, an opt-in skill or bundle stays installed on later `--all` runs, also when a later version of the source renames the bundle or moves a skill out of it, and a bundle one of whose members you chose, on its own or through another bundle, is completed by them. An opt-in skill installed only because another skill depends on it does not count as chosen, so `--all` adds neither it nor its bundle.

Deployer configuration is stored under `~/.claude/deployer/config/`. Deployed authoritative skills and the ownership manifest are stored under `~/.claude/skills/`; generated thin runtime adapters for Codex and GitHub Copilot CLI are transactionally maintained under `~/.agents/skills/`. The code-review suite keeps its runtime-neutral configuration and state under `~/.coding-agent-skills/code-review/` by default. [Recovery](recovery.md#where-the-deployer-keeps-its-state) lists every path the deployer uses.

### Checking runtime discovery

If you use Codex CLI or Copilot CLI, check that each one finds the deployed skills. This starts no AI session and changes nothing:

```bash
python deploy.py verify
```

It reports each adapter as `FOUND`, `NOT FOUND`, `DISABLED`, or `SHADOWED` for every installed runtime, and skips a runtime that is not installed. A runtime may spell a directory differently from the deployer, such as by its 8.3 short name (`RUNNER~1` for `runneradmin`); `verify` compares the directories the paths resolve to, not their text. A same-named skill can shadow an adapter: Copilot CLI gives personal skills under `~/.copilot/skills/` precedence over the generated adapters, and Codex offers both copies. The deployer reports Copilot shadows when it deploys but never modifies the user-managed Copilot directory. See [Copilot support](copilot-support.md) and [Codex support](codex-support.md).

### Skipped items

If an unmanaged or locally modified destination differs from the rendered template, deployment skips it and lists it under `SKIPPED`, with a diff for unmanaged skills; a dry run lists the same items under `CONFLICT`. Review the difference before explicitly replacing only that item:

```bash
python deploy.py --all --force-item dotnet-format
```

`--force-item` takes the name the report shows; an agent may also be named without its `.md`, as `code-review-reviewer`. A name no selection from the source can install is a command-line error (exit 2), refused before taking the lock, recovering an interrupted deployment, claiming a canary home, or prompting for a selection, with the names the source accepts and a help hint. A known name must also match an item the selected run installs; otherwise it is refused before staging, with the names that selection accepts, without reporting a deployment failure or attempting failure recovery. Forced replacements are retained under `.backups/<run-id>/` in the item's root: `~/.claude/skills/` for skills and shared assets, `~/.agents/skills/` for runtime adapters, and `~/.claude/agents/` for agents.

### When a command fails

Every `deploy.py` command exits 0 when it succeeds, 1 when it fails, 2 when its command line is wrong (it names the `--help` to read), and 130 when it is cancelled, by Ctrl+C or by the end of input at a prompt. `python deploy.py --help` lists the deployment options and the `configure`, `check`, and `verify` commands, and each command takes `--help` too. It ends a failure with lines that say what failed, on which path, and what to do next, and prints no traceback. To see the traceback behind the message, add `--debug` to the command, or set the `DEPLOYER_DEBUG` environment variable to `1` for a run whose command line you do not control, such as the one `update-coding-agent-skills` starts. The traceback follows the message.

An unknown `--include` name or a refused `--canary-home` value is a command-line error. The deployer refuses it
before claiming the home, taking the lock, or recovering an interrupted deployment, leaving the home and any
pending recovery evidence untouched.

## Updating

`main` is the supported line, and releases are checkpoints on it, so an installation follows `main` rather than the latest release. From any runtime, start `update-coding-agent-skills`: `/update-coding-agent-skills` in Claude Code or Copilot CLI, `$update-coding-agent-skills` in Codex. It fast-forwards this clone to `origin/main` and redeploys, and it stops without changing anything if the clone has uncommitted changes or local commits. It also stops before a release that raises the major version (or the minor version, while the major is 0), since that release may ask you to act; read its notes, then start the skill again with `--cross-major` to apply it. Over a deployment pinned with `--at`, described below, it stops until you start it with `--unpin`. See [Versioning](releasing.md#versioning) for what each level means.

To update by hand, update the clone, preview the deployment, and then apply it:

```bash
git pull --ff-only
python deploy.py --all --dry-run
python deploy.py --all
```

The dry run and the deployment work from one plan, so the dry run shows, item by item, what the deployment with the same options will do, `--force` and `--force-item` included. It groups items by planned action, with anything that needs your attention, such as conflicts, first, and lists each group alphabetically. Installed items whose rendered content would not change are grouped under `UNCHANGED`, so the `UPDATE` group shows exactly what the update changes. A runtime adapter gets its own line, marked `(runtime adapter)`, only when it needs attention or does something its skill does not. The deployment itself ends with the same report, with the same reasons, in the past tense: `CONFLICT` becomes `SKIPPED`, `REPLACE` becomes `REPLACED`, `FRESH INSTALL` becomes `INSTALLED`, and so on, followed by the run ID and manifest path.

Deploy from the same clone each time: the manifest records it, and a deployment from another checkout refuses. If you move or re-clone the repository, deploy once from the new checkout with `--take-over-source`; see [Deploying from another checkout](recovery.md#deploying-from-another-checkout).

If an updated skill requires a new configuration value, the deployer stops and asks you to run `python deploy.py configure` first.

To deploy a release or an earlier commit instead of the clone's working tree, without moving the clone off `main`, add `--at` with a tag, branch, or commit the clone holds, as in `python deploy.py --all --at <tag>`; see [Deploying an earlier commit](recovery.md#deploying-an-earlier-commit). The installation then stays pinned: a deployment without `--at` refuses before it changes anything until you pass `--unpin`, and `update-coding-agent-skills` stops earlier with `PINNED <ref> <commit>` and changes nothing until you start it with `--unpin`. That run, or `python deploy.py --all --unpin`, moves the installation back to the clone's `HEAD` and reports that it replaced the pinned deployment.

## Uninstalling

Run `python deploy.py --dry-run`, answer `none` at the selection prompt to preview the removal, then run `python deploy.py` and answer `none` again to remove every unmodified skill, shared asset, runtime adapter, and agent deployed from this repository.

Locally modified deployment outputs are preserved and reported for manual review. A preserved skill keeps its recorded transitive skill dependencies and their shared assets and agents, and a shared asset another installed source still needs is also kept. A skill that stays, preserved or kept for another, keeps its runtime adapter, so Codex and Copilot CLI still find it; a modified adapter is preserved like any other item. These dependencies are reported as `KEEP` in the preview and `KEPT` in the deployment report; resolve the preserved skill or uninstall that source, then rerun this uninstall to remove them. The manifest records deployed direct skill dependencies as `skill_deps`, shared assets as `shared_deps`, and agents as `agent_deps`, so a source change cannot erase what a preserved copy needs.

The next deployment automatically upgrades an older supported manifest. A skill with no recorded `skill_deps` conservatively keeps the other skills its source owns and their assets and agents; a deployment that selects and installs that skill records its exact current dependencies. Those conservative dependencies only keep: an opt-in bundle or skill the older installation chose stays chosen, by its request or as an installed skill, through a renamed bundle or a migration alike, and also after a deployment that recorded dependencies but not bundle members left such a skill in place, preserved or skipped, beside the skills it installed, unless that installation requested no skill on its own. Current metadata cannot establish the dependencies of an older preserved copy. A skill from an installation that did not record `agent_deps` still keeps no agent until a deployment selects and installs it, unless the agent itself was modified. The migration never recreates files an earlier deployment already removed; redeploy the source to restore missing dependencies.

Configuration and retained backups are not deleted, and content owned by other sources is not affected. The manifest keeps this source's entry, with no items and the checkout it was deployed from, so a later deployment from another clone needs `--take-over-source`; see [Deploying from another checkout](recovery.md#deploying-from-another-checkout).
