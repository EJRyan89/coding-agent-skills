# Agent Skills Development

This project packages portable Claude Code skills and a guarded deployer.

The deployer is a standard-library Python 3.11+ package and currently supports Windows only: configured paths are absolute drive-letter paths, and rendered Bash and PowerShell are validated with Git Bash, ShellCheck, and PowerShell. Keep every operating-system-specific behavior in `deployer/platform_support.py` so Linux and other Unix support can be added there without touching the pipeline.

## Architecture

- `skills/<name>/` contains immutable skill templates. Never render configuration into this source tree.
- `agents/<name>.md` holds Claude Code subagent definitions that skills declare in `agent_deps`; the deployer installs them to `~/.claude/agents`.
- `skills/<name>/scripts/` is the only permitted location for a skill's executable files. Keep substantial executable logic out of Markdown.
- `deploy-meta/<name>.json` declares each skill's required variables, shared dependencies, same-source skill dependencies, selection visibility, opt-in status, and the external tools it runs. Skills may run only standard commands or declared tools; see "Commands skills may run" in `docs/adding-a-skill.md`. `deployer/tools.py` is the single catalogue of those tools and the deployment prerequisites; `python deploy.py check` (`deployer/check.py`) reports them.
- `source.json` may group selectable skills into atomic bundles; the manifest records requested roots separately from their expanded dependency closure.
- `skills/<asset>` contains shared assets declared in `source.json` as either `owner` or `dependency`.
- `deploy.py` is the single command-line entry point. `deployer/cli.py` routes it, and `deployer/arguments.py` declares its `argparse` parsers.
- `python deploy.py configure` runs `deployer/configure.py`, which writes a strictly parsed, source-specific configuration under `~/.claude/deployer/config/`. `deployer/config.py` is the single declaration of configured and derived variables.
- Any other `deploy.py` invocation runs `deployer/pipeline.py`: it renders selected skills in memory, validates the result, applies journaled multi-root changes, and records source-scoped ownership in `~/.claude/skills/.deploy-manifest.json`. Authoritative skills live under `~/.claude/skills`; thin generated runtime adapters live under `~/.agents/skills`.
- Every mutating filesystem operation goes through `deployer/fsops.py`; tests inject failures by replacing those functions.
- `deployer/frontmatter.py` is the one reader for skill and agent frontmatter, used by the deployer, `tools/`, and the tests. `skills/analyze-skill-cost/scripts/frontmatter.py` is a byte-identical copy, because a deployed skill cannot import the deployer; validation fails when they differ, so change both together.
- `python deploy.py verify` (`deployer/verify.py`) checks, without starting a model, that Codex and Copilot CLI each find every manifest-owned runtime adapter, enabled and not shadowed. `deployer/discovery.py` reads each runtime's skill listing (Copilot's `copilot skill list --json`, Codex's app-server `skills/list` request) and is shared with `tools/runtime_canary.py`.
- `tests/deployer/test_*.py` are the deployer suites; their isolated fixture repositories and homes live in `tests/deployer/harness.py`.
- `docs/skills.md` is the skill reference. `tools/skill_reference.py` generates its summary table and the first block of each skill's section from `SKILL.md` frontmatter and metadata; the explanation after each block is hand-written. After changing a skill's frontmatter or metadata, run `python tools/skill_reference.py --write`; validation fails while the reference is stale.
- `tools/runtime_canary.py` deploys this checkout and the fixture source in `tests/fixtures/runtime-canary/` into a throwaway home with `deploy.py --canary-home`, then checks that Claude Code, Codex, and Copilot CLI find and run the named skills. It calls models, so it is a manual pull request gate run through the `runtime-canary` repository skill, never part of validation. Nothing under `tests/fixtures/` ships, which validation enforces.
- `.github/workflows/deployable.yml` is a manual, dispatch-only check on a fresh `windows-latest` runner: it installs the prerequisites and the three runtimes, deploys into the runner's real profile (not `--canary-home`, because Codex on Windows reads the real profile), runs `deploy.py verify`, uninstalls, and deploys again. `tools/deployable_report.py` judges Codex and Copilot discovery (`PASSED`, `SKIPPED` when it will not list skills without a sign-in, `FAILED`), checks Claude Code's installed version and the deployed file layout (it has no skill listing without a session), and writes the step summary; `tests/tools/test_deployable_report.py` covers it. It starts no model and uses no secret, and validation pins its triggers, permissions, action pins, and runtime versions.
- `tools/worktrees.py` coordinates parallel sessions (creating task worktrees, surveying them, and the hub guard hook declared in `.claude/settings.json`), `tools/new_skill.py` scaffolds a new skill's frontmatter, metadata, and reference section, and `tools/sync_action_pins.py` copies the reviewed action pins from `validate.yml` into `init-ai-config` after a Dependabot pull request (see `docs/dependency-updates.md`); `tests/tools/test_*.py` are the suites for `tools/`.
- `.claude/skills/<name>/` holds this repository's own skills, for working on it rather than for deployment. Each has a shim in `.agents/skills/<name>/` with the same frontmatter for Codex and Copilot, which validation enforces. A shim is hand-maintained and points at a repository skill; an adapter is what the deployer generates under `~/.agents/skills` for a deployed skill. Keep the two words distinct.

