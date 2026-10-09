# Contributing

Thank you for helping improve Coding Agent Skills. Keep changes focused, preserve the safety model, and include regression coverage for behavioral changes.

This page is the whole path from a clone to a merged pull request: getting the source, the tools, the worktree each task works in, the repository skills that carry the procedure, validation, upgrade notes, the pull request, and the gates the maintainer runs.

## Getting the source

Clone this repository, so that `origin` is this repository and `origin/main` and its release tags are what validation compares with:

```bash
git clone https://github.com/EJRyan89/coding-agent-skills.git
```

Without push access, fork it on GitHub and add the fork as a second remote to push branches to. `origin` stays this repository, so every instruction below that names `origin/main` holds as written:

```bash
git remote add fork 'https://github.com/<your-user>/coding-agent-skills.git'
```

Keep a full clone, and fetch `main` and the tags before validating:

```bash
git fetch origin main --tags
```

Validation reads both: the runner compares with `origin/main` to find a documentation-only change, and the upgrade-notes check compares the contract files at the last tag reachable from `origin/main` with the working tree. A missing `origin/main` or a shallow clone fails that check with the command that fixes it. A clone whose `origin` is a fork without this repository's tags passes it silently, because with no tag it treats nothing as released; cloning this repository and pushing to the fork avoids that.

## Development environment

Development and deployment are currently supported on Windows only. Contributors need:

- Python 3.11 or newer;
- Git for Windows, for Git Bash;
- ShellCheck;
- PowerShell 7 (`pwsh`);
- the PSScriptAnalyzer module;
- the pinned development dependencies, `ruff` and `mypy`; and
- GitHub CLI for code-review-operation changes.

Install the development dependencies into the Python that runs validation:

```powershell
python -m pip install -r requirements-dev.txt
```

