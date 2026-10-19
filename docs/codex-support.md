# Codex support

## Supported surfaces

Codex CLI finds each deployed public skill through its generated adapter under `~/.agents/skills/<name>`, which points to the authoritative skill under `~/.claude/skills/<name>`. The adapter carries the skill's description, a user-only skill's included, and, for a user-only skill, an `agents/openai.yaml` that turns off implicit invocation. Start a skill explicitly with `$<skill>`, or let Codex choose it when a request matches its description. See [Skills](skills.md) for each skill's arguments.

## Checking discovery

After deploying, check that Codex finds every adapter without starting a model:

```bash
python deploy.py verify
```

Codex CLI has no command that lists skills, so the verifier starts `codex app-server` from an empty directory and sends it a `skills/list` request. The app server answers without a sign-in and lists every copy of each skill it finds, user-only skills included, and each skill file it could not load, with its reason. Codex marks the app server experimental, so a Codex release may change that answer; the weekly run of the [`deployable` workflow](../.github/workflows/deployable.yml) on the latest Codex is what notices. An adapter Codex found but could not load is `NOT FOUND` with Codex's reason, such as ``could not load it: missing field `description` ``. An adapter is `FOUND` when Codex lists it, enabled, and lists no other enabled skill of the same name. A same-named skill elsewhere, such as under `~/.codex/skills`, is reported as `SHADOWED`, because Codex offers both and does not merge them. Codex CLI before 0.88.0 does not say whether a skill is enabled, so the verifier reports an older one as `OUTDATED` without starting it. The same command checks Copilot CLI when it is installed.

On Windows, Codex looks for personal skills in your Windows profile folder, whatever `HOME` or `USERPROFILE` says. The deployer therefore installs into the profile folder that `USERPROFILE` names even when `HOME` points somewhere else, as it does by default in MSYS2. A deployment, a dry run included, and `python deploy.py check` name that folder on their `Home:` line and warn when `HOME` differs. If `USERPROFILE` itself has been changed, the adapters land where Codex never looks, and the verifier reports them as `NOT FOUND`.

On Linux, Codex looks for personal skills under `HOME`, where the deployer installs, so the same check, run as `python3 deploy.py verify`, reads the same adapters; the [`deployable` workflow](../.github/workflows/deployable.yml) runs it on a fresh Ubuntu runner as it does on a fresh Windows one.

`codex debug prompt-input` also lists skills, but only those the model may choose for itself, so it leaves out user-only skills such as `update-coding-agent-skills`, whose adapter's `agents/openai.yaml` asks Codex never to offer them to the model. The verifier reads it, without a sign-in or a model, whenever a user-only adapter is deployed, and reports a user-only adapter it lists as `EXPOSED`, which fails the check.

## Running skill scripts

