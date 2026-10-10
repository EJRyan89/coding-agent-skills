---
name: flag-review-finding
description: "Add, list, or resolve a structured code-review improvement flag. Use it when the user says a review finding was wrong, noisy, or missed something and wants that recorded."
argument-hint: "add CATEGORY BODY [--repository owner/repo --pull N [--review-version V --finding ID]] | list | findings --repository owner/repo --pull N | resolve ID RESOLUTION"
allowed-tools: ["Bash(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "PowerShell(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)"]
---

# Flag a review finding

Use the configured code-review flag store through the commands below; do not edit the store, a Markdown ledger, or a review report. Each command prints one fact per line. A failure prints `FAILED <reason>` and changes nothing; show the reason.

Add a flag with a category and rationale. Associate it with a pull request and finding when it concerns one, using the full `owner/repo` identity and the finding's label in the review report, such as `v2 F002`: review version `2` and finding `F002`. When neither the user nor the conversation gives the label, list the pull request's open findings and ask the user which one they mean:

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/flag_review_finding.py" findings --repository "<owner/repo>" --pull "<number>"
```

It prints `FINDING v<version> <finding> <severity> <path>:<line> <headline>` per open or unverified finding, under the label its report shows, then `COUNT <n>`. Then add the flag; it refuses a finding that review does not have:

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/flag_review_finding.py" add "<category>" "<rationale>" --repository "<owner/repo>" --pull "<number>" --review-version "<version>" --finding "<finding id>"
```

It prints `ADDED <flag id>`. `review-insights` links a flag that names a finding to the recommendations covering it, and its synthesis can name any open flag, one that names no finding included; accepting a recommendation resolves the flags it links.

List the open flags:

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/flag_review_finding.py" list
```

It prints `FLAG <flag id> <category> <target> <body>` per open flag, with the body cut to 160 characters and `-` as the target of a flag that names no pull request, then `COUNT <n>`.

Resolve an open flag by its ID (such as `RF-000004`) with a non-empty resolution:

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/flag_review_finding.py" resolve "<flag id>" "<resolution>"
```

It prints `RESOLVED <flag id>`, or `ALREADY_RESOLVED <flag id>` when the flag was resolved earlier; it keeps that earlier resolution.