To add or change a skill, follow the `change-skill` repository skill. It applies `docs/adding-a-skill.md`, the skill contract; read that before adding or parameterizing a skill.

## Safety requirements

- Do not modify installed skills under `~/.claude/skills`, installed agents under `~/.claude/agents`, generated adapters under `~/.agents/skills`, or user-managed Copilot skills under `~/.copilot/skills` while developing or testing this project.
- Use an isolated temporary home for deployment fixtures and canaries: `--canary-home`, or `Paths` built on a temporary directory. `deployer/platform_support.py` takes the home from the Windows profile folder, so setting `HOME` alone does not isolate a run.
- Preserve immutable template sources and verify source hashes when testing rendering.
- Never source or execute configuration files; keep parsing restricted to validated `KEY=VALUE` entries.
- Treat tokens used in Bash, PowerShell, JSON, YAML, or other executable/structured contexts according to that context. Universal raw substitution is not sufficient.
- Reject unresolved tokens, unsafe names and paths, ownership collisions, and invalid shared dependencies before mutation.
- Preserve journaled rollback, recovery, locking, manifest ownership, and permanent backup behavior.
- Do not add personal paths, internal organization names, credentials, or generated deployer state.
- Do not change the source ID without an explicit ownership-migration plan.

## Parallel sessions

Several sessions may work in this repository at once, and sessions that share a checkout share a HEAD and a working tree, so one session's branch switch or commit carries another's uncommitted edits. Each task therefore gets its own worktree; see `docs/parallel-sessions.md`.

- The main checkout is the hub. It stays on `main`, stays clean, and is never edited. It is used only to pull `main`, create and retire worktrees, survey, and deploy.
- Start a task with `python tools/worktrees.py new <kind> <name>`, which creates `.claude/worktrees/<kind>-<name>` (gitignored) on branch `<kind>/<name>`. Then move the session into it (in Claude Code, `EnterWorktree` with that path) before editing. `python tools/worktrees.py list` surveys every worktree.
- Integrate through a pull request pushed from the worktree, never through a merge in the hub. After it merges, retire the worktree from a session outside it with `repo-cleanup` on the hub, which also fast-forwards `main`; see "Retiring a worktree" in `docs/parallel-sessions.md`. By hand, run `git worktree remove <path>` and `git branch -D <kind>/<name>`. A squash merge leaves the branch looking unmerged, so `-d` refuses.
- Deploy only from the hub on `main`. A deployment records its source path, so `deploy.py` refuses to deploy from a linked worktree; `--dry-run` and `--canary-home` (a throwaway home under the temporary directory) still work there.
- Never pop or drop a stash you did not create: the stash stack is shared by every worktree.
- In a clone that opts in with `git config coding-agent-skills.hubGuard true`, the Claude Code hook in `.claude/settings.json` refuses edits to the hub and git commands that would move its HEAD or write its tree. Other runtimes do not run the hook, so for them these rules are the guard.
- `.claude/settings.json` is tracked and may hold hooks only. Permissions and every other setting belong in each developer's user or `settings.local.json` settings, and validation enforces this.

## Required validation

For deployer, template, or metadata changes, run:

```powershell
python -B tests/run_validation.py
```