The skills' scripts run in Codex's sandbox. On Windows, Codex CLI cannot run them until the two machine-level settings below are in place, and on every platform a skill that reaches the network needs the [network access](#network-access) setting. On Linux neither applies, since both are Windows settings, and the skills' commands name Python `python3`, as Linux distributions do. The runtime canary has not yet run a skill in Codex on Linux, so a script Codex refuses there is worth reporting. Claude Code and Copilot CLI need neither. The Codex desktop app sets up its own sandbox, which the CLI does not inherit, so a skill that works in the app can still fail in `codex` or `codex exec`.

## Windows sandbox mode

Without a Windows sandbox setting in `~/.codex/config.toml`, `codex exec` uses a read-only sandbox even when given `--sandbox workspace-write`. With approvals turned off, which is how `codex exec` runs, Codex then refuses every shell command, even a `Get-Content` of the skill file, with a message such as:

```text
exec_command failed: CreateProcess ... Rejected(...) blocked by policy
```

Codex's Windows sandbox has the implementations that OpenAI's [Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox) page describes. Set the first one your machine allows:

- **MXC** is the one OpenAI recommends on compatible Windows devices. It uses Windows' own process isolation and needs no administrator-approved setup, no extra Windows accounts, and no firewall rules, but it needs a Windows 11 build recent enough, and capabilities Microsoft is still rolling out, as that page lists. `prefer_mxc` uses it where the device offers it, and the `[windows] sandbox` value below where it does not; `sandbox = "mxc"` instead fails where MXC is unavailable.
- **Elevated** is OpenAI's fallback. It runs commands as dedicated lower-privilege Windows users, which a one-time setup creates with an administrator's approval: Codex asks for it, through a Windows administrator prompt, the first time it needs the sandbox. On a managed machine where you are not an administrator, IT runs that setup for your account with `codex sandbox setup --elevated --user '<domain>\<user>' --codex-home '<your .codex folder>'`, which also saves the setting.
- **Unelevated** is a weaker legacy fallback, for a machine whose policy allows neither of the others.

For MXC with the elevated fallback, add this to `~/.codex/config.toml`:

```toml
[features]
prefer_mxc = true
[windows]
sandbox = "elevated"
```

Confirm that the sandbox starts a command, without starting a model, from any folder:

```powershell
codex sandbox --include-managed-config --permission-profile :workspace -- python -c "print(42)"
```

It prints `42`. A failure names the implementation that could not start, such as `MXC launcher: failed to create the process security environment`, so set the next one; `The file cannot be accessed by the system` means `python` is an app execution alias (below). The first run of the elevated sandbox may ask for the administrator's approval of its setup.

`python deploy.py check` reports the mode Codex would use, without calling a model, whenever Codex CLI is installed on Windows and meets its version floor. Its `codex sandbox` line is `FOUND` with `workspace-write` when the setting is read, and `UNUSABLE` while it is missing, naming the mode Codex would use instead, usually `read-only`, and this section. It reads the mode from `codex debug prompt-input -c sandbox_mode=workspace-write`, which prints the instructions Codex would send, including a line saying which `sandbox_mode` is in effect. It reports the configured mode only: it does not run the command above, because a first run of the elevated sandbox asks for an administrator's approval, which a check must not do, so a `FOUND` line still leaves that command to confirm that the sandbox starts.

The [runtime canary](../tools/runtime_canary.py) starts Codex with `--ignore-user-config`, so it does not read this setting. It passes the value on the command line instead, as `-c windows.sandbox=elevated`, which Codex applies even when it ignores `config.toml`, and prints it as `SUPPLIED codex "windows.sandbox=elevated"`. Use `--codex-sandbox mxc` or `--codex-sandbox unelevated` to run the canary on another implementation. It also sets `-c approval_policy=never` beside `--sandbox workspace-write`, as OpenAI asks of a non-interactive run. If Codex still refuses every command, the canary's `BLOCKED` line names the mode it used and points here.

## Subagents and hooks

Codex starts subagents with its `spawn_agent` tool, and a custom agent is a TOML file under `~/.codex/agents/` or a project's `.codex/agents/` that names its own `model` and `model_reasoning_effort`. `spawn_agent` also takes a `model` and a `reasoning_effort` for one subagent when a skill's instructions ask for them, with `fork_turns` set to `none`, since a subagent that inherits the whole conversation inherits its model and effort too. The code reviews use this: on Codex, `prepare` prints an `EFFORT <selector> <id> <effort>` line after a role whose specialist or configuration sets a reviewer effort, and the session starts that role's subagent at it. Codex takes its own model identifiers rather than the Claude aliases a reviewer profile names, so a reviewer still starts on the session's model, and `prepare` prints a `NOTE` for a role that asks for another.

Codex also runs hooks, on by default, and a `PreToolUse` hook can deny a shell, `apply_patch`, or MCP call. A hook that fails, times out, or returns output Codex does not support lets the call through, though, and a hook that is not managed runs only once the user trusts it with `/hooks`. The reviewer guard is a `PreToolUse` hook, and Claude Code's reviewer agent blocks the call when its guard fails or times out, so a reviewer there makes no call its guard has not allowed. Under Codex the guard would hold only while it runs, which is not the guard the [code-review contract](code-review-operations-contract.md#threat-model) promises, and an agent file's `sandbox_mode` and `approval_policy` do not override the session's, so an agent file cannot make a reviewer read-only either. So a Codex reviewer starts as a native subagent without the reviewer agent's guard, its prompt its only boundary, until Codex blocks a call when its hook fails; [Runtime support](skills.md#runtime-support) names the capability the reviews lack as `fail-closed-agent-hooks`, and the decision is in [Design](design.md).

These are the pages this was read from, beside the spawn tool's own instructions, which `codex debug prompt-input` prints without a model:

- [Hooks](https://learn.chatgpt.com/docs/hooks), where https://developers.openai.com/codex/hooks now leads
- [Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents), where https://developers.openai.com/codex/subagents now leads

## Network access

Codex's sandbox gives commands no network by default, on every platform ([Sandboxing](https://learn.chatgpt.com/docs/sandboxing)). A skill that calls GitHub through `gh`, or fetches with Git, therefore cannot reach it from the sandbox: in an interactive session Codex asks you to approve each such command to run outside the sandbox, and `codex exec`, which asks for no approval, fails it. The [Runtime support](skills.md#runtime-support) table marks these skills partial on Codex, lacking `sandbox-network`. To let their commands reach the network from the sandbox, add this to `~/.codex/config.toml`:

```toml
[sandbox_workspace_write]
network_access = true
```

`codex debug prompt-input -c sandbox_mode=workspace-write` says `Network access is enabled` once the setting is read, and `Network access is restricted` before. To confirm that `gh` reaches GitHub from the sandbox with your sign-in, without starting a model:

```powershell
codex sandbox -c sandbox_mode=workspace-write -- gh api rate_limit --jq .rate.limit
```

It prints your hourly API limit; a `proxyconnect` error means the network is still off. With the unelevated sandbox, the commands run as you, so `gh` finds your sign-in. With the elevated sandbox, they run as a separate Windows user, which may not find a sign-in stored for your account; run the command above to see.

## Python app execution aliases

Windows registers `python.exe` and `python3.exe` as app execution aliases: 0-byte reparse points in `%LOCALAPPDATA%\Microsoft\WindowsApps`. Every Windows install has them as Microsoft Store stubs, whichever Python installer you use, and the [Python install manager](https://docs.python.org/3/using/windows.html) replaces them with its own. Codex's sandbox cannot start either kind, and every skill script that runs `python` fails with:

```text
Program 'python.exe' failed to run: The file cannot be accessed by the system
```

Turn off those two aliases under **Settings > Apps > Advanced app settings > App execution aliases**, so that `python` resolves to a real executable: the install manager's launcher in `%LOCALAPPDATA%\Python\bin`, or the `python.exe` the classic installer put on your `PATH`. With the install manager, keep its `py` and `pymanager` aliases on. Check the result with `python deploy.py check` in a new terminal: its `python command` line is `FOUND` with the program `python` runs, your Python installation, and `UNUSABLE` while that is an alias in `WindowsApps`, naming the alias and this section.

Moving your Python installation ahead of `WindowsApps` in your user `PATH` is not always enough. Some installers also add `WindowsApps` to the system `PATH`, which Windows searches before the user `PATH`, so the aliases still win. Turning the aliases off works regardless of `PATH` order.
