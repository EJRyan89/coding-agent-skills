# GitHub Copilot support

## Supported surfaces

This repository currently supports:

- personal skill discovery in GitHub Copilot CLI through generated `~/.agents/skills/<name>/SKILL.md` adapters;
- zero-AI verification of the effective discovered skill path with `python deploy.py verify`; and
- code reviews with `review-prs`: the generic reviewer and specialists run inline in the session, and a repository entrypoint reviewer runs on a bounded, noninteractive Copilot CLI host. A specialists manifest that keeps `agent-delegation` in `required_capabilities` fails here.

[Runtime support](skills.md#runtime-support) lists, skill by skill, what runs fully in Copilot CLI and what runs in part: the code reviews, and user-only skills, which a headless session cannot start.

Repository instructions, IDE surfaces, the cloud coding agent, and GitHub code review require repository-specific configuration. They are not enabled merely by deploying these personal skills. Configure the surfaces a repository needs by hand, then check them with `audit-ai-config`.

## Discovery precedence

For repository skills, Copilot CLI prefers `.github/skills/`, then `.agents/skills/`, then `.claude/skills/`. For personal skills, `~/.copilot/skills/` takes precedence over `~/.agents/skills/`.

The deployer owns only its generated `~/.agents/skills` adapters. If a same-named path exists under `~/.copilot/skills`, deployment continues for Claude Code and Codex but reports that Copilot will use the higher-priority skill. Resolve that path manually; the deployer will not adopt, replace, or remove it.

After deployment, run the read-only discovery check:

```bash
python deploy.py verify
```

The verifier runs `copilot skill list --json` from an empty directory, so no repository skill takes part, and compares each manifest-owned adapter with the copy Copilot lists. Copilot lists one copy per name, the one it will use, so an adapter is `FOUND` only when that copy is the adapter and it is enabled. A same-named skill under `~/.copilot/skills` is reported as `SHADOWED`, with its path. The command performs discovery only; it does not start an AI session or consume a model request. It reports Copilot CLI older than 1.0.88 as `OUTDATED` without starting it. It checks Codex the same way when Codex is installed; see [Codex support](codex-support.md#checking-discovery).

## Headless sessions

The generated adapter sends Copilot to the authoritative skill under `~/.claude/skills`, which is outside the working directory. An interactive session asks for access to it. A headless `copilot -p` session cannot ask, so every read of the skill is denied and the skill cannot run. Grant the directory up front:

```powershell
copilot -p '<prompt>' --add-dir "$HOME/.claude/skills"
```

A headless session cannot start a user-only skill, such as `update-coding-agent-skills`. In Copilot CLI 1.0.91, `copilot -p` does not expand a prompt that begins with `/<skill>` into the skill, and the model's own `skill` tool reports a user-only skill as not found, even though `copilot skill list` lists it as enabled. Start user-only skills from an interactive session.

The [runtime canary](../tools/runtime_canary.py) reaches Copilot only through `copilot -p`, so for a user-only skill it prints `RUNTIME copilot <skill> UNSUPPORTED` and runs no model. After a Copilot CLI upgrade, run a user-only skill headless by hand; once `-p` starts it, remove `USER_ONLY_UNSUPPORTED` from the canary.

## Inline code reviews

Copilot CLI cannot start subagents, so a review whose reviewer is the suite's generic reviewer or a specialists manifest runs inline: `prepare` prints `INLINE <run directory>`, and the session works each reviewer role itself, one at a time, as `next-role` hands it out, writing each result file through the same result contract and self-check a subagent uses. `check` and `finalize` validate the results exactly as they do for subagents, and the record's `review.dispatch` is `inline`. A specialists manifest that lists `agent-delegation` in `required_capabilities` asks for subagents only, so it fails on Copilot CLI as before; drop it from the list to let its specialists run inline.

An inline reviewer has no reviewer agent and no `PreToolUse` guard: it is the session, with that session's tools, permissions, and context. `next-role`, `check`, and `finalize` fail the pull request if a role changes another run file, such as an earlier role's result or the request, and the core still validates every result and computes the verdict. Run inline reviews from a directory outside the repository's checkout, in a session allowed nothing beyond what the pipeline's commands need; the [threat model](code-review-operations-contract.md#threat-model) lists what remains.

## Bounded code-review host

The bounded host runs a repository entrypoint reviewer. It requires GitHub Copilot CLI 1.0.88 or newer. It:

- runs from a new isolated workspace with isolated `HOME`, `USERPROFILE`, and `COPILOT_HOME` values;
- disables custom instructions, built-in MCP servers, remote delegation, interactive questions, shell, URL, and memory tools;
- verifies the exact file set and SHA-256 hashes of the trusted materialized reviewer;
- requires the diff and verified source snapshot to be inside the isolated review run;
- grants read access to the isolated review run and trusted materialized reviewer only;
- grants write access only to the protocol result path;
- names the exact trusted materialized entrypoint in the prompt; and
- treats CLI JSON output as diagnostics, never as the review result.

Authentication supplied through the invoking environment may remain available, but ambient Copilot configuration and personal skills do not. A real end-to-end reviewer canary invokes the model and is therefore an explicit release/cutover step, not part of ordinary deterministic validation.
