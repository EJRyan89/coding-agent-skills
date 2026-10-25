---
name: audit-repository
description: >-
  Run this repository's audits per release: before tagging, the release audit of the diff since the last tag and
  the twin sweep of each fix since it; after it, the rotation audit of one area read whole and the guidance audit
  of each runtime's published guidance. Writes the briefs for read-only readers, then files what they confirm.
argument-hint: "release [--since REF] | twins [--since REF] | rotation [--record TAG] | guidance [--record TAG]"
disable-model-invocation: true
allowed-tools: ["Bash(python -B .claude/skills/audit-repository/scripts/audit_repository.py *)", "PowerShell(python -B .claude/skills/audit-repository/scripts/audit_repository.py *)"]
---

# Audit the repository

Each release is audited before its tag and after it, as "Before tagging" and "After tagging" in `docs/releasing.md` require. `.claude/skills/audit-repository/scripts/audit_repository.py` does every deterministic step: it sorts files into four areas (`deployer`, `code-review`, `other-skills`, `infrastructure`), reads the rotation record and the guidance record, splits an area too large for one reader, writes one brief per reader to a new temporary directory, looks up the milestone each finding is filed into and the issues the next milestone carries, searches the open issues, and removes the briefs. It prints one fact per line, and its docstring lists every line. Your part is starting the readers, confirming what they report, and filing it once the user agrees.

## 1. Choose the audit

- **release**, before tagging: every file changed since the last release, the highest `vX.Y.Z` tag `HEAD` holds; a nearer pre-release or other tag does not move it. It holds the release.
- **twins**, before tagging: every commit typed `fix` merged since the last release, each with the lines it removed, the construct it corrected, and the lines `HEAD` still holds that contain one of them. Readers judge which are twins, sites that still do what the fix stopped doing, and group them by pattern. It holds the release: the tag waits while a confirmed twin is unfiled.
- **rotation**, after tagging: the one area the rotation record shows read whole least recently, with the issues the next milestone carries. It holds nothing and feeds the next milestone.
- **guidance**, after tagging: the published guidance of each runtime the deployer supports, and of Python, page by page as `.claude/skills/audit-repository/references/guidance.json` lists them, against the files that state facts about that runtime, the provenance files of `audit-skill` and `audit-ai-config` among them. It holds nothing and feeds the next milestone.

## 2. Run the script

From a checkout of the commit being audited, run the mode you chose:

```bash
python -B .claude/skills/audit-repository/scripts/audit_repository.py release
python -B .claude/skills/audit-repository/scripts/audit_repository.py twins
python -B .claude/skills/audit-repository/scripts/audit_repository.py rotation
python -B .claude/skills/audit-repository/scripts/audit_repository.py guidance
```

Add `--since '<ref>'` to `release` or `twins` to start from another base. Keep these lines for the later steps:

- `MILESTONE <severity> "<title>"`: the milestone a finding of that severity is filed into, or `none`.
- `PROVENANCE "<sentence>"`: the sentence each filed issue's body opens with.
- `CARRIED <number> "<title>"`: an open issue the next milestone carries; every rotation brief names them too.
- `BRIEFS "<directory>"`, then one `BRIEF <name> "<file>"` per reader, each its whole prompt; `READERS` counts an area's, or the twin sweep's.
- `FIX <commit> <pull> "<title>"`: twins' fixes, oldest first, each followed by `CONSTRUCT <commit> <count>`, the removed lines searched for, and `HITS <commit> <count>`, the lines `HEAD` holds that contain one; `CONSTRUCT_CUT` and `HITS_CUT` say a brief lists fewer than there are. `FIXES 0` means no fix merged since the base: the sweep is done, with nothing to brief.
- `MECHANISM <name> <what>`: twins' mechanisms, one of which each pattern proposes.
- `SUBJECT <subject> <pages> <tag>`: guidance's runtimes and Python, each with its pages and the oldest of their last reads, or `never`.
- `DRIFT <kind> <what>`, `QUESTION <name> <what>`, `TEMPLATE <field> <what>`, and `SECTION <name> <what>`: the kinds of drift the briefs ask the readers to look for, the questions a release or rotation brief asks of every file, the fields of a finding or a twin, and the sections of a reader's report: what a finding step 4 confirms holds, and the fields step 5 files it with.

The rest are context, with nothing to do: `HEAD <commit>`, the commit the briefs describe; for `release` and `twins`, `BASE <ref> <commit>`, where the diff or the fixes start; for `release`, `CHANGED <count>`, the files changed since it, and `AREA <area> <count>` for each area; for `rotation`, `RECORD "<file>"`, the record read, `READ <area> <tag> <date>` or `UNREAD <area>` for each area, and `FILES <count>`, the files of the `NEXT` area that step 6 records; and for `guidance`, `GUIDANCE_RECORD "<file>"`, the record read.

