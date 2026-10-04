---
name: review-insights
description: "Analyze structured code-review findings for an explicit date range and repository set. Use it when asked which review findings were accepted or rejected, or what reviews keep flagging."
argument-hint: "START_DATE END_DATE [owner/repo ... | --repository-set NAME]"
allowed-tools: ["Bash(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "PowerShell(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "Read", "AskUserQuestion"]
---

# Review insights

Run the commands below exactly as shown; do not read the archive, the flag store, or the scripts yourself, and never edit a report or flag by hand. Commands print one fact per line and exit 0 on success; `FAILED <reason>` on stderr with exit code 2 is an expected failure to report.

1. **Report.** Require an explicit inclusive ISO date range; never guess one. Pass the user's `--repository owner/repo` (repeatable) or `--repository-set NAME`, or neither for the configured `review-insights` set:
   ```bash
   python -B "${CLAUDE_SKILL_DIR}/scripts/review_insights.py" report --start "<YYYY-MM-DD>" --end "<YYYY-MM-DD>"
   ```
   It prints `REPORT <insights.json>`, `MARKDOWN <insights.md>`, and one line per recommendation:
   - `RECOMMENDATION <id> <category> findings=<count> decision=<decision> flags=<flag ids or none>` for each finding category;
   - `ANALYZER <id> coverage=<coverage> tool=<tool> rule=<rule> findings=<count> repositories=<repos> decision=<decision> flags=<flag ids or none>` for each diagnostic analyzer rule reviewers said could catch findings, followed by up to three `EXAMPLE <id> <repo>#<pull> v<version> <finding> <headline>` lines. They come cheapest first: `available` (a rule in an analyzer the repository already has, not enforced), then `known` (a rule in an analyzer it does not use), then `custom-candidate` (a pattern that would need a custom rule).

   Each is followed by `REVIEWER <id> <reviewer> model=<model> findings=<count> flagged=<count>` lines: how many of its findings each reviewer raised on each model, and how many of those an open flag names. A finding raised by several reviewers counts for each, and `model=unknown` means the record names none. The report reads only validated review records and names every record file and payload hash it analyzed. Running it again for the same scope and range keeps each recommendation's ID, decision, and history.
2. **Ask** the user, one recommendation at a time or as one list, whether to accept, reject, or defer each recommendation, naming its category, or its coverage, tool, rule, and repositories; its finding count and linked flags; and the reviewer and model behind most of its findings when one stands out. Present analyzer recommendations in the order printed, since enforcing a rule the repository already has is the cheapest fix. When several `custom-candidate` rules' examples look like the same pattern, say so. Accepting resolves its linked flags; rejecting or deferring changes no flag. Leave unanswered recommendations as they are.
3. **Decide** each answered recommendation with the ID, subject, and `flags=` value from the line the user saw, adding `--note "<the user's reason>"` when they gave one. For a `RECOMMENDATION` line, the subject is `--category "<category>"`; for an `ANALYZER` line, it is `--analyzer <coverage> <tool> <rule>`:
   ```bash
   python -B "${CLAUDE_SKILL_DIR}/scripts/review_insights.py" decide --report "<insights.json>" <id> --category "<category>" --flags "<flags>" accepted
   ```
   If the report no longer gives that ID that subject and exactly those flags, it fails without changing anything: run the report again and confirm with the user. It prints `FLAG_RESOLVED <flag id>` for each flag it resolved, `FLAG_ALREADY_RESOLVED <flag id>` for one resolved earlier, and `DECIDED <id> <decision>`. Each decision is appended, with its time, to the recommendation's history in the report.

Finish by giving the user the Markdown report path and the decisions and flags recorded.

A flag is linked to a recommendation when it is open, names a repository, pull request, review version, and finding, and that review is analyzed and has that finding among the recommendation's findings. A finding an analyzer could catch belongs to both its category's recommendation and its rule's, so accepting either resolves its flags. Flags without a finding or review version, including flags created before review versions were recorded, are never resolved here; use `flag-review-finding` for them.
