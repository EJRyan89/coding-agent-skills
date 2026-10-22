# Code-review operations behavior contract

This is the behavior the public skills of the `code-review-operations` bundle, each a row under [Skill behavior](#skill-behavior), and the review archive they share, promise to keep: what each produces, how the archive and its state behave, what reviewers may and may not do, and the role of each runtime. A change that breaks one of these promises is a breaking change to the suite. How to configure and run the suite is in [Code-review operations](code-review-operations.md). Fixtures and tests use repository-neutral identities only.

## Skill behavior

| Public skill | Preserved behavior | Authoritative output |
| --- | --- | --- |
| `review-prs` | Review open non-draft pull requests and merged pull requests after a watermark; review one exact configured pull request, draft or not, without enumeration; skip an unchanged reviewed head; allow explicit subset selection; preserve retry eligibility after partial failure. | Validated JSON/Markdown review pair and per-repository state. |
| `review-prs --re-review` | Re-review a previously reviewed pull request; require a changed head unless forced; compare every prior finding; create the next review version without overwriting history; never post to GitHub. | Versioned JSON/Markdown review pair. |
| `update-pr-dashboard` | Track pull requests authored by, assigned to, or involving the configured user; place each in a counted section from the user's own GitHub review state; treat an update as unchanged only when every file the pull request changes is identical in content and file mode to what was reviewed, reading a commit's file modes only when its changed files already match the other commit's in path, status, previous path, and blob, and uncertain responses to requested changes as awaiting response; omit approved pull requests unchanged since approval; remove a row on the user's direct assessment without acting on GitHub, refusing a removal that names no pull request it tracks; pin a pull request the last update tracked to a status of the user's own, in its own section, only through a validated change to `dashboard.status_overrides`, and mark each override whose pull request it no longer tracks; show missing/current/stale/incomplete AI review status independently of section, and name each review that went stale since the last update, unless an override pins its pull request; never mark a review stale, or rewrite any row, when a GitHub call a comparison needs fails, but name each pull request whose comparison failed; optionally offer to generate missing or stale reviews, never for a pull request an override pins; preserve user-authored dashboard content, a symbolic link to the dashboard, and the file's mode. | One marker-owned dashboard section. |
| `review-insights` | Filter reviews by explicit inclusive dates; aggregate severity/category themes; count findings later judged addressed or still present per reviewer, model, and category; prepare a sealed synthesis input of grouped findings, outcomes, every open flag in scope, and each repository's guidance files, and record an analyst agent's validated recommendations targeting those files or analyzer rules; record an accept/reject/defer decision per recommendation; retain reproducible evidence. | Versioned summary JSON plus Markdown projection; synthesis input, context, prompt, and result beside it. |
| `flag-review-finding` | Add, list, and resolve review-improvement observations with stable IDs and optional PR/finding association; correct an open flag's category and rationale, leaving what it names and its status as they were, and refuse to edit a resolved one; take a finding by the report label its pull request's findings list prints, or by its ID and review version; refuse a finding the archive does not have; list a pull request's open findings under their report labels. | Locked structured flag store. |
| `review-document` | Review one document that is not in a pull request, from its file path, or each document a pull request changes, as a fixture canary under the reviewer guard, result validation, and record of a pull request review; by default review a file committed in a git checkout against its committed version, so only the uncommitted change is judged, and any other file whole; review a Word document or a PDF as the text the snapshot extracts, and give each finding its place in the document; refuse, before any review, a file it cannot review or whose change is not in its text; review a pull request's documents read from GitHub at its merge base and head, each with its own report and record, carrying none of the pull request's title, description, branch names, or comments into a fixture; read nothing from GitHub for a path, and write nothing to GitHub or the checkout. | Validated JSON/Markdown review pair under the canary root `finalize` names, and a copy of the report in the output directory. |

Repository targeting is always one or more full `owner/repo` identities or a named configured set. Pagination must complete per repository. Authentication, rate limits, malformed responses, and unexpected API failures fail closed.

## Archive and state behavior

- Archive keys are `<owner>/<repo>/pulls/<number>`; repository short names are display-only.
- JSON is the machine source of truth. Markdown is a hash-linked projection.
- Review versions are allocated under a per-PR lock and never overwrite history.
- Merged-pull watermarks are independent per repository. Incomplete enumeration, a failed eligible merge, or an eligible merge a bounded batch deferred cannot advance that repository past the missing work. No watermark moves past the pipeline's own UTC today: `advance` reads the batch file's date as input and refuses one later than today, or one that would move a watermark past it.
- A merged pull request is eligible when it merged on or after its repository's watermark date, judged by the UTC date of its merge time alone: one merged before the watermark stays out however recently it was updated (commented on, labeled, or edited) after merging. Because merging updates a pull request, every one merged on or after the watermark was last updated on or after it, so enumeration may stop listing closed pull requests, read from the most recently updated, at the first page whose last pull request was updated before the watermark. A repository without a watermark lists its whole history once.
- Mutable state, flags, and review versions use separate short-lived locks; network and semantic review work occurs outside those locks. The configuration file takes none: nothing in the suite changes it in place, and `review_config.py write` replaces it whole through a uniquely named temporary file, so a reader sees the old file or the new one, and of two writes at once the later one stays.

## Reviewer behavior

- The bundled generic reviewer and a repository's specialists work under the same prompt contracts, and each writes a [specialist result](#specialist-result), which the core validates and assembles into one adapter result. A repository's entrypoint reviewer is given the versioned request and writes the adapter result itself. Its one prompt carries the request's facts and the adapter result's contract, and states the input rules of a specialist's prompt, so it starts from the prompt and the trusted entrypoint the prompt names.
- The bundled generic reviewer's instructions and the adapter result schema resolve from the installed `ejryan89-code-review-core` skill, not from a repository checkout or branch.
- Re-review requires exactly one disposition for every prior finding, and every open or unverified entry of the pull request's finding ledger is a prior finding until a review closes it.
- A finding that repeats another one is linked to it with `repeats` and counted once, never twice. A link must name an existing finding at least as severe that is not itself a repeat, so a finding that restates another at a higher severity is recorded unlinked, as a new finding.
- The core assigns stable finding IDs, keeps the finding ledger, calculates verdicts from its open entries, renders reports, and owns durable writes. An initial review starts a fresh ledger; a record written before ledgers stays valid and is read as having no history.
- Repository reviewers are loaded only from a trusted commit: an explicitly configured trusted ref, else the pull request's base, else, for a configured review skill the base predates, the default branch's tip as origin reports it. When that tip lacks the skill too, or is the pull request's head, the suite's generic reviewer reviews the pull request instead. The record's `adapter.source` names which ran. Every loaded file is declared, materialized, and hashed. A profile the suite ships, which a specialists manifest names as `suite:<name>`, is read from the suite's own deployed references and hashed, never from the repository.
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
- Runtime output is untrusted until the core validates it as a specialist result or an adapter result.
- Review, re-review, tracker, flag, and insight operations only read GitHub state. None of them posts comments, creates pending reviews, or submits review state.
- Repository-provided reviewers are loaded only from an immutable trusted commit.
- Review source is materialized from the exact PR head into a hash-verified snapshot. On the checkout route it is lazy: the changed files and the analyzer settings are written up front, and any other file the head holds is written only when a reviewer asks for it, from the commit by its blob id and checked against that id. Agent configuration and instruction paths from the PR head are excluded; only reviewer-manifest files from the trusted base may instruct the reviewer.
- The suite-owned generic reviewer and result schema are resolved in the installed `ejryan89-code-review-core` skill's `references/` directory. They never depend on a configured repository checkout, its current branch, or a hard-coded main-worktree path.

When configuration selects `copilot-cli`, the pipeline runs the bounded host in `ejryan89-code-review-core/scripts/review_hosts.py`, never Copilot directly. The host requires Copilot CLI 1.0.88 or newer, verifies the exact file set and hashes of every materialized reviewer resource, requires the diff and source snapshot to be inside the isolated run directory, runs with isolated `HOME`, `USERPROFILE`, `COPILOT_HOME`, and working directory values and without git's repository-selecting variables, so a `GIT_DIR` the invoking shell exported never points Copilot or a git it runs at another repository, disables ambient instructions and MCP servers, and grants write access only to its attempt's staging file, which the host renames to the result path once it is a JSON object and the role has not been set aside. `dispatch` starts the host detached and `wait` follows it, so no runtime's command time limit stops a review. The host gives Copilot its prompt as its whole stdin, kills it at `COPILOT_TIMEOUT_SECONDS` in `review_hosts.py`, keeps at most `COPILOT_OUTPUT_BYTES` of each output stream in the attempt's diagnostic, naming how many bytes past that were cut, and runs it in a process tree, a Windows job object or elsewhere a process group, so every process Copilot started is ended with it. Authentication tokens inherited from the invoking environment remain available by design, so treat this as configuration isolation rather than credential isolation.

## Runtime roles

| Runtime | Role | Trust boundary |
| --- | --- | --- |
| Claude Code | Native skill host and native reviewer delegation. | Authoritative rendered skill plus immutable materialized repository reviewer. |
| Codex | Thin runtime adapter and native agent delegation. | The adapter loads the authoritative skill; durable outputs still pass core validation. |
| GitHub Copilot CLI | Personal skill host, inline reviewer of the generic reviewer's and specialists' roles, and bounded non-interactive driver of a repository entrypoint reviewer. | Inline: the session's own tools and permissions, with the run's files sealed and every result validated. Host: no custom instructions, shell, URL, memory, or interactive question tools; JSONL is diagnostics only. |
| Copilot cloud agent | Not a local suite host. | Cannot advance local state or commit local records without a separately approved transport. |
| Copilot code review | External evidence source. | PR-head instructions are untrusted for the suite protocol. |

## Threat model

The code-review bundle reads input an adversary can write. The adversary is a pull request's author, or the author of a document supplied to `review-document`: they control the reviewed content, and through it what a reviewer model reads. The configuration, the trusted reviewer commit, the archive, a canary's fixture infrastructure and metadata, and the session running the review are trusted. A document copied into a fixture can still be hostile author input; trusted metadata and reviewer routing do not change its trust or exempt it from the snapshot rules. Each row names what the author controls, what the suite guarantees against it, and the tests that hold the guarantee. Every guarantee fails closed: a file left out is an exclusion entry, a change no reviewer could see in full is a coverage gap that makes the verdict `INCOMPLETE`, a reviewer's call outside its role is denied, and anything else ends the pull request as one `FAILED` line with nothing recorded, never a traceback: `prepare`'s names the pull request's selector, and `check`'s or `finalize`'s names its run directory, each followed by the reason. Each row names at least one test in `test_adversarial_inputs.py` that asserts it; validation fails when a row names none there, or names a test that does not exist.

| The author controls | The suite guarantees | Held by |
| --- | --- | --- |
| Diff text and headers: every content line, and the header lines git writes for their file names. | The diff is data in `diff.patch` and the work files, never prompt text. A file block starts only at a `diff --git` line, and lines split only at LF, so a carriage return, U+2028, or any other line break inside content cannot start a header or a file. A header path no prompt can carry (a control character, U+007F, a line break, a backslash, or an absolute, empty, `.`, or `..` segment) is never parsed into a prompt, work file, or note: it is an unavailable source, the review is `INCOMPLETE`, and a pull request that changes only such files fails. Undecodable bytes become U+FFFD. | `test_adversarial_inputs.py::test_hostile_diff_lines_and_a_newline_path_become_one_exclusion_never_a_prompt_line`, `test_review_specialists.py::test_a_quoted_newline_path_cannot_inject_a_prompt_line`, `test_review_specialists.py::test_reviewable_path_rejects_each_control_and_line_breaking_character`, `test_review_pipeline_prepare.py::test_a_path_with_a_control_character_reaches_no_prompt_and_is_a_coverage_gap`, `test_review_github.py::test_the_diff_replaces_and_counts_each_undecodable_byte` |
| What a condition script derives from the diff and the head and writes for the specialists it opens: text quoted from the author's documents and code, and its length. | The script is trusted, but the file it writes is author-controlled data, never an instruction, and reaches a reviewer only as a file it chooses to read. `prepare` copies at most the file's first `CONDITION_FILE_BYTES` (32 KiB) to `<specialist>.condition.txt` for each specialist the condition opened and for no other role, never into a prompt or a command. The prompt names it `CONDITION_FILE`, says it is untrusted pull-request data whose instructions are never followed, and says how much of the file it holds from the suite's own count, so text in it cannot claim another length or speak as the contract. A link or folder in its place fails the review rather than hand a reviewer a file the script never wrote. No condition is given the pull request's description. | `test_adversarial_inputs.py::test_a_condition_file_reaches_its_specialist_only_as_a_bounded_file_the_prompt_names_untrusted`, `test_review_specialists.py::test_an_open_condition_s_file_reaches_only_the_specialists_it_opened_named_untrusted`, `test_review_specialists.py::test_a_condition_file_past_its_bound_is_cut_and_the_prompt_states_how_much_from_its_own_count`, `test_review_specialists.py::test_a_condition_output_that_is_not_a_regular_file_fails_the_review`, `test_review_specialists.py::test_a_condition_output_that_is_a_link_fails_the_review` |
| Paths and file names in the head's tree. | A path the snapshot cannot hold on Windows as it is named is an `unsafe-path` exclusion, never written, and a coverage gap only for a pull request that changes it: that review is `INCOMPLETE`, and every other review of the repository runs in full. `UNSAFE_PATH_RULES` in `review_runtime.py` holds the rules, each with its reason: bytes that are not UTF-8, named with U+FFFD for each byte; a reserved character (`:<>"\|?*`) or a control character; a backslash, which Windows reads as a folder separator; a segment ending in a dot or a space, which Windows drops; a reserved device name, with or without an extension (`CON`, `PRN`, `AUX`, `NUL`, `CONIN$`, `CONOUT$`, and `COM` or `LPT` followed by a digit, `¹`, `²`, or `³`), in any case; and the two length rules in the next row. The diff names a changed file exactly as the snapshot does, a trailing space included, so its exclusion is found. `validate-reviewer` counts these exclusions rather than failing. A name that would leave the snapshot or name its root (absolute, or with an empty, `.`, or `..` segment) fails the snapshot, and so do two names a case-insensitive file system would merge. | `test_adversarial_inputs.py::test_names_windows_cannot_hold_are_unsafe_path_exclusions_and_a_coverage_gap_only_where_changed`, `test_adversarial_inputs.py::test_each_rule_names_why_windows_cannot_hold_a_path_and_names_just_inside_are_kept`, `test_adversarial_inputs.py::test_names_a_case_insensitive_file_system_would_merge_fail_the_snapshot`, `test_review_runtime.py::test_an_undecodable_name_is_an_unsafe_path_and_a_coverage_gap_only_where_changed`, `test_review_specialists.py::test_a_name_ending_in_a_space_keeps_it_and_loses_only_the_tab_git_ends_it_with`, `test_review_pipeline.py::test_validate_reviewer_counts_names_windows_cannot_hold_as_unsafe_paths`, `test_review_followups.py::test_case_collisions_fail_closed` |
| Path length. | A segment over 255 UTF-16 code units, which no Windows file system holds, is an `unsafe-path` exclusion, and so is a path longer than the room the snapshot's folder leaves it. The folder and the path together may hold 259 units for a file and 247 for the folder it is in without long-path support (MAX_PATH less its terminating NUL, and less the 12 CreateDirectory keeps for an 8.3 name), and 32,507 for either with it (the 32,767 of an NT path less MAX_PATH left for the volume's device name, which Windows counts in place of the drive). The room is computed from the snapshot's own folder, and `validate-reviewer` measures at a folder as long as the one `prepare` creates. A path exactly at each limit is written. | `test_adversarial_inputs.py::test_paths_at_the_room_the_root_leaves_are_kept_and_one_unit_longer_are_unsafe_path_exclusions`, `test_adversarial_inputs.py::test_a_path_at_this_machines_limit_is_written_and_one_unit_longer_is_an_unsafe_path_exclusion`, `test_adversarial_inputs.py::test_the_limits_are_windows_path_limits_with_and_without_long_paths`, `test_adversarial_inputs.py::test_each_rule_names_why_windows_cannot_hold_a_path_and_names_just_inside_are_kept`, `test_review_pipeline.py::test_validate_reviewer_measures_at_a_path_as_long_as_the_source_folder_of_prepares_run` |
| Blob content: bytes, encodings, line endings, and text addressed to a reviewer. | The snapshot holds each kept file's exact committed bytes, hashed as written, except a changed Word document or PDF, which it holds as its extracted text (the next row). A file with a NUL byte in its first 8,000 bytes is a `binary` exclusion, and a changed one is a coverage gap unless its name ends in a suffix of a type the review does not read, when it is an unreviewed file instead; the suffix decides only once the bytes are found binary, so a text file the author names like an archive is reviewed as text, and an unexpected binary still makes the review `INCOMPLETE`. A file over the per-file limit (1 MiB, or 16 MiB for a changed file) is a `file-size-limit` exclusion, never read, and, when changed, a coverage gap. The one exception is a changed file over the limit whose name ends in such a suffix and that is no Word document or PDF: the snapshot reads only its first 8,000 bytes, from the tarball entry's stream, or on the checkout route from one `git cat-file --batch` it stops once they arrive, so the rest is never read whole, and when they hold a NUL byte it is a `binary` exclusion, so an unreviewed file. An oversized text file such a suffix names, and an oversized binary no suffix names, stay `file-size-limit` gaps. Content reaches reviewers only as files they read, never as prompt text. | `test_adversarial_inputs.py::test_hostile_blob_content_reaches_reviewers_only_as_exact_bytes_or_an_exclusion`, `test_adversarial_inputs.py::test_a_binary_of_a_type_the_review_does_not_read_is_named_and_any_other_binary_leaves_it_incomplete`, `test_adversarial_inputs.py::test_a_listed_binary_over_the_changed_file_limit_is_unreviewed_on_both_routes_and_any_other_one_a_gap`, `test_review_runtime.py::test_a_changed_listed_file_over_the_limit_is_probed_from_the_checkout_and_only_its_first_bytes_read`, `test_review_runtime.py::test_a_changed_listed_file_over_the_limit_is_probed_from_the_tarball_and_only_its_first_bytes_read`, `test_review_runtime.py::test_a_blob_prefix_is_read_only_to_the_probe_and_its_size_must_be_the_listed_one`, `test_review_runtime.py::test_source_snapshot_excludes_binary_and_oversized_files_from_limits`, `test_review_pipeline_prepare.py::test_a_changed_file_over_the_source_limit_is_kept_under_the_changed_file_limit` |
| The bytes of a Word document (`.docx`) or a PDF (`.pdf`) the pull request adds or changes, including one supplied through `review-document`'s fixture: a zip package and the XML inside it, or a PDF's own format. | A changed `.docx`, by its suffix ignoring case, whose blob the snapshot finds binary is held as its extracted text on every route, never as its bytes; an unchanged one stays `binary`. `review_documents.py` reads it with the standard library, from memory, in the review's own process, with no network and nothing written but the text. Before any XML is parsed, the document is refused when it is not a readable zip, holds more than 2,000 members, names a member with an absolute, drive, backslash, NUL, empty, `.`, or `..` path, holds two members whose names differ only by case or an encrypted one, lacks `word/document.xml`, or has a part that decompresses past 16 MiB, counted as it is read rather than taken from the size the package declares; and when a part it reads is not UTF-8, declares another encoding, or holds a document type or entity declaration, so no entity expands and none is fetched. The parser's own handlers refuse both again and never parse a parameter entity, and text over 1 MiB is refused. Any other failure of the reader, such as a compressed part whose stream is corrupt, refuses the document with a reason that names the failure's type and none of its message. A refused document is an `unextractable` exclusion and a coverage gap, so the review is `INCOMPLETE`, and `prepare` notes the reason, which names no content. The text reaches reviewers as the snapshot file and the diff do, never as prompt text, and its base is read by the blob id the diff names and must hash to it. A changed PDF, by its suffix ignoring case, is held as its extracted text whether or not the snapshot finds it binary, since one written without compressed streams has no NUL byte. The standard library cannot read it, so `pdftotext` does: an optional tool with separate Xpdf and Poppler support floors, declared in ejryan89-skill-core's `pdf_tool.py` and referenced by the catalogue. `deploy.py check` and every extraction share that policy. Before writing author bytes, extraction finds every copy of the tool as `deploy.py check` does, each on `PATH`, then in WinGet's Links folder, resolves each one's absolute path in that order, and probes it with `-v`, no document or stdin, a five-second deadline and at most 16 KiB of combined stdout/stderr, taking the first that passes. A probe requires an unambiguous vendor banner, a supported provider version, and the expected version status (Xpdf 0 or 99; Poppler 0). An old, unknown, malformed, ambiguous, failed, timed-out, oversized, or non-UTF-8 probe passes that copy over; when none passes, or none is installed, extraction is refused with a content-free, actionable reason that names each copy passed over, what its probe found, and where it was found, and no stale deployment result is reused. A successful probe is followed by extraction at that same absolute path, with `-q -enc UTF-8 -eol unix -raw`, given the bytes as a throwaway file in a folder of its own under the system temporary directory, removed afterwards, and its stdin closed before it reads anything. It runs as the user without a sandbox; version gating reduces exposure but does not authenticate the binary, protect against same-user replacement between probe and extraction, or guarantee native parser safety. A PDF over the changed-file limit (16 MiB) is a `file-size-limit` exclusion the tool never reads. The document is refused, an `unextractable` exclusion and a coverage gap, on a probe refusal; when extraction exits with a status other than 0; when it has not finished 60 seconds after it starts; when it writes text that is not UTF-8; and when it writes more than 1 MiB, past which its output is not read and it is stopped. The review is `INCOMPLETE`, never silently partial; standalone `review-document` refuses before creating its fixture. These checks bound accepted input, output, and waiting time, not the parser's memory safety or access as the user. Its text is held at a path in the snapshot's `source-snapshot-text` folder (the snapshot section), and a repository path in that folder, in any case, fails the snapshot as the manifest's own path does. | `test_adversarial_inputs.py::test_a_hostile_word_document_is_refused_before_parsing_and_leaves_the_review_incomplete`, `test_adversarial_inputs.py::test_a_hostile_pdf_is_refused_by_size_the_tools_answer_or_time_and_leaves_the_review_incomplete`, `test_adversarial_inputs.py::test_an_unsupported_pdf_parser_leaves_explicit_gaps_and_an_incomplete_review`, `test_review_documents.py::test_an_old_or_unidentifiable_parser_never_receives_document_bytes`, `test_review_documents.py::test_every_extraction_rechecks_the_current_path_and_reuses_only_the_checked_executable`, `test_review_documents.py::test_a_supported_parser_behind_an_unsupported_one_extracts_and_the_unsupported_one_never_gets_bytes`, `test_review_documents.py::test_only_unsupported_parsers_are_refused_naming_each_where_it_was_found`, `test_review_documents.py::test_a_probe_timeout_refuses_before_writing_the_pdf`, `test_review_documents.py::test_each_line_is_labelled_with_its_page_and_its_line_among_those_with_text`, `test_review_documents.py::test_a_pdf_the_tool_takes_too_long_over_is_refused_at_the_time_limit`, `test_review_documents.py::test_text_over_the_limit_is_refused_without_reading_the_rest`, `test_review_documents.py::test_without_pdftotext_a_pdf_is_refused_with_a_reason_that_names_it`, `test_review_documents.py::test_a_repository_path_in_the_held_text_folder_fails_the_snapshot`, `test_review_documents.py::test_a_document_type_or_entity_is_refused_before_parsing`, `test_review_documents.py::test_a_member_path_that_leaves_the_package_is_refused`, `test_review_documents.py::test_a_part_over_the_limit_is_refused_as_it_is_read`, `test_review_documents.py::test_any_other_failure_of_the_extractor_is_refused_by_its_type_alone`, `test_review_documents.py::test_a_document_the_extractor_fails_on_is_unextractable_and_the_others_are_still_held`, `test_review_documents.py::test_a_lazy_snapshot_holds_the_changed_document_and_source_file_returns_its_text`, `test_review_documents.py::test_the_base_is_read_at_the_merge_base_and_checked_against_the_diffs_blob` |
| The XML of an MSBuild project, `.props`, or `.targets` file in the head, which the analyzer inventory parses for the analyzers and analysis settings it declares. | The inventory parses it in the review's own process only through ejryan89-skill-core's `xml_reader.py`, the one reader of untrusted XML, which refuses a document carrying `<!DOCTYPE` or `<!ENTITY` anywhere, in a comment or a CDATA section included, before any of it is parsed, so no entity expands and nothing outside the snapshot is fetched or read. A refused or malformed file names no analyzer and is listed as present but unread, never expanded; what the inventory lists is data in the run's `analyzers.json`, at most 400 settings, never prompt text. | `test_adversarial_inputs.py::test_msbuild_xml_declaring_an_entity_is_listed_unread_and_never_expanded_or_fetched`, `test_review_analyzers.py::test_unparseable_files_are_listed_as_unread_and_never_expanded`, `test_review_analyzers.py::test_settings_are_bounded` |
| The TOML of a `ruff.toml`, `.ruff.toml`, or `pyproject.toml` in the head, which the analyzer inventory parses for the Python analyzers it configures and their rule selections. | The inventory reads it whole, within ejryan89-skill-core's `read_bounded` limit, decodes it strictly as UTF-8 or UTF-16 by its byte-order mark, and parses it in memory in the review's own process with the standard library, running nothing it names. It is parsed by `tomllib`. A file that is not valid TOML or UTF-8, that nests deeper than the interpreter's recursion limit, or that holds an integer past its int-to-string digit limit is listed as present but unread, and the review carries on: a `ruff.toml` or `.ruff.toml` still names ruff, by its name, and a `pyproject.toml` names no analyzer. What the inventory lists is data in the run's `analyzers.json`, at most 400 settings each on one line, never prompt text. | `test_adversarial_inputs.py::test_toml_nested_past_the_recursion_limit_or_holding_an_overlong_integer_is_listed_unread`, `test_review_analyzers.py::test_content_any_parser_raises_on_is_listed_unread_and_the_other_files_still_read`, `test_review_analyzers.py::test_unparseable_files_are_listed_as_unread_and_never_expanded` |
| The JSON of a `package.json` in the head, which the analyzer inventory parses for ESLint and its plugins. | The inventory reads it whole, within ejryan89-skill-core's `read_bounded` limit, decodes it strictly as UTF-8 or UTF-16 by its byte-order mark, and parses it in memory in the review's own process with the standard library, running nothing it names. It is parsed by `json`. A file that is not valid JSON or UTF-8, that nests deeper than the interpreter's recursion limit, that holds an integer past its int-to-string digit limit, or that spells a lone surrogate, which no UTF-8 file can hold, is listed as present but unread, names no analyzer, and the review carries on. What the inventory lists is data in the run's `analyzers.json`, at most 400 settings each on one line, never prompt text. | `test_adversarial_inputs.py::test_package_json_nested_past_the_recursion_limit_an_overlong_integer_or_a_lone_surrogate_is_listed_unread`, `test_review_analyzers.py::test_content_any_parser_raises_on_is_listed_unread_and_the_other_files_still_read` |
| The INI of a `setup.cfg`, `tox.ini`, or `.flake8` in the head, which the analyzer inventory parses for the Python analyzers it configures and flake8's rule selections. | The inventory reads it whole, within ejryan89-skill-core's `read_bounded` limit, decodes it strictly as UTF-8 or UTF-16 by its byte-order mark, and parses it in memory in the review's own process with the standard library, running nothing it names. It is parsed by `configparser` with interpolation off, so a value that names another is kept as its text and never expanded. A file `configparser` refuses, such as one with a setting before any section or a section header left open, or one that is not UTF-8, is listed as present but unread, and the review carries on: a `.flake8` still names flake8, by its name, and a `setup.cfg` or `tox.ini` names no analyzer. What the inventory lists is data in the run's `analyzers.json`, at most 400 settings each on one line, never prompt text. | `test_adversarial_inputs.py::test_ini_files_configparser_refuses_or_that_are_not_utf8_are_listed_unread`, `test_review_analyzers.py::test_flake8_setup_cfg_and_tox_ini` |
| An `.editorconfig` or `*.globalconfig` file in the head, which the analyzer inventory reads for its diagnostic severities. | The inventory reads it whole, within ejryan89-skill-core's `read_bounded` limit, decodes it strictly as UTF-8 or UTF-16 by its byte-order mark, and parses it in memory in the review's own process with the standard library, running nothing it names. The inventory reads it one `key = value` per line with no parser library and no continuation lines, and lists only diagnostic severities and the keys that make a file global. A line that is neither a section header, a setting, a comment, nor blank, or a file that is not UTF-8, leaves the whole file listed as present but unread, naming no analyzer, and the review carries on. What the inventory lists is data in the run's `analyzers.json`, at most 400 settings each on one line, never prompt text. | `test_adversarial_inputs.py::test_editorconfig_and_globalconfig_lines_that_are_not_settings_or_not_utf8_are_listed_unread`, `test_review_analyzers.py::test_editorconfig_lines_that_are_neither_section_nor_setting_leave_it_unread` |
| A `.shellcheckrc` or `shellcheckrc` file in the head, which the analyzer inventory reads for its ShellCheck directives. | The inventory reads it whole, within ejryan89-skill-core's `read_bounded` limit, decodes it strictly as UTF-8 or UTF-16 by its byte-order mark, and parses it in memory in the review's own process with the standard library, running nothing it names. The inventory lists each line that is neither blank nor a comment as one setting, stopping at the 400-setting limit for the whole inventory and marking it truncated, and a file that is not UTF-8 is listed as present but unread; either way it names ShellCheck, by its name, and the review carries on. What the inventory lists is data in the run's `analyzers.json`, at most 400 settings each on one line, never prompt text. | `test_adversarial_inputs.py::test_shellcheckrc_directives_stay_bounded_and_one_that_is_not_utf8_is_listed_unread`, `test_review_analyzers.py::test_settings_truncate_only_past_four_hundred` |
| `.gitattributes`: `export-ignore`, `export-subst`, `eol`, filters, and `binary`. | With `checkout_path`, the snapshot comes from git objects, which no attribute or configuration touches, so a file hidden from the diff or an archive is still in it with its exact bytes. Without one, GitHub's tarball is checked against the commit's tree, and any difference fails the pull request, naming the paths. | `test_adversarial_inputs.py::test_attributes_that_hide_or_rewrite_a_file_leave_the_snapshot_exact_or_fail_the_tarball`, `test_review_runtime.py::test_source_snapshot_holds_the_exact_blobs_whatever_attributes_and_configuration_say`, `test_review_followups.py::test_a_file_the_attributes_dropped_or_rewrote_fails_closed` |
| A change to a file the repository's `snapshot_exclude` matches. | The file is a `configured` exclusion on every route, never written, fetched, or searched, and a changed one is an unavailable source, so its review is `INCOMPLETE` while its reviewers still get its diff: an exclusion the maintainer sets for files reviewers rarely open cannot hide a change. `validate-reviewer` refuses a pattern that matches a file the reviewer declares. No pattern reaches outside the tree: the configuration refuses one with a backslash, a control character, or an empty, `.`, or `..` segment (a leading slash makes an empty one), and the rest are matched only against the head's repository-relative paths, never resolved on disk, so even the checkout's own absolute path matches nothing. | `test_adversarial_inputs.py::test_a_change_the_configured_exclusions_match_is_a_coverage_gap_and_no_pattern_leaves_the_tree`, `test_review_pipeline_prepare.py::test_a_configured_exclusion_leaves_files_out_and_a_changed_one_is_an_unavailable_source`, `test_review_pipeline_prepare.py::test_a_configured_exclusion_applies_to_the_tarball_and_a_changed_file_stays_a_coverage_gap`, `test_review_source.py::test_a_fetch_or_a_search_of_a_configured_path_is_refused_with_its_reason`, `test_review_followups.py::test_tarball_snapshot_leaves_out_configured_paths_and_a_changed_one_is_unavailable`, `test_review_pipeline.py::test_validate_reviewer_refuses_a_configured_exclusion_over_a_file_the_reviewer_declares` |
| Symbolic links, hard links, and other entries that are not regular files. | A symbolic link is a `symbolic-link` exclusion and any other such entry `non-regular`; none is written or followed. A tarball entry whose name leaves the snapshot fails the pull request with nothing written outside it. A link at the snapshot's reserved manifest path fails it too, and a link among a repository reviewer's declared files is refused before anything is materialized. | `test_adversarial_inputs.py::test_links_are_excluded_and_a_traversing_entry_fails_with_nothing_written_outside`, `test_review_runtime.py::test_source_snapshot_excludes_links_and_writes_nothing_for_a_submodule`, `test_review_runtime.py::test_source_snapshot_still_refuses_a_link_at_the_reserved_manifest_path`, `test_review_followups.py::test_tarball_snapshot_excludes_a_symbolic_link_and_a_fifo`, `test_review_runtime.py::test_git_symlink_entries_are_rejected_before_materialization` |
| The head moving during a review, and the snapshot changing before its reviewer starts. | `prepare` reads the pull request again after its diff and fails if the head or base moved, so a diff is never recorded under another head. The snapshot is the exact commit that diff belongs to, read by its SHA: a push after that check leaves it unchanged, and a force push that makes the commit unavailable fails the pull request. The Copilot CLI host starts its reviewer in a process of its own, so it checks the snapshot's structure again, which refuses a file added, removed, or replaced by a link since `prepare`, and compares a stamp `prepare` took: one SHA-256 over the manifest's bytes and the path, size, and modification time of each file `prepare` wrote. While the stamp matches, it re-hashes the files dated no earlier than the manifest and, on a lazy snapshot, each fetched file against its blob id; once it differs, it re-hashes every file, so a file changed since `prepare` fails the host before Copilot starts. | `test_adversarial_inputs.py::test_a_head_pushed_after_the_diff_is_read_is_snapshotted_exactly_or_fails`, `test_review_pipeline_prepare.py::test_a_push_while_preparing_fails_before_the_diff_is_written`, `test_review_pipeline_prepare.py::test_a_base_that_moves_while_preparing_fails_too`, `test_review_runtime_validation.py::test_a_file_changed_after_the_stamp_is_refused_by_the_full_pass`, `test_review_runtime_validation.py::test_a_file_added_after_the_stamp_is_refused_before_anything_is_read`, `test_review_runtime_validation.py::test_a_file_written_in_the_manifests_clock_tick_is_read_though_the_stamp_matches`, `test_review_runtime_validation.py::test_a_lazy_snapshots_fetched_files_are_read_whenever_they_are_held`, `test_review_hosts_copilot.py::test_a_file_changed_or_added_after_prepare_is_refused_before_copilot_starts`, `test_review_pipeline.py::test_a_snapshot_file_changed_after_prepare_fails_the_host_before_copilot_starts` |
| The size of the tree. | The snapshot holds at most 50,000 files and writes at most 256 MiB, and `prepare` fails and leaves no run on a tree over either limit by the route it takes. Written whole (GitHub's tarball, and the checkout for the Copilot CLI host or a routed condition that declares no `reads`), every file it keeps counts against both. Written lazily (the checkout otherwise), every file it can hold counts against the file-count limit when `prepare` lists it, and only the files it writes (the changed files, the analyzer settings, and the paths declared `reads` match) count against the size limit, so files a pull request leaves unchanged never fail it by their size; a fetch that would pass the size limit is refused. `validate-reviewer` measures the route `prepare` would take, applies the same limits, names the route, and names the largest directories of a snapshot it refuses. | Whole: `test_review_pipeline_prepare.py::test_the_size_limit_holds_for_the_files_each_route_writes`, `test_review_runtime.py::test_source_snapshot_materialization_enforces_file_count_limit`, `test_review_pipeline.py::test_a_whole_snapshot_over_the_size_limit_fails_naming_the_largest_directories`. Lazy: `test_adversarial_inputs.py::test_a_tree_over_the_size_limit_fails_prepare_and_leaves_no_run`, `test_adversarial_inputs.py::test_large_files_the_pull_request_leaves_unchanged_are_listed_never_written_or_counted_by_size`, `test_review_pipeline.py::test_a_lazy_snapshot_counts_only_the_files_it_writes_against_the_size_limit`, `test_review_pipeline.py::test_a_lazy_snapshot_over_the_size_limit_by_its_changed_files_fails_naming_the_largest_directories`, `test_review_pipeline.py::test_a_snapshot_over_the_file_count_limit_fails`, `test_review_pipeline.py::test_the_lazy_measurement_matches_the_lazy_snapshot_prepare_writes`, `test_review_source.py::test_a_lazy_snapshot_lists_no_more_paths_than_the_file_count_limit`, `test_review_source.py::test_the_snapshot_size_limit_holds_for_fetches` |
| The size of the change: a pull request past a limit of GitHub's diff endpoint, such as its 300 changed files, which refuses to serve the diff with HTTP 406. | `prepare` reads the diff from another source rather than leave the pull request unreviewed. On the checkout route it is git's diff in the checkout, from the merge base GitHub names to the head, with no option a user's configuration could change, read through a pipe. On the tarball route it is GitHub's files API, every page, read after the pull request's changed-file count and head: a listing shorter than the count (the API lists at most 3,000 files) or a head that moved fails. Neither source is bounded by GitHub, so a diff from either over 256 MiB, the most a snapshot writes, fails. A changed file the files API gives no patch for, as GitHub does for a diff too large to show, is an unavailable source, so the review is `INCOMPLETE`, and a `NOTE` names it; a file with no changed line is git's header alone, with git's binary line for an added, deleted, or modified file that is not empty, and the snapshot then finds a binary as it finds any other. A change neither source serves ends as one `FAILED` line naming the limit and the pull request, never gh's text. A `NOTE` names the source the diff came from, the record keeps it as `review.snapshot.diff`, and the report says so in one line. `validate-reviewer --pull` reads the diff as the checkout route does. | `test_adversarial_inputs.py::test_a_change_over_the_diff_endpoint_s_limit_is_read_whole_from_another_source_or_fails`, `test_review_large_change.py::test_a_change_over_the_limit_is_prepared_from_the_files_api_as_the_diff_endpoint_would_prepare_it`, `test_review_large_change.py::test_a_change_over_the_limit_is_prepared_from_git_s_diff_in_the_checkout`, `test_review_large_change.py::test_a_changed_file_github_gives_no_patch_for_is_a_coverage_gap_and_noted`, `test_review_large_change.py::test_a_change_the_files_api_does_not_list_whole_fails_naming_the_limit_and_the_pull_request`, `test_review_github.py::test_the_diff_endpoint_s_406_over_a_limit_names_the_limit_and_any_other_failure_stays_as_it_was` |
| Paths a reviewer asks for on a lazy snapshot, and the patterns it searches the head with. | On the checkout route, unless the reviewers cannot run a command (the Copilot CLI host) or a condition script of a routed specialist declares no `reads`, `prepare` writes only the changed files, the analyzer settings, and the paths the `reads` of the conditions it runs match, which those scripts read in place and no other path, and the manifest lists every other path the snapshot would hold under the rows above, by name, kind, and size, with its blob id. `source-file` in `review_source.py` looks a path up in that manifest and never makes a file name of what the reviewer passes: a path the head does not hold as a listed file, in any spelling (`..`, absolute, another case, a folder, a file only the base holds), fails with nothing written, and an excluded path is named with its reason. The blob is read from the checkout's object store by its id and must hash to it; a NUL byte in its first 8,000 bytes makes it `binary`, never written; and it is written into a folder checked for reparse points and escape, where verification holds it to its id. `source-search` runs `git grep` at the head commit and prints only matches in paths the snapshot can hold, so an agent-instruction file stays out, and only in files the same NUL-byte test finds text, so no `.gitattributes`, `info/attributes`, or `core.attributesFile` entry in the checkout hides a file from it or opens a binary one to it. The guard allows the two commands only for the reviewer's own run and role, with a path or pattern that holds no quote, backtick, dollar sign, backslash, or line break, so it never leaves its quotes. | `test_adversarial_inputs.py::test_a_lazy_snapshot_reviewer_obtains_only_files_the_head_commit_holds`, `test_review_source.py::test_a_path_outside_the_commit_is_refused_with_nothing_written`, `test_review_source.py::test_an_excluded_or_binary_path_is_named_with_its_reason_and_never_written`, `test_review_source.py::test_a_blob_the_checkout_answers_wrongly_is_refused`, `test_review_source.py::test_a_fetched_file_that_changed_or_a_file_no_manifest_lists_fails`, `test_review_source.py::test_a_reparse_point_in_the_snapshot_refuses_every_fetch`, `test_review_source.py::test_the_search_covers_the_commit_and_leaves_out_what_the_snapshot_excludes`, `test_review_source.py::test_the_search_judges_each_file_by_its_bytes_whatever_the_checkout_sets`, `test_review_source.py::test_a_binary_file_the_manifest_lists_or_excludes_is_never_searched`, `test_review_documents.py::test_source_search_leaves_out_a_held_document_in_a_whole_and_a_lazy_snapshot`, `test_review_documents.py::test_source_search_leaves_out_a_held_pdf_as_it_does_a_word_document`, `test_review_guard.py::test_bash_runs_the_source_commands_of_its_own_role_and_run_with_a_path_or_pattern_kept_in_quotes`, `test_review_pipeline_prepare.py::test_a_condition_that_declares_no_reads_reads_the_whole_snapshot`, `test_review_pipeline_prepare.py::test_a_condition_that_declares_its_reads_keeps_the_snapshot_lazy_and_decides_as_on_the_whole_one`, `test_review_pipeline_prepare.py::test_a_declared_condition_is_not_given_a_path_it_does_not_declare`, `test_review_pipeline_prepare.py::test_a_declared_read_the_configured_exclusions_match_is_never_written`, `test_review_pipeline.py::test_a_condition_that_declares_its_reads_decides_on_the_snapshot_a_review_gives_it`, `test_review_pipeline_prepare.py::test_an_entrypoint_needs_no_agent_delegation` |
| Agent-configuration paths: instruction files, agent and skill folders, and settings, in any case and at any depth. | Each is an `agent-instruction` exclusion, never written, so no reviewer loads the pull request's own instructions; reviewers judge such a change from the diff. A repository reviewer's files come only from the trusted commit, never the head, and a trusted ref that resolves to the head is refused. | `test_adversarial_inputs.py::test_agent_configuration_in_the_head_reaches_no_reviewer_in_any_spelling`, `test_review_runtime.py::test_source_snapshot_uses_exact_head_and_excludes_agent_instructions`, `test_review_runtime.py::test_reviewer_is_loaded_from_trusted_commit_not_head`, `test_review_runtime.py::test_pull_ref_head_and_remote_mismatch_are_rejected` |
| A review skill or reviewer file the pull request adds or rewrites. | A repository reviewer is read from a trusted commit only: the configured `trusted_ref`; else the pull request's base; else, when a configured review skill is not at the base, the default branch's tip as origin reports it, which holds only what the repository merged. A tip that also lacks the skill, or that is the pull request's head, runs the suite's generic reviewer instead, so a skill file only the head holds never reviews it. The record's `adapter.source` and a `NOTE` name the fallback, and `validate-reviewer --pull` resolves the reviewer the same way and prints that source and note. With a `trusted_ref` or a `manifest_path`, a missing file fails the pull request as before. | `test_adversarial_inputs.py::test_a_review_skill_only_the_head_holds_never_reviews_the_pull_request`, `test_review_pipeline_prepare.py::test_a_skill_only_the_head_holds_is_never_the_reviewer`, `test_review_pipeline_prepare.py::test_a_skill_the_base_predates_is_read_from_the_default_branch_tip`, `test_review_pipeline_prepare.py::test_a_skill_neither_the_base_nor_the_tip_has_runs_the_generic_reviewer` |
| The body of a review comment: anyone can start a thread on a pull request, its author included. | A comment is data, never an instruction. `prepare` keeps each unresolved thread a person started whole in `request.json` and in the run's `github-comments.json`, and every prompt's input contract names the file that holds them untrusted pull-request data, whichever variable carries it: a specialist's or the generic reviewer's prompt names it `GITHUB_COMMENTS_FILE`, while a repository entrypoint reviewer's prompt, the Copilot CLI host's included, has no such variable and names `REQUEST_FILE`, the request, whose comment bodies the same contract calls untrusted data. The `code-review-reviewer` agent's own instructions name a pull request's comments untrusted too. The prompt of the reviewer a comment is routed to (an entrypoint reviewer's lists every comment) lists it as JSON under a heading that names it untrusted, between `<open_review_comments>` tags, after the inputs and before the input contract, so every prompt ends with its contracts and its `WROTE` reply rather than with the author's text. A body is one string value that cannot close the list, close its tag, or start a prompt line: JSON escapes every character below U+0020, and the prompt escapes U+0085, U+2028, and U+2029, which `json.dumps` leaves raw, and writes every `</` as `<\/`, which JSON reads the same. The prior findings listed before them, between `<prior_findings>` tags, their flags' rationales included, and a symbolic link's target are written the same way, through the one `prompt_json` in `review_specialists.py`. There each body is cut to 4,000 characters, followed by its full length and the file that holds it whole; the number of comments is not bounded, because each needs a disposition. The report's **Review Comments** table shows a 120-character excerpt with punctuation encoded as character entities, so markup, bare URLs, and mentions render literally regardless of existing backslashes. Its disposition rationale keeps its paragraphs and line breaks inside the cell without becoming markup. The generated comment link stays active only for an absolute HTTPS destination, with delimiters percent-encoded. No command line carries a body, nor a prior finding or a path from the diff: the Copilot CLI host gives Copilot its prompt on standard input, and on Windows it never starts a shell script, such as the `copilot.cmd` an npm install puts on `PATH`, because Windows starts one through `cmd.exe`, which parses the whole command line by its own rules, where argument quoting does not hold. Elsewhere the kernel starts a script by its `#!` line and hands it the arguments as they are, so the first program on `PATH` is the one it starts, script or not. The cut, the literal projection, and standard input are fixes: a body used to reach the prompt whole, up to GitHub's 65,536 characters, the excerpt's HTML and Markdown rendered, and the Copilot CLI host's prompt was the value of its `--prompt` argument. | `test_adversarial_inputs.py::test_a_review_comment_reaches_its_reviewer_as_a_bounded_json_string_and_the_report_as_escaped_text`, `test_review_specialists.py::test_a_prior_findings_body_and_its_flags_rationale_cannot_start_a_prompt_line`, `test_review_specialists.py::test_a_link_target_cannot_start_a_prompt_line`, `test_adversarial_inputs.py::test_no_argument_of_the_copilot_command_carries_a_comment_body_a_prior_finding_or_a_diff_path`, `test_adversarial_inputs.py::test_the_lookup_refuses_a_shell_script_first_on_path_and_takes_the_winget_program`, `test_adversarial_inputs.py::test_the_lookup_walks_path_past_every_shell_script_to_the_program`, `test_adversarial_inputs.py::test_a_script_first_on_path_is_the_program_since_no_shell_parses_its_command_line`, `test_adversarial_inputs.py::test_the_lookup_takes_the_first_program_on_path_and_passes_over_a_file_it_cannot_run`, `test_review_rendering.py::test_comment_rationale_preserves_lines_inside_its_table_and_the_generated_comment_link`, `test_review_rendering.py::test_only_https_navigation_is_active_and_destinations_cannot_add_markup` |
| The pull request's title. | The title reaches no prompt the suite writes. It is a JSON string, kept whole, in `request.json`, which an entrypoint reviewer reads as data, and in the record. The report's **Title** row shows it on one line with punctuation encoded as character entities, so links, images, emphasis, math, tags, bare URLs, and mentions render literally; that is a fix, because the row used to escape only its pipes. Reaching no prompt, its size is not bounded. | `test_adversarial_inputs.py::test_a_title_reaches_no_prompt_and_the_report_as_escaped_text`, `test_adversarial_inputs.py::test_an_entrypoint_prompt_gives_the_request_s_facts_and_none_of_the_author_s_text` |
| Word headings and supplied report prose. | A Word heading reaches a finding's document location as extracted text, not as report markup. At the projection boundary, locations, finding titles and bodies (including suggestions), summaries, categories, sources, ledger judgments, flags, and reviewer display metadata encode punctuation as character entities. Prose keeps paragraphs and explicit line breaks, including inside table cells; evidence stays literal code with its line breaks. Supplied links, images, HTML, math, and formatting do not become active report content. Generated headings, tables, code labels, and finding navigation remain structural, and absolute HTTPS pull-request navigation keeps its destination delimiters encoded. Records and extraction keep their original text; existing archived reports are not rewritten. This output policy does not prevent a reviewer from being influenced by the content it reads. | `test_adversarial_inputs.py::test_word_locations_and_reviewer_prose_reach_the_report_as_literal_text`, `test_review_rendering.py::test_word_heading_location_is_literal_on_both_evidence_routes_in_a_validated_pair`, `test_review_rendering.py::test_summary_body_title_category_and_source_are_literal_without_changing_the_record`, `test_review_rendering.py::test_escape_parity_autolinks_tables_and_newlines_cannot_introduce_markup`, `test_review_rendering.py::test_ledger_status_flags_repeats_and_closed_rationales_render_literally`, `test_review_rendering.py::test_reviewer_metadata_and_scope_prose_are_literal_in_table_cells`, `test_review_rendering.py::test_multiline_evidence_stays_code_and_preserves_its_line_breaks` |
| The pull request's description (its body): all of it, up to GitHub's 65,536 characters, where the author states what the change intends and what it asks of a user. | The description is data, never an instruction, and reaches a reviewer only as a file it chooses to read. `prepare` writes it to the run's `pull-request-body.md`, never into a prompt, a command, or another work file, cut to its first 24,000 characters so a long one cannot fill a reviewer's context, with each lone surrogate, which no UTF-8 file can hold, replaced by U+FFFD. Every prompt names the file `PULL_REQUEST_BODY_FILE` among the untrusted pull-request data of its input contract, tells the reviewer to read it only when its instructions judge the change against what the description states, and says how much of it the file holds from the request's own count, so text in the description cannot claim another length or speak as the contract: it starts no prompt line because none of it is in the prompt. An entrypoint reviewer's prompt states the same rule, the Copilot CLI host's prompt names the description the request names untrusted, and the `code-review-reviewer` agent's own instructions name the description untrusted. A reviewer that reads it is still bound by the reviewer guard: it reads, writes, and runs only what its role allows. The request records the description's full length and how much was given, and the record keeps both, so the report's **Description** row shows whether reviewers saw it whole or cut. An inline review seals the file with the run's other files. No condition script is given it, so no routing decision rests on the author's text. A fixture's description comes from its trusted `pull.json`. | `test_adversarial_inputs.py::test_a_description_reaches_reviewers_only_as_a_bounded_file_the_prompt_names_untrusted`, `test_adversarial_inputs.py::test_an_entrypoint_prompt_gives_the_request_s_facts_and_none_of_the_author_s_text`, `test_review_specialists.py::test_a_prompt_states_how_much_of_the_description_its_file_holds_from_the_request_s_count`, `test_review_records.py::test_the_record_keeps_how_much_of_the_description_reviewers_were_given_and_the_report_says_so` |
| The name of the pull request's head branch. | The name is never an argument of a command: `prepare` fetches the head as `refs/pull/<number>/head` and reads every commit by its SHA, so shell punctuation, quotes, and backticks in it run nothing. It reaches no prompt the suite writes, and is a JSON string, kept whole, in `request.json` and the record. The report's **Branch** row shows it in a code span whose fence is longer than any run of backticks in it, with its pipes escaped; a label containing both backslashes and pipes instead uses literal HTML code so escape parity cannot split the cell. Nothing in it renders as markup. Reaching no prompt, its size is not bounded. | `test_adversarial_inputs.py::test_a_branch_name_is_never_a_command_argument_or_prompt_text_and_the_report_shows_it_as_code`, `test_review_rendering.py::test_code_labels_with_pipes_and_backslashes_cannot_split_a_table_cell` |
| The reviewer's own behaviour under hostile content. | In Claude Code the reviewer guard binds each `code-review-reviewer` to one role of one run: it reads only that run and the suite's references, writes only that role's result under any spelling of another path, and runs only that role's self-check and, for a lazy snapshot, its two source commands, with no quoted script or run ending in a backslash, which would escape its closing quote. It holds every path a call names to the temporary directory and `ejryan89-code-review-core`'s own folder by its text before resolving it, so a network or device path, or any other, is denied without a file system call, and it binds a first read only to the outermost run folder under the temporary directory, never to a run folder inside another run's tree, which a pull request's snapshot can hold. Its hook blocks the call when the guard cannot start, times out, or fails. The `review-insights` synthesis, which reads finding bodies, flag text, and guidance passages drawn from reviewed repositories and from people, is a role of the same agent: `report` stages it a run of that shape under the temporary directory, holding its prompt and a copy of its sealed input, whose `run.json` names its one role and declares that role's self-check, so the guard holds it to that run, its result, and that one command, exactly as declared. The Workflow script never reruns a role without the guard. A role's result counts only when the guard ran for it: on Claude Code, `check` and `finalize` hold every role dispatched as a native subagent, one by one or by the Workflow script, to the guard's claim, which starts the role's read log and which `check` keeps as the role's entry in `run.json`'s `reads` once it counts the log. A role with neither, whose reviewer ran with hooks off (`disableAllHooks`, or an organization's `allowManagedHooksOnly`, which leaves out a plugin's hooks), with the agent's or the plugin's hook not loaded, or as another agent type such as the general-purpose fallback, is a role error that starts `the reviewer ran without its guard`, so `check` retries it once and then fails the pull request rather than record a result no hook held; recording the synthesis with `synthesize --host claude-code` refuses one the same way. An inline run, the Copilot CLI host, and a Codex subagent run no guard, so none is held to a claim. The Copilot CLI host grants a write to its staging file only. The core validates every result against the request and computes the verdict itself, so a persuaded reviewer can omit a finding but cannot clear a coverage gap. | `test_adversarial_inputs.py::test_a_reviewer_cannot_write_or_run_outside_its_result_by_spelling_the_path_another_way`, `test_review_guard.py::test_a_reviewer_writes_only_its_own_roles_result_in_its_own_run`, `test_review_guard.py::test_reads_are_allowed_only_inside_its_own_run_or_the_references`, `test_review_guard.py::test_a_reviewer_reads_nothing_before_its_prompt`, `test_review_guard.py::test_without_an_agent_id_every_guarded_call_is_denied`, `test_review_guard.py::test_network_and_device_paths_are_denied_by_their_text`, `test_review_guard.py::test_a_path_outside_the_temporary_directory_and_the_skill_is_denied_unresolved`, `test_review_guard.py::test_a_run_folder_inside_another_runs_tree_is_never_claimed`, `test_review_guard.py::test_bash_runs_only_the_self_check_of_its_own_role_and_run_with_every_value_kept_in_quotes`, `test_review_guard.py::test_a_role_that_declares_its_self_check_runs_that_command_alone`, the `GuardTests` cases in `skills/review-insights/scripts/test_review_synthesis.py`, the `GuardClaimTests` cases in `test_review_pipeline.py`, `test_review_pipeline.py::test_an_inline_role_on_claude_code_needs_no_guard_claim`, `test_review_pipeline.py::test_one_script_starts_every_role_of_several_runs`, `test_review_hosts_copilot.py::test_a_review_runs_version_then_review_then_promotes`, `test_review_records.py::test_adapter_result_rejects_unsafe_paths_and_mismatched_sha` |
| The reviewer's own behaviour under hostile content, when it runs inline. | On Copilot CLI, whose hooks let a call through when they time out, or where `prepare --inline` asks for it, the orchestrating session works each role itself, one at a time as `next-role` hands it out, from the same prompt, with the same exclusions, as a subagent would. No hook confines it, so the run is sealed instead: `prepare` records the SHA-256 of every run file it wrote except `run.json`, which the pipeline itself updates; the source snapshot, which its own manifest hashes; a fixture's throwaway repository, which a lazy snapshot's files are fetched from and checked against by blob id; `recorded.json`, the mark `finalize` leaves on a run it recorded; and the results, which `next-role` seals one by one (the request, the diff, the plan, every prompt and work file, and the materialized reviewer), and `next-role` seals each result it moves past. `next-role`, `check`, and `finalize` fail the pull request, recording nothing, when a sealed file changed or is gone, so a later role cannot rewrite an earlier role's result, clear a coverage gap or prior finding from the request, or rewrite the next role's instructions unnoticed. The core validates every result and computes the verdict as in every mode. A specialists manifest that lists `agent-delegation` never runs inline. | `test_adversarial_inputs.py::test_an_inline_reviewer_cannot_clear_a_coverage_gap_or_rewrite_its_inputs_unnoticed`, `test_review_pipeline.py::test_a_later_role_that_rewrites_an_earlier_result_fails_the_pull_request`, `test_review_pipeline.py::test_check_sets_aside_an_invalid_inline_result_and_next_role_hands_it_out_again`, `test_review_pipeline.py::test_manifests_with_and_without_agent_delegation_run_as_the_dispatch_allows`, `test_review_runtime.py::test_an_inline_review_offers_only_reading_the_diff_and_writing_the_result` |
| Concurrent reviews of the same pull request. | `finalize` records only on top of the archive version `prepare` saw, and allocates that version under the pull request's lock, so of two sessions finalizing at once exactly one records and the other fails with the archive untouched and its run still unfinalized. | `test_adversarial_inputs.py::test_two_sessions_finalizing_the_same_pull_request_record_exactly_one_version`, `test_review_pipeline.py::test_finalize_fails_closed_when_another_version_was_recorded_after_prepare`, `test_review_archive.py::test_versions_are_allocated_under_lock`, `test_review_state.py::test_recent_lock_of_live_owner_times_out_and_is_kept`, `test_review_state.py::test_release_succeeds_while_a_waiter_reads_the_owner_file` |
| A fixture directory given to `prepare --canary --fixture`, including a document copied into one by `review-document`. | Fixture infrastructure, `pull.json`, prior records, and reviewer routing are trusted inputs, as the configuration is. Repository test fixtures live under `tests/fixtures/`, which validation keeps from shipping; `review-document` builds a fixture in its output directory and copies the supplied document into its head tree, or, for a pull request, writes the document's blobs it reads from GitHub at the pull request's head and merge base, each checked against its blob id, into the two trees, and only the pull request's repository, number, and commits into `pull.json`, never its title, description, branch names, or comments. The document's bytes remain potentially hostile author input. A fixture takes no shortcut past the snapshot: `prepare` commits its two trees byte for byte to a throwaway repository whose origin names the fixture's repository, and snapshots the head from it as it snapshots a checkout's, so every exclusion above applies; that repository moves into the run, where a lazy snapshot's reviewers fetch from it, and goes with the run when `finalize` removes it; a link or another entry that is not a regular file or folder fails it. `pull.json` and a re-review's prior record are validated before anything is written. Nothing is read from GitHub, and the pair is written only under a new canary root. The suite's generic reviewer reviews it, unless `pull.json` names a `manifest_path`, a reviewer manifest in the `base/` tree that is read from the base commit, as a configured repository's is, and never from the head. | `test_adversarial_inputs.py::test_a_fixture_is_snapshotted_as_a_pull_requests_head_and_reads_nothing_from_github`, `test_review_pipeline.py::test_a_fixture_with_a_planted_defect_runs_through_every_step_with_no_gh_call`, `test_review_pipeline.py::test_a_prior_record_that_is_not_this_pull_requests_first_review_fails_with_nothing_prepared`, `test_review_canary.py::test_a_link_or_special_file_in_a_tree_is_refused`, `test_review_canary.py::test_both_trees_are_committed_byte_for_byte_whatever_their_attributes_say`, `test_review_canary.py::test_a_reviewer_manifest_in_the_base_tree_is_named_by_a_safe_relative_path` |
| The paths of the documents a pull request changes, which `review-document` writes into a fixture before any snapshot is made. | A path is refused before any of its blobs is read: naming it alone when the pipeline's own rule, `reviewable_path`, imported from `review_specialists.py` rather than copied, gives it no reviewer: a control character, a line break such as U+0085, U+2028, or U+2029, which would also break the printed `DOCUMENT` line, a backslash, a drive letter, or an absolute, empty, `.`, or `..` segment, and naming it and the rule when one of `UNSAFE_PATH_RULES`, imported from `review_runtime.py` rather than copied, refuses it by its name: a reserved character, a drive's `:` among them, a segment ending in a dot or a space, or a reserved device name. Before any fixture is written, and still before any blob is read, the same rules are applied again in the room the folder of the document's own fixture leaves the path, and its target must lie inside that fixture's tree; a path in the fixture's manifest folder or in the snapshot's reserved `source-snapshot-text/`, in any case, is refused too, as is an output directory that leaves a fixture no room for the reviewer manifest. Each file's target is checked again before it is written. Without `--document`, a refused document is skipped with a `SKIPPED` line naming it and the others are reviewed; a pull request with no document left fails with nothing written. `review-document`'s own suite, `skills/review-document/scripts/test_review_document.py`, holds each refusal and the target check. | `test_adversarial_inputs.py::test_a_drive_or_device_name_in_any_segment_is_unsafe_by_its_name_whatever_the_room`, `test_adversarial_inputs.py::test_each_rule_names_why_windows_cannot_hold_a_path_and_names_just_inside_are_kept` |

The model does not cover these, by design:

- **Native PDF parser safety.** `pdftotext` parses author-controlled bytes with the invoking user's access. The provider-specific maintained-release support floors are backed by [vendor release evidence](design.md#decisions) and enforced before extraction, but are not vulnerability certifications. Version banners do not authenticate binaries, and a same-user process can replace a checked executable. Version gating and the size and timeout checks do not guarantee memory safety, isolate credentials, sandbox extraction, or prevent other effects of a vulnerable native parser. The execution environment remains part of the user's trust decision. Reviewing a generated PDF's source can avoid parsing that PDF, but does not make a PDF safe to parse.
- **A reviewer persuaded to miss a problem.** Hostile content can still talk a reviewer out of a finding; no check can tell an honest "no findings" from a persuaded one. The verdict, the ledger, and the coverage gaps stay the core's.
- **Reviewers outside the guard.** Codex reviewers and the Codex `review-insights` synthesis, the general-purpose subagent `review-insights` starts its synthesis as on Copilot CLI, and the general-purpose subagent `review-prs`, `review-document`, and `review-insights` fall back to on Codex or Copilot CLI when a session lacks the `code-review-reviewer` type, run without the reviewer guard, so their prompt is their only boundary. On Claude Code that fallback's result is refused, as a reviewer that ran without its guard. Codex runs `PreToolUse` hooks, but one that fails, times out, or returns output Codex does not support lets the call through, and one that is not managed runs only once the user trusts it, so a guard there would hold only while it runs; reviewers start under it only on a runtime that blocks the call when its hook fails, as Claude Code's reviewer agent does (the fail-closed decision in [Design](design.md#decisions)).
- **Inline reviewers.** An inline reviewer is the orchestrating session itself, so it keeps that session's tools, permissions, instruction files, and context: no hook confines what it reads, writes, or runs, and hostile content it reads reaches a session that can do whatever that session is allowed. Each role also sees the roles before it in its context. The seal catches a changed run file, but not one rewritten together with the digests in `run.json`, and the source snapshot is not hashed again between roles, because hashing a large one takes minutes. Run inline reviews from a directory outside the checkout, in a session allowed nothing beyond what the pipeline's commands need. The skills' own grants hold to that: `review-prs` and `review-document` pre-approve `review_pipeline.py` and `review_source.py` by name, and `review-document` its `review_document.py`, each pattern ending at the script's closing quote, because Claude Code normalizes no path in a shell grant and a pattern ending in the folder would also approve every other `ejryan89-code-review-core` script, such as `review_config.py write`, and any `..` path out of it. Validation fails a grant of a sibling skill's folder, and any pattern whose `*` continues a path, such as another skill's own `scripts/*`, which would approve these scripts through `scripts/../../` in that skill's turn ("Granting tools" in [Adding a skill](adding-a-skill.md#granting-tools)), and `advance` refuses a batch file dated after the pipeline's own today or one that would move a watermark past it, so a batch file cannot set a watermark no merged pull request passes.
- **The guard's failure on an older Claude Code.** The guard's hook declares `onFailure: "block"`, so a guard that cannot start, as when no Python is on the hook's `PATH`, that the machine slows past its timeout, such as a temporary directory or profile folder on a network share that stops answering, or that exits with a status other than 0 or 2 denies the call, as a guard that fails inside Python, a missing deployed script included, already did. A Claude Code older than its floor in [Dependency updates](dependency-updates.md#runtime-tool-floors) ignores the setting and lets that one call through; `python deploy.py check` reports such a Claude Code as `OUTDATED`. On Windows, the first releases that honor the setting still let a call through when Git for Windows is not installed, because the hook names no shell, until a later release made such a hook block; the deployer and every skill need Git, so that takes removing it after deploying.
- **The claim's proof.** A role's read log is a file in its run folder, so it shows that the guard claimed the role, not that it held every call: a reviewer that ran with no guard could create the log itself, and a claim from an earlier attempt at the role still counts for its rerun. The refusal catches hooks that never ran, not a reviewer that forges a claim.
- **The guard's agent ID.** The guard tells parallel reviewers apart by the `agent_id` Claude Code puts in a subagent's hook event, which is observed rather than documented. Without it, every guarded call is denied and reviews fail visibly.
- **The same user.** Run folders and the guard's claims live in the per-user temporary directory, and authentication tokens stay available to reviewer hosts, so a process running as the user is trusted. The Copilot CLI host's stamp, for one, does not see a change that keeps a file's size and modification time, which takes setting the time back or a writer still holding the file open when the host lists its folder.

## Formats

These tables are the single statement of every structured file the suite keeps between operations or takes from a reviewer, `review-insights`' report and the synthesis files beside it included. The report is stated here rather than in a document of its own because it is durable user data like the records and the flag store: this section's tables are what a release compares to decide whether a change needs an upgrade note, and this suite of checks already holds them. Each format names the function that validates it, and `tests/code-review/test_format_contract.py` fails when a table and that function disagree on the suite's fixtures. The adapter result, which a repository's entrypoint reviewer writes and the core assembles from specialist results, is stated instead by `skills/ejryan89-code-review-core/references/review-adapter.schema.json`, because an entrypoint's author reads that file; the same suite checks it against `validate_adapter_result` in `review_records.py`. A file one operation writes and reads before it ends, such as a run's `run.json`, plan, prompts, and work files, the batch file, and the tracker input, is not stated here: the script that writes it is the only reader. Every step after `prepare` still reads a run's `run.json` field by field through the typed-field reader below, so a damaged or hand-edited one fails that step's `FAILED` line naming the first wrong field from `run`, as in `run.roles[1].model must be a non-empty string or null, found integer 5`, before anything acts on it; a run prepared before an optional field was recorded loads without it. How to write and use each file is in [Code-review operations](code-review-operations.md).

How to read a table:

- **Type** is a JSON type, or several joined by `or`. `any` means the reader does not check the field.
- **Required** is `yes` (always present), `no` (may be absent), `with X` (may be absent only together with `X`), or `when ...` (required in the case it names, and may be absent otherwise).
- **One of** in a field's meaning lists every value it may have, besides `null` when its type allows null.
- A validated object rejects any field its table does not list. A field whose meaning says what it is keyed by is an object of named entries rather than of fixed fields.
- Paths are repository-relative POSIX paths: no leading `/`, no `\`, and no `..` part. Absolute paths take the platform's form: drive-letter paths on Windows, and elsewhere POSIX paths from one leading `/`, neither a filesystem root. Hashes are lowercase hexadecimal, 40 or 64 characters for a commit, 64 for SHA-256.
- The validators in `ejryan89-code-review-core` and `validate_tracker_record` in `update_pr_dashboard.py`, which together hold every format before the [Insights report](#insights-report), read a field's type through the one typed-field reader, `typed_fields.py` in ejryan89-skill-core, by these kinds: `string` (not blank, unless its meaning allows an empty one), `integer` (never `true` or a float such as `1.0`), `number` (finite), `boolean`, `hex_digest` (the hashes above), `one_of` (the values **One of** lists), `array`, `json_object` (with the fields its table lists), `optional` (a type that allows `null`), and `either` (several types joined by `or`). A value of another kind is refused as `<path> must be <expected>, found <what it is>`, the path starting with the format's name from its table's heading, as in `config.repositories.owner/repo.reviewer.id`. `review-insights` checks the types in the insights report and the synthesis result, with messages of its own.

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
| `dashboard_file` | string | yes | Absolute path of the Markdown file whose marked section `update-pr-dashboard` owns. |
| `github_login` | string or null | no | The user's GitHub login for the tracker. Null or absent uses the account `gh` is signed in as. |
| `runtime` | string | no | One of `auto`, `claude-code`, `codex`, or `copilot-cli`. The runtime that starts reviewers; defaults to `auto`. |
| `verdict_policy` | object | no | When findings request changes; see [verdict policy](#verdict-policy-configverdict_policy). |
| `operation_repository_sets` | object | no | Keyed by operation (`review-prs`, `update-pr-dashboard`, or `review-insights`). Each value names a key of `repository_sets`: the operation's own default set. |
| `reviewer_effort` | string or null | no | One of `low`, `medium`, `high`, `xhigh`, or `max`. Reasoning effort for reviewers the Workflow tool starts, those Codex starts, and the Copilot CLI host; null or absent keeps the session's effort. |
| `re_review_scope` | object | no | When an `auto` re-review reviews in full; see [re-review scope](#re-review-scope-configre_review_scope). |
| `model_names` | object | no | Keyed by a model identifier reviewers report, a trimmed single line of at most 200 characters. Each value is the non-empty single-line name of at most 100 characters that a report's Reviewers table shows instead. |
| `dashboard` | object | no | How the tracker section is marked and shown; see [dashboard](#dashboard-configdashboard). |

#### Repository (`config.repositories.<repository>`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `reviewer` | object | yes | Who reviews the repository; see [reviewer](#reviewer-configrepositoriesrepositoryreviewer). |
| `checkout_path` | string or null | when the reviewer's `scope` is `repository` | Absolute path of a local clone, from which the reviewer's files are read at the trusted commit. |
| `snapshot_exclude` | array | no | Glob patterns of head files to leave out of the source snapshot, at most 100 distinct ones ignoring case, each 1 to 200 characters with no backslash, control character, or empty, `.`, or `..` segment. Each matches a whole path relative to the repository root, ignoring case: `*`, `?`, and `[...]` within one segment, and a `**` segment any number of segments, none included. A matching file is a `configured` exclusion on every route. Defaults to `[]`. |
| `unreviewed_suffixes` | array | no | File-name suffixes of binary file types the review does not read, beside the [shipped ones](#source-snapshot-requestsource_snapshot): at most 100 distinct ones, each at most 32 characters, lowercase, a dot and then one or more parts joined by dots, each part a letter or digit followed by letters, digits, `_`, `+`, or `-`, such as `.nupkg` or `.tar.lz`. A changed file the snapshot finds binary whose name ends in one, ignoring case, is an unreviewed file rather than an unavailable source. Defaults to `[]`. |
| `batch_size` | integer or null | no | The most of the repository's eligible pull requests one batch run reviews, at least 1: the oldest, merged ones by merge date first, and the rest are deferred to the next run. Null or absent sets no bound. |

#### Reviewer (`config.repositories.<repository>.reviewer`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | A slug: a lowercase letter or digit, then up to 63 lowercase letters, digits, or `-`. `generic` for the generic scope. |
| `protocol_version` | integer | yes | One of `1`. |
| `scope` | string | no | One of `generic` or `repository`; defaults to `repository`. `generic` is the suite's own reviewer and allows none of the fields below except as null. |
| `trusted_ref` | string or null | no | The ref the reviewer's files are read at, instead of the pull request's base commit. Not blank and not a `refs/pull/` ref; a ref that resolves to the reviewed head is refused when a review starts. A `skill` it lacks fails the pull request; without a trusted ref, a `skill` the base lacks is read from the default branch's tip, or the suite's generic reviewer runs when the tip lacks it too. |
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
| `status_overrides` | object | no | Keyed by `owner/repo#number`. Each value is a non-empty status shown for that pull request, and may not be a computed one (`to review`, `awaiting response`, `my prs`, `my pull requests`, `drafts`, `missing`, `current`, `stale`, or `incomplete`, in any case). |
| `author_names` | object | no | Keyed by GitHub login, unique ignoring case. Each value is the non-empty single-line name the Requestor column shows instead of the author's profile name. |

### Reviewer manifests

A repository reviewer's manifest, validated by `validate_adapter_manifest` in `review_runtime.py` when a review loads it. Every file a manifest names is a path in the repository, except a specialist's `profile` given as `suite:<name>`, a profile the suite ships; no list names a file twice and no file has two roles. In a specialists manifest a file has one role, because each role is read from its own commit: a top-level resource, a specialist's profile, a specialist's own resource (a guideline, read from the pull request's base when it has it), or a condition's script. Several specialists may name the same profile or the same guideline, and several conditions the same script, but no list names a file twice and no file has two roles; a manifest that breaks either is refused with `Adapter declares a file more than once`. That reference is a name from a fixed catalogue (`SUITE_PROFILES` in `review_runtime.py`), never a path, so a manifest cannot reach another file of the suite or anything outside it through one, and an unknown name fails the manifest. It changes nothing a pull request's author can reach: the manifest itself still comes only from a trusted commit or the configured local manifest. `materialization.json` at the top of the reviewer's files is reserved in both schemas, in any case, because materialization writes its own record there and a file system that ignores case would let the record replace a file such as `Materialization.json`. A manifest that names it, as an entrypoint, resource, agent profile, specialist profile or resource, or condition script, is refused with `Adapter declares a reserved path`, and nothing is materialized. The same name below the top, such as `references/materialization.json`, is allowed.

#### Entrypoint manifest (`entrypoint-manifest`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `schema_version` | integer | yes | One of `1`. |
| `id` | string | yes | A slug, as for a configured reviewer. |
| `protocol_version` | integer | yes | One of `1`. |
| `supports` | array | yes | The review modes it handles: a non-empty array of distinct `initial` and `re-review`. |
| `required_capabilities` | array | yes | Distinct non-empty capability names the runtime must offer: Claude Code and Codex offer `agent-delegation`, `read-diff`, and `write-result`, and Copilot CLI offers `isolated-added-root`, `read-diff`, and `write-result`. An inline review offers only `read-diff` and `write-result`. |
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
| `required_capabilities` | array | yes | As in an entrypoint manifest. Listing `agent-delegation` has the specialists run only as subagents; without it they also run inline, as they do on Copilot CLI. |
| `resources` | array | yes | Distinct files any specialist may read. |
| `specialists` | array | yes | The specialists, at least one. |
| `conditions` | object | yes | Keyed by condition name, a slug; each value is a [condition](#condition-specialists-manifestconditionscondition). May be empty. |
| `uncovered` | string | no | One of `review` or `ignore`. What happens to changed files no specialist matches when some do, even if every matching specialist's condition is closed: `review`, the default, gives them to the generic reviewer, and `ignore` gives them to no generic reviewer and lists in the record those no specialist that shares its files reviews on an open condition, which are left unreviewed. Files matched only by closed-condition specialists stay skipped; a specialist that shares its files matches none for this. When no specialist matches at all, the generic reviewer reviews the whole change regardless of this setting. |
| `finding_categories` | array | no | Distinct one-line names (matched without regard to case) of at most 60 characters, without backticks, quotes, or pipes; at least one. When given, every role names one per finding as its `category`, which the record keeps; without it, a finding's category is its specialist's `category`. |
| `fallback_finding_category` | string | no | A name that `finding_categories` lists, such as Other: the category reviewers are told to use only when no other fits. A finding must still name a listed category. |

#### Specialist (`specialists-manifest.specialists[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | A slug, unique in the manifest and not `generic-review`. |
| `category` | string | yes | The category of its findings. Not blank. |
| `profile` | string | yes | Its agent file: a repository path, or `suite:<name>` for a profile the suite ships (`suite:design-review`), read from `ejryan89-code-review-core`'s deployed references and recorded in `adapter.source_hashes` under that reference. |
| `include` | array | yes | Non-empty Python regular expressions searched against changed paths; the specialist runs when one matches a changed file. A renamed file is matched by its old path and its new one, and a specialist either path matches, and does not exclude, reviews it. |
| `exclude` | array | yes | Regular expressions for changed paths it skips even when included. |
| `resources` | array | yes | Its guideline files, read from the pull request's base commit when that commit has them. |
| `when` | string or null | yes | A key of `conditions` that must hold for it to run, or null to run whenever files match. |
| `model` | string or null | no | One of `inherit`, `sonnet`, `opus`, `haiku`, or `fable`. Its reviewer's model, over its profile's; `inherit` uses the session's; null or absent keeps the profile's, else the session's. |
| `shares_files` | boolean or null | no | True has it review its matched files beside whoever else reviews them: its matches take no file from the generic reviewer, skip none when its condition is closed, and own no prior finding or comment. False, null, or absent: its matches are its own, as without it. |
| `effort` | string or null | no | One of `low`, `medium`, `high`, `xhigh`, or `max`. Its reviewer's reasoning effort when the Workflow tool or Codex starts it, over the configuration's `reviewer_effort`; null or absent keeps the configuration's `reviewer_effort`, else the session's effort. |

#### Condition (`specialists-manifest.conditions.<condition>`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `script` | string | yes | A script beside the manifest, run as `python <script> --source-root <snapshot>`, with the pull request's unified diff named in the environment variable `CODE_REVIEW_DIFF_FILE` and a file it may write in `CODE_REVIEW_CONDITION_OUTPUT`. Exit code 0 runs the specialists that name it, 1 skips them, and anything else fails the review. When it exits 0 having written that file, each specialist it opened is given it as `CONDITION_FILE`. |
| `reads` | array | no | The snapshot paths the script opens besides the changed files: at most 100 glob patterns, distinct ignoring case, each 1 to 200 characters, repository-relative, without a backslash, a control character, or an empty, `.`, or `..` segment, matched as a repository's `snapshot_exclude` is. When every condition a review runs declares it, the checkout route's snapshot stays lazy and also holds the paths they match that `snapshot_exclude` does not, and the script reads no other; without it, the snapshot is written whole. May be empty. |

A condition must finish within `CONDITION_TIMEOUT_SECONDS` in `review_specialists.py`. A script that exceeds that
deadline is killed and waited for, and fails the review with `Condition script timed out: <script>` before any
reviewer plan is written. A refused `prepare` leaves no published run. A script reads no stdin, and its output goes
to files, of which at most `CONDITION_OUTPUT_BYTES` a stream is kept for a failure's detail, which ends by naming
how many bytes past that were cut. It runs in a process tree, a Windows job object or elsewhere a process group, so every process it
started, but for one that leaves a process group by starting a session or group of its own, is ended when it exits or
is killed, and none holds the review waiting. It writes its file in a throwaway folder, which is removed,
with anything a condition left there, before the plan writes anything. Only the file of a condition that opened is
read: an empty file is no file, and anything at its name but a regular file fails the review with `Condition output is
not a regular file: <condition>.txt`. What a closed condition leaves there is never read. Each specialist the condition opened is given at
most the file's first `CONDITION_FILE_BYTES` (in `review_specialists.py`), stopping before a character the bound
splits, as its own `CONDITION_FILE`.

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
| `body_file` | string | with `body_characters` | Absolute path of the run's `pull-request-body.md`, which holds the part of its description reviewers are given, the first 24,000 characters, each lone surrogate replaced by U+FFFD. `prepare` always writes it. |
| `body_characters` | integer | with `body_file` | The full length of its description, in characters. |
| `body_given` | integer | with `body_file` | How many of those characters `body_file` holds: the smaller of `body_characters` and 24,000. |

#### Source snapshot (`request.source_snapshot`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `root` | string | yes | Absolute path of the snapshot folder. |
| `manifest_path` | string | yes | Absolute path of the snapshot's manifest, which lists every file it holds and its hash and, for a lazy snapshot, every other file it can hold and its blob id. |
| `source_commit` | string | yes | The commit it holds: the head commit. |

The snapshot's manifest records each path it leaves out under `excluded_paths`, with one reason: `agent-instruction`, `binary`, `configured` (a regular file the repository's `snapshot_exclude` matches, changed or not), `file-size-limit`, `unsafe-path` (a path Windows cannot hold as it is named; the "Threat model" section lists the rules), `symbolic-link`, `non-regular` (any other entry that is not a regular file or a directory, such as a hard link, FIFO, or device), or `unextractable` (a changed document whose text extraction was refused; the "Threat model" section lists the reasons). Only `binary`, `configured`, `file-size-limit`, `unsafe-path`, and `unextractable` are coverage gaps, which put a changed file in `coverage.unavailable_sources`, so the author of a pull request cannot hide a change behind the maintainer's exclusion or in a file no reviewer can read: the review is `INCOMPLETE`, and its reviewers still get the change's diff. The others are deliberate, and reviewers judge those files from the diff. A changed `binary` file whose name ends, ignoring case, in a suffix of a file type the review knows it does not read is no gap: it goes in `coverage.unreviewed_files` with the suffix that matched, the longest when several do, and the report names it. The suite ships these suffixes, and a repository's `unreviewed_suffixes` adds its own: archives (`.7z`, `.bz2`, `.cab`, `.gz`, `.jar`, `.rar`, `.tar`, `.tgz`, `.xz`, `.zip`, `.zst`), installers and packages (`.apk`, `.appx`, `.deb`, `.dmg`, `.msi`, `.msix`, `.msp`, `.nupkg`, `.pkg`, `.rpm`, `.snupkg`, `.vsix`, `.whl`), images (`.bmp`, `.gif`, `.icns`, `.ico`, `.jpeg`, `.jpg`, `.png`, `.psd`, `.tif`, `.tiff`, `.webp`), fonts (`.eot`, `.otf`, `.ttf`, `.woff`, `.woff2`), compiled binaries and symbol files (`.a`, `.class`, `.dll`, `.dylib`, `.exe`, `.lib`, `.o`, `.obj`, `.pdb`, `.pyc`, `.pyd`, `.so`, `.wasm`), and media (`.aac`, `.avi`, `.flac`, `.m4a`, `.mkv`, `.mov`, `.mp3`, `.mp4`, `.ogg`, `.wav`, `.webm`). The suffix decides only for a file the snapshot found binary, so a text file is reviewed as text whatever its name. A changed file over the size limit whose name ends in one of them, and that is no Word document or PDF, has only its first 8,000 bytes read, and is `binary` when they hold a NUL byte, so an installer too large to snapshot is unreviewed rather than a gap; any other file over the limit is never read, and it, like one whose first bytes hold no NUL byte, stays a `file-size-limit` gap. A lazy snapshot lists no excluded path under `fetchable`, so `source-file` names it with its reason and writes nothing, and `source-search` leaves it out.

A Word document (`.docx`) the pull request changes is held as its extracted text instead of its bytes, on every route, and the manifest lists it under `extracted`, an object keyed by its path whose value is its format, `docx`; its `source_hashes` entry hashes the text. Each line of the text is one paragraph or table row of the document's body and starts with its place: `[P<n>]` for the body's n-th block, a paragraph or a whole table, every paragraph counted and an empty one given no line, and `[P<n> R<r>]` for row r of the table at block n. A heading starts with a `#` for each level, up to six, and one space, and any other paragraph whose text starts with `#`, alone or after backslashes, gets one more `\` in front, as Markdown escapes it, so only a heading's line starts with `#`; a list item starts with `- ` indented two spaces a level, a row's cells are joined by ` | `, and a tracked change stays in its line as `{+inserted+}` or `[-deleted-]`. Headers, footers, footnotes, and comments are not extracted. `prepare` gives the document's diff block in `diff.patch` a header line starting `extracted:` that says so, and in place of git's binary line a diff of the base and head text, matched on the text without its places, so an inserted paragraph is one added line, and shown with them, so its new side is the snapshot file line for line and a finding's line is that file's. The base is read by the blob the diff names, from the checkout's object store, or on the tarball route from GitHub at the merge base, where the blob must start with that id and hash to its own; a base that cannot be read or extracted shows the whole head as added, and `prepare` notes why. `source-file` returns a held document's text as it returns any changed file, and `source-search` never searches it, as it searches no binary blob. An unchanged document, and every other binary format, stays `binary`.

A PDF (`.pdf`) the pull request changes is held the same way, with format `pdf`, whether or not the snapshot finds it binary, and its text is read by `pdftotext` only after the shared provider/version probe accepts the current resolved executable. Without a supported, identifiable tool or on probe failure the PDF is `unextractable`, with an explicit reason, and the review is `INCOMPLETE`. Every head/base extraction repeats the probe, then runs the checked absolute path. Each line of a PDF's text is one line of text as `pdftotext` reads it with `-raw`, in the order the PDF draws its text, and starts with `[p<page> l<line>]`, the line counted among the page's lines that hold text. Both supported families, Xpdf and Poppler, accept `-raw` and read one line per line of text with it; without it, Xpdf joins a paragraph's evenly set lines into one. They can still differ where one line's pieces are drawn apart, such as a raised suffix or the words of a widely spaced title, which Poppler may put on lines of their own. Its text is held beside the document rather than at its path, because Claude Code's file reader parses any file named `.pdf` as a PDF and refuses text there: at `source-snapshot-text/<path>.txt`, which `source_hashes` lists and hashes, while `extracted` stays keyed by the document's own path and nothing is held there. No repository path may be in `source-snapshot-text`, in any case, as none may be the manifest; the snapshot fails on one, and a manifest that lists a file there no extracted PDF names fails verification. A PDF whose held path would be longer than the room the snapshot's folder leaves is `unextractable`. `source-file` returns the held text for the PDF's path, and the diff's `extracted:` line names where the text is held.

#### Request coverage (`request.coverage`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `unavailable_sources` | array | yes | Changed files the snapshot could not hold, for size, an unsafe name, the repository's `snapshot_exclude`, or binary content of no type the review knows it does not read, changed documents whose text extraction was refused, changed paths no reviewer prompt can carry safely, and changed files whose patch GitHub's files API left out when it served the diff. |
| `unreviewed_files` | object | yes | Keyed by the path of each changed binary file of a type the review does not read; each value is the suffix its name matched. Empty when there is none. |

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

### Specialist result

The result the bundled generic reviewer and each specialist write to their role's result file, in the shape the output contract of their prompt shows. `load_role_result` in `review_specialists.py` validates it whenever `validate-result`, `check`, or `finalize` reads it, against the role's files and the added lines of its diff, the prior findings and review comments it was given, and the analyzers the repository has.

#### Specialist result (`specialist-result`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `model` | string | yes | The model ID the reviewer's system prompt names, or `unknown`: one trimmed line of at most 200 characters. |
| `summary` | string | yes | A short assessment. Not blank. |
| `findings` | array | yes | Its findings, each on an added line of its own files. Empty for a role that only gives dispositions. |
| `prior_dispositions` | array | yes | Exactly one for each prior finding the role was given; empty when it was given none. |
| `comment_dispositions` | array | when the role was given review comments | Exactly one for each. |

#### Specialist finding (`specialist-result.findings[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `path` | string | yes | One of the role's files, as its `diff --git` header names it. |
| `line` | integer | yes | A number shown on an added line of that file in the role's diff. |
| `severity` | string | yes | One of `MUST_FIX`, `SHOULD_FIX`, `SUGGESTION`, `MUST FIX`, or `SHOULD FIX`; the last two are read as the first two. |
| `title` | string | yes | A headline: one trimmed line of at most 120 characters. |
| `category` | string | when the specialists manifest declares `finding_categories` | One of those categories, matched ignoring case. Without them, the role's own category is recorded in its place, though a `category` given anyway must still be a string. |
| `body` | string | yes | The problem. Not blank. |
| `analyzer` | object | no | How a diagnostic analyzer could catch it instead. |
| `repeats` | integer or string | no | The finding it repeats: the 0-based index of another finding in this result, or the ID of a prior finding this result judges `still_present` or `partially_addressed`. At least as severe, and not itself a repeat. |

### Review record

A review version's JSON record, the archive's source of truth, validated by `validate_record` in `review_records.py` whenever it is written or read.

#### Record (`record`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `schema_version` | integer | yes | One of `1`, or `2`. A review writes `2`; a `1` record, written before unreviewed files were recorded, is still read, and has no `review.coverage.unreviewed_files`. |
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
| `body_characters` | integer | with `body_given` | The full length of its description, in characters; absent from records written before reviewers were given it. |
| `body_given` | integer | with `body_characters` | How many of those characters reviewers were given, at most `body_characters`. |

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
| `coverage` | object | no | Changed files no reviewer saw in full. Present only when one of its lists or objects is not empty. |
| `reviewers` | array | no | The reviewers that ran, at least one; absent from older records and for a single reviewer. |
| `patches` | object | no | Keyed by changed file path; each value is that file's [patch fingerprint](#patch-fingerprint-recordreviewpatchespath), so the next re-review can tell which files changed since. Not empty; absent from older records. |
| `scope` | object | no | A re-review's scope. Only a re-review has one; absent from older records. |
| `dispatch` | string | no | One of `subagents`, `copilot-host`, or `inline`. How the reviewers were worked: as subagents (or by a Claude Code Workflow), by the bounded Copilot CLI host, or inline by the orchestrating session. Absent from older records. |
| `snapshot` | object | no | The [source snapshot](#source-snapshot-measured-recordreviewsnapshot) the reviewers read: where it came from, how large it was, and how long `prepare` took around it. Absent from older records. |

#### Review coverage (`record.review.coverage`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `unavailable_sources` | array | yes | Distinct changed files whose source the snapshot could not provide, so reviewers saw only their diff, and changed paths no reviewer prompt can carry safely, which no reviewer was given. One makes the verdict `INCOMPLETE` unless findings request changes. |
| `uncovered_files` | array | no | Distinct changed files no specialist covers and no specialist that shares its files reviews, left unreviewed because the manifest sets `uncovered` to `ignore`. A deliberate opt-out that does not change the verdict. |
| `unreviewed_files` | object | no | Keyed by the path of each changed binary file of a type the review does not read, as the [request's](#request-coverage-requestcoverage) lists it; each value is the lowercase suffix its name ends in that matched. Never one of `unavailable_sources`, and does not change the verdict. Only in a `schema_version` `2` record. |

#### Reviewer that ran (`record.review.reviewers[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | The specialist or reviewer, unique in the list. |
| `category` | string | yes | Its category. |
| `files` | integer | yes | How many changed files it covered. Not negative, as for each count here. |
| `findings` | integer | yes | How many findings it raised. |
| `retries` | integer | yes | How many times it was rerun. |
| `dispositions_only` | boolean | yes | Whether it only gave dispositions: the role reviews nothing new, because none of its files changed or every specialist matching them is closed (or, for the generic reviewer, the manifest sets `uncovered` to `ignore`). |
| `model` | string | no | The model it reported running on: one trimmed line of at most 200 characters. Absent for a repository entrypoint reviewer and from older records. |
| `seconds` | integer | no | Whole seconds from handing it the role to its accepted result, reruns included, and 0 for a result dated less than a second before it was handed out; absent when not timed or when the result is dated a second or more before. |
| `files_read` | integer or null | with `bytes_read` | How many distinct files of the source snapshot, not counting its manifest, it opened, with Read or a Grep of one file, or fetched or found with `source-file`, as the reviewer guard logged them, reruns included. Null when no guard counted its reads (an inline, Copilot CLI host, Codex, or general-purpose fallback reviewer), which means unknown, not zero. Absent from older records. |
| `bytes_read` | integer or null | with `files_read` | Those files' total size in bytes, each counted whole whatever part of it was read; null together with `files_read`. |

#### Source snapshot measured (`record.review.snapshot`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `source` | string | yes | One of `checkout`, `checkout-lazy`, or `tarball`. Where the snapshot came from: the configured checkout's git objects (a fixture canary's throwaway repository counts as a checkout), written whole, or `checkout-lazy`, its changed files and analyzer settings written and every other file fetched when a reviewer asked for it; or GitHub's tarball. |
| `files` | integer | yes | How many files it held when `prepare` wrote it, its own manifest left out. Not negative, as for each count here. |
| `bytes` | integer | yes | Their total size in bytes. |
| `diff` | object | no | Where the [diff came from](#diff-source-recordreviewsnapshotdiff) when GitHub's diff endpoint refused it over one of its limits. Absent when the endpoint served it, and from older records. |
| `excluded` | object | no | Keyed by exclusion reason, a lowercase name of at most 40 letters and hyphens, as the snapshot's manifest records it under `excluded_paths`. Each value is the positive number of the head's paths left out for that reason, every path the head holds counted on every route. An empty object when nothing was left out. Absent from older records. |
| `seconds` | object | yes | How long each [phase](#snapshot-phase-seconds-recordreviewsnapshotseconds) of `prepare` took. |

#### Diff source (`record.review.snapshot.diff`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `source` | string | yes | One of `checkout` or `files-api`. Where `prepare` read the diff: git's diff in the configured checkout, from the merge base GitHub names to the head, or GitHub's files API, its patches joined into git's form. |
| `limit` | string | yes | The limit GitHub's diff endpoint named, as a count and what it counts, as in `300 files`. |

#### Snapshot phase seconds (`record.review.snapshot.seconds`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `fetch` | number | yes | Fetching the head: checking the checkout's remote and fetching the pull request's head when the commit is not local, or downloading GitHub's tarball and checking it against the commit's tree. Seconds to a tenth, not negative, as for each value here. |
| `materialize` | number | yes | Writing the snapshot and hashing each file, from the checkout's objects or the tarball. |
| `prompts` | number | yes | Writing the request and every role's prompt and work files. |

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
| `name` | string | yes | The ID of the reviewer that ran: `generic` for the suite's generic reviewer, whether the repository is configured with it or it ran in place of a repository reviewer (`source` `generic-fallback`), and the manifest's `id` for a repository reviewer, which for a configured review `skill` is the configured reviewer's ID. |
| `protocol_version` | integer | yes | One of `1`. |
| `scope` | string | yes | One of `generic` or `repository`. |
| `source` | string | no | One of `generic`, `trusted-ref`, `base`, `default-branch`, or `generic-fallback`. Where the reviewer came from: with scope `generic`, the suite's generic reviewer the repository is configured with (`generic`) or one that ran in place of a configured review skill the base predates and the default branch's tip could not supply (`generic-fallback`); with scope `repository`, the commit `source_commit` names, which is the configured trusted ref, the pull request's base, or the default branch's tip when the base predates the review skill. Absent from older records. |
| `source_commit` | string or null | yes | The commit a repository reviewer's files were read at; null for the generic reviewer. |
| `source_hashes` | object | yes | Keyed by path of each loaded reviewer file; each value is the SHA-256 of its exact committed bytes. A suite profile a specialist names is keyed by its `suite:<name>` reference, with the SHA-256 of the suite's deployed file. Empty for the generic reviewer. |
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
| `location` | string | no | Where in a changed document the finding's line is, as `finalize` reads it from the place that line of the document's extracted text starts with: `paragraph <n>`, then `, table row <r>` and `, under the heading "<heading>"` when they apply, the heading cut to 80 characters, or, for a PDF, `page <p>, line <l>`. One trimmed line of at most 200 characters, which the suite adds and a reviewer's result may not carry. Absent from a finding on any other file, and from older records. |

#### Analyzer coverage (`record.findings[].analyzer`, `specialist-result.findings[].analyzer`, `synthesis-result.recommendations[].target.analyzer`, `insights.recommendations[].target.analyzer`, `insights.synthesis.superseded[].recommendations[].target.analyzer`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `coverage` | string | yes | One of `available`, `known`, or `custom-candidate`: a rule in an analyzer the repository already has but does not enforce, a rule in an established analyzer it does not use, or a pattern no rule covers. |
| `tool` | string | yes | The analyzer: at most 100 characters, with no whitespace, backticks, pipes, or angle brackets. In a specialist result, an `available` tool is one the repository's analyzer inventory lists, matched ignoring case, and a `known` one is not. |
| `rule` | string | yes | The rule, written like `tool`. For `custom-candidate`, a lowercase kebab-case pattern name of at most 60 characters. |

#### Finding reference (`record.findings[].repeats`, `record.ledger[].repeats[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `version` | integer | yes | The review version of the finding. |
| `id` | string | yes | Its finding ID in that version. |

#### Prior disposition (`record.prior_dispositions[]`, `specialist-result.prior_dispositions[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `finding_id` | string | yes | The prior finding: its ledger entry's `v<version>:F<nnn>` in a record with a ledger or a specialist result, or a bare finding ID of the version it compared with in an older record. Unique. |
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
| `body` | string | yes | The thread's first comment, which may be empty. |
| `url` | string | yes | Its web address. |

#### Comment disposition (`record.comment_dispositions[]`, `specialist-result.comment_dispositions[]`)

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

The flag store shared by `flag-review-finding` and `review-insights`, validated by `validate_store` in `review_flags.py` whenever it is read or written. A version 1 store is read as version 2 with no review versions. A store is read only up to 4194304 bytes (4 MiB), so a change that would write a larger one is refused and leaves the store as it was.

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
| `pull_number` | integer or null | yes | The pull request it is about, or null. `flag-review-finding add` writes one only with its `repository`; an older flag's pull request without one loads and links to nothing. |
| `review_version` | integer or null | yes | The review version its finding is in, which needs `pull_number`; or null. `flag-review-finding add` writes one only with its `finding_id`; an older flag's review version without one loads and links to nothing. |
| `finding_id` | string or null | yes | The finding it names in that review version, or null. `flag-review-finding add` writes only a finding ID that review has; its form is not checked when read, so an older flag's ID loads and links to nothing. |
| `category` | string | yes | Its category. Not blank. `flag-review-finding` writes at most 120 characters; an older, longer one loads. |
| `body` | string | yes | The observation. Not blank. `flag-review-finding` writes at most 4000 characters; an older, longer one loads. |
| `resolution` | string or null | yes | How it was resolved; null while open. |

### Review state

The mutable state `review-prs` keeps per repository, at `~/.coding-agent-skills/code-review/state.json` unless the `CODE_REVIEW_STATE` environment variable names another file, validated by `validate_state` in `review_state.py` whenever it is read or written. A missing file is an empty state. `advance` changes it under the state lock and only ever moves a watermark forward.

#### Review state (`state`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `schema_version` | integer | yes | One of `1`. |
| `repositories` | object | yes | Keyed by `owner/repo` identity; each value is a [repository's state](#repository-state-staterepositoriesrepository). |

#### Repository state (`state.repositories.<repository>`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `merged_since` | string | no | The merged-pull watermark: pull requests merged on or after this UTC date are eligible. Its first ten characters are a `YYYY-MM-DD` calendar date, and only they are read; `validate_state` refuses any other value, so a state that holds one fails every operation that reads it. Absent until a batch run first advances it. |
| `updated_at` | string | no | The UTC date of the batch run whose `advance` last moved the watermark. |

### Tracker record

The pull requests `update-pr-dashboard` holds and the AI review of each row it showed, beside the file the code-review configuration path leads to, through any symbolic link: `tracker.json` for a configuration named `config.json`, and `<name>.tracker.json` for a configuration named `<name>.json`, so two configurations in one folder keep records of their own. `validate_tracker_record` in `update_pr_dashboard.py` validates it whenever it is read or written. Each `update` that writes the dashboard reads it, to name each review that went stale since, then replaces it. `override` reads it: a `--set` must name a pull request it lists, and the listing marks each override it does not. `review-arguments` reads it: a confirmed pull request must be a row it holds as `missing` or `stale`. A missing file holds no pull request.

#### Tracker record (`tracker`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `schema_version` | integer | yes | One of `1`, or `2`. An update writes `2`; a `1` record, written before records held reviews, is still read, as holding no reviews. |
| `pulls` | array | yes | Every pull request the last update collected that the configured user authored, is asked to review, or takes part in, whether or not its row was shown, as `owner/repo#number` keys matched ignoring case, each once. |
| `reviews` | object | when `schema_version` is `2` | Keyed by the `owner/repo#number` key of each row the last update showed, a pull request `pulls` lists; each value is that row's [AI review](#tracker-review-trackerreviewspull). |

#### Tracker review (`tracker.reviews.<pull>`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `state` | string | yes | One of `missing`, `current`, `stale`, or `incomplete`: the row's AI review state. |
| `reviewed_head_sha` | string or null | yes | The head its latest AI review covered; null exactly when `state` is `missing`. |

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
| `verdict` | string | yes | The legacy verdict, normally `APPROVED` or `CHANGES REQUESTED`. Its type is checked when read, but its string value is not; dashboards replace spaces with underscores. A non-string or missing verdict makes the index absent. |
| `source_sha256` | any | yes | A hash of the legacy review's content. Not checked when read. |
| `source_path` | any | yes | Where the legacy review was copied from. Not checked when read. |
| `source_file_sha256` | any | yes | SHA-256 of the legacy review file. Not checked when read. |

### Fixture pull request

A fixture canary, `prepare --canary --fixture <directory>`, reviews a fixture directory in place of a pull request: `base/` and `head/` hold the change as two trees, and `pull.json` the pull request around it, validated by `validate_fixture_pull` in `review_canary.py`. `prepare` commits each tree's files, byte for byte, to a throwaway repository, and takes the base and head commits and the diff from it; a fixture tree holds only regular files and folders, at paths git accepts in an index (none inside `.git`, for one). A re-review canary's prior record is a `record`, the pull request's first review. With `manifest_path`, the fixture's reviewer is that manifest, read from the base commit; without it, the suite's generic reviewer. See "Fixture canaries" in [Code-review operations](code-review-operations.md#fixture-canaries).

#### Fixture pull request (`fixture-pull`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `schema_version` | integer | yes | One of `1`. |
| `repository` | string | yes | The `owner/repo` the fixture stands for, in any case; the record names it, and no configuration needs to. |
| `number` | integer | yes | The pull request's number, from 1. |
| `title` | string | yes | Its title. Not blank. |
| `base_ref` | string | yes | Its base branch. Not blank. |
| `head_ref` | string | yes | Its head branch. Not blank. |
| `threads` | array | yes | The unresolved review threads a person started, in order, which `prepare` numbers `C1`, `C2`, and so on. May be empty. |
| `manifest_path` | string | no | A reviewer manifest in the `base/` tree, as a repository-relative path, read from the base commit as a configured repository's `manifest_path` is. Without it, the suite's generic reviewer reviews the fixture. |
| `body` | string | no | Its description, which reviewers are given as they are a pull request's. May be empty; without it, the description is empty. |
| `document_stage` | string | no | The stage of the design document the fixture holds: `draft`, `review`, or `final`. `prepare` gives each role of the suite's design-review profile the stage's one fixed sentence, which sets the bar its instructions' "Document stage" section gives; without it, no stage is named, and the profile holds the document to `review`. `review-document` writes it. |

#### Fixture thread (`fixture-pull.threads[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `author` | string | yes | The person who started the thread. Not blank. |
| `path` | string | yes | The file it is on. Not blank. |
| `line` | integer or null | yes | Its line, from 1, or null when it has none. |
| `outdated` | boolean | yes | Whether the code it was on has changed since. |
| `body` | string | yes | The thread's first comment. Not blank. |
| `url` | string | yes | Its web address. Not blank. |

### Insights report

The report `review-insights report` writes to `<summary_root>/<set>/<start>--<end>/insights.json`, with a Markdown projection beside it, and that `synthesize`, `decide`, and `decide-custom` record into. `load_report` in `review_insights.py` reads it back. It upgrades a report of any earlier version, 1 to 8, to the current one, 9, and refuses any other version, but outside `signals` it checks only the fields it uses and rejects no unlisted field. The `signals` subtree is validated against its tables by `review_signals.read`, which refuses a missing field, a field of the wrong type, and a field its table does not list, so a report whose signals carry an unlisted field is refused on load as `<path> has invalid signals: <reason>`. So the other tables are checked against the reports `create_report`, `synthesize`, `decide`, and `decide-custom` write, as the adapter request's are against what its writer writes, and not against a validator. A change to the report's shape raises `SCHEMA_VERSION`, adds the step that upgrades the version before it, and records the new shape in `tests/code-review/test_format_contract.py`, which fails while the report's tables differ from the shape recorded for the current version. "Review insights" and "Synthesis" in [Code-review operations](code-review-operations.md#review-insights) describe how the report is built.

#### Report (`insights`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `schema_version` | integer | yes | One of `9`. |
| `repository_set` | string | yes | The set the report summarizes, or `repositories-<digest>` for an explicit repository list. |
| `repositories` | array | yes | The `owner/repo` identities analyzed, sorted. |
| `start_date` | string | yes | The first day of the range, as `YYYY-MM-DD`. |
| `end_date` | string | yes | The last day of the range, as `YYYY-MM-DD`. |
| `record_count` | integer | yes | The review records reviewed in the range. |
| `finding_count` | integer | yes | The findings analyzed, each counted once across the reviews that carry it. |
| `severity_counts` | object | yes | The analyzed findings keyed by severity, each a count. |
| `category_counts` | object | yes | The analyzed findings keyed by category, each a count. |
| `recommendations` | array | yes | The category recommendations, then the analyzer recommendations, then the synthesized ones. |
| `next_recommendation_number` | integer | yes | The first `REC-` number the report has never given, so a subject a regeneration drops never has its ID given again. |
| `records` | array | yes | Every review record the report analyzed. |
| `synthesis` | object or null | yes | The synthesis of this range; null in a report upgraded from before version 7 until it is regenerated. |
| `signals` | object | yes | The consistency signals of this range and the candidates found in its guidance, computed before the synthesis and never decided; empty in a report upgraded from before version 9 until it is regenerated. |

#### Analyzed record (`insights.records[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `path` | string | yes | The record's JSON file. |
| `repository` | string | yes | Its repository. |
| `pull_number` | integer | yes | Its pull request. |
| `review_version` | integer | yes | Its review version. |
| `payload_sha256` | string | yes | The record's payload hash, from its `artifacts`. |

#### Recommendation (`insights.recommendations[]`, `insights.synthesis.superseded[].recommendations[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | `REC-` and at least three digits; a subject keeps its ID when the report is regenerated. |
| `kind` | string | yes | One of `category`, `analyzer`, or `synthesized`. |
| `finding_count` | integer | yes | The findings it covers, or for a synthesized one, its evidence references. |
| `decision` | string | yes | One of `accepted`, `rejected`, or `deferred`: the latest decision, `deferred` until one is recorded. |
| `decision_history` | array | yes | Every decision recorded, oldest first. |
| `linked_flags` | array | yes | The open flag IDs linked when the report was written: for a category or analyzer recommendation, the flags naming one of its findings; for a synthesized one, the flags its synthesis named. |
| `reviewers` | array | yes | Its findings counted by the reviewer and model that raised them; empty for a synthesized one. |
| `category` | string | no | A category recommendation's finding category. |
| `recommendation` | string | no | A category or analyzer recommendation's suggested action. |
| `coverage` | string | no | An analyzer recommendation's coverage. One of `available`, `known`, or `custom-candidate`. |
| `tool` | string | no | An analyzer recommendation's analyzer, in its first spelling in review order. |
| `rule` | string | no | An analyzer recommendation's rule, spelled as `tool` is. |
| `repositories` | array | no | The repositories an analyzer recommendation's findings came from. |
| `evidence` | array | no | An analyzer recommendation's findings, each an object, or a synthesized one's finding references, each a string. |
| `type` | string | no | A synthesized recommendation's kind of change. One of `new-rule`, `strengthen-rule`, `remove-rule`, `consolidate`, `stop-flagging`, `start-flagging`, `new-analyzer`, `move-to-script`, or `flagged`. |
| `priority` | string | no | A synthesized recommendation's priority. One of `high`, `medium`, or `low`. |
| `target` | object | no | What a synthesized recommendation changes. |
| `title` | string | no | A synthesized recommendation's title, one line of at most 120 characters without quotes, backticks, `$`, backslashes, control characters, or the line separators U+0085, U+2028, and U+2029. |
| `change` | string | no | A synthesized recommendation's change, exactly what to add, change, or remove. |
| `rationale` | string | no | Why a synthesized recommendation is made. |

#### Reviewer count (`insights.recommendations[].reviewers[]`, `insights.synthesis.superseded[].recommendations[].reviewers[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `reviewer` | string | yes | The reviewer that raised the findings. |
| `model` | string | yes | The model it ran on, or `unknown` when its record names none. |
| `findings` | integer | yes | The recommendation's findings it raised. |
| `flagged_findings` | integer | yes | How many of them an open flag names. |
| `addressed` | integer or null | yes | How many of them a later review judged addressed; null in a report upgraded from before version 6 until it is regenerated. |
| `still_present` | integer or null | yes | How many of them a later review judged still present; null as `addressed` is. |

#### Covered finding (`insights.recommendations[].evidence[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `repository` | string | yes | The finding's repository, lowercase. |
| `pull_number` | integer | yes | Its pull request. |
| `review_version` | integer | yes | The review it was raised in. |
| `finding_id` | string | yes | Its ID in that review. |
| `path` | string | yes | Its file. |
| `line` | integer | yes | Its line. |
| `title` | string or null | yes | Its headline, or null when the record gives none. |

#### Decision (`insights.recommendations[].decision_history[]`, `insights.synthesis.superseded[].recommendations[].decision_history[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `decision` | string | yes | One of `accepted`, `rejected`, or `deferred`. |
| `decided_at` | string | yes | When it was recorded, as an ISO 8601 time. |
| `note` | string or null | yes | The user's reason, or null. |
| `resolved_flags` | array | yes | The flags this decision resolved. A decision is written before its flags are resolved, and rewritten to list only those resolved when the flag store fails partway. |

#### Recommendation target (`insights.recommendations[].target`, `insights.synthesis.superseded[].recommendations[].target`, `synthesis-result.recommendations[].target`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `repository` | string | with `path` | The repository whose guidance changes, one of the scope's; absent for a `new-analyzer` recommendation. |
| `path` | string or null | with `repository` | The guidance file to change, one of the repository's guidance files, or null when none fits and the change says where the guidance belongs. For a `consolidate` recommendation, the guidance file whose wording survives; for a `move-to-script` one, the script, validator, or test that takes the rule over, a repository-relative file that is no prose guidance and need not exist yet, in at most 260 characters free of quotes, backticks, `$`, backslashes, control characters, and the line separators U+0085, U+2028, and U+2029. |
| `merge` | array | when the recommendation is a `consolidate` one | The other guidance files of the repository a `consolidate` recommendation merges into `path`, at least one, each once; absent for every other type. |
| `analyzer` | object | no | A `new-analyzer` recommendation's rule, with no other field. |

#### Synthesis (`insights.synthesis`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `status` | string | yes | One of `pending`, `complete`, or `skipped`: the input is written and no result recorded, a result is recorded, or the range has no findings and no open flags in scope and nothing was written. |
| `input_sha256` | string or null | yes | The hash that seals the input and the context; null when skipped. |
| `input` | string or null | yes | The input file; null when skipped. |
| `prompt` | string or null | yes | The prompt file, in the synthesis's run under the temporary directory, which is removed once a result is recorded or a later report stages another; null when skipped. |
| `result` | string or null | yes | The one result file `synthesize` records, in the same run; null when skipped. |
| `previous_report` | string or null | yes | The previous period's report the input compares with, or null. |
| `recorded_at` | string or null | yes | When the result was recorded, as an ISO 8601 time; null until then. |
| `themes` | array | yes | The recorded result's themes; empty until one is recorded. |
| `mistakes` | array | yes | The recorded result's mistakes. |
| `persistent_patterns` | array | yes | The recorded result's persistent patterns. |
| `reviewer_effectiveness` | object or null | yes | The recorded result's reviewer effectiveness; null until one is recorded. |
| `comparison` | object or null | yes | The recorded result's comparison with the previous period, or null. |
| `categories` | array | yes | The recorded result's commentary on each category. |
| `custom_rule_patterns` | array | yes | The recorded result's custom-rule patterns. |
| `superseded` | array | yes | Each recorded synthesis a changed input replaced, oldest first. |

#### Superseded synthesis (`insights.synthesis.superseded[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `recorded_at` | string | yes | When it was recorded. |
| `input_sha256` | string | yes | The input it was recorded against. |
| `recommendations` | array | yes | Its synthesized recommendations, with their decisions; their IDs are never given again. |

#### Signals (`insights.signals`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `severity_drift` | array | yes | The strongest groups and ledger entries whose findings carry more than one severity: the most severities first, then the most findings. |
| `verdict_flips` | array | yes | The strongest pairs of consecutive reviews of one head commit whose verdicts differ: a flip between `APPROVED` and `CHANGES_REQUESTED` first, then the most findings that differ. |
| `reviewer_disagreements` | array | yes | The strongest places where reviewers that share no source gave one head commit's findings different severities: the widest spread of severities first, then the most findings. |
| `guidance_overlaps` | array | yes | The strongest candidate pairs of rules from two guidance files that conflict or repeat each other: conflicts first, then the most similar. |
| `unsupplied_references` | array | yes | The first Markdown files a reviewer's guidance names that the reviewer is not given. |
| `counts` | object | yes | Keyed by each of the fields above, how many it found before it kept the strongest. |

#### Severity drift (`insights.signals.severity_drift[]`, `synthesis-input:severity-drift`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `kind` | string | no | On an input line only. One of `severity-drift`. |
| `id` | string | yes | `D` and its rank, from 1. |
| `basis` | string | yes | One of `group` or `ledger`: findings with the same repository, category, and headline, or a finding and its repeats. |
| `repository` | string | yes | The repository, lowercase. |
| `category` | string | yes | Its first finding's category. |
| `headline` | string | yes | Its first finding's headline. |
| `severity` | object | yes | Its findings keyed by severity, each a count; more than one severity. |
| `examples` | array | yes | Up to three findings, each `<severity> <reference>`, one of each severity first. |

#### Verdict flip (`insights.signals.verdict_flips[]`, `synthesis-input:verdict-flip`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `kind` | string | no | On an input line only. One of `verdict-flip`. |
| `id` | string | yes | `V` and its rank, from 1. |
| `repository` | string | yes | The pull request's repository, lowercase. |
| `pull_number` | integer | yes | The pull request. |
| `head_sha` | string | yes | The head commit both reviews reviewed. |
| `versions` | array | yes | The two review versions, earlier first. |
| `verdicts` | array | yes | Their verdicts, in the same order. |
| `differences` | array | yes | Up to ten findings the reviews held open whose severity or presence differs, each `<path>:<line> <before> -> <after> <references>`, `absent` for a review that did not hold it. |

#### Reviewer disagreement (`insights.signals.reviewer_disagreements[]`, `synthesis-input:reviewer-disagreement`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `kind` | string | no | On an input line only. One of `reviewer-disagreement`. |
| `id` | string | yes | `R` and its rank, from 1. |
| `repository` | string | yes | The pull request's repository, lowercase. |
| `pull_number` | integer | yes | The pull request. |
| `head_sha` | string | yes | The head commit the findings were raised on. |
| `path` | string | yes | The file. |
| `line` | integer | yes | The first finding's line; the others are joined to it through disagreeing pairs, each pair within three lines of one another, so a cluster may span more than three lines. |
| `reports` | array | yes | Up to five findings there, in place order, each `<severity> <reference> <source>`: a pair that disagrees is always among them, then one of each other severity. |

#### Guidance overlap (`insights.signals.guidance_overlaps[]`, `synthesis-input:guidance-overlap`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `kind` | string | no | On an input line only. One of `guidance-overlap`. |
| `id` | string | yes | `O` and its rank, from 1: conflicts first, then by similarity. |
| `repository` | string | yes | The repository both files belong to. |
| `relation` | string | yes | One of `conflict` or `duplicate`: the rules differ in a force word, a severity, or a number, or they agree. |
| `similarity` | number | yes | The share of their words the rules have in common, from 0 to 1, leaving out force words and numbers. |
| `differs` | array | yes | The force words, severities, and numbers only one of the rules has; empty for a duplicate. |
| `paths` | array | yes | The two files, in path order. |
| `lines` | array | yes | The line each rule starts on, in the same order. |
| `texts` | array | yes | Each rule's sentence on one line of at most 300 characters. |
| `reviewers` | array | yes | The reviewers given both files at the versions the rules' texts came from; a reviewer given another version of either file is not named. May be empty. |

#### Unsupplied reference (`insights.signals.unsupplied_references[]`, `synthesis-input:guidance-unsupplied`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `kind` | string | no | On an input line only. One of `guidance-unsupplied`. |
| `id` | string | yes | `U` and its number, from 1. |
| `repository` | string | yes | The repository. |
| `reviewer` | string | yes | The reviewer whose guidance names the file. |
| `path` | string | yes | The guidance file that names it. |
| `line` | integer | yes | The line that names it. |
| `reference` | string | yes | The Markdown file named, from the repository root. |

### Synthesis result

The result the analyst agent writes to the file the report's `synthesis.result` names, validated by `check_result` in `review_synthesis.py` against the context the report sealed, both by `synthesize --check` and when it is recorded. Its analysis is recorded in the report's `synthesis`, so the tables of its parts below state the report's too.

#### Result (`synthesis-result`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `schema_version` | integer | yes | One of `1`. |
| `input_sha256` | string | yes | The input's seal, copied from its `seal` line. |
| `themes` | array | yes | At most 10 high-level patterns. |
| `mistakes` | array | yes | At most 15 specific recurring mistakes. |
| `persistent_patterns` | array | yes | At most 5 patterns of findings later reviews judged still present. |
| `reviewer_effectiveness` | object | yes | Which reviewers helped most and least. |
| `comparison` | object or null | yes | The comparison with the previous period: null exactly when the input has no `previous` line. |
| `recommendations` | array | yes | At most 12 recommendations, titles distinct ignoring case. |
| `categories` | array | yes | One entry for each analyzed category, no more. |
| `custom_rule_patterns` | array | yes | At most 15 patterns that place every custom-candidate rule in exactly one; empty when there are none. |

#### Synthesized recommendation (`synthesis-result.recommendations[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `type` | string | yes | One of `new-rule`, `strengthen-rule`, `remove-rule`, `consolidate`, `stop-flagging`, `start-flagging`, `new-analyzer`, `move-to-script`, or `flagged`. |
| `priority` | string | yes | One of `high`, `medium`, or `low`. |
| `target` | object | yes | An analyzer rule for `new-analyzer`; a repository, the surviving guidance file, and the files it merges for `consolidate`; a repository and the script that takes the rule over for `move-to-script`; a repository and guidance file for every other type. |
| `title` | string | yes | One line of at most 120 characters without quotes, backticks, `$`, backslashes, control characters, or the line separators U+0085, U+2028, and U+2029. |
| `change` | string | yes | Exactly what to add, change, or remove, at most 2000 characters. |
| `rationale` | string | yes | Why, at most 1500 characters. |
| `evidence` | array | yes | At most 10 finding references from the input. |
| `flags` | array | yes | The distinct open flags it addresses; a `flagged` one names at least one, and every other names evidence, flags, or both. |

#### Theme (`synthesis-result.themes[]`, `insights.synthesis.themes[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `theme` | string | yes | The pattern, at most 300 characters. |
| `count` | integer | yes | How many findings show it, at least 1. |
| `examples` | array | yes | 1 to 3 finding references. |
| `areas` | array | yes | The paths or domains it touches. May be empty. |

#### Mistake (`synthesis-result.mistakes[]`, `insights.synthesis.mistakes[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `mistake` | string | yes | The mistake, at most 300 characters. |
| `count` | integer | yes | How many findings show it, at least 1. |
| `severity` | object | yes | Its findings keyed by severity, each a positive count. |
| `examples` | array | yes | 1 to 3 finding references. |

#### Persistent pattern (`synthesis-result.persistent_patterns[]`, `insights.synthesis.persistent_patterns[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `pattern` | string | yes | The pattern, at most 300 characters. |
| `count` | integer | yes | How many findings show it, at least 1. |
| `likely_reason` | string | yes | Why the findings are likely left, at most 1500 characters. |
| `examples` | array | yes | 1 to 3 finding references. |

#### Reviewer effectiveness (`synthesis-result.reviewer_effectiveness`, `insights.synthesis.reviewer_effectiveness`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `most_useful` | string | yes | The reviewer whose findings helped most. |
| `least_useful` | string | yes | The reviewer whose findings helped least. |
| `false_positive_candidates` | array | yes | Findings or rules that may be noise. May be empty. |

#### Comparison (`synthesis-result.comparison`, `insights.synthesis.comparison`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `persistent` | array | yes | Themes in both periods. |
| `new` | array | yes | Themes only in this one. |
| `resolved` | array | yes | Themes only in the previous one. |
| `previous_recommendations` | array | yes | An assessment of previous recommendations. |

#### Previous recommendation assessed (`synthesis-result.comparison.previous_recommendations[]`, `insights.synthesis.comparison.previous_recommendations[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | A previous period's synthesized recommendation the input lists. |
| `assessment` | string | yes | Whether this period's findings suggest it worked, at most 1500 characters. |

#### Category commentary (`synthesis-result.categories[]`, `insights.synthesis.categories[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `category` | string | yes | An analyzed category, given once. |
| `topics` | array | yes | 1 to 5 concrete topics within it. |
| `assessment` | string | yes | What its findings show and what should change, at most 1500 characters. |
| `addressed_by` | array | yes | Titles of the result's recommendations that act on it. May be empty. |
| `findings` | array | yes | For a category of five findings or fewer, an assessment of exactly each of its findings; empty for a larger one. |

#### Topic (`synthesis-result.categories[].topics[]`, `insights.synthesis.categories[].topics[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `topic` | string | yes | The topic, at most 300 characters. |
| `count` | integer | yes | How many findings it covers, at least 1. |
| `examples` | array | yes | 1 to 3 finding references. |

#### Finding assessment (`synthesis-result.categories[].findings[]`, `insights.synthesis.categories[].findings[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `ref` | string | yes | One of the category's findings, as `owner/repo#pull vN F001`. |
| `assessment` | string | yes | Whether it is valid, noise, or a gap, and what follows, at most 1500 characters. |

#### Custom-rule pattern (`synthesis-result.custom_rule_patterns[]`, `insights.synthesis.custom_rule_patterns[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `pattern` | string | yes | The pattern, at most 300 characters. |
| `rules` | array | yes | The custom-candidate rules it groups, as `<tool> <rule>`, matched ignoring case; at least one. |
| `assessment` | string | yes | Whether a custom rule is worth writing for it, at most 1500 characters. |
| `addressed_by` | array | yes | Titles of the result's recommendations that act on it. May be empty. |

### Synthesis input

The analyst agent's input, `synthesis-input.jsonl` beside the report: JSON Lines, one object per line, each naming what it is in `kind`, a `seal` line first. `prepare` in `review_synthesis.py` writes it with its context whenever the report is written with a synthesis to run, and no reader validates it: the agent reads a copy of it in the synthesis's run, and `synthesize` checks only that it still matches the seal. Each table below states one kind of line, under the heading path `synthesis-input:<kind>`, and is checked against the inputs the reports above wrote. The prompt in the run, `synthesis-prompt.md`, is the agent's task as Markdown and holds no fields.

#### Seal line (`synthesis-input:seal`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `kind` | string | yes | One of `seal`. |
| `input_sha256` | string | yes | The hash of every line after this one and of the context, which the result repeats. |

#### Totals line (`synthesis-input:totals`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `kind` | string | yes | One of `totals`. |
| `start` | string | yes | The range's first day. |
| `end` | string | yes | Its last day. |
| `repositories` | array | yes | The repositories analyzed. |
| `records` | integer | yes | The records reviewed in the range. |
| `findings` | integer | yes | The findings analyzed. |
| `severity` | object | yes | The findings keyed by severity, each a count. |
| `categories` | object | yes | The findings keyed by category, each a count. |
| `sources` | object | yes | The findings keyed by the reviewer source that raised them, each a count. |

#### Guidance line (`synthesis-input:guidance`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `kind` | string | yes | One of `guidance`. |
| `repositories` | object | yes | Keyed by repository, each the sorted guidance files its repository-scoped reviewers were built from, empty for one only a generic reviewer reviews. |

#### Guidance file line (`synthesis-input:guidance-file`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `kind` | string | yes | One of `guidance-file`. |
| `repository` | string | yes | The repository. |
| `path` | string | yes | One of its guidance files, at one version: a file the reviewers' records pinned at different hashes has a line for each. |
| `role` | string | yes | One of `prose` or `code`: a guidance document, or a script, manifest, or test, listed by path only. |
| `reviewers` | array | yes | The reviewers whose latest record was built from this version of it. |
| `status` | string | yes | One of `included`, `listed`, `too-large`, `over-budget`, or `unavailable`: its text is given, it is a `code` file, its text is past the file limit, past what the input has left, or no checkout copy matches the hash the reviewer's record pinned. |
| `content` | string or null | yes | Its text as the reviewers were given it when `status` is `included`, otherwise null. |

#### Previous period line (`synthesis-input:previous`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `kind` | string | yes | One of `previous`. |
| `start` | string | yes | The previous period's first day. |
| `end` | string | yes | Its last day, at most seven days before this range starts. |
| `category_counts` | object | yes | Its findings keyed by category, each a count. |
| `themes` | array | yes | Its recorded themes. |
| `recommendations` | array | yes | Its synthesized recommendations with their decisions. |

#### Previous recommendation (`synthesis-input:previous.recommendations[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | Its ID. |
| `type` | string | yes | Its type. |
| `target` | string | yes | Its target, as `owner/repo:<file>`, a consolidation's files joined by `+` with the surviving one first, or `analyzer <coverage> <tool> <rule>`. |
| `title` | string | yes | Its title. |
| `decision` | string | yes | Its latest decision. |

#### Category line (`synthesis-input:category`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `kind` | string | yes | One of `category`. |
| `category` | string | yes | An analyzed finding category. |
| `count` | integer | yes | Its findings. |
| `findings` | array | no | Every one of its findings, given only for a category of five findings or fewer. |

#### Category finding (`synthesis-input:category.findings[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `ref` | string | yes | The finding, as `owner/repo#pull vN F001`. |
| `severity` | string | yes | Its severity. |
| `headline` | string | yes | Its title, or its body, on one line of at most 120 characters. |
| `path` | string | yes | Its file. |
| `line` | integer | yes | Its line. |
| `source` | string | yes | The reviewer source that raised it. |
| `outcome` | string | yes | The latest disposition a later review gave it, or `unjudged`. |
| `body` | string | yes | Its body on one line of at most 600 characters. |

#### Custom-rule line (`synthesis-input:custom-rule`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `kind` | string | yes | One of `custom-rule`. |
| `rule` | string | yes | A custom-candidate rule, as `<tool> <rule>` in its first spelling in review order. |
| `count` | integer | yes | Its findings. |
| `examples` | array | yes | Up to two findings, each its reference and headline. |

#### Group line (`synthesis-input:group`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `kind` | string | yes | One of `group`. |
| `group` | string | yes | The group's number in rank order, `G001` first. |
| `repository` | string | yes | The repository its findings share. |
| `category` | string | yes | The category they share. |
| `headline` | string | yes | Its first finding's headline, which its findings share ignoring case and everything but letters. |
| `count` | integer | yes | Its findings. |
| `severity` | object | yes | Its findings keyed by severity, each a count. |
| `sources` | object | yes | Its findings keyed by the reviewer source that raised them, the five most common, each a count. |
| `outcomes` | object | yes | Its findings keyed by their latest later disposition, or `unjudged`, each a count. |
| `flagged` | integer | yes | How many of its findings an open flag names. |
| `paths` | array | yes | Its three most common files, each with its count. |
| `analyzers` | array | yes | The analyzer rules reviewers said could catch its findings, as `<coverage> <tool> <rule>`. May be empty. |
| `examples` | array | yes | Its first two findings. |

#### Group example (`synthesis-input:group.examples[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `ref` | string | yes | The finding's reference. |
| `line` | integer | yes | Its line. |
| `body` | string | yes | Its body on one line of at most 240 characters. |

#### Remaining groups line (`synthesis-input:remaining`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `kind` | string | yes | One of `remaining`. |
| `repository` | string | yes | The repository of the groups beyond the 150 shown in full. |
| `category` | string | yes | Their category. |
| `groups` | integer | yes | How many groups it summarizes. |
| `findings` | integer | yes | Their findings. |
| `severity` | object | yes | Their findings keyed by severity, each a count. |
| `headlines` | array | yes | The first eight groups' headlines, each cut to 80 characters. |

#### Flag line (`synthesis-input:flag`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `kind` | string | yes | One of `flag`. |
| `id` | string | yes | An open flag on the scope's repositories or on none. |
| `category` | string | yes | Its category. |
| `created` | string | yes | The day it was recorded, as `YYYY-MM-DD`. |
| `in_range` | boolean | yes | Whether it was recorded within the range. |
| `repository` | string or null | yes | The repository it names, lowercase, or null. |
| `pull_number` | integer or null | yes | The pull request it names, or null. |
| `finding` | string or null | yes | The reference of the finding it names, in the range or not, or null when it names none. |
| `body` | string | yes | Its body on one line of at most 1500 characters. |

### Synthesis context

What `synthesize` checks a result against, `synthesis-context.json` beside the report, written with the input and sealed with it. The agent never reads it, and no reader validates it beyond the seal.

#### Context (`synthesis-context`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `refs` | array | yes | Every finding reference the input gives: each analyzed finding's, and each one an open flag names. |
| `open_flags` | array | yes | The IDs of the input's flag lines. |
| `guidance` | object | yes | Keyed by repository, each its guidance files, as the guidance line gives them. |
| `previous_recommendations` | array or null | yes | The previous period's synthesized recommendation IDs, or null without a previous line. |
| `categories` | object | yes | Keyed by analyzed category, each its findings' references when it has five or fewer, otherwise null. |
| `custom_rules` | array | yes | The custom-rule lines' rules, sorted. |
