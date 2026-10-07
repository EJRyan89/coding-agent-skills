# Dependency updates

This document owns every dependency the repository pins or checks against a floor: where each is declared, how it is updated and reviewed, and the check that fails when it drifts. Change a pin or a floor here and in its declaration in the same pull request, then run the complete validation:

```powershell
python -B tests/run_validation.py
```

## Validation tools

`tests/run_validation.py` stops before running any test when one of these tools is missing or older than its floor, and names the tool, the version it found, and the floor. CI installs Python, ShellCheck, PSScriptAnalyzer, ruff, and mypy at their floors, so each floor is exercised on every run.

| Tool | Floor | Declared in | How it is reviewed |
|---|---|---|---|
| Python | 3.11 | `MINIMUM_PYTHON` in `deployer/tools.py`; the CI matrix; `docs/installation.md`, `CONTRIBUTING.md`, and the README | CI runs every suite on 3.11 on every run, and the weekly scheduled run adds the latest stable release (`3.x`), so a new interpreter surfaces there as a red scheduled run without blocking any pull request. Raise the floor only when its release is past end of life or the code needs a newer feature, and update every place that names it. |
| ShellCheck | 0.9.0 | `VALIDATION_FLOORS` in `tests/run_validation.py`; the Chocolatey pin in `.github/workflows/validate.yml` | The weekly scheduled run validates a second time with the latest Chocolatey release, which is the standing review of new releases. A red scheduled run means a new release raises a finding; fix the finding rather than pinning around it, because deployment stops on any ShellCheck finding on users' machines. Raise the pin and floor together. Chocolatey is not a Dependabot ecosystem. |
| PSScriptAnalyzer | 1.25.0 | `VALIDATION_FLOORS` in `tests/run_validation.py`; the `Install-Module -RequiredVersion` pin in `.github/workflows/validate.yml` | A PowerShell module from the PowerShell Gallery, installed with `pwsh -Command 'Install-Module PSScriptAnalyzer -Scope CurrentUser -Force'`; validation loads the newest installed version. The gallery is not a Dependabot ecosystem, so the verify step prints the installed versions and the latest release, which is the standing review. To adopt a release, raise the pin and floor together and fix every new finding at its cause in the same pull request; no rule is suppressed, and there is no settings file. Validation-only: deployment does not run it. |
| PowerShell 7 (`pwsh`) | 7.0 | `VALIDATION_FLOORS` in `tests/run_validation.py` | Arrives unpinned with the runner image; the verify step prints its version. Deployment itself needs only Windows PowerShell 5.1 (`docs/installation.md`). |
| ruff | 0.16.10 | `requirements-dev.txt`; `VALIDATION_FLOORS` in `tests/run_validation.py` | The floor is the pin, because a new minor release can change the formatting style or the lint rules and fail `ruff format --check` or `ruff check` on an unchanged tree. Dependabot proposes new releases; see [Development dependencies](#development-dependencies). |
| mypy | 2.4.0 | `requirements-dev.txt`; `VALIDATION_FLOORS` in `tests/run_validation.py` | The floor is the pin, because a new release can report new errors in an unchanged tree and fail the type check. Dependabot proposes new releases; see [Development dependencies](#development-dependencies). |
| Git Bash | none | Git for Windows | The repository's shell scripts use no Bash 4 features, so no floor is checked. Arrives with the runner image; the verify step prints its version. |

## Development dependencies

Validation and CI take two Python packages from PyPI, pinned in `requirements-dev.txt`, and no others: `ruff`, the formatter and linter, and `mypy`, the type checker. They are contributor and CI dependencies only. The deployer and every shipped skill stay standard-library, so deploying needs neither. Install them into the interpreter that runs validation:

```powershell
python -m pip install -r requirements-dev.txt
```

`pyproject.toml` configures the tools and installs nothing, so it has no `[project]` table. The line length is 120. `tests/run_validation.py` runs `ruff format --check` and `ruff check` on `deployer/`, `tools/`, `tests/`, `skills/`, and `deploy.py`, excluding none. The format check names every file it would change; run `python -m ruff format` on those files to fix it. The lint check names every finding of the rule sets `pyproject.toml` selects; fix each one. Validation also runs mypy, with the `[tool.mypy]` configuration in `pyproject.toml`, once on `deployer/`, `tools/`, `deploy.py`, and `tests/` together and once on each skill's `scripts/` directory, and names every error, regression suites included.

Dependabot (`.github/dependabot.yml`) checks `requirements-dev.txt` weekly, on the same cadence as the actions, and groups the bumps into one pull request, with security updates in their own group. A new development dependency is a decision recorded here first.

### After a Dependabot pull request for ruff

A ruff bump raises the floor with it. In the same pull request, set `VALIDATION_FLOORS` in `tests/run_validation.py`, the row above, and the pin `test_ci_exercises_each_floor` checks to the new version, then run `python -m ruff format` on the five roots and commit the result on its own, and fix anything `ruff check` newly names in a commit of its own. Run the complete validation before merging.

### After a Dependabot pull request for mypy

A mypy bump raises the floor with it. In the same pull request, set `VALIDATION_FLOORS` in `tests/run_validation.py`, the row above, and the pin `test_ci_exercises_each_floor` checks to the new version, then fix each error the type check newly names, one commit per root. Run the complete validation before merging.

## Runtime tool floors

`python deploy.py check` reports each tool a skill declares and each runtime `python deploy.py verify` lists, with its version, and marks one older than its floor; `verify` reports a runtime below its floor as `OUTDATED` instead of listing its skills. Raise a floor in `deployer/tools.py` (`SKILL_TOOLS`, or `VERIFY_TOOLS` for the runtimes) only when a skill or `verify` starts using a newer feature, with a comment naming that feature, as the `gh` and `codex` entries have.

| Tool | Floor | Why |
|---|---|---|
| `gh` | 2.48.0 | `gh api --paginate --slurp`, which skill scripts use to parse paginated output as JSON. |
| `codex` | 0.88.0 | The first release whose app-server `skills/list` answer says whether each skill is enabled, which `verify` reads to report a disabled adapter. |
| `copilot` | 1.0.88 | The bounded Copilot code-review host requires it, and declares it again as `MINIMUM_COPILOT_CLI_VERSION` in `skills/code-review-core/scripts/review_hosts.py`; change both together, along with `docs/installation.md`, `docs/copilot-support.md`, and `docs/code-review-operations.md`. |

## Tested runtimes

The README's "Supported platforms and runtimes" table records the Claude Code, Codex CLI, and Copilot CLI versions the skills were last checked with, in two columns. These are records, not floors, and the table stays a matrix of the latest versions, not a log. Update the maintainer's column after the `runtime-canary` repository skill passes on a new version. The fresh-runner column and the date under the table follow the `deployable.yml` workflow: the column equals that workflow's default `claude-version`, `codex-version`, and `copilot-version`, which validation enforces, so change them together after a run passes. Confirm every cell at each release ([Releasing](releasing.md)).

## GitHub Actions

Every action in `.github/workflows/validate.yml` is pinned to a full commit SHA with its version in a comment beside it. Dependabot (`.github/dependabot.yml`) checks the pins weekly and groups the bumps into one pull request; with Dependabot security updates enabled for the repository, a security advisory opens its own grouped pull request. Review the upstream release notes and the proposed commit before merging, and keep the version comment.

`.github/workflows/deployable.yml` pins its actions the same way, and Dependabot bumps it in the same grouped pull request. `tests/run_validation.py` fails unless every action it shares with `validate.yml` carries the same SHA and version comment, so a Dependabot pull request that touches only one of them cannot merge green. The Codex CLI and Copilot CLI versions it installs by default are the README's tested versions, and validation fails when they differ; change the README row and the workflow default together.

### After a Dependabot pull request

No file outside the two workflows carries these pins, so the pull request needs no follow-up commit. Once its CI passes and the review above is done, merge it.

## Runner image

CI runs on `windows-latest`, which GitHub updates on its own schedule, along with the Git Bash, PowerShell, and Python patch releases on it. CI runs on every push to `main` and weekly as well as on pull requests, so an image change surfaces within a week even when no pull request is open, and the verify step prints every tool's version for comparison with the last green run. Pin a dated image such as `windows-2025` only to unblock CI, and open an issue to return to `windows-latest`.
