---
name: change-skill
description: >-
  Procedure for adding or changing a skill in this repository, shipped under skills/ or repository-only under
  .claude/skills: its SKILL.md, scripts, metadata, or documentation. Use it when asked to add, create, change, fix,
  or extend a skill here. Not for deployer-only, repository-tool, or documentation-only changes.
allowed-tools: ["Bash(python -B .claude/skills/change-skill/scripts/change_skill.py *)", "PowerShell(python -B .claude/skills/change-skill/scripts/change_skill.py *)", "Bash(python tools/new_skill.py *)", "PowerShell(python tools/new_skill.py *)", "Bash(python tools/skill_shims.py --write)", "PowerShell(python tools/skill_shims.py --write)", "Bash(python tools/skill_reference.py --write)", "PowerShell(python tools/skill_reference.py --write)", "Bash(python tools/skill_reference.py)", "PowerShell(python tools/skill_reference.py)", "Bash(python tools/runtime_prompts.py --write)", "PowerShell(python tools/runtime_prompts.py --write)", "Bash(python -B tests/run_validation.py *)", "PowerShell(python -B tests/run_validation.py *)"]
---

# Adding or changing a skill

A skill change is finished only when it is tested, documented, validated, and audited in one pull request. This procedure adds the skill-specific steps to the `implement-change` repository skill: follow it for the worktree, intake, size gate, plan, and handoff. Do not start editing before the plan is approved.

`CLAUDE.md` holds the rules this procedure applies, and `docs/adding-a-skill.md` the skill contract. A quoted name, such as "Granting tools", is a section of the contract unless another document is named. Read only that section, which this prints from its heading to the next, adding `--document` and the document's path, such as `docs/implementing-changes.md`, for another document:

```bash
python -B .claude/skills/change-skill/scripts/change_skill.py section "<title>"
```

It prints `SECTION "<document>" <first>-<last> <heading>`, then the section. When no single heading matches the title, it prints `HEADING <line> <heading>` for each heading of the document before a last line `FAILED <reason>`: run it again with the title from that listing. Any other `FAILED <reason>` line is a stop to report.

A repository skill, under `.claude/skills/<name>/`, meets the shipped standard but has no `deploy-meta` entry, `docs/skills.md` section, or README row; its shim `.agents/skills/<name>/SKILL.md` carries the same frontmatter, and `tools/skill_shims.py` writes it.

## 1. Plan the skill

A skill change never passes the size gate's "no design choice remains" lightly, so plan it. Read the skill's `SKILL.md`, its `deploy-meta/<name>.json`, its scripts and their tests, and its section in `docs/skills.md`. In the plan settle, with the user where it is a real choice:

- **Arguments**: the `argument-hint`, in the notation "Reading the argument syntax" in `docs/skills.md` explains, and what the skill does without them.
- **Who starts it**: `disable-model-invocation: true` for a skill that deletes, deploys, or otherwise acts on the user's behalf; `user-invocable: false` too for a hidden dependency. Never set either on a skill another skill invokes by name ("Files" in `docs/adding-a-skill.md`).
- **What it runs**: only standard commands or tools from `deployer/tools.py`, declared in `tools` ("Commands skills may run"), each granted as "Granting tools" describes.
- **What it needs**: `required_vars` for every `{{TOKEN}}` it uses, and nothing more ("Template values").
- **Scripts versus prose**: anything deterministic belongs in a tested script under `scripts/`, with prose kept for judgment and confirmations. A fence in Markdown is at most five lines, and names the skill's scripts through the skill-directory variable ("Paths to a skill's own files").
- **Description**: what the skill does and when to use it ("Description"). Claude Code loads every model-invocable description into every session, so keep it short.

## 2. Scaffold a new skill

For a new shipped skill, with the values the plan settled:

```bash
python tools/new_skill.py "<name>" --description "<description>" --argument-hint "<hint>" --tool gh
```

Add `--user-only` and `--opt-in` as planned, and `--skill-dep <dependency>` for each sibling whose scripts the skill's scripts import; omit `--argument-hint` or `--tool` when there is none. Pass the planned grants as `--allowed-tools`, comma-separated, unless they are its default: both shells for its script named after it, `scripts/<name with underscores>.py`, and the file-reading tool; a skill with more scripts grants each by name. It writes the frontmatter, the metadata, and the generated part of the reference section, printing `CREATED <path>` or `UPDATED <path>` for each file, refuses without changing anything when the name is taken or a tool is unknown, prints the `SCRIPT_HEADER` lines a script with those dependencies starts with, and prints a `REMAINING` line for each thing validation still needs from you. A skill inside a bundle also needs its entry in `source.json`.

For a new repository skill, add `--repository`, which writes `.claude/skills/<name>/SKILL.md` and its shim and takes no `--tool` or `--opt-in`; it grants nothing unless you pass `--allowed-tools`:

