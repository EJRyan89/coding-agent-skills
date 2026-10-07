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
- Results are structured JSON that the core validates as [Formats](#formats) states; nothing is scraped from Markdown reports or ledgers. An unknown future schema or protocol version fails closed.
- Executable logic lives in tested `scripts/` files, not in skill prose.
- Repository reviewer sources never use pull-request-head instructions, mutable working-tree substitutions, `permissionMode: bypassPermissions`, or a fallback to whichever repository happens to be the current directory.
- Token usage and cost are never estimated or priced. A review record carries only the `usage` object a reviewer adapter returns, or null.

## Safety invariants

- Configuration selects symbolic runtime and reviewer identifiers, never executable commands.
- Repository identities are always full `owner/repo` values.
- Mutable files use validated temporary writes and atomic replacement.
- Review records are written as linked JSON/Markdown pairs under collision-safe owner/repository paths.
- Runtime output is untrusted until it satisfies the adapter-result schema.
- Review, re-review, tracker, flag, and insight operations only read GitHub state. None of them posts comments, creates pending reviews, or submits review state.
- Repository-provided reviewers are loaded only from an immutable trusted commit.
- Review source is materialized from the exact PR head into a hash-verified snapshot. Agent configuration and instruction paths from the PR head are excluded; only reviewer-manifest files from the trusted base may instruct the reviewer.
- The suite-owned generic reviewer and result schema are resolved in the installed `code-review-core` skill's `references/` directory. They never depend on a configured repository checkout, its current branch, or a hard-coded main-worktree path.

When configuration selects `copilot-cli`, the pipeline runs the bounded host in `code-review-core/scripts/review_hosts.py`, never Copilot directly. The host requires Copilot CLI 1.0.88 or newer, verifies the exact file set and hashes of every materialized reviewer resource, requires the diff and source snapshot to be inside the isolated run directory, runs with isolated `HOME`, `USERPROFILE`, `COPILOT_HOME`, and working directory values, disables ambient instructions and MCP servers, and grants write access only to its attempt's staging file, which the host renames to the result path once it is a JSON object and the role has not been set aside. `dispatch` starts the host detached and `wait` follows it, so no runtime's command time limit stops a review. Authentication tokens inherited from the invoking environment remain available by design, so treat this as configuration isolation rather than credential isolation.

## Runtime roles

| Runtime | Role | Trust boundary |
| --- | --- | --- |
| Claude Code | Native skill host and native reviewer delegation. | Authoritative rendered skill plus immutable materialized repository reviewer. |
| Codex | Thin runtime adapter and native agent delegation. | The adapter loads the authoritative skill; durable outputs still pass core validation. |
| GitHub Copilot CLI | Personal skill host and bounded non-interactive reviewer driver. | No custom instructions, shell, URL, memory, or interactive question tools; JSONL is diagnostics only. |
| Copilot cloud agent | Not a local suite host. | Cannot advance local state or commit local records without a separately approved transport. |
| Copilot code review | External evidence source. | PR-head instructions are untrusted for the suite protocol. |

## Formats

These tables are the single statement of every file the suite reads or writes. Each format names the function that validates it, and `tests/code-review/test_format_contract.py` fails when a table and that function disagree on the suite's fixtures. The adapter result, which reviewers write, is stated instead by `skills/code-review-core/references/review-adapter.schema.json`, because reviewers read that file; the same suite checks it against `validate_adapter_result` in `review_records.py`. How to write and use each file is in [Code-review operations](code-review-operations.md).

How to read a table:

- **Type** is a JSON type, or several joined by `or`. `any` means the reader does not check the field.
- **Required** is `yes` (always present), `no` (may be absent), `with X` (may be absent only together with `X`), or `when ...` (required in the case it names, and may be absent otherwise).
- **One of** in a field's meaning lists every value it may have, besides `null` when its type allows null.
- A validated object rejects any field its table does not list. A field whose meaning says what it is keyed by is an object of named entries rather than of fixed fields.
- Paths are repository-relative POSIX paths: no leading `/`, no `\`, and no `..` part. Absolute paths are Windows drive-letter paths. Hashes are lowercase hexadecimal, 40 or 64 characters for a commit, 64 for SHA-256.

### Configuration

The code-review configuration file, validated by `validate_config` in `review_config.py` whenever it is read or written.

#### Configuration (`config`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `schema_version` | integer | yes | One of `1`. |
| `default_repository_set` | string | yes | The set an operation uses when no repositories or set are named and `operation_repository_sets` gives it none. Names a key of `repository_sets`. |
| `repository_sets` | object | yes | Keyed by set name (a letter or digit, then letters, digits, `_`, `.`, or `-`). Each value is a non-empty array of distinct `owner/repo` identities, each of which needs an entry in `repositories`. At least one set. |
| `repositories` | object | yes | Keyed by `owner/repo` identity, matched ignoring case; each value is a [repository](#repository-configrepositoriesrepository). Every repository a set names needs an entry. |
| `archive_root` | string | yes | Absolute path of the review archive. Not a drive root. |
| `local_mirror_root` | string or null | no | Absolute path of a second archive that each record is committed to before the archive; the two must hold the same latest version of a pull request. Null or absent for none. |
| `summary_root` | string | yes | Absolute path where `review-insights` writes its reports. |
| `dashboard_file` | string | yes | Absolute path of the Markdown file whose marked section `update-pr-tracker` owns. |
| `github_login` | string or null | no | The user's GitHub login for the tracker. Null or absent uses the account `gh` is signed in as. |
| `runtime` | string | no | One of `auto`, `claude-code`, `codex`, or `copilot-cli`. The runtime that starts reviewers; defaults to `auto`. |
| `verdict_policy` | object | no | When findings request changes; see [verdict policy](#verdict-policy-configverdict_policy). |
| `operation_repository_sets` | object | no | Keyed by operation (`review-prs`, `update-pr-tracker`, or `review-insights`). Each value names a key of `repository_sets`: the operation's own default set. |
| `reviewer_effort` | string or null | no | One of `low`, `medium`, `high`, `xhigh`, or `max`. Reasoning effort for reviewers the Workflow tool starts; null or absent keeps the session's effort. |
| `re_review_scope` | object | no | When an `auto` re-review reviews in full; see [re-review scope](#re-review-scope-configre_review_scope). |
| `model_names` | object | no | Keyed by a model identifier reviewers report, a trimmed single line of at most 200 characters. Each value is the non-empty single-line name of at most 100 characters that a report's Reviewers table shows instead. |
| `dashboard` | object | no | How the tracker section is marked and shown; see [dashboard](#dashboard-configdashboard). |

#### Repository (`config.repositories.<repository>`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `reviewer` | object | yes | Who reviews the repository; see [reviewer](#reviewer-configrepositoriesrepositoryreviewer). |
| `checkout_path` | string or null | when the reviewer's `scope` is `repository` | Absolute path of a local clone, from which the reviewer's files are read at the trusted commit. |

#### Reviewer (`config.repositories.<repository>.reviewer`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | A slug: a lowercase letter or digit, then up to 63 lowercase letters, digits, or `-`. `generic` for the generic scope. |
| `protocol_version` | integer | yes | One of `1`. |
| `scope` | string | no | One of `generic` or `repository`; defaults to `repository`. `generic` is the suite's own reviewer and allows none of the fields below except as null. |
| `trusted_ref` | string or null | no | The ref the reviewer's files are read at, instead of the pull request's base commit. Not blank and not a `refs/pull/` ref; a ref that resolves to the reviewed head is refused when a review starts. |
| `manifest_path` | string or null | when the scope is `repository` and `skill` is not set | A [reviewer manifest](#entrypoint-manifest-entrypoint-manifest) committed to the repository, as a path in it. Cannot be combined with `skill` or `manifest`. |
| `skill` | string or null | when the scope is `repository` and `manifest_path` is not set | The repository's own review skill or agent file, as a path in it, run as one entrypoint reviewer. |
| `manifest` | string or boolean or null | no | With `skill`: a [specialists manifest](#specialists-manifest-specialists-manifest) kept outside the repository, as an absolute path, or `true` for `reviewers/<owner>/<repo>/manifest.json` beside the configuration file. |

#### Verdict policy (`config.verdict_policy`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `request_changes_for` | array | no | The severities of which one open finding requests changes: a non-empty array of `MUST_FIX`, `SHOULD_FIX`, and `SUGGESTION`. Defaults to `["MUST_FIX"]`. |
| `should_fix_threshold` | integer | no | Changes are also requested when at least this many `SHOULD_FIX` findings are open. Positive; defaults to `3`. |

#### Re-review scope (`config.re_review_scope`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `full_share` | number | no | Review in full when at least this share of the pull request's changed lines changed since the last review. Above 0 and at most 1; defaults to `0.5`. |
| `full_lines` | integer | no | Review in full when at least this many changed lines changed since the last review. Positive; defaults to `1000`. |

#### Dashboard (`config.dashboard`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `start_marker` | string | no | The line that opens the owned section. A trimmed, non-empty single line; defaults to `<!-- code-review-pr-tracker:start -->`. |
| `end_marker` | string | no | The line that closes it, different from `start_marker`; defaults to `<!-- code-review-pr-tracker:end -->`. |
| `status_overrides` | object | no | Keyed by `owner/repo#number`. Each value is a non-empty status shown for that pull request, and may not be a computed one (`to review`, `awaiting response`, `my pull requests`, `drafts`, `missing`, `current`, or `stale`, in any case). |
| `author_names` | object | no | Keyed by GitHub login, unique ignoring case. Each value is the non-empty single-line name the Requestor column shows instead of the author's profile name. |

### Reviewer manifests

A repository reviewer's manifest, validated by `validate_adapter_manifest` in `review_runtime.py` when a review loads it. Every file a manifest names is a path in the repository and may not be named twice. `materialization.json` at the top of the reviewer's files is reserved in both schemas, in any case, because materialization writes its own record there and a file system that ignores case would let the record replace a file such as `Materialization.json`. A manifest that names it, as an entrypoint, resource, agent profile, specialist profile or resource, or condition script, is refused with `Adapter declares a reserved path`, and nothing is materialized. The same name below the top, such as `references/materialization.json`, is allowed.

#### Entrypoint manifest (`entrypoint-manifest`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `schema_version` | integer | yes | One of `1`. |
| `id` | string | yes | A slug, as for a configured reviewer. |
| `protocol_version` | integer | yes | One of `1`. |
| `supports` | array | yes | The review modes it handles: a non-empty array of distinct `initial` and `re-review`. |
| `required_capabilities` | array | yes | Distinct non-empty capability names the runtime must offer: Claude Code and Codex offer `agent-delegation`, `read-diff`, and `write-result`, and Copilot CLI offers `isolated-added-root`, `read-diff`, and `write-result`. |
| `entrypoint` | string | yes | The reviewer's instructions. |
| `resources` | array | yes | Further files the reviewer may read. |
| `agent_profiles` | array | yes | Agent files the reviewer may start. |

#### Specialists manifest (`specialists-manifest`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `schema_version` | integer | yes | One of `2`. |
| `id` | string | yes | A slug. |
| `protocol_version` | integer | yes | One of `1`. |
| `kind` | string | yes | One of `specialists`. |
| `supports` | array | yes | As in an entrypoint manifest. |
| `required_capabilities` | array | yes | As in an entrypoint manifest, and includes `agent-delegation`. |
| `resources` | array | yes | Distinct files any specialist may read. |
| `specialists` | array | yes | The specialists, at least one. |
| `conditions` | object | yes | Keyed by condition name, a slug; each value is a [condition](#condition-specialists-manifestconditionscondition). May be empty. |
| `uncovered` | string | no | One of `review` or `ignore`. What happens to changed files no specialist matches when some do: `review`, the default, gives them to the generic reviewer, and `ignore` leaves them unreviewed and lists them in the record. |

#### Specialist (`specialists-manifest.specialists[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | A slug, unique in the manifest and not `generic-review`. |
| `category` | string | yes | The category of its findings. Not blank. |
| `profile` | string | yes | Its agent file. |
| `include` | array | yes | Non-empty Python regular expressions searched against changed paths; the specialist runs when one matches a changed file. |
| `exclude` | array | yes | Regular expressions for changed paths it skips even when included. |
| `resources` | array | yes | Its guideline files, read from the pull request's base commit when that commit has them. |
| `when` | string or null | yes | A key of `conditions` that must hold for it to run, or null to run whenever files match. |
| `model` | string | no | One of `inherit`, `sonnet`, `opus`, `haiku`, or `fable`. Its reviewer's model, over its profile's; `inherit` uses the session's. |
| `effort` | string | no | One of `low`, `medium`, `high`, `xhigh`, or `max`. Its reviewer's reasoning effort when the Workflow tool starts it, over the configuration's `reviewer_effort`. |

#### Condition (`specialists-manifest.conditions.<condition>`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `script` | string | yes | A script beside the manifest, run as `python <script> --source-root <snapshot>`. Exit code 0 runs the specialists that name it, 1 skips them, and anything else fails the review. |

### Adapter request

The request a reviewer is given, as `build_adapter_request` in `review_runtime.py` writes it. No validator reads it back: reviewers read it, and the core checks a result against it.

#### Request (`request`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `protocol_version` | integer | yes | One of `1`. |
| `mode` | string | yes | One of `initial` or `re-review`. |
| `repository` | string | yes | The `owner/repo` identity, lowercase. |
| `pull_number` | integer | yes | The pull request's number. |
| `pull_request` | object | yes | The pull request reviewed. |
| `diff_path` | string | yes | Absolute path of the pull request's diff. |
| `source_snapshot` | object | yes | The hash-verified snapshot of the head commit. |
| `prior_findings` | array | yes | The findings a re-review must give a disposition for; empty for an initial review. |
| `github_comments` | array | yes | The open review comments a person started, each needing a disposition. |
| `coverage` | object | yes | Changed files reviewers see only as diff. |

#### Request pull request (`request.pull_request`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `title` | string | yes | Its title. |
| `url` | string | yes | Its web address. |
| `base_ref` | string | yes | The branch it merges into. |
| `base_sha` | string | yes | The base commit. |
| `head_sha` | string | yes | The head commit reviewed. |
| `head_ref` | string | no | Its head branch, when known. |

#### Source snapshot (`request.source_snapshot`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `root` | string | yes | Absolute path of the snapshot folder. |
| `manifest_path` | string | yes | Absolute path of the snapshot's manifest, which lists every file and its hash. |
| `source_commit` | string | yes | The commit it holds: the head commit. |

The snapshot's manifest records each path it leaves out under `excluded_paths`, with one reason: `agent-instruction`, `binary`, `file-size-limit`, `unsafe-path`, `symbolic-link`, or `non-regular` (any other entry that is not a regular file or a directory, such as a hard link, FIFO, or device). Only `file-size-limit` and `unsafe-path` are coverage gaps, which put a changed file in `coverage.unavailable_sources`. The others are deliberate, and reviewers judge those files from the diff.

#### Request coverage (`request.coverage`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `unavailable_sources` | array | yes | Changed files the snapshot could not hold, for size or an unsafe name, and changed paths no reviewer prompt can carry safely. |

#### Prior finding (`request.prior_findings[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | Its ledger entry's ID, `v<version>:F<nnn>`. |
| `severity` | string | yes | One of `MUST_FIX`, `SHOULD_FIX`, or `SUGGESTION`. |
| `category` | string | yes | Its category. |
| `path` | string | yes | Where it was last reported. |
| `line` | integer | yes | The line it was last reported at. |
| `title` | string | no | Its headline, when it had one. |
| `body` | string | yes | What it said. |
| `flags` | array | no | The flags in the `flag-review-finding` store that name the finding or one of its repeats, resolved or not; absent when there are none. |

#### Prior finding flag (`request.prior_findings[].flags[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | The flag's ID, such as `RF-000001`. |
| `category` | string | yes | The flag's category, a label the user chose. |
| `rationale` | string | yes | The flag's body: why the user judged the finding wrong or noisy. |

### Review record

A review version's JSON record, the archive's source of truth, validated by `validate_record` in `review_records.py` whenever it is written or read.

#### Record (`record`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `schema_version` | integer | yes | One of `1`. |
| `repository` | string | yes | The `owner/repo` identity. |
| `pull_request` | object | yes | The pull request reviewed. |
| `review` | object | yes | This review version. |
| `findings` | array | yes | This review's findings. |
| `prior_dispositions` | array | yes | One per prior finding the review was given; empty for an initial review. |
| `ledger` | array | when a finding has `repeats` | Every problem raised on the pull request since its latest initial review, one entry each, ordered by version and finding ID. Absent from records written before ledgers, which read as having no history. |
| `github_comments` | array | with `comment_dispositions` | The open review comments the reviewers were given, at least one. |
| `comment_dispositions` | array | with `github_comments` | Exactly one per comment. |
| `artifacts` | object or null | no | Hashes that tie the record to its Markdown report, added when the pair is written, so every archived record has them. Null reads as absent. |

#### Record pull request (`record.pull_request`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `number` | integer | yes | Its number. Positive. |
| `url` | string | yes | Its web address. |
| `title` | string | yes | Its title. |
| `base_ref` | string | yes | The branch it merges into. |
| `base_sha` | string | yes | The base commit. |
| `head_sha` | string | yes | The head commit reviewed. |
| `head_ref` | string | no | Its head branch; absent from older records. |

#### Review (`record.review`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `version` | integer | yes | This version, counting from 1 per pull request. A version is never overwritten. |
| `mode` | string | yes | One of `initial` or `re-review`. |
| `reviewed_at` | string | yes | When it was recorded, as an ISO 8601 time. |
| `summary` | string | yes | The reviewer's summary. |
| `verdict` | string | yes | One of `APPROVED`, `CHANGES_REQUESTED`, or `INCOMPLETE`. `INCOMPLETE` needs `coverage.unavailable_sources`. |
| `counts` | object | yes | Keyed by severity: `MUST_FIX`, `SHOULD_FIX`, and `SUGGESTION`, each the number of this review's findings of that severity. |
| `adapter` | object | yes | The reviewer that produced it. |
| `coverage` | object | no | Changed files no reviewer saw in full. Present only when one of its lists is not empty. |
| `reviewers` | array | no | The reviewers that ran, at least one; absent from older records and for a single reviewer. |
| `patches` | object | no | Keyed by changed file path; each value is that file's [patch fingerprint](#patch-fingerprint-recordreviewpatchespath), so the next re-review can tell which files changed since. Not empty; absent from older records. |
| `scope` | object | no | A re-review's scope. Only a re-review has one; absent from older records. |

#### Review coverage (`record.review.coverage`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `unavailable_sources` | array | yes | Distinct changed files whose source the snapshot could not provide, so reviewers saw only their diff, and changed paths no reviewer prompt can carry safely, which no reviewer was given. One makes the verdict `INCOMPLETE` unless findings request changes. |
| `uncovered_files` | array | no | Distinct changed files no specialist covers, left unreviewed because the manifest sets `uncovered` to `ignore`. A deliberate opt-out that does not change the verdict. |

#### Reviewer that ran (`record.review.reviewers[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | The specialist or reviewer, unique in the list. |
| `category` | string | yes | Its category. |
| `files` | integer | yes | How many changed files it covered. Not negative, as for each count here. |
| `findings` | integer | yes | How many findings it raised. |
| `retries` | integer | yes | How many times it was rerun. |
| `dispositions_only` | boolean | yes | Whether it only gave dispositions, because none of its files changed. |
| `model` | string | no | The model it reported running on: one trimmed line of at most 200 characters. Absent for a repository entrypoint reviewer and from older records. |
| `seconds` | integer | no | Whole seconds from handing it the role to its accepted result, reruns included; absent when not timed. |

#### Patch fingerprint (`record.review.patches.<path>`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `sha256` | string | yes | SHA-256 of the file's added and removed lines and header lines, never hunk positions or context. |
| `lines` | integer | yes | How many changed lines the file has. Not negative. |

#### Re-review scope (`record.review.scope`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `requested` | string | yes | One of `auto`, `full`, or `incremental`. |
| `used` | string | yes | One of `full` or `incremental`. `incremental` needs the changed counts. |
| `reason` | string | yes | Why that scope ran. |
| `since_version` | integer | yes | The earlier version it compared with. |
| `files_changed` | integer or null | yes | Files changed since that version, at most `files_total`; null, with `lines_changed`, when it could not compare. |
| `files_total` | integer | yes | The pull request's changed files. |
| `lines_changed` | integer or null | yes | Changed lines changed since that version, at most `lines_total`; null with `files_changed`. |
| `lines_total` | integer | yes | The pull request's changed lines. |

#### Record adapter (`record.review.adapter`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `name` | string | yes | The configured reviewer's ID. |
| `protocol_version` | integer | yes | One of `1`. |
| `scope` | string | yes | One of `generic` or `repository`. |
| `source_commit` | string or null | yes | The commit a repository reviewer's files were read at; null for the generic reviewer. |
| `source_hashes` | object | yes | Keyed by path of each loaded reviewer file; each value is the SHA-256 of its exact committed bytes. Empty for the generic reviewer. |
| `reviewer` | string | yes | The reviewer the result named. |
| `status` | string | yes | One of `complete`, `partial`, or `failed`. Only a complete result is archived. |
| `usage` | object or null | yes | The usage object the reviewer returned, or null. Never estimated or priced. |

#### Finding (`record.findings[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | `F001`, `F002`, and so on, in order of path, line, and candidate key. IDs restart in every review. |
| `candidate_key` | string | yes | The reviewer's own key for it. |
| `severity` | string | yes | One of `MUST_FIX`, `SHOULD_FIX`, or `SUGGESTION`. |
| `category` | string | yes | Its category. |
| `path` | string | yes | The changed file. |
| `line` | integer | yes | A positive line number in the changed file. |
| `title` | string | no | A headline: one trimmed line of at most 120 characters. Absent from older records. |
| `body` | string | yes | The problem. |
| `evidence` | string | yes | What shows it. |
| `source` | string | yes | The reviewer that raised it. |
| `analyzer` | object | no | How a diagnostic analyzer could catch it instead. |
| `repeats` | object | no | The finding it repeats, which it is counted with: one in this review, or the first finding of an earlier ledger entry. At least as severe, and not itself a repeat. |

#### Analyzer coverage (`record.findings[].analyzer`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `coverage` | string | yes | One of `available`, `known`, or `custom-candidate`: a rule in an analyzer the repository already has but does not enforce, a rule in an established analyzer it does not use, or a pattern no rule covers. |
| `tool` | string | yes | The analyzer: at most 100 characters, with no whitespace, backticks, pipes, or angle brackets. |
| `rule` | string | yes | The rule, written like `tool`. For `custom-candidate`, a lowercase kebab-case pattern name of at most 60 characters. |

#### Finding reference (`record.findings[].repeats`, `record.ledger[].repeats[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `version` | integer | yes | The review version of the finding. |
| `id` | string | yes | Its finding ID in that version. |

#### Prior disposition (`record.prior_dispositions[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `finding_id` | string | yes | The prior finding: its ledger entry's `v<version>:F<nnn>` in a record with a ledger, or a bare finding ID of the version it compared with in an older record. Unique. |
| `disposition` | string | yes | One of `addressed`, `partially_addressed`, `still_present`, `superseded`, or `unable_to_verify`. |
| `rationale` | string | yes | Why. Not blank. |

#### Ledger entry (`record.ledger[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `version` | integer | yes | The review version where the problem first appeared. |
| `id` | string | yes | Its finding ID in that version. |
| `severity` | string | yes | One of `MUST_FIX`, `SHOULD_FIX`, or `SUGGESTION`. |
| `category` | string | yes | Its category. |
| `state` | string | yes | One of `open`, `closed`, or `unverified`. `open` when its latest judgment raised or repeated it or found it `still_present` or `partially_addressed`; `closed` when `addressed` or `superseded`; `unverified` when `unable_to_verify`. Open entries count toward the verdict. |
| `judged_in` | integer | yes | The version that last raised, repeated, or disposed of it. |
| `dispositions` | array | yes | Each later review's disposition of it, oldest first. |
| `repeats` | array | yes | The findings linked to it as repeats, oldest first. |

#### Ledger disposition (`record.ledger[].dispositions[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `version` | integer | yes | The later review that judged it. |
| `disposition` | string | yes | One of `addressed`, `partially_addressed`, `still_present`, `superseded`, or `unable_to_verify`. |

#### Review comment (`record.github_comments[]`, `request.github_comments[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | `C1`, `C2`, and so on, unique. |
| `author` | string | yes | The person who started the thread. |
| `path` | string | yes | The file it is on. |
| `line` | integer or null | yes | Its line, or null when it has none. |
| `outdated` | boolean | yes | Whether the code it was on has changed since. |
| `body` | string | yes | The thread's first comment. |
| `url` | string | yes | Its web address. |

#### Comment disposition (`record.comment_dispositions[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `comment_id` | string | yes | The comment's ID. Unique. |
| `disposition` | string | yes | One of `addressed`, `partially_addressed`, `still_present`, `superseded`, or `unable_to_verify`. |
| `rationale` | string | yes | Why. Not blank. |

#### Artifacts (`record.artifacts`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `payload_sha256` | string | yes | SHA-256 of the record without its artifacts. |
| `markdown_sha256` | string | yes | SHA-256 of the Markdown report, which names the payload hash. |

### Flag store

The flag store shared by `flag-review-finding` and `review-insights`, validated by `validate_store` in `review_flags.py` whenever it is read or written. A version 1 store is read as version 2 with no review versions.

#### Flag store (`flag-store`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `schema_version` | integer | yes | One of `2`. |
| `next_id` | integer | yes | The number the next flag gets. Greater than every allocated one. |
| `flags` | array | yes | The flags, oldest first. |

#### Flag (`flag-store.flags[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | `RF-` and six digits, unique. |
| `status` | string | yes | One of `open` or `resolved`. |
| `created_at` | string | yes | When it was added, as an ISO 8601 time. |
| `resolved_at` | string or null | yes | When it was resolved, as an ISO 8601 time; null while open. |
| `repository` | string or null | yes | The `owner/repo` it is about, or null. |
| `pull_number` | integer or null | yes | The pull request it is about, or null. |
| `review_version` | integer or null | yes | The review version its finding is in, which needs `pull_number`; or null. |
| `finding_id` | any | yes | The finding it names in that review version, as written by `flag-review-finding`, or null. Its form is not checked when read. |
| `category` | string | yes | Its category. Not blank. |
| `body` | string | yes | The observation. Not blank. |
| `resolution` | string or null | yes | How it was resolved; null while open. |

### Legacy review index

A pull request migrated from the legacy review skills keeps a `legacy-review.json` index beside the copied `legacy-review.md`. It remains readable as a reviewed head; its findings were never converted. `legacy_index` in `review_operation.py` reads it, and treats an index that fails these rules as absent, so the pull request is offered for review again. See "Legacy reviews" in [Code-review operations](code-review-operations.md#legacy-reviews).

#### Legacy review index (`legacy-index`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `schema_version` | integer | yes | One of `1`. |
| `kind` | string | yes | One of `legacy-review-index`. |
| `repository` | string | yes | The `owner/repo` of the archive folder it is in, in any case. |
| `pull_number` | integer | yes | The pull request of the archive folder it is in. |
| `reviewed_at` | any | yes | When the legacy review was written, as an ISO 8601 time. Not checked when read. |
| `reviewed_head_sha` | string | yes | The head commit the legacy review covered. |
| `verdict` | any | yes | The legacy verdict, `APPROVED` or `CHANGES REQUESTED`. Not checked when read. |
| `source_sha256` | any | yes | A hash of the legacy review's content. Not checked when read. |
| `source_path` | any | yes | Where the legacy review was copied from. Not checked when read. |
| `source_file_sha256` | any | yes | SHA-256 of the legacy review file. Not checked when read. |
