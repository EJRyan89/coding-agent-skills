# Dependency updates

This document owns every dependency the repository pins or checks against a floor: where each is declared, how it is updated and reviewed, and the check that fails when it drifts. Change a pin or a floor here and in its declaration in the same pull request, then run the complete validation:

```powershell
python -B tests/run_validation.py
```

## Validation tools

`tests/run_validation.py` stops before running any test when one of these tools is missing or older than its floor, and names the tool, the version it found, and the floor. CI installs Python and ShellCheck at their floors, so each floor is exercised on every run.

| Tool | Floor | Declared in | How it is reviewed |
|---|---|---|---|
| Python | 3.11 | `MINIMUM_PYTHON` in `deployer/tools.py`; the CI matrix; `docs/installation.md`, `CONTRIBUTING.md`, and the README | CI runs every suite on 3.11 and on the latest stable release (`3.x`), so a new interpreter surfaces on the next weekly run. Raise the floor only when its release is past end of life or the code needs a newer feature, and update every place that names it. |
| ShellCheck | 0.9.0 | `VALIDATION_FLOORS` in `tests/run_validation.py`; the Chocolatey pin in `.github/workflows/validate.yml` | The weekly scheduled run validates a second time with the latest Chocolatey release, which is the standing review of new releases. A red scheduled run means a new release raises a finding; fix the finding rather than pinning around it, because deployment stops on any ShellCheck finding on users' machines. Raise the pin and floor together. Chocolatey is not a Dependabot ecosystem. |
| PowerShell 7 (`pwsh`) | 7.0 | `VALIDATION_FLOORS` in `tests/run_validation.py` | Arrives unpinned with the runner image; the verify step prints its version. Deployment itself needs only Windows PowerShell 5.1 (`docs/installation.md`). |
| Git Bash | none | Git for Windows | The repository's shell scripts use no Bash 4 features, so no floor is checked. Arrives with the runner image; the verify step prints its version. |

## Runtime tool floors

`python deploy.py check` reports each tool a skill declares, with its version, and marks one older than its floor. Raise a floor in `SKILL_TOOLS` in `deployer/tools.py` only when a skill starts using a newer feature, with a comment naming that feature, as the `gh` entry has.

| Tool | Floor | Why |
|---|---|---|
| `gh` | 2.48.0 | `gh api --paginate --slurp`, which skill scripts use to parse paginated output as JSON. |
| `copilot` | 1.0.88 | The bounded Copilot code-review host requires it, and declares it again as `MINIMUM_COPILOT_CLI_VERSION` in `skills/code-review-core/scripts/review_hosts.py`; change both together, along with `docs/installation.md`, `docs/copilot-support.md`, and `docs/code-review-operations.md`. |

## Tested runtimes

The README's "Supported platforms and runtimes" table records the Claude Code, Codex CLI, and Copilot CLI versions the skills were last checked with. These are records, not floors: update a row after the `runtime-canary` repository skill passes on a new version, and confirm every row at each release ([Releasing](releasing.md)).

## GitHub Actions

Every action in `.github/workflows/validate.yml` is pinned to a full commit SHA with its version in a comment beside it. Dependabot (`.github/dependabot.yml`) checks the pins weekly and groups the bumps into one pull request; with Dependabot security updates enabled for the repository, a security advisory opens its own grouped pull request. Review the upstream release notes and the proposed commit before merging, and keep the version comment.

`.github/workflows/deployable.yml` pins its actions the same way, and Dependabot bumps it in the same grouped pull request. `tests/run_validation.py` fails unless every action it shares with `validate.yml` carries the same SHA and version comment, so a Dependabot pull request that touches only one of them cannot merge green. The Codex CLI and Copilot CLI versions it installs by default are the README's tested versions, and validation fails when they differ; change the README row and the workflow default together.

The `init-ai-config` skill generates workflows that pin the same actions: its generator `skills/init-ai-config/scripts/ai_config_template.py`, the generator's test, and any `skills/init-ai-config/references/*.yml`. `tests/ai-config/test_cross_skill_contracts.py` fails until they match `validate.yml`, which Dependabot alone never edits.

### After a Dependabot pull request

1. Check out the Dependabot branch in a worktree.
2. Run `python tools/sync_action_pins.py`. It copies each SHA and version comment from `validate.yml` into the files above; `--check` reports stale files without changing them.
3. Run the complete validation, commit, and push to the Dependabot branch, then confirm CI passes before merging.

The synced files are part of the `init-ai-config` skill, so the pull request ships a skill change; the release that includes it says so.

## Runner image

CI runs on `windows-latest`, which GitHub updates on its own schedule, along with the Git Bash, PowerShell, and Python patch releases on it. CI runs on every push to `main` and weekly as well as on pull requests, so an image change surfaces within a week even when no pull request is open, and the verify step prints every tool's version for comparison with the last green run. Pin a dated image such as `windows-2025` only to unblock CI, and open an issue to return to `windows-latest`.
