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

Anything else is planned first. The pull request states which way it went and why.

## Worktree

From the hub, run `python tools/worktrees.py new <kind> <name>` with `feat` or `fix` as the kind, then enter the
printed path (in Claude Code, `EnterWorktree` with `path:`). [Parallel sessions](parallel-sessions.md) explains why
and how a worktree is retired.

## Validation

Run `python -B tests/run_validation.py` in full. `-k <pattern>` narrows it while iterating only. A change to skill
paths, `allowed-tools`, runtime adapters, or agents also runs the `runtime-canary` repository skill, and a change to a
skill under `skills/` or `.claude/skills/` also runs `analyze-skill-cost` on it. `CLAUDE.md` ("Required validation")
holds the rules validation enforces.

## Contract files

A change to any of these is a contract change and raises the release level (the Versioning section of
[releasing.md](releasing.md) holds the contracts and levels):

- `MANIFEST_VERSION` and `OLDEST_READABLE_VERSION` in `deployer/manifest.py`;
- a `required_vars` list under `deploy-meta/`;
- `skills/code-review-core/references/review-adapter.schema.json`, or a format table in
  `docs/code-review-operations-contract.md`;
- a skill directory name, or a skill's arguments or status lines;
- the code-review configuration file's format and the reviewer manifest format.

## Documentation

Each fact has one owner; update the owner in the same change.

| Document | Owns |
|---|---|
| `CLAUDE.md` | Architecture, safety rules, validation and pull request rules for every agent runtime. |
| `docs/skills.md` | Each skill's reference. Generated blocks come from `python tools/skill_reference.py --write`. |
| `README.md` | The "Included skills" table and the front-door summary. |
| `docs/adding-a-skill.md` | The skill contract. |
| `docs/parallel-sessions.md` | Worktrees and the hub guard. |
| `docs/releasing.md` | Versioning, contracts, and the release procedure. |
| `docs/code-review-operations.md` | Code-review configuration and records. |
| `docs/installation.md`, `docs/recovery.md` | Install, update, and recovery. |
| `docs/codex-support.md`, `docs/copilot-support.md` | Per-runtime support. |

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