`tests/run_validation.py` is the single entry point used locally and in CI. It runs the repository layout policies, then static shell checks and every regression suite under `tests/` and each skill's `scripts/` in one pool of workers. Pass `-k <pattern>` to run the policies and suites whose name or path matches while iterating, but run the full suite before finishing. Suites run in parallel, and `tests/run_shard.py` splits each large Python suite into shards run in separate processes, so every suite must isolate its own temporary home and fixtures, and no test may depend on another test's order or state. It requires Python 3.11 or newer, Git Bash, ShellCheck, and PowerShell 7 (`pwsh`).

Never work around a failing check. Do not mark tests as expected failures or skipped, weaken or disable a gate, or leave TODO or placeholder comments to land partial work; finish the change so every check passes, or keep it uncommitted. Never disable or suppress ShellCheck diagnostics to make validation pass; fix the cause. The only sanctioned suppression is the fragment-lint header `deployer/render.py` adds to Markdown command examples it extracts for validation.

Add regression coverage for every behavior change. When a token appears in executable content, include a rendered execution fixture with representative values containing spaces and other allowed punctuation.

Every skill with executable files must include an executable regression suite under its `scripts/` directory. Markdown may use executable-language fences only for command examples of five lines or fewer; extract longer programs into tested files under `scripts/`.

The runner decides what a documentation-only change needs. When every file changed since `origin/main` (the pull request's base in CI), uncommitted or untracked, is under `docs/` or `.github/ISSUE_TEMPLATE/`, or is the top-level `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CLAUDE.md`, `AGENTS.md`, or `.github/pull_request_template.md`, it runs the policy checks and only the suites that name a changed file. Any other change, or one it cannot determine, runs every suite, and so does `--full`. Markdown under `skills/`, `agents/`, and `.claude/` is skill and agent behavior, not documentation. Keep `DOCUMENTATION_FILES` and `DOCUMENTATION_DIRECTORIES` in `tests/run_validation.py` to files no suite executes.

## Issues and pull requests

The backlog is this repository's GitHub issues. Write every issue body from the matching template in `.github/ISSUE_TEMPLATE/` (enhancement, bug, or documentation) and every pull request body from `.github/pull_request_template.md`, keeping their section headings. Write the body to a file and pass it with `gh issue create --body-file` or `gh pr create --body-file`: `--body` bypasses the template, and a file avoids shell quoting of backticks and `$`. Name the originating issue with `Closes #N` when the pull request is opened.

A pull request stays current with `main`. Before `gh pr create`, run `git fetch origin main` and `git rebase origin/main` in the worktree; the branch is unpublished, so the rebase rewrites nothing anyone has. Rebase onto `origin/main`, not the local `main`, which is only as fresh as the hub's last pull. While the pull request is open, merge `origin/main` into it before pushing more commits, and never rebase or force-push it. Rerun `python -B tests/run_validation.py` whenever either step brought in commits. If a push is rejected, fetch and inspect the remote branch before anything else: the maintainer may have updated it.

## Maintaining AI Agent Config

This `CLAUDE.md` is the single canonical, hand-authored instruction source for every agent runtime working in this repository.

- `AGENTS.md` is a hand-maintained Codex wrapper that only redirects to this file. Keep it thin; never copy guidance into it or replace it with a generated adapter.
- Claude Code, Codex (through `AGENTS.md`), and Copilot CLI/app load these instructions directly, so the repository intentionally has no generated projections: no `.github/copilot-instructions.md`, `.github/ai-config-manifest.json`, `.github/scripts/ai_config.py`, or `ai-config-parity` workflow.
- The `init-ai-config` parity pipeline was evaluated and not adopted. Reconsider it only if a surface that does not read `CLAUDE.md` (Copilot code review, which reads only the redirect in `AGENTS.md`, or JetBrains) becomes a supported way of working on this repository; wire any adopted parity check into `validate.yml` rather than adding an uncalled workflow.

## Source authority

This project directory is the authoritative source for templates, metadata, deployment logic, tests, and documentation. Installed files under `~/.claude/skills/` and any legacy copy under `~/.claude/skill-templates/` are not authoritative and must not be edited as the source of a change. Synchronizing or deploying to those locations must be an explicit operation, never an incidental validation step.
