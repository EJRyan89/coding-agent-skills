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

Validation reads both: the runner compares with `origin/main` to find a documentation-only change, and the upgrade-notes check compares the contract files at the last tag reachable from `origin/main` with the working tree and reads the upgrade notes of the commits since that tag. A missing `origin/main` or a shallow clone fails that check with the command that fixes it. A clone whose `origin` is a fork without this repository's tags passes it silently, because with no tag it treats nothing as released; cloning this repository and pushing to the fork avoids that.

## Development environment

Development and deployment are supported on Windows, and the whole validation runs on Linux too, where [Validating on Linux](#validating-on-linux) sets it up; the README says where Linux support stands. Contributors need:

- Python 3.11.4 or newer;
- Git for Windows, for Git Bash, or on Linux Git and the system's Bash;
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

Validation runs `Invoke-ScriptAnalyzer -Severity Warning,Error` on every `.ps1` under `tools/`, `tests/`, `skills/`, and `.claude/skills/` and on every PowerShell fence in Markdown outside `tests/`. Fix each finding at its cause: no rule is suppressed, never with a `SuppressMessageAttribute` or a settings file that disables rules, and a PowerShell fence changed to satisfy a rule must stay the same command. Validation fails on a `SuppressMessageAttribute` in a `.ps1` file or a Markdown PowerShell fence, on a ShellCheck `disable` directive in a Bash file or a Markdown Bash fence, and on a `PSScriptAnalyzerSettings.psd1` or `.shellcheckrc` file. It also runs ShellCheck on every Bash fence in Markdown outside `skills/` and `tests/`; write a placeholder there quoted, as in `'<file>'`, so the fence still parses.

[Installation](docs/installation.md#installing-the-tools) lists install commands for the other tools. After installing a tool, open a new terminal so it is on `PATH`; `tests/run_validation.py` stops before running any test and lists every missing tool and every tool older than its floor in [Dependency updates](docs/dependency-updates.md). `python deploy.py check` is read-only and reports, as `MISSING`, `OUTDATED`, `UNUSABLE`, `FOUND`, or `OPTIONAL`, the tools a deployment needs (Python, Git Bash, ShellCheck, and PowerShell) and the ones the skills run, such as `gh`; it does not check `ruff`, `mypy`, or PSScriptAnalyzer, which only validation needs.

Run the validation entry point from PowerShell or Git Bash, and use Git Bash for Bash scripts. Follow `.editorconfig` and `.gitattributes`; do not commit generated build, test, Python-cache, IDE, deployment, or personal configuration artifacts.

### Validating on Linux

Validation runs on Linux as it does on Windows, and CI runs the whole of it there in the `Validation on Linux` job on every push and pull request, and the required `Validation` check needs that job, so a pull request merges only when validation passes on Linux as well as on Windows. On Linux, validation uses the system Bash and the first `pwsh` and `shellcheck` on `PATH`. On Debian or Ubuntu, with PowerShell 7 installed from [Microsoft's package repository](https://learn.microsoft.com/powershell/scripting/install/install-ubuntu) or with `sudo snap install powershell --classic`, these commands install the rest from the checkout's root, the development dependencies into a virtual environment outside the checkout, since Debian and Ubuntu manage their own Python's packages, and serve as a cloud session's start hook:

```bash
sudo apt-get update && sudo apt-get install --yes git shellcheck python3-venv
pwsh -NoProfile -Command 'Install-Module PSScriptAnalyzer -Scope CurrentUser -Force'
python3 -m venv ~/.venvs/coding-agent-skills
~/.venvs/coding-agent-skills/bin/python -m pip install -r requirements-dev.txt
```

Then run validation with that environment's interpreter, `~/.venvs/coding-agent-skills/bin/python -B tests/run_validation.py`.

## Working in a worktree

Each task gets its own git worktree, so agent sessions working in parallel never share a working tree. The main checkout, the hub, stays on `main`, stays clean, and is never edited. From the hub, bring `main` up to date and create the task's worktree, with the type its pull request's title will carry as the kind, such as `feat` or `fix`:

```bash
git pull --ff-only origin main
python tools/worktrees.py new feat '<name>'
```

That creates `.claude/worktrees/feat-<name>` on the branch `feat/<name>`; edit, commit, validate, and push from there (in Claude Code, move the session in with `EnterWorktree`). [Parallel sessions](docs/parallel-sessions.md) explains the layout, the optional hub guard, and how to retire a worktree after its pull request merges.

## The repository skills

Two repository skills carry the procedure. Claude Code loads them from `.claude/skills/` when a session starts in this repository, and Codex and Copilot CLI find them through the shims in `.agents/skills/`:

- `implement-change` takes any change from the worktree and the plan to the pull request. It reads every repository-specific fact, such as the size gate, the contract files, the validation to run, and which document owns what, from [Implementing changes](docs/implementing-changes.md), which is worth reading without an agent too.
- `change-skill` adds or changes a skill, shipped or repository-only. It builds on `implement-change` and applies [Adding a skill](docs/adding-a-skill.md), the skill contract.

Two more, `runtime-canary` and `evaluate-skill`, are gates; see [Gates the maintainer runs](#gates-the-maintainer-runs). `audit-repository` runs the maintainer's two audits of each release, one before the tag and one after; [Releasing](docs/releasing.md#before-tagging) says when. The last, `review-repository`, is this repository's own pull request reviewer, which `review-prs` runs on a machine whose code-review configuration names it; its `SKILL.md` gives that configuration.

## Making changes

- Treat `skills/`, `deploy-meta/`, `source.json`, and the deployer files in this repository as authoritative. Do not edit installed copies under a user profile as the source of a change.
- Keep executable skill logic and its tests under `skills/<name>/scripts/`. Do not embed substantial programs in `SKILL.md`.
- Add regression coverage for every behavior change, especially parsing, path handling, rendering, ownership, recovery, and runtime-host boundaries.
- Preserve fail-closed behavior. Do not weaken validation, overwrite protection, journaling, rollback, or trust boundaries to make a test pass.
- Do not add personal paths, organization-specific references, credentials, generated deployment state, or private fixtures.
- Keep pull requests small enough to review and explain any safety or compatibility tradeoffs.

For the complete skill template and metadata contract, see [Adding a skill](docs/adding-a-skill.md). For pinned dependency maintenance, see [Dependency updates](docs/dependency-updates.md).

### Upgrade notes

A change to a contract, such as a skill directory name, a `required_vars` list, a tool floor, the manifest version, or a code-review record format, states what it asks of a user who updates under `## Upgrade note` in the pull request body, in the format [Upgrade notes](docs/upgrade-notes.md) states; so does a fix or an addition a user meets. Nobody edits `docs/upgrade-notes.md` for a change: the repository squash-merges with the body as the commit message, and `tools/release_notes.py` writes each release's section from those messages before tagging. The [Versioning](docs/releasing.md#versioning) section of Releasing lists the contracts and the levels. Validation's upgrade-notes check compares those contract files at the last tag with the working tree and fails, naming the item and what the entry must say, while a changed item is named by no upgrade note since the tag. In a pull request it reads the body, which CI passes it, and the commits merged into `origin/main`; locally it reads the branch's commit messages, or the body from a file with `python -B tests/run_validation.py --pr-body '<file>'`. Editing the body reruns the check. Skill arguments, status lines, and deployer flags have no contract file, so add their entry without being asked. A change that touches nothing a user meets writes `None` under the heading.

## Validation

Validation is continuous integration's job. The `Validation` check runs the complete sequence on every push and on every pull request that is not documentation-only, a documentation-only pull request runs the subset the runner selects (see below), and a pull request merges only when the check passes. From the repository root, the sequence is:

```powershell
python -B tests/run_validation.py
```

Locally, run what the change reaches with `python -B tests/run_validation.py --changed`, which selects the policy checks, the static checks for the kinds of file changed, and the suites that the files changed since `origin/main` reach, as [Validation](docs/implementing-changes.md#validation) in the implementation profile describes; the reaches a file's name and imports miss, such as the deployer's rendering suite for a change under `skills/` and the suites that read the floors for a tool floor, are `CHANGE_REACHES` in `tests/validation/job_selection.py`. `-k <pattern>` runs only the policies and suites whose name or path matches, such as `-k upgrade_notes`, and refuses a pattern that matches nothing. Run the sequence in full locally only when the change's reach warrants it, such as a change to a validation policy or the runner. When a leg fails on the pull request, reproduce that leg locally with its `--shard` option, as below.

Fetch `main` and the tags first, as in [Getting the source](#getting-the-source). In CI the `Validation` check runs one leg per entry of the shard matrix in `.github/workflows/validate.yml`, each a `windows-latest` runner named `Windows shard <index>/<count>` that runs its share of the policy checks and suite jobs with `--shard <index>/<count>`, and the `Validation on Linux` job, which runs all of them unsharded on `ubuntu-latest`; the check passes only when every leg and the Linux job pass. The suite jobs are dealt by the median of the seconds each took across recent `main` runs, recorded in `tests/validation/job_seconds.json` and refreshed at each release, so the legs carry about the same work; a new suite with no entry is dealt by its test count until the next refresh. A leg's log and step summary name its shard and how many of the run's policy checks and suite jobs it ran; to rerun one leg's share locally, pass the same option with that leg's index and the matrix's count. The workflow passes `--full` on a push, so a push runs every suite. On a pull request the runner decides: when every changed file is documentation, such as `docs/`, `README.md`, or this file, it runs only the policy checks, the suites that name a changed file, and the Markdown fence checks, and otherwise it runs every suite; `--full` runs everything.

The runner reports each failing suite by path. A suite or check whose program was ended before it printed anything, with a termination status rather than an exit code of its own, is reported as that program's termination, not as a failing test, because nothing in it ran to a verdict. The report names the command and the status: on Windows the status in hexadecimal, such as `0x80000004`, and on Linux the name of the signal that ended it, such as `SIGKILL`. The runner starts such a job once more and says so in the output and the step summary with the retry's result, and a second termination fails the job; a job that printed anything, or that ran and failed, is never started again. When diagnosing a failure, run that suite directly, as in `python -B tests/deployer/test_recovery_migration.py`, or narrow the runner with `-k <pattern>`, as in `-k deployer`. Suites run in parallel, with large ones split into shards; set `VALIDATION_JOBS` to change the worker count.

Changes to deployment behavior should also be exercised against an isolated temporary home: `python deploy.py --canary-home <dir>` with a directory under the temporary directory, or a test that builds the deployer's paths on one. On Windows the deployer takes its home from the profile folder, not `HOME`, so setting `HOME` alone does not isolate a run. Never use ordinary development validation to deploy into the contributor's real agent directories.

### Python checks

This section is the one full account of the Python format, lint, and type checks; `CLAUDE.md` keeps the rules an agent session acts on, and [Adding a skill](docs/adding-a-skill.md#validation) adds what a skill's scripts need.

Validation checks formatting with `ruff format --check` and lints with `ruff check`, both at a line length of 120 and with the rule sets `pyproject.toml` selects, on `deployer/`, `tools/`, `tests/`, `skills/`, `.claude/skills/`, and `deploy.py`. Run `python -m ruff format` on any file the first names, and fix each finding the linter names (`python -m ruff check --fix` applies the fixes ruff marks safe). No rule or file is exempt; a finding that must stay is suppressed on its line as `# noqa: <code> - <reason>`, and validation fails on a `noqa` without its codes and reason. It also fails on a comment that turns a check off beyond one finding on its line: `# ruff: noqa` or `# flake8: noqa` for a whole file, `# ruff: disable[...]` for the lines up to its `# ruff: enable[...]`, an isort skip, the formatter's `fmt: off`, `fmt: skip`, or `yapf: disable`, and mypy's inline `# mypy:` configuration, such as `ignore-errors`; and on a `ruff.toml`, `.ruff.toml`, or `pyproject.toml` anywhere but the repository root, which ruff would read in place of the root's rule set for every file beneath it.

A comment or docstring states its reason in plain words, never as an issue number such as `#12`, `(#12)`, `#12's`, or `issue 12`, because a deployed skill's reader has only the source; validation fails on one in the Python under those roots, and string literals, the fixture trees under `tests/fixtures/`, and the templates' `Closes #N` are outside the rule.

Function complexity (C901) and length in statements (PLR0915) are the exception: they take no `noqa`, and validation fails on one that names either. Their ceilings, `max-complexity` and `max-statements` in `pyproject.toml`, are a ratchet that only goes down. Validation fails a change that raises either ceiling, so split a function that exceeds them into helpers of one concern; a pull request that splits the function at a ceiling lowers it to the new maximum, and the literal its test pins, in the same change.

The bandit security rules (`S`) are selected except S603 and S607, which flag every subprocess call made without a shell and every program started by its PATH name. The repository runs declared tools as argument lists found on PATH, which the checks under [Commands skills may run](docs/adding-a-skill.md#commands-skills-may-run) hold instead, so those two describe the design and are left unselected rather than suppressed on every call.

Validation type-checks with `mypy` at its default strictness and with the `[tool.mypy]` configuration in `pyproject.toml`, once on `deployer/`, `tools/`, `deploy.py`, and `tests/` together and once on each shipped or repository skill's `scripts/` directory from inside it, and names each error. The configuration sets no platform, so mypy checks each module as the host running it sees it, and code for another platform carries a `type: ignore` naming that platform. Fix it, or, where an error must stay, write `# type: ignore[<code>]  # <reason>` on its line; validation fails on a `type: ignore` without its codes and reason. Regression suites are type-checked too, so a fixture that is malformed on purpose is typed as the loose shape it is, never silenced. `mypy_path` lists each `skills/<name>/scripts` directory a module or suite puts on `sys.path`, and each test directory whose suites import a module beside them by its bare name; validation fails until the list matches.

## How `main` is protected

`main` changes only through a pull request, and its branch protection is what keeps it green:

- the `Validation` check is required, and it is strict: a branch must be up to date with `main` before it can merge, so a pull request that is behind is refused until `origin/main` is merged into it;
- linear history is required, and the repository allows squash merges only, with merge commits and rebase merges turned off, so each pull request lands as one commit;
- the squash commit takes the pull request's title as its title and its body as its message, so each change's upgrade note reaches `main`'s history, which the release notes are written from;
- unresolved review conversations block the merge until each one is resolved;
- no approval is required, so a passing, current, resolved pull request can merge;
- force pushes to `main` and deleting it are refused; and
- the rules are enforced for administrators too, so nobody bypasses them.

The repository's security settings back this up: secret scanning, push protection, private vulnerability reporting, and Dependabot security updates are enabled, and the default workflow token is read-only.

The workflows themselves are held by `tests/validation/workflows.py`. Every file under `.github/workflows/` is one `WORKFLOW_GUARDS` there names, with the triggers, actions, and crons it expects, and validation fails on any other file, so a new workflow is added to `WORKFLOW_GUARDS` in the pull request that adds the file. Each one it names is held to those triggers and crons, `contents: read` at the top, no job permission beyond reading, no secret and no `GITHUB_TOKEN`, and every action pinned to a full commit SHA with its version in a comment. An expression may read the `github` context only as `github.event_name`, `github.ref`, or `github.workflow`, which hold no text a pull request's author writes: the whole context, the event payload, `github.token`, and any field reached through `toJSON`, `fromJSON`, `format`, or brackets fail, and so does an expression the policy cannot read. A step reads the event payload from the file `GITHUB_EVENT_PATH` names, as `validate.yml` reads a pull request's body and title.

Branch protection and these settings are live repository settings, not tracked files, so they can drift without a diff. `python tools/branch_protection.py` reads them, the merge settings, and the two issue settings [Questions](#questions) names through `gh api` and prints `PROTECTED`, or `DRIFTED` and each invariant that no longer holds, then each security setting as it found it; reading branch protection and the security settings needs admin rights on the repository. The [release procedure](docs/releasing.md#before-tagging) runs it before every tag.

## Issues and pull requests

Open issues from the templates in `.github/ISSUE_TEMPLATE/`, one per kind of issue, and pull requests from `.github/pull_request_template.md`, whose Validation checklist is the list below. From the command line, write the body to a file based on the template and pass it with `gh issue create --body-file` or `gh pr create --body-file`; `--body` skips the template. Report a suspected vulnerability as the [Security policy](SECURITY.md) describes, never in an issue.

The [project board](https://github.com/users/EJRyan89/projects/1) is the view of the issues by release: every issue on a milestone is an item on it, grouped by milestone, and milestones remain the record of scope. A pull request's `Closes #<number>` line is what moves its issue across the board, from Todo to In Progress when the pull request opens and to Done when it merges, so the line in step 4 below is required, not a courtesy, for every issue the change resolves: an issue a change resolved without its own line stays open.

A pull request's title becomes its squash commit's title, so `main`'s history reads by the titles. The title opens with a type, an optional scope in parentheses, a colon, and a space, as in `fix(deployer): ...`. The types are `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `ci`, and `build`, and [Pull requests](docs/implementing-changes.md#pull-requests) in the implementation profile says what each covers. The type agrees with the worktree's branch kind when the kind is one of them, and a title carries no `!`: the Level under `## Upgrade note` is the one statement of a release level. Validation's title check reads the title from the pull request in CI, and locally from a file with `python -B tests/run_validation.py --pr-title '<file>'`; editing the title reruns it.

To open a pull request from the task's worktree:

1. Commit the change, and run the local checks [Validation](#validation) names until they pass.
2. Bring the branch current with `main`: run `git fetch origin main --tags` and `git rebase origin/main`. The branch is unpublished, so the rebase rewrites nothing anyone has. CI validates the result, so it needs no local rerun.
3. Push the branch, to `origin` or, without push access, to `fork`, as in `git push -u fork '<kind>/<name>'`. With two remotes, run `gh repo set-default EJRyan89/coding-agent-skills` once, so `gh` opens pull requests here.
4. Write the body from `.github/pull_request_template.md` to a file, keeping its headings, name each issue the change resolves in its own `Closes #<number>` line, and open it with `gh pr create --title '<title>' --body-file '<file>'`, the title typed as above.
5. While it is open, merge `origin/main` into the branch to bring it current, never rebase or force-push it. `gh pr update-branch` is one way to make that merge; it moves the remote branch, so pull it into the worktree before the next push. The merge needs no local rerun: CI runs on it. If a push is rejected, fetch and inspect the remote branch first: the maintainer may have updated it.

The maintainer merges; the repository squash-merges only, so the pull request lands as one commit.

The template's sections ask for the problem and the chosen behavior, and the user-visible, compatibility, or security implications. Before requesting review, check each item of its Validation checklist:

- the local checks [Validation](#validation) names for the touched files pass, with no skipped prerequisites, and the `Validation` check runs every suite, or the subset the runner selects when the change is documentation-only;
- regression coverage is added or updated for every behavior change;
- for a change to skill paths, `allowed-tools`, runtime adapters, or agents, the `runtime-canary` lines are in the body, with each runtime's version and any `SKIPPED` or `NOTHING_CHECKED` reason;
- for a change to a skill's prompt, model guidance, or reviewer instructions, the `evaluate-skill` run is cited with its table, and `docs/skill-evaluations.md` holds its result;
- for a skill change, `analyze-skill-cost` audited each changed skill from the source tree with no MUST FIX left, and any SUGGESTION left is named with why;
- for a contract change, the body's `## Upgrade note` has an entry naming each changed contract item;
- documentation is updated in the same change; and
- the diff is reviewed for personal paths, organization names, credentials, and generated artifacts.

Under the checklist, name anything that could not be run, and why.

## Questions

Ask a question in an issue from the question template, which applies the `question` label. Discussions stay off, so the issue tracker is the one place to ask, and blank issues stay off (`blank_issues_enabled: false` in `.github/ISSUE_TEMPLATE/config.yml`), so every issue starts from a template. `python tools/branch_protection.py`, described in [How `main` is protected](#how-main-is-protected), reports `DRIFTED` when either is on.

An absent issue template configuration on `main` leaves blank issues enabled, so the check reports `DRIFTED` with exit 1. A whitespace-separated YAML comment after `blank_issues_enabled: false` does not change that setting. Authentication, permission, network, and other read failures still report `ERROR` with exit 2; unsupported or unreadable configuration content still counts as blank issues enabled.

## Gates the maintainer runs

Some gates need more than a clone and the validation tools. Run them when you can and put their output in the pull request; when you cannot, say so under the checklist, and the maintainer runs them before merging:

- `runtime-canary`, for a change to skill paths, `allowed-tools`, runtime adapters, or agents. It needs Claude Code, Codex, and Copilot CLI installed and signed in, and each run calls a model.
- `evaluate-skill`, for a change to a skill's prompt, model guidance, or reviewer instructions when the skill has scenarios under `tests/fixtures/skill-evals/`. It runs headless Claude Code sessions, so it needs Claude Code signed in, and each run calls models.
- `analyze-skill-cost`, for a skill change. It is a shipped skill, so it runs from a deployment of this suite into your own profile ([Installation](docs/installation.md)); run from the worktree, it audits the source tree, not the deployed copy. Without a deployment, leave it to the maintainer.
- The `deployable` workflow, which deploys onto a fresh GitHub-hosted runner. It is dispatched by hand before each release ([Releasing](docs/releasing.md#before-tagging)), not for each pull request.

By contributing, you agree that your contribution is licensed under the repository's [MIT License](LICENSE).
