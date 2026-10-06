---
name: init-ai-config
description: "Creates or upgrades AI agent configuration (Claude Code, Codex, Copilot) across runtimes from a single authoritative CLAUDE.md. May write files. Use it when asked to set up, add, or migrate a repository's agent instructions, not only to check them."
allowed-tools: ["Bash(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "PowerShell(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "Bash(git rev-parse --show-toplevel)", "PowerShell(git rev-parse --show-toplevel)", "Read", "Write", "Edit", "AskUserQuestion", "Skill"]
---

# Initialize AI Agent Configuration

Create or upgrade a repository's AI agent configuration so that every runtime-specific file is derived from one authoritative `CLAUDE.md`. Scripts do every deterministic step; you author `CLAUDE.md`, repository skills, and a JSON spec, and you ask the user the scoping questions. Never hand-create, copy, or edit generated files or the generator's Python: `--write` produces them with ownership markers, and hand-made copies collide with it.

The setup commands below print one fact per line. `FAILED <reason>` on stderr with exit code 2 is an expected failure to report, not a reason to improvise. In each command, replace `<repository root>` with the repository's top-level directory.

## Precondition

Confirm the target is a Git repository (`git rev-parse --show-toplevel`). If it is not, ask the user which repository to target; never initialize Git without explicit authorization. Before generating, tell the user which limitations in `${CLAUDE_SKILL_DIR}/references/known-limitations.md` apply to their selection.

## 1. Inventory

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/init_ai_config.py" --root "<repository root>" inventory
```

Each `FILE <path> owner=generated|user|hash-mismatch` line is an existing configuration file; each `CONFLICT <path> <reason>` line must be resolved deliberately with the user before step 6. Never delete or replace user-authored content without the user's approval. Present the inventory.

## 2. Detect and choose scope

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/init_ai_config.py" --root "<repository root>" detect
```

It prints `BUILD_FILE`, `FORMAT_CONFIG`, `WORKFLOW`, `PARITY_CALLER` (an existing workflow that already calls the parity workflow), `MCP_SERVER <name> transport=<t> source=<file>`, and `UNREADABLE <file> <reason>` lines. Read the build and format files you need to learn the exact build, test, and formatting commands; do not assume Linux, sandboxed networking, or permissive tool approval.

Ask the user which runtimes (Claude Code always; Codex optionally) and which Copilot surfaces to target (VS Code, JetBrains, Copilot app, CLI, cloud agent, code review), which existing MCP servers to keep and for which targets, and, if parity CI is wanted, whether the repository's own CI will call the parity workflow or the generator should add a pull-request caller. `${CLAUDE_SKILL_DIR}/references/spec-reference.md` lists what each choice generates and the transport and surface compatibility matrices.

## 3. Write CLAUDE.md

Create or update `CLAUDE.md` from `${CLAUDE_SKILL_DIR}/references/claude-md-template.md`, filling every placeholder with the facts from step 2. Make these prominent callouts: never disable or suppress the repository's own rules, never work around a failing check, the external quality gate thresholds, and any dogfooding requirement. Its H2 headings become the Copilot projection sections.

## 4. Repository skills (if the repository has repeatable workflows)

Write each as `.claude/skills/<name>/SKILL.md` with `name` and `description` frontmatter, using an existing repository skill as the example when there is one. Use runtime-neutral language (for example "search for X", never one runtime's tool name), and never depend on user-level files or skills from another repository. `--write` generates the `.agents/skills` shims; create `.github/skills` entries only when a Copilot-specific adapter is intentional.

## 5. Author the spec

For an upgrade of a repository that already has a generator, export its current spec first and edit that:

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/init_ai_config.py" --root "<repository root>" export-spec
```

It prints `SPEC <spec file>`, the file it wrote under a new temporary directory; edit that file.

Otherwise write a new JSON spec file in a new temporary directory, outside the repository and every skill directory, following `${CLAUDE_SKILL_DIR}/references/spec-reference.md` and `${CLAUDE_SKILL_DIR}/references/example-spec.json`. The spec records the step 2 choices: runtimes, surfaces, features, Copilot sections, cloud-agent setup commands, and MCP servers. Classify every MCP tool by risk as that reference describes and record the classification in the `MCP Tools` table of `CLAUDE.md`.

## 6. Install the generator

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/init_ai_config.py" --root "<repository root>" install --spec "<spec file>"
```

On success it prints `INSTALLED .github/scripts/ai_config.py`, `INSTALLED .github/scripts/test_ai_config.py`, any `CONFIG_WARNING <message>` lines to relay, and `CONFIG_VALID`. On exit code 1 it writes nothing and prints `SPEC_ERROR`, `CONFIG_ERROR`, or `CONFLICT` lines: fix the spec or `CLAUDE.md` and rerun. Add `--replace` only when the user agrees to replace a generator that `init-ai-config` did not install, after exporting its spec.

## 7. Generate and check

From the repository root, generate every derived file, then confirm parity:

```bash
python -B .github/scripts/ai_config.py --write
python -B .github/scripts/ai_config.py --check
```

Each prints one success line and exits 0. Otherwise it exits 1 and prints each problem on stderr, one labelled line each (a `DRIFT` line is followed by a diff); `WARNING:` lines do not fail. Resolve each problem with the user, by changing `CLAUDE.md`, a skill, or the spec and reinstalling, then rerun.

## 8. Configure what is not a file

- **Cloud agent and code review MCP** come from GitHub repository settings: each server needs a `type`, an explicit `tools` list, and `COPILOT_MCP_*` secrets for credentials; remote OAuth is not supported. Document the required settings in `CLAUDE.md`.
- **JetBrains MCP** is not generated; give the user manual configuration guidance. Selecting JetBrains does not affect MCP for other surfaces.

## 9. Verify

If `audit-ai-config` appears in the available-skills list, invoke it via native skill invocation and relay its report. Then review the content the scripts cannot judge:

- [ ] Build and test commands use exact flags, and the local validation section mirrors CI.
- [ ] Formatting rules point to the canonical configuration file.
- [ ] Every registered MCP tool appears in the `MCP Tools` table with a risk classification.
- [ ] A resource allocation strategy is documented if parallel agents are expected.
- [ ] Dogfooding constraints are prominent, not buried.

## 10. Runtime checks for the user

Some checks need a running Codex or Copilot session. Give the user the path `${CLAUDE_SKILL_DIR}/references/runtime-checks.md` and name its sections for the selected targets (`Codex`, `Copilot CLI/app`, `VS Code`, `Cloud agent`, `Code review`) for them to confirm; do not copy the checks into your reply.
