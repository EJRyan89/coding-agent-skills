# Codex support

## Supported surfaces

Codex CLI finds each deployed public skill through its generated adapter under `~/.agents/skills/<name>`, which points to the authoritative skill under `~/.claude/skills/<name>`. Start a skill explicitly with `$<skill>`, or let Codex choose it when a request matches its description. See [Skills](skills.md) for each skill's arguments.

## Checking discovery

After deploying, check that Codex finds every adapter without starting a model:

```bash
python deploy.py verify
```

Codex CLI has no command that lists skills, so the verifier starts `codex app-server` from an empty directory and sends it a `skills/list` request. The app server answers without a sign-in and lists every copy of each skill it finds, user-only skills included. An adapter is `FOUND` when Codex lists it, enabled, and lists no other enabled skill of the same name. A same-named skill elsewhere, such as under `~/.codex/skills`, is reported as `SHADOWED`, because Codex offers both and does not merge them. Codex CLI before 0.88.0 does not say whether a skill is enabled, so the verifier reports an older one as `OUTDATED` without starting it. The same command checks Copilot CLI when it is installed.

On Windows, Codex looks for personal skills in your Windows profile folder, whatever `HOME` or `USERPROFILE` says. The deployer therefore installs into the profile folder that `USERPROFILE` names even when `HOME` points somewhere else, as it does by default in MSYS2. Every run names that folder on its `Home:` line and warns when `HOME` differs. If `USERPROFILE` itself has been changed, the adapters land where Codex never looks, and the verifier reports them as `NOT FOUND`.

`codex debug prompt-input` also lists skills, but only those the model may choose for itself, so it leaves out user-only skills such as `update-coding-agent-skills`.

## Running skill scripts

The skills' scripts run in Codex's sandbox. On Windows, Codex CLI cannot run them until the two machine-level settings below are in place. Claude Code and Copilot CLI need neither. The Codex desktop app sets up its own sandbox, which the CLI does not inherit, so a skill that works in the app can still fail in `codex` or `codex exec`.

## Windows sandbox mode

Without a `[windows] sandbox` value in `~/.codex/config.toml`, `codex exec` uses a read-only sandbox even when given `--sandbox workspace-write`. With approvals turned off, which is how `codex exec` runs, Codex then refuses every shell command, even a `Get-Content` of the skill file, with a message such as:

```text
exec_command failed: CreateProcess ... Rejected(...) blocked by policy
```

Add this to `~/.codex/config.toml`:

```toml
[windows]
sandbox = "elevated"
```

`"unelevated"` failed with `CreateProcessAsUserW failed` in the environment where this was tested, which was itself running inside another agent's sandbox. It has not been tested from a plain terminal, so start with `"elevated"`.

To confirm the setting took effect, ask Codex to run one command:

```powershell
codex exec --skip-git-repo-check -s read-only 'Run python -c "print(42)" and reply with its output'
```

It replies `42`. Each session also records the policy it actually used: the `turn_context` record in its rollout file under `~/.codex/sessions/<year>/<month>/<day>/` holds a `sandbox_policy` value. A `read-only` policy in a session started with `--sandbox workspace-write` means the setting is missing or was not read.

The [runtime canary](../tools/runtime_canary.py) starts Codex with `--ignore-user-config`, so it does not read this setting. It passes the value on the command line instead, as `-c windows.sandbox=elevated`, which Codex applies even when it ignores `config.toml`, and prints it as `SUPPLIED codex "windows.sandbox=elevated"`. Use `--codex-sandbox unelevated` to try the other mode. If Codex still refuses every command, the canary's `BLOCKED` line names the mode it used and points here.

The mode Codex will actually use can be checked without calling a model. `codex debug prompt-input` prints the instructions Codex would send, including a line saying which `sandbox_mode` is in effect:

```powershell
codex debug prompt-input -c sandbox_mode=workspace-write | Select-String 'sandbox_mode` is `[a-z-]+' | ForEach-Object { $_.Matches.Value }
```

The mode it prints is `workspace-write` when the Windows sandbox setting is present, and `read-only` when it is missing. Add `-c windows.sandbox=elevated` to the command to see the mode the canary's override gives.

## Python app execution aliases

Windows registers `python.exe` and `python3.exe` as app execution aliases: 0-byte reparse points in `%LOCALAPPDATA%\Microsoft\WindowsApps`. Every Windows install has them as Microsoft Store stubs, whichever Python installer you use, and the [Python install manager](https://docs.python.org/3/using/windows.html) replaces them with its own. Codex's sandbox cannot start either kind, and every skill script that runs `python` fails with:

```text
Program 'python.exe' failed to run: The file cannot be accessed by the system
```

Turn off those two aliases under **Settings > Apps > Advanced app settings > App execution aliases**, so that `python` resolves to a real executable: the install manager's launcher in `%LOCALAPPDATA%\Python\bin`, or the `python.exe` the classic installer put on your `PATH`. With the install manager, keep its `py` and `pymanager` aliases on. Check the result in a new terminal:

```powershell
Get-Command python -All
```

The first entry should be your Python installation, not `WindowsApps`.

Moving your Python installation ahead of `WindowsApps` in your user `PATH` is not always enough. Some installers also add `WindowsApps` to the system `PATH`, which Windows searches before the user `PATH`, so the aliases still win. Turning the aliases off works regardless of `PATH` order.
