---
name: flag-review-finding
description: "Add, list, edit, or resolve a structured code-review improvement flag. Use it when the user says a review finding was wrong, noisy, or missed something and wants that recorded."
argument-hint: "add CATEGORY BODY [--repository owner/repo --pull N [--finding LABEL]] | list | findings --repository owner/repo --pull N | edit ID [--category CATEGORY] [--body BODY] | resolve ID RESOLUTION"
allowed-tools: ["Bash({{PYTHON}} -B \"${CLAUDE_SKILL_DIR}/scripts/flag_review_finding.py\" *)", "PowerShell({{PYTHON}} -B \"${CLAUDE_SKILL_DIR}/scripts/flag_review_finding.py\" *)"]
---

# Flag a review finding

Use the configured code-review flag store through the commands below; do not edit the store, a Markdown ledger, or a review report. Each command prints one fact per line. A failure prints `FAILED <reason>` and changes nothing; show the reason. Keep each command's form: `--` before the free-text values and `=` joining `--category` and `--body` to theirs let a value begin with `-`.

Add a flag with a category and rationale. Associate it with a pull request and finding when it concerns one, using the full `owner/repo` identity and the finding's label, such as `v2 F002`, as the review report shows it. When neither the user nor the conversation gives the label, list the pull request's open findings and ask the user which one they mean:

```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/flag_review_finding.py" findings --repository "<owner/repo>" --pull "<number>"
```

It prints `FINDING v<version> <finding> <severity> <path>:<line> <headline>` per open or unverified finding, under the label its report shows, then `COUNT <n>`. Then add the flag with the label as written; it refuses a finding that review does not have:

```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/flag_review_finding.py" add --repository "<owner/repo>" --pull "<number>" --finding "<label>" -- "<category>" "<rationale>"
```

Without a finding, leave out the options before `--`. It prints `ADDED <flag id>`. `review-insights` links a flag that names a finding to the recommendations covering it, and its synthesis can name any open flag, one that names no finding included; accepting a recommendation resolves the flags it links.

List the open flags:

```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/flag_review_finding.py" list
```

It prints `FLAG <flag id> <category> <target> <body>` per open flag, with the body cut to 160 characters and `-` as the target of a flag that names neither a repository nor a pull request, then `COUNT <n>`.

Correct an open flag's category, rationale, or both by its ID; what it names and its status stay as they are:

```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/flag_review_finding.py" edit "<flag id>" --category="<category>" --body="<rationale>"
```

Leave out the option for a value that stays. It prints `EDITED <flag id>`, and refuses a resolved or unknown flag.

Resolve an open flag by its ID (such as `RF-000004`) with a non-empty resolution:

```bash
{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/flag_review_finding.py" resolve -- "<flag id>" "<resolution>"
```

It prints `RESOLVED <flag id>`, or `ALREADY_RESOLVED <flag id>` when the flag was resolved earlier; it keeps that earlier resolution.
