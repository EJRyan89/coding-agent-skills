# Contributing

Thank you for helping improve Coding Agent Skills. Keep changes focused, preserve the safety model, and include regression coverage for behavioral changes.

## Development environment

Development and deployment are currently supported on Windows only. Contributors need:

- Python 3.11 or newer;
- Git for Windows, for Git Bash;
- ShellCheck;
- PowerShell 7 (`pwsh`); and
- GitHub CLI for code-review-operation changes.

[Installation](docs/installation.md#installing-the-tools) lists install commands. After installing a tool, open a new terminal so it is on `PATH`; `tests/run_validation.py` stops before running any test and lists every missing tool and every tool older than its floor in [Dependency updates](docs/dependency-updates.md).

Run the validation entry point from PowerShell or Git Bash, and use Git Bash for Bash scripts. Follow `.editorconfig` and `.gitattributes`; do not commit generated build, test, Python-cache, IDE, deployment, or personal configuration artifacts.

## Making changes

- Treat `skills/`, `deploy-meta/`, `source.json`, and the deployer files in this repository as authoritative. Do not edit installed copies under a user profile as the source of a change.
- Keep executable skill logic and its tests under `skills/<name>/scripts/`. Do not embed substantial programs in `SKILL.md`.
- Add regression coverage for every behavior change, especially parsing, path handling, rendering, ownership, recovery, and runtime-host boundaries.
- Preserve fail-closed behavior. Do not weaken validation, overwrite protection, journaling, rollback, or trust boundaries to make a test pass.
- Do not add personal paths, organization-specific references, credentials, generated deployment state, or private fixtures.
- Keep pull requests small enough to review and explain any safety or compatibility tradeoffs.

For the complete skill template and metadata contract, see [Adding a skill](docs/adding-a-skill.md). For pinned dependency maintenance, see [Dependency updates](docs/dependency-updates.md).

## Validation

Run the complete validation sequence from the repository root:

```powershell
python -B tests/run_validation.py
```

The runner reports each failing suite by path. When diagnosing a failure, run that suite directly, as in `python -B tests/deployer/test_recovery_migration.py`, or narrow the runner with `-k <pattern>`, as in `-k deployer`. Suites run in parallel, with large ones split into shards; set `VALIDATION_JOBS` to change the worker count.

Changes to deployment behavior should also be exercised against an isolated temporary home: `python deploy.py --canary-home <dir>` with a directory under the temporary directory, or a test that builds the deployer's paths on one. On Windows the deployer takes its home from the profile folder, not `HOME`, so setting `HOME` alone does not isolate a run. Never use ordinary development validation to deploy into the contributor's real agent directories.

## Issues and pull requests

Open issues and pull requests from the templates in `.github/`: an enhancement, bug, or documentation issue template, and the pull request template, whose Validation checklist mirrors the list below. From the command line, write the body to a file based on the template and pass it with `gh issue create --body-file` or `gh pr create --body-file`; `--body` skips the template. Report a suspected vulnerability as the [Security policy](SECURITY.md) describes, never in an issue.

Open a pull request current with `main`: rebase the branch onto `origin/main` before opening it, and merge `origin/main` into it, rather than rebasing, before pushing to it once it is open. Rerun the validation sequence whenever that brings in commits.

Before requesting review:

- describe the problem and the chosen behavior;
- identify user-visible, compatibility, or security implications;
- add or update tests and documentation;
- confirm the complete validation sequence passes without skipped prerequisites;
- for a change to skill paths, `allowed-tools`, runtime adapters, or agents, run the `runtime-canary` repository skill and include its lines, with each runtime's version and any `SKIPPED` reason;
- for a skill change, audit each changed skill from the source tree with `analyze-skill-cost`, leave no MUST FIX, and name any SUGGESTION left with why;
- review the diff for private references and generated artifacts; and
- report any validation that could not be run and why.

By contributing, you agree that your contribution is licensed under the repository's [MIT License](LICENSE).
