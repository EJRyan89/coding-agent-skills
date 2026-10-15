# Implementing changes

This is the implementation profile for this repository. The `implement-change` repository skill
(`.claude/skills/implement-change/`) carries the generic procedure and reads every repository-specific fact from the
sections below. `tests/tools/test_implementing_changes_profile.py` fails when a section goes missing. The skill that
adds or changes a shipped skill, `change-skill`, builds on `implement-change`.

## Size gate

A change skips the plan and is implemented directly only when all of these hold:

- one component changes (one skill, one deployer module, one tool, or one document);
- the fix is known before exploring;
- no design choice remains;
- no contract file is touched.

Anything else is planned first.

## Worktree

From the hub, run `python tools/worktrees.py new <kind> <name>` with the change's title type as the kind, such as
`feat` or `fix` (see [Pull requests](#pull-requests)), then enter the printed path (in Claude Code, `EnterWorktree`
with `path:`). [Parallel sessions](parallel-sessions.md) explains why and how a worktree is retired.

## Validation

Validation is continuous integration's job: the `Validation` check runs `python -B tests/run_validation.py`, split
across its Windows legs and whole on Linux, in full on every push and on every pull request that is not
documentation-only. A documentation-only pull request runs the subset the runner selects (the policy checks, the suites
that name a changed file, and, when a changed file is Markdown, the Markdown fence checks), and a pull request merges
only when the check passes on both platforms. Locally, run what the change touches:

- `python -m ruff format --check` and `python -m ruff check` on the changed Python paths;
- `python -B tests/run_validation.py -k <pattern>` for the policies and suites that name the touched files, with
  `--pr-body <file>` and `--pr-title <file>` once the body and the title are written;
- for a change to a shipped skill's scripts, also the deployer's rendering tests, `-k rendering_locking`;
- for a change to a tool floor, also the suites that read the floors, `-k toolchain -k upgrade_notes -k test_check`.

Run the suite in full locally only when the change's reach warrants it, such as a validation policy, the runner, or a
shared module whose callers `-k` does not capture. When a leg fails in continuous integration, reproduce that leg with
`python -B tests/run_validation.py --shard <index>/<count>`, adding `--full` for a push, schedule, or dispatch leg,
which the workflow runs with it: the runner deals the legs after it selects, so without `--full` a branch whose changes
are all documentation deals a different leg. A pull-request leg needs no `--full`. Bringing an open pull request current
with its base needs no local rerun, since continuous integration runs on the result.

The gates that call a model never run in continuous integration, so they stay with the author: a change to skill
paths, `allowed-tools`, runtime adapters, or agents runs the `runtime-canary` repository skill, a change to a skill's
prompt, model guidance, or reviewer instructions runs `evaluate-skill` when the skill has scenarios, and a change to a
skill under `skills/` or `.claude/skills/` runs `analyze-skill-cost` on it. `CLAUDE.md` ("Required validation") holds
the rules validation enforces.

## Contract files

A change to any of these is a contract change, raises the release level, and is named under `## Upgrade note` in the
pull request body (the Versioning section of [releasing.md](releasing.md) holds the contracts and levels):

- `MANIFEST_VERSION` and `OLDEST_READABLE_VERSION` in `deployer/manifest.py`;
- a `required_vars` list under `deploy-meta/`;
- a schema under `skills/code-review-core/references/`, or a format table in
  `docs/code-review-operations-contract.md`, among them the code-review configuration file's format and the reviewer
  manifest format;
- a skill directory name under `skills/`, or a skill's arguments or status lines;
- a tool floor in `deployer/tools.py`: `MINIMUM_PYTHON`, or a tool's `minimum`.
- a provider-specific PDF tool floor in `skills/skill-core/scripts/pdf_tool.py`, referenced by the catalogue's
  `provider_minimum`.

The upgrade-notes check in `tests/validation/upgrade_notes.py` compares every item above at the last tag except the
arguments and the status lines, which no file holds, and fails while this list or the Versioning section of
[releasing.md](releasing.md) leaves out a file or directory it reads.

## Pull requests

A pull request's title becomes its squash commit's title, so `main`'s history reads by the titles. The title opens
with a type, an optional scope in parentheses, a colon, and a space, as in `fix(deployer): ...`:

- `feat` adds or extends a capability;
- `fix` corrects a defect;
- `docs` changes documentation only;
- `test` adds or changes tests only;
- `refactor` changes structure without changing behavior;
- `chore` is upkeep that changes no behavior, such as a rename or a recorded table;
- `ci` changes a workflow;
- `build` changes the development dependencies or the deployment prerequisites.

The type agrees with the worktree's branch kind when the kind is one of them. A title carries no `!` and the body no
breaking-change footer: the Level under `## Upgrade note` is the one statement of a release level. Validation's title
check, in `tests/validation/pull_request_title.py`, reads the title from `--pr-title <file>` locally and from the pull
request in continuous integration.

The body names every issue the change resolves, each in its own `Closes #<number>` line. The project board moves an
issue by that line, and an issue a change resolved without one stays open.

## Documentation

Each fact has one owner; update the owner in the same change.

| Document | Owns |
|---|---|
| `CLAUDE.md` | Architecture, safety rules, validation and pull request rules for every agent runtime. |
| `docs/design.md` | Why the repository is built as it is: the trust model, the invariants, and each decision with its reason, date, and source, and what was declined. |
| `docs/skills.md` | Each skill's reference. Generated blocks come from `python tools/skill_reference.py --write`. |
| `README.md` | The "Included skills" table and the front-door summary. |
| `docs/adding-a-skill.md` | The skill contract. |
| `docs/parallel-sessions.md` | Worktrees and the hub guard. |
| `docs/releasing.md` | Versioning, contracts, and the release procedure. |
| `docs/code-review-operations.md` | Code-review configuration and records. |
| `docs/installation.md`, `docs/recovery.md` | Install, update, and recovery. |
| `docs/codex-support.md`, `docs/copilot-support.md` | Per-runtime support. |
| `CONTRIBUTING.md` | The contributor path from a clone to a merged pull request, and the full account of the Python format, lint, and type checks (`CLAUDE.md` keeps the rules a session acts on). |
| `SECURITY.md` | The supported line, how to report a vulnerability, the disclosure policy for advisories, and the security-sensitive areas. |
| `docs/dependency-updates.md` | Every pin and floor: where it is declared, how it is updated and reviewed, and the check that holds it. |
| `docs/upgrade-notes.md` | What each release changes for an installation, one entry per change a user meets, written at release from each pull request body's `## Upgrade note`. |
| `docs/code-review-operations-contract.md` | The behavior the code-review skills promise to keep, and the formats of their records. |
| `docs/skill-evaluations.md` | The latest `evaluate-skill` result per skill and model, written by `python tools/skill_evals.py --write`. |

## Model guidance

The strongest model plans, and implements, a change in any of these areas, because a subtle error there passes tests
and ships:

- rendering and token substitution into Bash, PowerShell, JSON, or YAML, and quoting;
- trust boundaries: the configuration parser, ownership, journaled rollback, locking, and backups;
- the code-review pipeline and its records;
- any contract file above.

A balanced model suffices for a concrete plan that follows an existing pattern, such as a new tool test, a
documentation change, or a skill whose scripts and tests mirror an existing one. The cheapest model suits a fully
specified mechanical edit whose test already exists.

## Shared code

Shared code has one home. A module that more than one of the skills, the deployer, and `tools/` need lives in
`skills/skill-core/scripts`, and each imports it from there; never copy it. [Validation](adding-a-skill.md#validation)
in the skill contract states how each part reaches it and what validation holds.