They are the only ones: contributor and CI tools, pinned, while the deployer and every shipped skill stay standard-library. [Python checks](#python-checks) states what validation runs with them, and [Dependency updates](docs/dependency-updates.md#development-dependencies) records the decision and how the pins are updated.

Install PSScriptAnalyzer from PowerShell 7; `-Force` also upgrades an older version and answers the untrusted-repository prompt:

```powershell
pwsh -Command 'Install-Module PSScriptAnalyzer -Scope CurrentUser -Force'
```

Validation runs `Invoke-ScriptAnalyzer -Severity Warning,Error` on every `.ps1` under `tools/`, `tests/`, `skills/`, and `.claude/skills/` and on every PowerShell fence in Markdown outside `tests/`. Fix each finding at its cause: no rule is suppressed, never with a `SuppressMessageAttribute` or a settings file that disables rules, and a PowerShell fence changed to satisfy a rule must stay the same command. It also runs ShellCheck on every Bash fence in Markdown outside `skills/` and `tests/`; write a placeholder there quoted, as in `'<file>'`, so the fence still parses.

[Installation](docs/installation.md#installing-the-tools) lists install commands for the other tools. After installing a tool, open a new terminal so it is on `PATH`; `tests/run_validation.py` stops before running any test and lists every missing tool and every tool older than its floor in [Dependency updates](docs/dependency-updates.md). `python deploy.py check` is read-only and reports, as `FOUND`, `MISSING`, `OUTDATED`, or `OPTIONAL`, the tools a deployment needs (Python, Git Bash, ShellCheck, and PowerShell) and the ones the skills run, such as `gh`; it does not check `ruff`, `mypy`, or PSScriptAnalyzer, which only validation needs.

Run the validation entry point from PowerShell or Git Bash, and use Git Bash for Bash scripts. Follow `.editorconfig` and `.gitattributes`; do not commit generated build, test, Python-cache, IDE, deployment, or personal configuration artifacts.

## Working in a worktree

Each task gets its own git worktree, so agent sessions working in parallel never share a working tree. The main checkout, the hub, stays on `main`, stays clean, and is never edited. From the hub, bring `main` up to date and create the task's worktree, with `feat` or `fix` as the kind:

```bash
git pull --ff-only origin main
python tools/worktrees.py new feat '<name>'
```

That creates `.claude/worktrees/feat-<name>` on the branch `feat/<name>`; edit, commit, validate, and push from there (in Claude Code, move the session in with `EnterWorktree`). [Parallel sessions](docs/parallel-sessions.md) explains the layout, the optional hub guard, and how to retire a worktree after its pull request merges.

## The repository skills

Two repository skills carry the procedure. Claude Code loads them from `.claude/skills/` when a session starts in this repository, and Codex and Copilot CLI find them through the shims in `.agents/skills/`:

- `implement-change` takes any change from the worktree and the plan to the pull request. It reads every repository-specific fact, such as the size gate, the contract files, the validation to run, and which document owns what, from [Implementing changes](docs/implementing-changes.md), which is worth reading without an agent too.
- `change-skill` adds or changes a skill, shipped or repository-only. It builds on `implement-change` and applies [Adding a skill](docs/adding-a-skill.md), the skill contract.

Two more, `runtime-canary` and `evaluate-skill`, are gates; see [Gates the maintainer runs](#gates-the-maintainer-runs). The last, `audit-repository`, runs the maintainer's two audits of each release, one before the tag and one after; [Releasing](docs/releasing.md#before-tagging) says when.

## Making changes

- Treat `skills/`, `deploy-meta/`, `source.json`, and the deployer files in this repository as authoritative. Do not edit installed copies under a user profile as the source of a change.
- Keep executable skill logic and its tests under `skills/<name>/scripts/`. Do not embed substantial programs in `SKILL.md`.
- Add regression coverage for every behavior change, especially parsing, path handling, rendering, ownership, recovery, and runtime-host boundaries.
- Preserve fail-closed behavior. Do not weaken validation, overwrite protection, journaling, rollback, or trust boundaries to make a test pass.
- Do not add personal paths, organization-specific references, credentials, generated deployment state, or private fixtures.
- Keep pull requests small enough to review and explain any safety or compatibility tradeoffs.

For the complete skill template and metadata contract, see [Adding a skill](docs/adding-a-skill.md). For pinned dependency maintenance, see [Dependency updates](docs/dependency-updates.md).

### Upgrade notes

A change to a contract, such as a skill directory name, a `required_vars` list, a tool floor, the manifest version, or a code-review record format, adds an entry under `## Unreleased` in [Upgrade notes](docs/upgrade-notes.md) in the same pull request, in the format that page states. The [Versioning](docs/releasing.md#versioning) section of Releasing lists the contracts and the levels. Validation's upgrade-notes check compares those contract files at the last tag with the working tree and fails, naming the item and what the entry must say, while a changed item has no new entry. Skill arguments, status lines, and deployer flags have no contract file, so add their entry without being asked. A change that touches no contract needs no entry.

## Validation

Run the complete validation sequence from the repository root:

```powershell
python -B tests/run_validation.py
```

Fetch `main` and the tags first, as in [Getting the source](#getting-the-source). A full run took about three minutes on a recent workstation, and the `validate` job takes about six to eleven minutes in CI. When every changed file is documentation, such as `docs/`, `README.md`, or this file, the runner itself runs only the policy checks, the suites that name a changed file, and the Markdown fence checks, which took about a minute; `--full` runs everything.

The runner reports each failing suite by path. When diagnosing a failure, run that suite directly, as in `python -B tests/deployer/test_recovery_migration.py`, or narrow the runner with `-k <pattern>`, as in `-k deployer`. Suites run in parallel, with large ones split into shards; set `VALIDATION_JOBS` to change the worker count.

Changes to deployment behavior should also be exercised against an isolated temporary home: `python deploy.py --canary-home <dir>` with a directory under the temporary directory, or a test that builds the deployer's paths on one. On Windows the deployer takes its home from the profile folder, not `HOME`, so setting `HOME` alone does not isolate a run. Never use ordinary development validation to deploy into the contributor's real agent directories.

### Python checks

This section is the one full account of the Python format, lint, and type checks; `CLAUDE.md` keeps the rules an agent session acts on, and [Adding a skill](docs/adding-a-skill.md#validation) adds what a skill's scripts need.

Validation checks formatting with `ruff format --check` and lints with `ruff check`, both at a line length of 120 and with the rule sets `pyproject.toml` selects, on `deployer/`, `tools/`, `tests/`, `skills/`, `.claude/skills/`, and `deploy.py`. Run `python -m ruff format` on any file the first names, and fix each finding the linter names (`python -m ruff check --fix` applies the fixes ruff marks safe). No rule or file is exempt; a finding that must stay is suppressed on its line as `# noqa: <code> - <reason>`, and validation fails on a `noqa` without its codes and reason.

A comment or docstring states its reason in plain words, never as an issue number such as `#12`, `(#12)`, `#12's`, or `issue 12`, because a deployed skill's reader has only the source; validation fails on one in the Python under those roots, and string literals, the fixture trees under `tests/fixtures/`, and the templates' `Closes #N` are outside the rule.

Function complexity (C901) and length in statements (PLR0915) are the exception: they take no `noqa`, and their ceilings, `max-complexity` and `max-statements` in `pyproject.toml`, are a ratchet that only goes down. Validation fails a change that raises either ceiling, so split a function that exceeds them into helpers of one concern; a pull request that splits the function at a ceiling lowers it to the new maximum, and the literal its test pins, in the same change.

The bandit security rules (`S`) are selected except S603 and S607, which flag every subprocess call made without a shell and every program started by its PATH name. The repository runs declared tools as argument lists found on PATH, which the checks under [Commands skills may run](docs/adding-a-skill.md#commands-skills-may-run) hold instead, so those two describe the design and are left unselected rather than suppressed on every call.

Validation type-checks with `mypy` at its default strictness and with the `[tool.mypy]` configuration in `pyproject.toml`, once on `deployer/`, `tools/`, `deploy.py`, and `tests/` together and once on each shipped or repository skill's `scripts/` directory from inside it, and names each error. Fix it, or, where an error must stay, write `# type: ignore[<code>]  # <reason>` on its line; validation fails on a `type: ignore` without its codes and reason. Regression suites are type-checked too, so a fixture that is malformed on purpose is typed as the loose shape it is, never silenced. `mypy_path` lists each `skills/<name>/scripts` directory a module or suite puts on `sys.path`, and each test directory whose suites import a module beside them by its bare name; validation fails until the list matches.

## How `main` is protected

`main` changes only through a pull request, and its branch protection is what keeps it green:

- the `validate` check is required, and it is strict: a branch must be up to date with `main` before it can merge, so a pull request that is behind is refused until `origin/main` is merged into it;
- linear history is required, and the repository allows squash merges only, with merge commits and rebase merges turned off, so each pull request lands as one commit;
- unresolved review conversations block the merge until each one is resolved;
- no approval is required, so a passing, current, resolved pull request can merge;
- force pushes to `main` and deleting it are refused; and
- the rules are enforced for administrators too, so nobody bypasses them.

The repository's security settings back this up: secret scanning, push protection, private vulnerability reporting, and Dependabot security updates are enabled, and the default workflow token is read-only.

Branch protection and these settings are live repository settings, not tracked files, so they can drift without a diff. `python tools/branch_protection.py` reads them and the merge settings through `gh api` and prints `PROTECTED`, or `DRIFTED` and each invariant that no longer holds, then each security setting as it found it; reading branch protection and the security settings needs admin rights on the repository. The [release procedure](docs/releasing.md#before-tagging) runs it before every tag.

## Issues and pull requests

Open issues and pull requests from the templates in `.github/`: an enhancement, bug, or documentation issue template, and the pull request template, whose Validation checklist is the list below. From the command line, write the body to a file based on the template and pass it with `gh issue create --body-file` or `gh pr create --body-file`; `--body` skips the template. Report a suspected vulnerability as the [Security policy](SECURITY.md) describes, never in an issue.

To open a pull request from the task's worktree:

1. Commit the change, and run the validation sequence until it passes.
2. Bring the branch current with `main`: run `git fetch origin main --tags` and `git rebase origin/main`. The branch is unpublished, so the rebase rewrites nothing anyone has. Rerun validation if it brought in commits.
3. Push the branch, to `origin` or, without push access, to `fork`, as in `git push -u fork '<kind>/<name>'`. With two remotes, run `gh repo set-default EJRyan89/coding-agent-skills` once, so `gh` opens pull requests here.
4. Write the body from `.github/pull_request_template.md` to a file, keeping its headings, name the issue with `Closes #<number>`, fill in its two model lines, and open it with `gh pr create --body-file '<file>'`.
5. While it is open, merge `origin/main` into the branch before pushing more commits, never rebase or force-push it, and rerun validation whenever the merge brings in commits. If a push is rejected, fetch and inspect the remote branch first: the maintainer may have updated it.

The maintainer merges; the repository squash-merges only, so the pull request lands as one commit.

The template's sections ask for the problem and the chosen behavior, the user-visible, compatibility, or security implications, and the models that planned and implemented the change. Before requesting review, check each item of its Validation checklist:

- `python -B tests/run_validation.py` passes in full, with no skipped prerequisites;
- regression coverage is added or updated for every behavior change;
- for a change to skill paths, `allowed-tools`, runtime adapters, or agents, the `runtime-canary` lines are in the body, with each runtime's version and any `SKIPPED` reason;
- for a change to a skill's prompt, model guidance, or reviewer instructions, the `evaluate-skill` run is cited with its table, and `docs/skill-evaluations.md` holds its result;
- for a skill change, `analyze-skill-cost` audited each changed skill from the source tree with no MUST FIX left, and any SUGGESTION left is named with why;
- for a contract change, an entry under `## Unreleased` in `docs/upgrade-notes.md` names each changed contract item;
- documentation is updated in the same change; and
- the diff is reviewed for personal paths, organization names, credentials, and generated artifacts.

Under the checklist, name anything that could not be run, and why.

## Questions

Ask a question in an issue from the question template, which applies the `question` label. Discussions stay off, so the issue tracker is the one place to ask, and blank issues stay off, so every issue starts from a template.

## Gates the maintainer runs

Some gates need more than a clone and the validation tools. Run them when you can and put their output in the pull request; when you cannot, say so under the checklist, and the maintainer runs them before merging:

- `runtime-canary`, for a change to skill paths, `allowed-tools`, runtime adapters, or agents. It needs Claude Code, Codex, and Copilot CLI installed and signed in, and each run calls a model.
- `evaluate-skill`, for a change to a skill's prompt, model guidance, or reviewer instructions when the skill has scenarios under `tests/fixtures/skill-evals/`. It runs headless Claude Code sessions, so it needs Claude Code signed in, and each run calls models.
- `analyze-skill-cost`, for a skill change. It is a shipped skill, so it runs from a deployment of this suite into your own profile ([Installation](docs/installation.md)); run from the worktree, it audits the source tree, not the deployed copy. Without a deployment, leave it to the maintainer.
- The `deployable` workflow, which deploys onto a fresh GitHub-hosted runner. It is dispatched by hand before each release ([Releasing](docs/releasing.md#before-tagging)), not for each pull request.

By contributing, you agree that your contribution is licensed under the repository's [MIT License](LICENSE).
