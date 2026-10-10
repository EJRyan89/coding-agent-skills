---
name: review-repository
description: >-
  This repository's own pull request reviewer: a specialists manifest that routes the deployer, the skills, the
  validation policies, the trust-boundary files, and the documents to briefed reviewers, which review-prs runs. Set
  it up on a machine, check it, or tune it from canary reviews.
argument-hint: "[PULL_NUMBER ...]"
disable-model-invocation: true
allowed-tools: ["Bash(python -B skills/code-review-core/scripts/review_pipeline.py validate-reviewer *)", "PowerShell(python -B skills/code-review-core/scripts/review_pipeline.py validate-reviewer *)"]
---

# This repository's reviewer

`review-prs` reviews a pull request of this repository with the specialists manifest `.claude/skills/review-repository/references/specialists.json`, read at the pull request's base as every repository reviewer is. This skill does not review: it sets the reviewer up, checks it, and tunes it. "Specialist reviewers" in `docs/code-review-operations.md` describes the mechanism. Each route's profile, under `references/`, names the invariant or decision in `docs/design.md` it holds and the drift the v0.4.0 audit found there:

| Route | Files | Holds |
| --- | --- | --- |
| `deployer` | `deployer/`, `deploy.py` | One write point, one platform seam, refusals before mutation, the journal, the kinds table |
| `skill-contract` | skills' `SKILL.md`, `agents/`, `deploy-meta/`, `source.json`, repository skills and shims | Grants that match what runs, script over prose, metadata, cost |
| `validation-policy` | `tests/validation/`, the runner, `pyproject.toml` | Each policy holds the sentence that cites it, with a fixture, read through `ast` |
| `trust-boundary` | the code-review contract, `review_documents.py`, `review_guard.py`, `review_pipeline.py`, `review_runtime.py`, `review_source.py`, `review_specialists.py`, `test_adversarial_inputs.py` | A threat-model row and its adversarial test for what an author reaches |
| `documentation` | `docs/`, top-level Markdown, the templates | Each sentence true of the code and of its owner |
| `upgrade-notes` | contract files, skills' `SKILL.md`, `deployer/arguments.py` | The body's upgrade-note entry for each contract change, at the level and with the user action it asks for |

The generic reviewer takes every other file: `tools/`, skill scripts, the deployer and tools suites, CI, and this skill's own profiles. Every finding names one of the manifest's `finding_categories`, each a kind of drift. `review-prs` gives every reviewer the pull request's body as untrusted data, `PULL_REQUEST_BODY_FILE`, so `upgrade-notes` reads its `## Upgrade note` and holds each entry's level and user action to the change in the diff, beside validation, which holds only that the body names each changed contract file. No route has a condition, and no condition is given the body, so the review keeps the lazy snapshot.

## 1. Configure

The configuration stays on each machine and never in the repository. Add this entry to the code-review configuration's `repositories`, and the repository to a set, putting the hub checkout's absolute path for the placeholder:

```json
"EJRyan89/coding-agent-skills": {
  "reviewer": {"id": "review-repository", "protocol_version": 1, "scope": "repository", "trusted_ref": null,
               "manifest_path": ".claude/skills/review-repository/references/specialists.json"},
  "checkout_path": "<hub checkout>",
  "snapshot_exclude": ["tests/fixtures/**"]
}
```

`snapshot_exclude` leaves out the evaluation and canary fixtures, whose planted defects and own `README.md` and `docs/design.md` `source-search` would return beside the repository's. A pull request that changes a fixture is therefore `INCOMPLETE`: its fixtures are reviewed from the diff, and `evaluate-skill` exercises them.

## 2. Check

Check the reviewer against each pull request given, or a recent merged one of each route:

```bash
python -B skills/code-review-core/scripts/review_pipeline.py validate-reviewer --repository EJRyan89/coding-agent-skills --pull '<number>'
```

Expect `FILES`, `BODY` with how much of the body reviewers would be given, a `ROUTE` line per route that matches, `GENERIC files=<n>` for the rest, `SNAPSHOT ... source=checkout-lazy`, and `VALID`. A base that predates the manifest fails: set `trusted_ref` to a commit that has it, for that check only.

## 3. Tune

Tune from records, not from reading the profiles. Commit the change, set `trusted_ref` to that commit, and run `/review-prs --canary EJRyan89/coding-agent-skills#<number>` on merged pull requests that reach each route it changes. Compare each record's findings, by reviewer and category, with what the pull request later needed, then reset `trusted_ref` to `null`. A profile is what a reviewer reads on every turn, so keep each one short. Run `analyze-skill-cost` on this skill before the pull request, as for any repository skill.
