---
name: curate-agent-memory
description: "Audit and clean up Claude Code auto-memory for a project: find stale, duplicated, or misplaced memories, move durable rules to their proper home, and apply only the changes the user approves. Use it when asked to review, tidy, or prune a project's memories."
argument-hint: "[repository path] [--memory-dir DIR]"
allowed-tools: ["Bash(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "PowerShell(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "Read", "Edit", "Write", "Grep", "AskUserQuestion", "Skill"]
---

# Curate agent memory

Memory is for facts about the user and their work that nothing else records. Rules that belong to a repository, a skill, or the user's global instructions drift out of sync when they live only in memory. This skill finds those, proposes where each belongs, and changes nothing without approval.

## Step 1: Resolve the memory directory

Everything later deletes or rewrites files, so the target must be the store Claude Code actually uses.

- **Repository**: the argument, else the current Git repository root. Worktrees of one repository share a single store.
- **Memory directory**: use `--memory-dir` when given. Otherwise resolve it:

  ```bash
  python -B "${CLAUDE_SKILL_DIR}/scripts/memory_audit.py" resolve --repo "<repository>"
  ```

  It follows Claude Code's documented order: `autoMemoryDirectory` from managed, local, project, then user settings; then `CLAUDE_CODE_PROJECT_DIR_NAME` under the config directory; then a directory derived from the repository's main worktree. It reports the directory, where it came from, whether it exists, and notes such as the `--settings` launch flag it cannot see.
- If the result has no directory, or the directory does not exist, list its `candidates` and ask the user to choose or supply `--memory-dir`. Never guess.
- Show the user the resolved directory and its source, and get explicit confirmation before continuing.

## Step 2: Run the mechanical audit

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/memory_audit.py" audit --memory-dir "<memory dir>" --repo "<repository>"
```

It prints `REPORT <path>`, a new JSON file in the system temporary directory; read that file. It is read-only and reports: memory files missing from `MEMORY.md`, index entries whose file is gone, duplicate index entries, an index over or near Claude Code's load limit (the first 200 lines or 25KB, whichever comes first), broken `[[links]]`, cited paths that no longer exist, each memory's age, and up to three passages whose wording overlaps each memory. It searches the repository's `CLAUDE.md`, `CLAUDE.local.md`, `.claude/rules/`, `AGENTS.md`, README, contributor and `docs/` guidance, Copilot instructions, and skill files, plus the user's `CLAUDE.md` and `rules/`. Overlaps are candidates only; the script cannot tell whether a passage states the same rule.

## Step 3: Verify and classify every memory

Read each memory file in full, and read every overlapping passage in context. Memories are point-in-time notes, so check any claim about code, paths, or configuration by searching the current files before relying on it.

Assign each memory exactly one outcome, with a one-line reason and evidence (`file:line` for anything that exists elsewhere):

| Outcome | When |
|---|---|
| Keep | A personal preference, identity detail, or fact about the user's work that nothing else records. |
| Delete: obsolete | It describes something that no longer exists or no longer applies. |
| Delete: already covered | Its destination already states the same rule. Cite the passage; similar wording is not enough. |
| Merge | It overlaps other memories; name the survivor and show the merged text. |
| Move to global instructions | A rule the user wants in every repository. |
| Move to repository guidance | A convention for everyone working in this repository (`CLAUDE.md`, coding guidelines, contributor docs). |
| Move to a skill | A rule about how a specific skill must behave; name the skill file. |
| Record as a review finding | A correction to an automated reviewer's findings rather than a preference. Use `flag-review-finding` when it is available. |

A memory whose rule is not yet at its destination is never simply deleted. Its outcome is the move, and the memory is deleted only after the destination change is applied. If the user declines the move, keep the memory. Record nothing about deferrals; the next run checks again.

Fix mechanical problems from Step 2 as part of the relevant outcome: repair or remove broken links, and bring an index that is over or near its load limit back under it by merging or moving entries. Memories missing from the index, entries whose file is gone, and duplicate entries form one "Rebuild index" group rather than per-memory outcomes.

## Step 4: Get approval by group

Present the proposal grouped by outcome, headed by the memory directory it applies to, one line per memory: file, outcome, reason, evidence. For every move, show the exact text that would be added and where.

Ask which groups to apply with a multi-select question, one option per non-empty group. Offer to review a group item by item when the user wants to exclude individual memories. Apply nothing that was not approved.

## Step 5: Apply the approved changes

- Edit destination files first, reading each before changing it and keeping its existing style and structure. Never replace a whole existing file.
- Only then delete or rewrite the memories those changes cover. When rewriting one, keep its frontmatter `name` and `description` accurate: the index is built from them.
- For review findings, invoke `flag-review-finding` with the memory's substance; delete the memory once the finding is recorded.
- When any memory changed or the "Rebuild index" group was approved, rebuild `MEMORY.md` last instead of editing it by hand:

  ```bash
  python -B "${CLAUDE_SKILL_DIR}/scripts/memory_audit.py" reindex --memory-dir "<memory dir>" --write
  ```

  It writes one `- [Title](file.md) — hook` line per memory file and never deletes or changes a memory. Entries keep their order, title, and surrounding headings; entries for missing files or repeated links are dropped (`DROPPED <reason> <file>`); unindexed memories are appended (`ADDED <file>`). The hook is the memory's `description`, else the entry's existing hook, else the body's first line. `OVER_LIMIT` or `NEAR_LIMIT` means the index still needs merging or moving entries.
- Do not commit, push, or open pull requests; report which repository files changed so the user can review them.

## Step 6: Re-audit and report

Rerun Step 2. Report what changed per outcome, any destination files edited, and anything the audit still flags. Everything left should be a memory you deliberately kept.
