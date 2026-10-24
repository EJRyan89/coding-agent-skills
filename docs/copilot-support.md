# GitHub Copilot support

## Supported surfaces

This repository currently supports:

- personal skill discovery in GitHub Copilot CLI through generated `~/.agents/skills/<name>/SKILL.md` adapters, which carry each skill's description and argument hint for `/skills list` and the `/` picker, and `disable-model-invocation: true` for a user-only skill;
- zero-AI verification of the effective discovered skill path with `python deploy.py verify`; and
- code reviews with `review-prs`: the generic reviewer and specialists run inline in the session, and a repository entrypoint reviewer runs on a bounded, noninteractive Copilot CLI host. A specialists manifest that keeps `agent-delegation` in `required_capabilities` fails here.

[Runtime support](skills.md#runtime-support) lists, skill by skill, what runs fully in Copilot CLI and what runs in part: the code reviews, and user-only skills, which a headless session cannot start.

Repository instructions, IDE surfaces, the cloud coding agent, and GitHub code review require repository-specific configuration. They are not enabled merely by deploying these personal skills. Configure the surfaces a repository needs by hand, then check them with `audit-ai-config`.

## Discovery precedence

For repository skills, Copilot CLI prefers `.github/skills/`, then `.agents/skills/`, then `.claude/skills/`. For personal skills, `~/.copilot/skills/` takes precedence over `~/.agents/skills/`. With `COPILOT_HOME` set, Copilot's personal skills are under `$COPILOT_HOME/skills/` instead, and it may not read `~/.agents/skills/` at all; see [Copilot's configuration directory](#copilots-configuration-directory).

The deployer owns only its generated `~/.agents/skills` adapters. If a same-named path exists under `~/.copilot/skills`, or under `$COPILOT_HOME/skills` while `COPILOT_HOME` is set, deployment continues for Claude Code and Codex but reports that Copilot will use the higher-priority skill. Resolve that path manually; the deployer will not adopt, replace, or remove it.

After deployment, run the read-only discovery check:

```bash
python deploy.py verify
```

The verifier runs `copilot skill list --json` from an empty directory, so no repository skill takes part, and compares each manifest-owned adapter with the copy Copilot lists. Copilot lists one copy per name, the one it will use, so an adapter is `FOUND` only when that copy is the adapter and it is enabled. A same-named skill under Copilot's personal skills is reported as `SHADOWED`, with its path. When Copilot lists none of the adapters because `COPILOT_HOME` hides them, the report says so after its `NOT FOUND` and `SHADOWED` lines. The command performs discovery only; it does not start an AI session or consume a model request. It reports Copilot CLI older than 1.0.88 as `OUTDATED` without starting it. It checks Codex the same way when Codex is installed; see [Codex support](codex-support.md#checking-discovery). On Linux, Copilot reads personal skills under `HOME`, where the deployer installs, and the check runs the same way, as `python3 deploy.py verify`; the [`deployable` workflow](../.github/workflows/deployable.yml) runs it on a fresh Ubuntu runner as it does on a fresh Windows one.

## Copilot's configuration directory

Copilot CLI keeps its configuration in `~/.copilot` unless `COPILOT_HOME` names another directory, or a session starts with `--config-dir`, which Copilot deprecates in favor of the variable. Either one moves Copilot's personal skills to that directory's `skills` folder and stops Copilot reading `~/.agents/skills`, so it finds none of the deployed adapters. Claude Code and Codex are unaffected. A `COPILOT_HOME` that names `~/.copilot` itself changes nothing, but Copilot compares the two by their spelling, letter case included, so the same folder named in another case on Windows still hides the adapters.

The deployer reads `COPILOT_HOME` but never writes into Copilot's configuration directory:

- the shadow check looks for same-named skills in `$COPILOT_HOME/skills` while the variable is set;
- a deployment warns when the variable names another directory than `~/.copilot`, printing the folder to set it to; and
- `python deploy.py verify`, which starts Copilot with your environment, explains the `NOT FOUND` and `SHADOWED` lines when the variable hides every adapter.

The deployer cannot see `--config-dir`, an argument of one session, so a session started with it finds none of the adapters without a warning. To use the deployed skills in Copilot CLI, leave `COPILOT_HOME` unset, or set it to the `.copilot` folder of your home spelled as the warning prints it, and start Copilot without `--config-dir`. A `--canary-home` deployment ignores `COPILOT_HOME`, since its runtimes read the throwaway home.

## Headless sessions

The generated adapter sends Copilot to the authoritative skill under `~/.claude/skills`, which is outside the working directory. An interactive session asks for access to it. A headless `copilot -p` session cannot ask, so every read of the skill is denied and the skill cannot run. Grant the directory up front:

```powershell
copilot -p '<prompt>' --add-dir "$HOME/.claude/skills"
```

A headless session cannot start a user-only skill, such as `update-coding-agent-skills`. In the Copilot CLI releases checked, `copilot -p` does not expand a prompt that begins with `/<skill>` into the skill, and the model's own `skill` tool reports a user-only skill as not found, even though `copilot skill list` lists it as enabled. Start user-only skills from an interactive session, either by typing `/<skill>` or by passing it as the session's first prompt:

```bash
copilot -i '/<skill> <arguments>'
```

The tested Copilot CLI versions are in the supported-runtimes table of the [README](../README.md#supported-platforms-and-runtimes), and the floor is in `deployer/tools.py`.

The [runtime canary](../tools/runtime_canary.py) reaches Copilot only through `copilot -p`, so for a user-only skill it prints `RUNTIME copilot <skill> UNSUPPORTED` and runs no model. After a Copilot CLI upgrade, run a user-only skill headless by hand; once `-p` starts it, remove `USER_ONLY_UNSUPPORTED` from the canary.

## Subagents and hooks

Copilot CLI starts subagents: a custom agent's work runs in a subagent with its own context, which the model picks by its description, the user names, or `copilot --agent <name>` starts ([Creating and using custom agents for GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/create-custom-agents-for-cli)). Built-in agents, such as `task`, `explore`, and `general-purpose`, need no definition ([Built-in agents](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference#built-in-agents)), and `--fleet` runs subagents in parallel ([Command-line options](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference#command-line-options)). A custom agent's definition takes a `model` ([Custom agents configuration](https://docs.github.com/en/copilot/reference/custom-agents-configuration)) and a `reasoningEffort` ([Custom agent frontmatter fields](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference#custom-agent-frontmatter-fields)), and the command line takes `--model` and `--reasoning-effort` ([Command-line options](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference#command-line-options)). A skill that starts a general-purpose subagent, such as the `review-insights` synthesis, therefore starts it here too, but without the reviewer agent's guard, so the synthesis's prompt is its only boundary: its support is `partial`, lacking `fail-closed-agent-hooks`, as [Runtime support](skills.md#runtime-support) says and the hook paragraph below explains.

Copilot CLI also runs hooks, a custom agent's own among them, and a `preToolUse` hook denies a call by exiting with code 2, as it does when the hook fails. A hook that times out lets the call through, though. The reviewer guard is a `PreToolUse` hook, and Claude Code's reviewer agent blocks the call when its guard fails or times out, so a reviewer there makes no call its guard has not allowed. Under Copilot CLI the guard would hold only while it runs, which is not the guard the [code-review contract](code-review-operations-contract.md#threat-model) promises. So the code reviews keep their reviewers inline here, under the seal below, until Copilot CLI blocks a call when its hook times out; [Runtime support](skills.md#runtime-support) names the capability they lack as `fail-closed-agent-hooks`, and the decision is in [Design](design.md).

These are the pages this was read from:

- [Creating and using custom agents for GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/create-custom-agents-for-cli)
- [Custom agents configuration](https://docs.github.com/en/copilot/reference/custom-agents-configuration)
- [GitHub Copilot CLI command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference), for the built-in agents, `--fleet`, `--reasoning-effort`, and a custom agent's `reasoningEffort`
- [GitHub Copilot CLI programmatic reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference)
- [The Copilot CLI changelog](https://github.com/github/copilot-cli/blob/main/changelog.md), for subagents, hooks inside them, agent-scoped hooks, a `preToolUse` hook's exit code 2, a failed `preToolUse` hook's denial, and tool calls that continue when a hook times out

## Inline code reviews

The generic reviewer and the specialists of a manifest run inline, for the reason above: `prepare` prints `INLINE <run directory>`, and the session works each reviewer role itself, one at a time, as `next-role` hands it out, writing each result file through the same result contract and self-check a subagent uses. `check` and `finalize` validate the results exactly as they do for subagents, and the record's `review.dispatch` is `inline`. A specialists manifest that lists `agent-delegation` in `required_capabilities` asks for subagents only, so it fails on Copilot CLI as before; drop it from the list to let its specialists run inline.

An inline reviewer has no reviewer agent and no `PreToolUse` guard: it is the session, with that session's tools, permissions, and context. `next-role`, `check`, and `finalize` fail the pull request if a role changes another run file, such as an earlier role's result or the request, and the core still validates every result and computes the verdict. Run inline reviews from a directory outside the repository's checkout, in a session allowed nothing beyond what the pipeline's commands need; the [threat model](code-review-operations-contract.md#threat-model) lists what remains.

## Bounded code-review host

The bounded host runs a repository entrypoint reviewer. It requires GitHub Copilot CLI 1.0.88 or newer, installed from WinGet or the GitHub release: it runs Copilot CLI's own program, the first on `PATH` or else WinGet's, and passes over a shell script such as the `copilot.cmd` an npm install puts on `PATH`, wherever it stands there, since Windows starts one through `cmd.exe`. With only a shell script on `PATH` and no WinGet program, it refuses and names each script it passed over. On Linux it runs the first `copilot` on `PATH`, npm's included: the kernel starts a script by its `#!` line with its arguments as they are, so no shell parses its command line, and its isolated `HOME` is the profile Copilot reads there. The host has not yet run a review against a signed-in Copilot CLI on Linux. It:

- reviews at the configuration's `reviewer_effort` through `--reasoning-effort` when one is set, and otherwise at Copilot CLI's default, and passes no `--model`, since a reviewer profile names a Claude alias that Copilot CLI does not take. The review therefore runs on the model Copilot CLI chooses for a prompt-mode run: its default model, or, on an account where Auto model selection is managed, the model Auto picks, which Copilot CLI's changelog says prompt mode honors for piped-stdin runs such as the host's. Auto picks within the account's policies and may pick another model from one review to the next. Copilot's [programmatic-use guide](https://docs.github.com/en/copilot/how-tos/copilot-cli/automate-copilot-cli/run-cli-programmatically) advises `--model` for consistent behavior, but the code-review configuration has no Copilot model setting, and the host's isolated `COPILOT_HOME` leaves out the model a user's own Copilot settings choose. The [runtime canary](../tools/runtime_canary.py) runs `copilot -p` the same way;
- runs from a new isolated workspace with isolated `HOME`, `USERPROFILE`, and `COPILOT_HOME` values, and with `COPILOT_AUTO_UPDATE=false`, so the Copilot CLI whose version the host checked is the one that reviews;
- gives Copilot its prompt on standard input, so no review text is part of its command line;
- disables custom instructions, built-in MCP servers, remote delegation, interactive questions, shell, URL, and memory tools;
- verifies the exact file set and SHA-256 hashes of the trusted materialized reviewer;
- requires the diff and source snapshot to be inside the isolated review run, and checks the snapshot against the stamp `prepare` took, re-reading only what can have changed since ("Source snapshots" in [Code-review operations](code-review-operations.md#source-snapshots));
- grants read access to the isolated review run and trusted materialized reviewer only;
- grants write access only to an attempt's staging result path, which the host promotes while its claim holds;
- renders the prepared entrypoint's shared input and result contracts, binding `RESULT_FILE` and the completion reply to that staging path and omitting native shell-based self-check and source commands;
- names the exact trusted materialized entrypoint and classifies request metadata, prior findings, review-comment bodies, source, diff, and pull-request description as untrusted data, never instructions; and
- treats CLI JSON output as diagnostics, never as the review result.

The internal `run_copilot` interface requires the caller's promotion callback and returns no review object.
The pipeline supplies the callback that checks the host's claim under its lock; review data is read from the
promoted result file, and CLI output remains in the diagnostic file.

Authentication supplied through the invoking environment may remain available. The isolated `HOME`, `USERPROFILE`, and `COPILOT_HOME` leave out the user's Copilot configuration, personal skills, and the hooks in that configuration. Copilot CLI's policy hooks remain: a machine's administrator installs them machine-wide, under `C:\ProgramData\GitHub\Copilot\policy.d` or `HKLM\Software\Policies\GitHub\Copilot` on Windows and `/etc/github-copilot/policy.d` on Linux, and Copilot CLI loads them before all other hooks, whatever `disableAllHooks` and folder trust say, so they run in the host as in any other Copilot CLI session ([Policy hooks](https://docs.github.com/en/copilot/reference/hooks-reference#policy-hooks)). A real end-to-end reviewer canary invokes the model and is therefore an explicit release/cutover step, not part of ordinary deterministic validation.
