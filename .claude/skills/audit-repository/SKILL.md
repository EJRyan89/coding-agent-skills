---
name: audit-repository
description: >-
  Run this repository's two audits per release: the release audit of the diff since the last tag, before tagging,
  and the rotation audit of one area read whole, after it. Writes one brief per area for read-only readers, then
  files what they confirm.
argument-hint: "release [--since REF] | rotation [--record TAG]"
disable-model-invocation: true
allowed-tools: ["Bash(python -B .claude/skills/audit-repository/scripts/audit_repository.py *)", "PowerShell(python -B .claude/skills/audit-repository/scripts/audit_repository.py *)"]
---

# Audit the repository

Each release is audited twice, as "Before tagging" and "After tagging" in `docs/releasing.md` require. `.claude/skills/audit-repository/scripts/audit_repository.py` does every deterministic step: it sorts files into four areas (`deployer`, `code-review`, `other-skills`, `infrastructure`), reads the rotation record, writes one brief per area to a new temporary directory, and prints one fact per line. Its docstring lists every line. Your part is starting the readers, confirming what they report, and filing it.

## 1. Choose the audit

- **release**, before tagging: every file changed since the latest tag, one brief per area with changes. It holds the release.
- **rotation**, after tagging: the one area the rotation record shows read whole least recently, plus the issues the new milestone carries. It holds nothing and feeds the next milestone.

## 2. Run the script

From a checkout of the commit being audited, run the mode you chose:

```bash
python -B .claude/skills/audit-repository/scripts/audit_repository.py release
python -B .claude/skills/audit-repository/scripts/audit_repository.py rotation
```

Add `--since '<ref>'` to `release` to start from another base. `AREA` lines count each area's changed files; `NEXT` names the rotation area and `FILES` its size. Each `BRIEF <area> "<file>"` is one reader's whole prompt. A `FAILED` line names what stopped the run, such as no tag reachable from `HEAD`.

## 3. Start one reader per brief

Start one read-only reader per `BRIEF` line, all at once: in Claude Code a subagent that cannot edit, such as the `Plan` type; elsewhere a separate read-only session. Give each this prompt: `Read <file> and follow it.` For the rotation audit, add the carried issues to the prompt by number and title. If an area is too large for one reader to read whole, split its file list between two readers by directory and tell each which part is theirs.

## 4. Confirm before filing

A reader's finding is a lead, not a verdict. Open both sides it quotes and keep it only when both say what the reader claims; otherwise drop it or list it as unconfirmed. Merge findings two readers share, and search the open issues with `gh issue list --search` so nothing is filed twice. Show the user the confirmed list, each with its severity, and ask before filing anything.

## 5. File

- File each confirmed finding as one issue written from the template its `template` field names in `.github/ISSUE_TEMPLATE/`, keeping the template's headings and quoting both sides, with `gh issue create --body-file`, into the milestone the rule below gives.
- A `trust-boundary` finding, or any finding that shows a way past a trust boundary, is a vulnerability. Report it privately as `SECURITY.md` says, through a private security advisory, and never in a public issue, commit message, or pull request.
- **Release audit:** a confirmed defect is fixed before the tag. It goes into the release's own milestone, and the tag waits for its pull request. Drift and polish go into the next milestone, and the release notes' scope section names each as deferred.
- **Rotation audit:** every finding goes into the next milestone, and nothing waits.

## 6. Record

Add a line to each milestone that received findings, naming the audit, its scope (the base tag, or the area read whole), the date, and the issues filed. After a rotation audit, record its area from a worktree, never the hub, with the tag just released, and merge the change through a pull request:

```bash
python -B .claude/skills/audit-repository/scripts/audit_repository.py rotation --record '<tag>'
```

`RECORDED <tag> <area> <date>` confirms the entry it appended to `.claude/skills/audit-repository/references/rotation.json`. Then delete the `BRIEFS` directory.
