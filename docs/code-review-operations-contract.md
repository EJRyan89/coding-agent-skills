# Code-review operations behavior contract

This is the behavior the four public skills of the `code-review-operations` bundle, and the review archive they share, promise to keep: what each produces, how the archive and its state behave, what reviewers may and may not do, and the role of each runtime. A change that breaks one of these promises is a breaking change to the suite. How to configure and run the suite is in [Code-review operations](code-review-operations.md). Fixtures and tests use repository-neutral identities only.

## Skill behavior

| Public skill | Preserved behavior | Authoritative output |
| --- | --- | --- |
| `review-prs` | Review open non-draft pull requests and merged pull requests after a watermark; review one exact configured pull request, draft or not, without enumeration; skip an unchanged reviewed head; allow explicit subset selection; preserve retry eligibility after partial failure. | Validated JSON/Markdown review pair and per-repository state. |
| `review-prs --re-review` | Re-review a previously reviewed pull request; require a changed head unless forced; compare every prior finding; create the next review version without overwriting history; never post to GitHub. | Versioned JSON/Markdown review pair. |
| `update-pr-tracker` | Track pull requests authored by, assigned to, or involving the configured user; place each in a counted section from the user's own GitHub review state; treat an update as unchanged only when every file the pull request changes is identical in content and file mode to what was reviewed, and uncertain responses to requested changes as awaiting response; omit approved pull requests unchanged since approval; remove a row on the user's direct assessment without acting on GitHub; show missing/current/stale AI review status independently of section; optionally offer to generate missing or stale reviews; preserve user-authored dashboard content. | One marker-owned dashboard section. |
| `review-insights` | Filter reviews by explicit inclusive dates; aggregate severity/category themes; count findings later judged addressed or still present per reviewer, model, and category; record an accept/reject/defer decision per recommendation; retain reproducible evidence. | Versioned summary JSON plus Markdown projection. |
| `flag-review-finding` | Add, list, and resolve review-improvement observations with stable IDs and optional PR/finding association. | Locked structured flag store. |

Repository targeting is always one or more full `owner/repo` identities or a named configured set. Pagination must complete per repository. Authentication, rate limits, malformed responses, and unexpected API failures fail closed.

## Archive and state behavior

- Archive keys are `<owner>/<repo>/pulls/<number>`; repository short names are display-only.
- JSON is the machine source of truth. Markdown is a hash-linked projection.
- Review versions are allocated under a per-PR lock and never overwrite history.
- Merged-pull watermarks are independent per repository. Incomplete enumeration or a failed eligible merge cannot advance that repository past the missing work.
- Configuration, mutable state, flags, and review versions use separate short-lived locks; network and semantic review work occurs outside those locks.

## Reviewer behavior

- The bundled generic reviewer and a repository-provided specialist both receive the same versioned request and must return the same normalized result shape.
- The bundled generic reviewer prompt and result schema resolve from the installed `code-review-core` skill, not from a repository checkout or branch.
- Re-review requires exactly one disposition for every prior finding, and every open or unverified entry of the pull request's finding ledger is a prior finding until a review closes it.
- A finding that repeats another one is linked to it with `repeats` and counted once, never twice. A link must name an existing finding at least as severe that is not itself a repeat.
- The core assigns stable finding IDs, keeps the finding ledger, calculates verdicts from its open entries, renders reports, and owns durable writes. An initial review starts a fresh ledger; a record written before ledgers stays valid and is read as having no history.
- Repository reviewers are loaded only from an immutable merge-base target or an explicitly configured trusted ref. Every loaded file is declared, materialized, and hashed.
- GitHub review comments, including Copilot code-review comments, are evidence only. Terminal prose and JSONL runtime diagnostics are never the durable adapter result.

## Design constraints

- No personal defaults, hard-coded organizations or repositories, implicit organization-wide queries, or repository-short-name archive paths.
- Mutable watermarks live in per-repository state, never in the user configuration file.
- Results are structured JSON validated against a schema; nothing is scraped from Markdown reports or ledgers.
- Executable logic lives in tested `scripts/` files, not in skill prose.
- Repository reviewer sources never use pull-request-head instructions, mutable working-tree substitutions, `permissionMode: bypassPermissions`, or a fallback to whichever repository happens to be the current directory.
- Token usage and cost are never estimated or priced. A review record carries only the `usage` object a reviewer adapter returns, or null.

## Runtime roles

| Runtime | Role | Trust boundary |
| --- | --- | --- |
| Claude Code | Native skill host and native reviewer delegation. | Authoritative rendered skill plus immutable materialized repository reviewer. |
| Codex | Thin runtime adapter and native agent delegation. | The adapter loads the authoritative skill; durable outputs still pass core validation. |
| GitHub Copilot CLI | Personal skill host and bounded non-interactive reviewer driver. | No custom instructions, shell, URL, memory, or interactive question tools; JSONL is diagnostics only. |
| Copilot cloud agent | Not a local suite host. | Cannot advance local state or commit local records without a separately approved transport. |
| Copilot code review | External evidence source. | PR-head instructions are untrusted for the suite protocol. |

## Legacy review records

An archive may hold `legacy-review.json` indexes from an earlier review format. They remain readable as reviewed heads; their findings are not converted. See "Legacy reviews" in [Code-review operations](code-review-operations.md#legacy-reviews).