```bash
python tools/new_skill.py "<name>" --repository --description "<description>"
```

## 3. Implement test first

Write the failing test, then make it pass:

- A script's regression suite lives beside it under `scripts/`, named `test_*` or one of the other patterns in "Validation".
- A `{{TOKEN}}` in Bash or PowerShell needs a rendered execution fixture in `tests/deployer/` whose values contain spaces and other allowed punctuation.
- A contract between skills, such as one skill invoking another, belongs in `tests/ai-config/test_cross_skill_contracts.py`.
- A shipped skill the model may start has a trigger scenario, `tests/fixtures/skill-evals/<name>/description/scenario.json`, in the shape `docs/skill-evaluations.md` describes: requests a user would type, drawn from its "Use it when" sentence, and near misses from its neighbours' sentences and from a request no skill serves. Validation fails without one.

Keep `SKILL.md` to orchestration: the commands to run, what their output means, and what to ask the user.

## 4. Document in the same change

Once the change is in place, name what it owes. This reads the files changed since `origin/main`, committed, uncommitted, or untracked, and prints `BASE <base> <commit>` and `CHANGED <count>`, the merge base it compared with and the files it read, then a `REGENERATE` or `GATE` line for each regeneration and manual gate they owe, then `OWED` and their count, or a last line `FAILED <reason>` to report; run it again whenever the change grows:

```bash
python -B .claude/skills/change-skill/scripts/change_skill.py gates
```

- Run the command of each `REGENERATE` line: `python tools/skill_reference.py --write`, then `python tools/skill_reference.py` without `--write` to list anything still missing; `python tools/skill_shims.py --write`, which prints `WROTE <path>` for each shim file it writes; or `python tools/runtime_prompts.py --write`. Validation fails while a reference, shim, or sample is stale.
- Update the hand-written part of the skill's section in `docs/skills.md` to match what shipped: what each argument means, what it does without them, and an example.
- A new skill, or bundle, gets a row in the "Included skills" table in `README.md`, linking its section in `docs/skills.md`.
- A code-review skill's configuration or records belong in `docs/code-review-operations.md`.
- When the change sets or clears `disable-model-invocation`, or adds or removes an `Agent` or `Workflow` grant, update the shipped skill's `runtime_support` in `deploy-meta/<name>.json` as "Metadata" describes; validation fails while it disagrees with the frontmatter.

## 5. Verify

Run the local checks "Validation" in `docs/implementing-changes.md` names for the files the change touches, the deployer's rendering tests among them for a shipped skill's scripts, and get them green; continuous integration runs the full suite. Never skip, weaken, or suppress a check to get there.

```bash
python -B tests/run_validation.py -k '<pattern>'
```

Then read the whole diff once more for personal paths, organization names, and generated artifacts, and leave no scratch file in the worktree and no background command running.

Validation never starts a runtime. For a `GATE runtime-canary` line, also run the `runtime-canary` repository skill for the skills it names and record its result in the pull request.

For a `GATE evaluate-skill <name> if-prompt` line, decide whether the change touches that skill's prompt, model guidance, or reviewer instructions; when it does, also run the `evaluate-skill` repository skill for it, and commit the result it writes to `docs/skill-evaluations.md`. It calls models, so validation never runs it.

A `GATE evaluate-skill <name> trigger` line is owed whenever it appears: the skill's description, all the model reads when deciding whether to start it, is new or changed. Before the pull request, run the `evaluate-skill` repository skill for that skill, which runs its trigger scenario, and commit and cite its result the same way.

## 6. Audit the cost

Validation checks the contract; `audit-skill` judges what the change costs: token footprint, deterministic work left to prose, delegation, and the `model`, `allowed-tools`, and listing frontmatter. From the worktree, run `audit-skill <name>` for each `GATE audit-skill` line, and check its `Scope`: `locate` reports `SCOPE source` for `skills/<name>` here and `SCOPE project-claude` for `.claude/skills/<name>`, never the deployed copy.

`audit-skill` is a shipped skill, so it runs from your deployment under `~/.claude/skills`, not from this checkout. If it is not deployed, or reports another scope because the deployed copy predates this behavior, deploy from the hub on an up-to-date `main` and rerun it, or skip the audit and say why in the pull request. Resolve every MUST FIX before opening the pull request and rerun validation if that changed anything. A SUGGESTION may stay, but the pull request body names each one left and why.

## 7. Open the pull request

Follow step 8 of `implement-change` for the rebase, the typed title, and the body from the template, opening the pull request with its `pull_request.py open` command, and name each issue the change resolves in its own `Closes #<number>` line, which its `draft --closes` writes. Put the `runtime-canary` result, the `evaluate-skill` run with its table, and any SUGGESTION left from the audit in the body. A change to a skill's arguments or status lines is a contract change that no check compares, so state it under `## Upgrade note` as "Contract files" in `docs/implementing-changes.md` says.