A `FAILED` line is the last line and names what stopped the run, such as no release tag reachable from `HEAD`, no file changed since the base or none in the rotation area, which no reader could audit, a guidance record naming no page for a runtime the deployer supports or for Python, or gh's own reason. A run that fails while writing the briefs prints no `BRIEFS` line and leaves its directory in place: the folder holding the file its `FAILED` line names. Once you have diagnosed the failure, remove that folder as step 6 says; a failure writing the marker, `.audit-repository-briefs.json`, leaves a folder `remove-briefs` refuses, so delete it yourself.

## 3. Start one reader per brief

Start one read-only reader per `BRIEF` line, all at once: in Claude Code a subagent that cannot edit, such as the `Plan` type, which can also fetch the pages a guidance brief names; elsewhere a separate read-only session. Give each this prompt: `Read <file> and follow it.`

## 4. Confirm before filing

A reader's finding is a lead, not a verdict. Open both sides it quotes and keep it only when both say what the reader claims; otherwise drop it or list it as unconfirmed. Merge findings two readers share.

For a twin, the two sides are the fix's diff and the twin's line at `HEAD`. Drop a site a `*_ALLOWED` table sanctions beside the code, one a validation policy under `tests/validation/` already holds, a suite's case of what the fix refuses, and one a later fix already corrected. Then merge the readers' patterns: one pattern per construct, each with its twins and the one mechanism you judge would stop it, the reader's or your own, and the `docs/lessons.md` pattern it is an instance of, or none.

Then search the open issues so nothing is filed twice, giving one query per confirmed finding, such as its file's name and a distinctive word of its title:

```bash
python -B .claude/skills/audit-repository/scripts/audit_repository.py duplicates '<query>' '<query>'
```

`MATCHES <query> <count>` and one `MATCH <query> <number> "<title>"` per open issue it found, queries numbered from 1, then `MATCHES_CUT <query> <limit>` when the query found more than it lists: narrow that query and search again. Drop a finding an open issue already covers, or name the issue beside it. Show the user the confirmed list, each with its severity and milestone, and for twins the patterns with their mechanisms, and ask before filing anything.

## 5. File

- File each confirmed finding as one issue written from the template its `template` field names in `.github/ISSUE_TEMPLATE/`, keeping the template's headings and quoting both sides. Open its body with the `PROVENANCE` sentence, name the finding's `question` beside its auditor gap when a `QUESTION` found it, then file it with `gh issue create --body-file '<file>' --milestone '<title>'`, the title its severity's `MILESTONE` line names. When that line says `none`, ask the user which milestone to create or use.
- `gh issue create` is deliberately ungranted, so the user approves each filing at its prompt: an issue is public once it is filed, and the prompt is the last check that it is one the user agreed to and no trust-boundary finding.
- A `trust-boundary` finding, or any finding that shows a way past a trust boundary, is a vulnerability. Report it privately as `SECURITY.md` says, through a private security advisory, and never in a public issue, commit message, or pull request.
- **Release audit:** a confirmed defect is fixed before the tag; the tag waits for its pull request. The release notes' scope section names each drift and polish finding as deferred.
- **Twin sweep:** file each confirmed twin from the bug template, naming its fix's pull request and its pattern; twins of one pattern may share an issue. The tag waits until none is unfiled, and a twin of severity defect is fixed before the tag as a release audit defect is. File each mechanism the user adopts as an enhancement issue naming its pattern's twins; its pull request adds or extends that pattern in `docs/lessons.md`.

## 6. Record

The filed issues are the audit's record, so no milestone gets a line. After a rotation audit, record its area for the tag just released, from a worktree (the script refuses the main checkout), and merge the change through a pull request whose body names the areas both audits read, as `docs/releasing.md` requires: the release audit's areas, its `BRIEF` lines from the earlier run, and the rotation area, the `NEXT` line of this one. The milestone does not record them, so the body is their only record:

```bash
python -B .claude/skills/audit-repository/scripts/audit_repository.py rotation --record '<tag>'
```

`RECORDED <tag> <area> <date>` confirms the entry it appended to `.claude/skills/audit-repository/references/rotation.json`. If the write fails, the `FAILED` line says the record is unchanged and names any copy left beside it to delete; fix the cause and run the command again.

After a guidance audit, record the tag the same way, in the same kind of pull request, which also gives each moved page its new address in `.claude/skills/audit-repository/references/guidance.json` and moves each of its changelogs' `read_through` to the latest release whose entries the readers read; `RECORDED_PAGES <tag> <count>` confirms the pages the tag was appended to:

```bash
python -B .claude/skills/audit-repository/scripts/audit_repository.py guidance --record '<tag>'
```

When each audit's findings are filed, delete its briefs directory, the one its `BRIEFS` line named. `REMOVED "<directory>"` confirms it; the script deletes nothing but a briefs directory holding the marker its run wrote there:

```bash
python -B .claude/skills/audit-repository/scripts/audit_repository.py remove-briefs '<directory>'
```
