# Installation

The two routes the skills install by, the deployer and a Claude Code plugin; what the deployer needs, how to install it, and how to deploy, update, and uninstall the skills; and how to install, update, and remove the plugin. The [README](../README.md#quick-start) has the short version, and [Setting up a machine](setting-up-a-machine.md) walks a new Windows machine through these tools, the runtimes, and the ones only validation needs, in order.

## Choosing a route

The same skills install by either route, and both are supported for the long term. Each serves people the other does not.

| | The deployer | The Claude Code plugin |
|---|---|---|
| For | Managed machines, Codex and GitHub Copilot CLI, and anyone who wants files they can read | Claude Code users whose organization lets them install a plugin |
| Runtimes | Claude Code, Codex, and Copilot CLI | Claude Code only |
| Installs | Into `~/.claude/skills`, with runtime adapters under `~/.agents/skills` | Into Claude Code's plugin cache, under `~/.claude/plugins` |
| Starts a skill as | `/<skill>` | `/coding-agent-skills:<skill>` |
| Follows | Releases, or `main` on request | Releases only |
| Gives | Journaled rollback and recovery, an ownership manifest that refuses to replace another source's skill, a permanent backup of a skill you changed, configured values checked against an allowlist, `check` and `verify` | One install command and Claude Code's own updates, and a namespace that lets suites with skills of the same names coexist |
| Carries | Every skill | Every skill but `update-coding-agent-skills`; the **Plugin** column of the [summary](skills.md#summary) says which plugin carries each |

Some organizations block plugin installation or restrict marketplaces; the deployer is the route there. Two managed settings in Claude Code's [settings reference](https://code.claude.com/docs/en/settings-reference) take from the deployer route in Claude Code, so a managed machine's owner should check for them before choosing it. Under `allowManagedPermissionRulesOnly`, Claude Code ignores the `allowed-tools` of skills in `~/.claude/skills`, so every script a deployed skill runs asks first, and a non-interactive `-p` run is denied it. Under `strictPluginOnlyCustomization`, its `skills` lock stops Claude Code loading skills from `~/.claude/skills` and its `agents` lock stops it loading agents from `~/.claude/agents`, so the deployed skills, or the agents they start, such as the code-review reviewer, do not load at all; plugins from a marketplace keep loading under it. Codex and Copilot CLI read neither setting. [The Claude Code plugin](#the-claude-code-plugin) describes the plugin route, and the rest of this page the deployer's.

## Requirements

Every deployment needs:

| Tool | Why |
|---|---|
| Python 3.11.4 or newer | Runs the deployer. The deployer and the `audit-ai-config` engine use only the standard library. |
| Git for Windows, or on Linux Git and Bash | The Bash, Git for Windows' on Windows and the system's on Linux, checks the syntax of rendered Bash scripts and Bash blocks in Markdown; several skills run Bash commands. |
| ShellCheck | Lints the same rendered Bash content. Deployment stops if it is missing. CI uses 0.9.0 and checks the latest release weekly; newer releases also work. |
| PowerShell | Parses rendered `.ps1` files regardless of extension case. Windows PowerShell 5.1 is enough for deployment on Windows; Linux and the validation suite need PowerShell 7 (`pwsh`), and GitHub Copilot CLI on Windows needs PowerShell 6 or newer. |

Some skills need more when you use them, not when you deploy them:

| Tool | Needed by |
|---|---|
| GitHub CLI (`gh`) 2.48.0 or newer, signed in with `gh auth login` | The code-review operations bundle, `github-activity-report`, and `repo-cleanup`; `dotnet-format` uses it, when present, to find a pull request's base branch |
| Claude Code (optional) | Running the skills from Claude Code; the deployer itself does not need it |
| Codex CLI 0.88.0 or newer (optional) | Verifying Codex skill discovery |
| GitHub Copilot CLI 1.0.88 or newer (optional) | Verifying Copilot skill discovery, and the bounded Copilot code-review host |
| .NET SDK 9.0.200 or newer, whose `dotnet format` formats a `.slnx` solution or a `.sln` of SDK-style projects; the deprecated `dotnet-format` global tool only for any other `.sln` | `dotnet-format`, which checks for them and reports the install command; `dotnet-dependencies` uses the SDK, when present, for `dotnet nuget why` |
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
| PowerShell 7 | `Microsoft.PowerShell --installer-type wix` | `pwsh` | `pwsh` | [GitHub releases](https://github.com/PowerShell/PowerShell/releases), the `.msi` |
| GitHub CLI | `GitHub.cli` | `gh` | `gh` | [cli.github.com](https://cli.github.com/) |
| Claude Code | none | none | none | `npm install -g @anthropic-ai/claude-code` with Node.js |
| Codex CLI | `OpenAI.Codex`, which can trail the npm release | none | none | `npm install -g @openai/codex` with Node.js, or [GitHub releases](https://github.com/openai/codex/releases) |
| GitHub Copilot CLI | `GitHub.Copilot` | `github-copilot-cli` (community-maintained) | `copilot-cli` | `npm install -g @github/copilot` with Node.js 22 or newer, or [GitHub releases](https://github.com/github/copilot-cli/releases); on Windows it needs PowerShell 6 or newer, such as the PowerShell 7 above ([Installing GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli)); the code-review host needs WinGet's or the release's program ([Copilot support](copilot-support.md#bounded-code-review-host)) |

Install PowerShell 7's MSI build, as the table's `--installer-type wix` does: WinGet's default is now the packaged (MSIX) PowerShell, whose `pwsh` is an app execution alias that Codex CLI's sandbox cannot start, which stops every command Codex runs ([PowerShell app execution alias](codex-support.md#powershell-app-execution-alias)).

The deployer finds Git Bash in `C:\Program Files\Git`, or next to the `git` on your `PATH`, which covers Scoop and per-user Git installs. It never uses another `bash`, such as WSL's. If Git isn't on your `PATH`, set `GIT_BASH` to the absolute path of its `bin\bash.exe`. A `GIT_BASH` or `PROGRAMFILES` that is not an absolute path is ignored, so Git Bash is never found relative to the folder you run from.

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
| GitHub Copilot CLI | none | none | `npm install -g @github/copilot` with Node.js 22 or newer, `brew install --cask copilot-cli` with Homebrew, the install script `https://gh.io/copilot-install`, or [GitHub releases](https://github.com/github/copilot-cli/releases) ([Installing GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli)) |
| `pdftotext` | none: the archive's `poppler-utils` is below Poppler's floor | none, for the same reason | the [Xpdf command-line tools](https://www.xpdfreader.com/download.html), as [Requirements](#requirements) describes |

On a release whose `python3` is older than the deployer's floor, `check` says so; install a newer Python from python.org or with a version manager. On Linux the deployer uses the first `bash` and `pwsh` on your `PATH`, your home is `HOME`, and a configured directory is an absolute path such as `/home/you/GitHub`. Linux distributions name Python `python3`, so run every command on this page as `python3`, as in `python3 deploy.py check`; the skills' own commands already name it so there.

### After installing

**After installing a tool, open a new terminal.** Windows only updates `PATH` for programs started after the installation. An editor or agent application that was already running, including one minimized to the system tray, must be fully quit and restarted before its terminals can find the new tool. Run `python deploy.py check` to see what is installed. The validation suite checks for its tools before running any test, and the deployer checks before changing any file; both list every missing tool with its install command. Deploying also warns when a selected skill uses a tool that is not installed.

## Codex CLI settings

Codex CLI on Windows needs two machine settings before it can run a skill's scripts; on Linux it needs neither. Until both are in place, every skill script fails in Codex, with `blocked by policy` or `The file cannot be accessed by the system`:

- add a Windows sandbox setting to `~/.codex/config.toml`: MXC, which OpenAI recommends, where the machine offers it, and the elevated sandbox as its fallback, whose one-time setup an administrator approves, as [Windows sandbox mode](codex-support.md#windows-sandbox-mode) shows; and
- turn off the `python.exe` and `python3.exe` app execution aliases.

`python deploy.py check` reports both on Windows: a `python command` line, `UNUSABLE` while `python` runs an app execution alias, and, when Codex CLI is installed, a `codex sandbox` line, `UNUSABLE` while Codex would run skill scripts in a read-only sandbox. Each names the section to read, and both are `FOUND` once the settings are in place. Where `pwsh` is on `PATH`, a `pwsh command` line is `UNUSABLE` while it is the packaged PowerShell's alias, which stops every command Codex runs, as [PowerShell app execution alias](codex-support.md#powershell-app-execution-alias) shows. The `codex sandbox` line reports the configured mode, not whether the sandbox starts a command; [Windows sandbox mode](codex-support.md#windows-sandbox-mode) gives the command that confirms it without starting a model.

On every platform, Codex's sandbox gives commands no network by default, so a skill that calls GitHub through `gh` or fetches with Git needs approval for each such command in an interactive session, and fails in `codex exec`, until `~/.codex/config.toml` turns network access on, as [Network access](codex-support.md#network-access) shows.

Windows registers `python.exe` and `python3.exe` as app execution aliases in `%LOCALAPPDATA%\Microsoft\WindowsApps`: every Windows install has them as Microsoft Store stubs, and the Python install manager registers its own. Codex CLI's sandbox cannot start those aliases, so turn them off under **Settings > Apps > Advanced app settings > App execution aliases** whichever Python installer you used, and keep `py` and `pymanager` on if they are listed. Reordering your user `PATH` is not always enough, because `WindowsApps` can also be on the system `PATH`, which Windows searches first. See [Python app execution aliases](codex-support.md#python-app-execution-aliases).

## Deploying

Clone the repository and run every command from its directory, in PowerShell or Git Bash on Windows, or in any shell on Linux with `python3` in place of `python`:

```bash
python deploy.py check
python deploy.py configure
python deploy.py --all --dry-run
python deploy.py --all
```

`check` lists every tool the deployer and the skills need, with its version and which skills use it, and changes nothing. It also lists Git and Claude Code with their versions, so its output is what a bug report asks for, and reports a Claude Code older than its floor in [Dependency updates](dependency-updates.md#runtime-tool-floors) as `OUTDATED`, because the code-review reviewer's guard needs that release to deny a call when the guard cannot run. A tool with a floor whose `--version` fails or prints no version is `UNUSABLE`, naming what its version probe did, until that probe reads a version. Its `Deployed commit` line names the commit the skills were last deployed from, with the nearest tag when Git can describe it, such as `v0.1.0-14-g71ad815 (<full hash>)`, the full hash alone when there is no checkout or Git to ask or the checkout no longer holds the commit, or `unknown` before the first deployment that records one; when Git fails to describe it for any other reason, `check` stops and names Git's reason. A deployment from a Git checkout whose commit Git cannot read, because Git is missing or fails, refuses before it changes anything and names Git's reason, so it never replaces the recorded commit with none. `deploy.py` is the only script you run to deploy; run `python deploy.py --help` for every option.

`configure` prompts for the root directory containing your local repositories, and for `UPDATE_CHANNEL`, what [Updating](#updating) deploys: `release`, the default, or `main`. It also asks for `REPO_CLEANUP_REGENERABLE` and `REPO_CLEANUP_PROTECTED`, what `repo-cleanup` adds to the directories it may delete with a worktree and to the branch prefixes it never deletes; leave both empty to keep its shipped lists, which [`repo-cleanup`](skills.md#repo-cleanup) describes with how to extend them. Rerun it to change a value, or add `--reset` to start from an empty configuration. At any prompt, Enter keeps the current value and Ctrl+C cancels without saving. Configured paths must be absolute paths of directories that already exist: drive-letter paths on Windows, and paths from the root, such as `/home/you/GitHub`, on Linux. On Windows `configure` accepts them as you would type or paste them, such as `C:\GitHub` or `"C:\GitHub\"`, and stores them with forward slashes, such as `C:/GitHub`.

Every prompt, `configure`'s and the deployment's selection menu alike, goes to stderr, and results go to stdout, so a script that reads `deploy.py`'s output never receives a question.

`--all` deploys every skill except opt-in ones, which only some users need, such as the `dotnet` bundle for C#; add one with `--include`, as in `python deploy.py --all --include dotnet`, or choose it from the menu. Once chosen, an opt-in skill or bundle stays installed on later `--all` runs, also when a later version of the source renames the bundle or moves a skill out of it, and a bundle one of whose members you chose, on its own or through another bundle, is completed by them. An opt-in skill installed only because another skill depends on it does not count as chosen, so `--all` adds neither it nor its bundle, except once after updating the older installation [Updating](#updating) names.

Deployer configuration is stored under `~/.claude/deployer/config/`. Deployed authoritative skills and the ownership manifest are stored under `~/.claude/skills/`; generated thin runtime adapters for Codex and GitHub Copilot CLI are transactionally maintained under `~/.agents/skills/`. A deployed skill holds every file of its source directory but its regression suites, which run only in this repository's validation. The code-review suite keeps its runtime-neutral configuration and state under `~/.coding-agent-skills/code-review/` by default. [Recovery](recovery.md#where-the-deployer-keeps-its-state) lists every path the deployer uses.

### Checking runtime discovery

If you use Codex CLI or Copilot CLI, check that each one finds the deployed skills. This starts no AI session and changes nothing:

```bash
python deploy.py verify
```

It reports each adapter as `FOUND`, `NOT FOUND` (with the runtime's reason when it found the adapter but could not load it), `DISABLED`, or `SHADOWED` for every installed runtime, or, for Codex, `EXPOSED` for a user-only adapter Codex offers the model anyway and `OMITTED` for another adapter Codex lists but its skills budget leaves out of the model's catalog. It skips a runtime that is not installed. With neither installed it verifies nothing and exits 1. A runtime may spell a directory differently from the deployer, such as by its 8.3 short name (`RUNNER~1` for `runneradmin`); `verify` compares the directories the paths resolve to, not their text. A same-named skill can shadow an adapter: Copilot CLI gives personal skills under `~/.copilot/skills/` precedence over the generated adapters, and Codex offers both copies. With `COPILOT_HOME` naming another directory, Copilot reads none of the adapters, which `verify` names. The deployer reports Copilot shadows when it deploys but never modifies the user-managed Copilot directory. See [Copilot support](copilot-support.md) and [Codex support](codex-support.md).

### Skipped items

If an unmanaged or locally modified destination differs from the rendered template, deployment skips it and lists it under `SKIPPED`, with a diff for unmanaged skills; a dry run lists the same items under `CONFLICT`. Review the difference before explicitly replacing only that item:

```bash
python deploy.py --all --force-item dotnet-format
```

`--force-item` takes the name the report shows; an agent may also be named without its `.md`, as `code-review-reviewer`. A name no selection from the source can install is a command-line error (exit 2), refused before taking the lock, recovering an interrupted deployment, claiming a canary home, or prompting for a selection, with the names the source accepts and a help hint. A known name must also match an item the selected run installs; otherwise it is refused before staging, with the names that selection accepts, without reporting a deployment failure or attempting failure recovery. Forced replacements are retained under `.backups/<run-id>/` in the item's root: `~/.claude/skills/` for skills and shared assets, `~/.agents/skills/` for runtime adapters, and `~/.claude/agents/` for agents.

A destination of the wrong type, such as a file where a skill's directory goes or a directory where an agent's `.md` file goes, is also skipped, with the reason `destination is not a directory` or `destination is not a file`. `--force` and `--force-item` do not replace it, and the report gives no force hint for it. Move the file or directory out of the root, then deploy again.

### When a command fails

Every `deploy.py` command exits 0 when it succeeds, 1 when it fails, 2 when its command line is wrong (it names the `--help` to read), and 130 when it is cancelled, by Ctrl+C or by the end of input at a prompt; at the end of input it first names what the prompt waited for, such as the `configure` variable that piped answers left out. `python deploy.py --help` lists the deployment options and the `configure`, `check`, and `verify` commands, and each command takes `--help` too. It ends a failure with lines that say what failed, on which path, and what to do next, and prints no traceback. `check` exits 1 when a tool needed to deploy is missing or outdated, after printing its report and what to install, and 0 otherwise, also when a tool only a skill uses is missing, outdated, or unusable. `verify` exits 1 when an installed runtime fails verification, and also when neither Codex CLI nor Copilot CLI is installed, since then nothing was verified. To see the traceback behind the message, add `--debug` to the command, or set the `DEPLOYER_DEBUG` environment variable to `1` for a run whose command line you do not control, such as the one `update-coding-agent-skills` starts. The traceback follows the message.

An unknown `--include` name or a refused `--canary-home` value is a command-line error. The deployer refuses it
before claiming the home, taking the lock, or recovering an interrupted deployment, leaving the home and any
pending recovery evidence untouched.

## Updating

An installation follows releases: fixes land on `main` and reach users at the next release tag, since `main` between two tags carries changes the release audit has not yet read. From any runtime, start `update-coding-agent-skills`: `/update-coding-agent-skills` in Claude Code or Copilot CLI, `$update-coding-agent-skills` in Codex. It fast-forwards this clone to `origin/main`, leaving it on `main`, and deploys the latest release tag `origin/main` holds, a pre-release included, with `--at`, described below; it stops without changing anything if the clone has uncommitted changes or local commits. Following `main` itself is the opt-in: set `UPDATE_CHANNEL` to `main` with `python deploy.py configure`, or start the skill with `--channel main` for one run, and it redeploys the clone at `origin/main` instead. It also stops before a release that raises the major version (or the minor version, while the major is 0), since that release may ask you to act; read its notes, then start the skill again with `--cross-major` to apply it. Over a deployment you pinned with `--at`, described below, it stops until you start it with `--unpin`; the pin it makes itself, at a release, moves to the next release. See [Versioning](releasing.md#versioning) for what each level means.

To update by hand, update the clone, preview the deployment, and then apply it:

```bash
git pull --ff-only
python deploy.py --all --dry-run
python deploy.py --all
```

Read the dry run before you apply it. A chosen opt-in bundle stays chosen when the source renames it or moves a skill out of it. An installation last deployed by a release that did not record bundle members does not record what its bundle requests held, so the first `--all` after updating it keeps every installed skill the source still offers, as the manifest-upgrade paragraph under [Uninstalling](#uninstalling) explains. A skill that installation holds only because another skill depended on it, which the source no longer has that skill depend on, then counts as chosen, and the dry run installs the rest of its opt-in bundle; if that is a bundle you never chose, deploy from the menu of `python deploy.py` instead and leave the bundle out.

The dry run and the deployment work from one plan, so the dry run shows, item by item, what the deployment with the same options will do, `--force` and `--force-item` included. It groups items by planned action, with anything that needs your attention, such as conflicts, first, and lists each group alphabetically. Installed items whose rendered content would not change are grouped under `UNCHANGED`, so the `UPDATE` group shows exactly what the update changes, and the deployment leaves them as they are: it neither moves nor rewrites them, so the caches their scripts made stay, and one a terminal or an editor holds open does not stop the run. A runtime adapter gets its own line, marked `(runtime adapter)`, only when it needs attention or does something its skill does not. The deployment itself ends with the same report, with the same reasons, in the past tense: `CONFLICT` becomes `SKIPPED`, `REPLACE` becomes `REPLACED`, `FRESH INSTALL` becomes `INSTALLED`, and so on, followed by the run ID and manifest path.

Deploy from the same clone each time: the manifest records it, and a deployment from another checkout refuses. If you move or re-clone the repository, deploy once from the new checkout with `--take-over-source`; see [Deploying from another checkout](recovery.md#deploying-from-another-checkout).

If an updated skill requires a new configuration value, the deployer stops and asks you to run `python deploy.py configure` first.

To deploy a release or an earlier commit instead of the clone's working tree, without moving the clone off `main`, add `--at` with a tag, branch, or commit the clone holds, as in `python deploy.py --all --at <tag>`; see [Deploying an earlier commit](recovery.md#deploying-an-earlier-commit). The installation then stays pinned: another deployment, with or without `--at`, refuses before it changes anything until you pass `--unpin`, and `update-coding-agent-skills` stops with `PINNED <ref> <commit>` and deploys nothing until you start it with `--unpin`, unless your pin is already at the release it would deploy. The skill's own deployment of a release is a pin too, made with the tag's full name, `--at refs/tags/<tag>`, which the skill moves to each later release and the main channel replaces. Pinning its commit by hand, with the tag's short name or the commit, makes the pin yours. An update started with `--unpin` moves the installation to what its channel deploys, and `python deploy.py --all --unpin` back to the clone's `HEAD`; each reports that it replaced the pinned deployment.

## The Claude Code plugin

The repository is a Claude Code marketplace, `ejryan89`, offering two plugins, built from each release's skills: `coding-agent-skills`, every skill the deployer installs by default that a plugin can carry, and `coding-agent-skills-dotnet`, the opt-in `dotnet` bundle. In Claude Code:

```text
/plugin marketplace add EJRyan89/coding-agent-skills
/plugin install coding-agent-skills@ejryan89
```

From a shell, `claude plugin marketplace add EJRyan89/coding-agent-skills` and `claude plugin install coding-agent-skills@ejryan89 --config "repos_root=<folder>"` do the same; install `coding-agent-skills-dotnet@ejryan89` the same way. `coding-agent-skills` asks for one option, `repos_root`, the absolute path of the folder holding your git repositories, which `repo-cleanup` sweeps: `/plugin install` asks for it, and `claude plugin configure coding-agent-skills@ejryan89` shows or changes it. The plugin uses it as typed, without the allowlist the deployer holds `REPOS_ROOT` to, and `repo-cleanup` refuses an empty or relative one. Start a skill with the plugin's name first, such as `/coding-agent-skills:review-prs`, which keeps it apart from a same-named skill of another suite. The plugin's skills run `python` by that name on every platform, so `python` must start a Python as new as [Requirements](#requirements) names (on Linux, through a package such as `python-is-python3`), and each skill needs the tools its row of the [summary](skills.md#summary) lists, installed as [Installing the tools](#installing-the-tools) says.

Update with `claude plugin update coding-agent-skills@ejryan89`, or from `/plugin`; Claude Code updates a plugin from this marketplace by itself only when you turn on its auto-update. Each release's plugin carries that release's version, so an update brings the next release, and its notes are in [Upgrade notes](upgrade-notes.md). Remove it with `claude plugin uninstall coding-agent-skills@ejryan89`. `update-coding-agent-skills` updates a deployer's clone and does nothing for a plugin install, which is why the plugin does not carry it.

What a plugin install does not give:

- **The reviewer guard runs in every session.** The code-review skills' reviewer runs under a hook that keeps it to its own review run. Claude Code ignores a plugin agent's own hooks, so the plugin's hooks file runs the guard on every Read, Grep, Glob, Write, Edit, and Bash call of every session while the plugin is enabled, and the guard passes every call but the reviewer's. That costs a Python start per call. A missing or broken `python` blocks those calls until the plugin is disabled, as the guard fails closed. An organization setting that admits only managed hooks turns the guard off, and the reviewers then run without it.
- **The deployer's guarantees.** There is no journal, recovery, ownership manifest, or permanent backup: Claude Code replaces the plugin's copy at each update, and a change made inside that copy is lost. `deploy.py check` and `verify` do not see a plugin install.
- **`update-coding-agent-skills`.** It needs a deployer's clone, so it comes only through the deployer.
- **Other runtimes and `main`.** A plugin serves Claude Code alone and follows releases only.

A machine may hold both routes. The plugin's copy lives in Claude Code's plugin cache, which the deployer never reads or writes, so neither route refuses the other, and Claude Code lists both copies, `/review-prs` and `/coding-agent-skills:review-prs`, each with its own reviewer agent. For Claude Code, choose one route per machine unless you want both.

## Uninstalling

Run `python deploy.py --uninstall --dry-run` to preview the removal, then run `python deploy.py --uninstall` to remove every unmodified skill, shared asset, runtime adapter, and agent deployed from this repository. `--uninstall` selects nothing, as answering `none` at the selection prompt does, and shows no prompt, so a script runs it without piping an answer.

Locally modified deployment outputs are preserved and reported for manual review. So is a destination of the wrong type, with the reason `destination is not a directory` or `destination is not a file`; like a modified output it is never removed, so move it out of the root yourself. A preserved skill keeps its recorded transitive skill dependencies and their shared assets and agents, and a shared asset another installed source still needs is also kept. A skill that stays, preserved or kept for another, keeps its runtime adapter, so Codex and Copilot CLI still find it; a modified adapter is preserved like any other item. These dependencies are reported as `KEEP` in the preview and `KEPT` in the deployment report; resolve the preserved skill or uninstall that source, then rerun this uninstall to remove them. The manifest records deployed direct skill dependencies as `skill_deps`, shared assets as `shared_deps`, and agents as `agent_deps`, so a source change cannot erase what a preserved copy needs.

The next deployment automatically upgrades an older supported manifest. A skill with no recorded `skill_deps` conservatively keeps the other skills its source owns and their assets and agents; a deployment that selects and installs that skill records its exact current dependencies. Those conservative dependencies only keep, and they never decide what the older installation chose. An installation that does not record the members of its bundle requests counts as chosen each installed skill that no other installed skill depends on by a dependency both the installation records and the source still declares, so an opt-in bundle or skill it chose stays chosen, by its request or as an installed skill, through a renamed bundle, a moved member, or a migration alike, also after a deployment that recorded dependencies but not bundle members left a skill in place, and the first `--all` after updating removes no installed skill the source still offers. A skill installed only as another's dependency, which the source no longer has depend on it, counts as chosen too, as [Updating](#updating) shows. Current metadata cannot establish the dependencies of an older preserved copy. A skill from an installation that did not record `agent_deps` still keeps no agent until a deployment selects and installs it, unless the agent itself was modified. The migration never recreates files an earlier deployment already removed; redeploy the source to restore missing dependencies.

Configuration and retained backups are not deleted, and content owned by other sources is not affected. The manifest keeps this source's entry, with no items and the checkout it was deployed from, so a later deployment from another clone needs `--take-over-source`; see [Deploying from another checkout](recovery.md#deploying-from-another-checkout).
