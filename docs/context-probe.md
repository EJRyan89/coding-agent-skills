# Context probe

The skills here enforce a repository's own declared rules, so the shapes they meet in other repositories, and where
they fail on them, are the context their mechanisms need most. Much of that context sits in private repositories,
where copying anything out by hand risks carrying proprietary detail with it. `tools/context_probe.py` gathers it as
statistics only: a profile of a repository's rule sources, languages, and build systems, and of how the code-review
skills fared on it, that names nothing the repository chose.

You run the probe from a checkout of this repository on the machine that holds the private repository. A session
only reads a profile afterwards, once you have previewed it and written it out.

## Running it

Preview the profile first. The probe builds it in memory, checks it, scans it against your private-term list, and
prints the whole of it with a last line `DIGEST <sha256>`:

```bash
python tools/context_probe.py preview '<repository>'
```

Read every line of it. Then write it out with the digest the preview printed:

```bash
python tools/context_probe.py write '<repository>' --digest '<sha256>' --out '<file>'
```

`write` builds the profile again and writes it, printing `WROTE <file>`, only when its digest is the one you
previewed. When the repository, the review archive, or the flag store changed in between, it prints
`CHANGED <sha256>` and writes nothing; preview again. A last line `FAILED <reason>` is a run that could not finish,
such as one with no private-term list or a git that cannot read this checkout's commit. A FAILED line never quotes
what was read: a failure while the repository or the review records are read names the part and the error's kind
alone, such as `FAILED the repository's files could not be read: PermissionError`, since an error's text can name a
private path or ref. The only paths it names are the private-term list's and `--out`'s, which you give. The exit
statuses are in the tool's docstring.

Both commands take `--terms <file>`, the private-term list, which defaults to `~/.coding-agent-skills/private-terms.txt`.

## What a profile holds

Every number in a profile is a count rounded down to a power of two, so a count narrows a repository to a range and
never to one value. Every string is a word the probe defines or a closed vocabulary of the code-review core.

- `probe`: the profile's layout version, `schema`, and `commit`, the commit of this checkout the probe ran from, or
  `unknown` when git answers that it has none, so a reader knows which probe produced it.
- `files`: the files the walk found. The walk skips version control, dependency, and build output folders as
  `folder_walk` does, follows no link, and does not enter another checkout.
- `languages`: files by language, from a table of file-name suffixes; a suffix in no entry counts as `other`.
- `build_systems`: the build, package, and continuous integration files present, by a fixed set of names (such as
  `pyproject.toml` or `package.json`), by suffix (`*.csproj` and the other project and solution files), and
  `github-workflows`.
- `walk`: the folders and entries the walk did not search, by kind: `linked`, `loop`, `nested-checkout`, `unlistable`,
  `unexamined`, and `bounded`.
- `rule_sources`: for each kind of instruction an agent runtime reads (`CLAUDE.md` and `AGENTS.md` at any depth,
  Copilot's repository and path instructions, Claude Code's rules, commands, agents, skills, and settings, the
  `.agents` and `.github` skills and agents, and `.mcp.json`), its `files`, `bytes`, `lines`, and, for Markdown,
  `headings` and `fences`; `unread` counts a file that was a link or could not be read.
- `hooks`: from `.claude/settings.json`, the number of hook `events` and `handlers`, and `hook_events` by name where
  the name is an event Claude Code documents, as audit-ai-config lists them, else `other`.
- `reviews`: from the code-review archive and flag store the code-review configuration names, for the configured
  repository whose checkout holds the one probed. `totals` counts records, unreadable records, pull requests,
  findings, distinct finding categories, reviewer runs and their retries, unavailable sources, uncovered, unreviewed,
  and excluded files, and review comments. Beside it, counts by closed vocabulary: `verdicts`, `severities`, `modes`,
  `dispatch`, `dispositions`, `comment_dispositions`, `ledger_states`, `snapshot_sources`, `diff_fallbacks`,
  `scope_requested`, `scope_used`, and `analyzer_coverages`; `unreviewed_languages`, the unreviewed files by language;
  and `flags`, the flags on the repository by status. A value outside a vocabulary counts as `other`.
- `reviews_absent`, in place of `reviews`: `no_configuration`, `configuration_unreadable`, `checkout_unreadable`, or
  `not_configured`, when no configured repository's checkout holds the one probed.

## What a profile never holds

No file contents, no file or folder name the repository chose, no path, no repository, organization, or branch name,
no person, login, or commit message, no review's title, summary, finding, evidence, or comment, no model name, and no
finding category, reviewer, or unreviewed suffix the repository configured, which are counted and never named. No
hook matcher, command, or setting, no skill or agent name, and no exact size or count.

`tests/tools/test_context_probe.py` holds this: its fixture plants a distinctive marker in every place the probe reads,
from file and folder names, rule source contents, hook matchers and commands, the remote, the branch, the author, and
the commit message to every text field of a review record and a flag and the archive's own folders, and fails when any
marker reaches the preview, the written file, or either output stream.

## The checks before anything is shown

Three checks run, in this order, before any byte of a profile is printed or written:

1. **The allowlist.** The probe refuses a profile holding any string that is not one of its own words or a closed
   vocabulary, apart from its own commit, a hexadecimal object name, and any value that is not a whole number. A
   field that came to echo the repository would stop the run with `FAILED`, naming the field and never the value.
2. **The private-term scan.** Each key and value is matched against every term in the list, as the release
   procedure's scan matches the tree: as a fixed string, ignoring case, and as a whole word, so a term inside a longer
   word is not matched. A match prints `PRIVATE_TERM <field> key` or `PRIVATE_TERM <field> value`, naming the field by
   the keys above it that matched no term, so no line repeats a term, then `STOPPED private term`, and nothing of the
   profile is printed or written. A list that is missing or cannot be read stops the run: the scan never runs without
   one.
3. **The preview.** `preview` prints the whole profile, and `write` writes only a profile whose digest you previewed.

## From profiles to a generic issue

A profile stays on your machine until you hand it to a session, and is never committed. The session reads it, and
profiles from other repositories, for a failure kind that recurs, such as a review that keeps falling back to another
diff source or file types it keeps leaving unreviewed, or for a shape a mechanism does not handle, such as a rule
source kind no skill reads. It files each mechanism gap as an enhancement from the template in
`.github/ISSUE_TEMPLATE/`, worded generically: it names the profile fields that show the gap, such as
`reviews.diff_fallbacks`, and never a repository, a team, or the machine. Before the issue is filed, its body passes
the same private-term scan the release procedure runs over the tree, against its body file.

## Limits

- Only the code-review skills keep records of their runs, so a profile's failure kinds come from review records
  alone; another skill's refusals are not profiled.
- A rule source is measured over its first mebibyte, and the walk stops after a bounded number of folders, which it
  reports as `bounded`.
- The review counts follow the configuration's `checkout_path`: a repository reviewed but not configured with the
  checkout probed has `reviews_absent` `not_configured`.
- The whole-word scan, like the release scan, does not match a term written inside a longer word.
- A FAILED line withholds the text of every failure: one a reader raises names the part being read and the error's
  kind, and any other, a defect in the probe itself, ends as `FAILED <kind>`.
