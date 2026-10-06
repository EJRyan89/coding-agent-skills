---
name: flag-review-finding
description: "Add, list, or resolve a structured code-review improvement flag. Use it when the user says a review finding was wrong, noisy, or missed something and wants that recorded."
argument-hint: "add CATEGORY BODY [--repository owner/repo --pull N [--review-version V --finding ID]] | list | resolve ID RESOLUTION"
allowed-tools: ["Bash(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "PowerShell(python -B \"${CLAUDE_SKILL_DIR}/scripts/*)", "Read"]
---

# Flag a review finding

Use the configured code-review flag store through the commands below; do not edit the store or a Markdown ledger. Each command prints JSON, and a failure exits nonzero with the reason.

Add a flag with a category and rationale. Associate it with a pull request and finding when it concerns one, using the full `owner/repo` identity and the finding's label in the review report, such as `v2 F002`: review version `2` and finding `F002`. A re-review report shows a finding carried from an earlier review under that review's label, so take the version from the label, never from the report's **Mode** row. When neither the user nor the conversation gives them, read the report. Finding IDs restart at `F001` in every review, so the command refuses `--finding` without `--review-version`. `review-insights` can resolve only flags that name a finding:

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/flag_review_finding.py" add "<category>" "<rationale>" --repository "<owner/repo>" --pull "<number>" --review-version "<version>" --finding "<finding id>"
```

List the open flags:

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/flag_review_finding.py" list
```

Resolve an open flag by its ID (such as `RF-000004`) with a non-empty resolution:

```bash
python -B "${CLAUDE_SKILL_DIR}/scripts/flag_review_finding.py" resolve "<flag id>" "<resolution>"
```

The core allocates IDs and commits each update under a short-lived lock. The JSON flag store is authoritative; anything you show the user is a projection of it.
