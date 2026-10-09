# Code-review operations behavior contract

This is the behavior the four public skills of the `code-review-operations` bundle, and the review archive they share, promise to keep: what each produces, how the archive and its state behave, what reviewers may and may not do, and the role of each runtime. A change that breaks one of these promises is a breaking change to the suite. How to configure and run the suite is in [Code-review operations](code-review-operations.md). Fixtures and tests use repository-neutral identities only.

## Skill behavior

| Public skill | Preserved behavior | Authoritative output |
| --- | --- | --- |
| `review-prs` | Review open non-draft pull requests and merged pull requests after a watermark; review one exact configured pull request, draft or not, without enumeration; skip an unchanged reviewed head; allow explicit subset selection; preserve retry eligibility after partial failure. | Validated JSON/Markdown review pair and per-repository state. |
| `review-prs --re-review` | Re-review a previously reviewed pull request; require a changed head unless forced; compare every prior finding; create the next review version without overwriting history; never post to GitHub. | Versioned JSON/Markdown review pair. |
| `update-pr-tracker` | Track pull requests authored by, assigned to, or involving the configured user; place each in a counted section from the user's own GitHub review state; treat an update as unchanged only when every file the pull request changes is identical in content and file mode to what was reviewed, reading a commit's file modes only when its changed files already match the other commit's in path, status, previous path, and blob, and uncertain responses to requested changes as awaiting response; omit approved pull requests unchanged since approval; remove a row on the user's direct assessment without acting on GitHub; pin a pull request to a status of the user's own, in its own section, only through a validated change to `dashboard.status_overrides`; show missing/current/stale AI review status independently of section; never mark a review stale, or rewrite any row, when a GitHub call a comparison needs fails, but name each pull request whose comparison failed; optionally offer to generate missing or stale reviews; preserve user-authored dashboard content. | One marker-owned dashboard section. |
| `review-insights` | Filter reviews by explicit inclusive dates; aggregate severity/category themes; count findings later judged addressed or still present per reviewer, model, and category; prepare a sealed synthesis input of grouped findings, outcomes, every open flag in scope, and each repository's guidance files, and record an analyst agent's validated recommendations targeting those files or analyzer rules; record an accept/reject/defer decision per recommendation; retain reproducible evidence. | Versioned summary JSON plus Markdown projection; synthesis input, context, prompt, and result beside it. |
| `flag-review-finding` | Add, list, and resolve review-improvement observations with stable IDs and optional PR/finding association; refuse a finding the archive does not have; list a pull request's open findings under their report labels. | Locked structured flag store. |

Repository targeting is always one or more full `owner/repo` identities or a named configured set. Pagination must complete per repository. Authentication, rate limits, malformed responses, and unexpected API failures fail closed.

## Archive and state behavior

- Archive keys are `<owner>/<repo>/pulls/<number>`; repository short names are display-only.
- JSON is the machine source of truth. Markdown is a hash-linked projection.
- Review versions are allocated under a per-PR lock and never overwrite history.
- Merged-pull watermarks are independent per repository. Incomplete enumeration or a failed eligible merge cannot advance that repository past the missing work.
- A merged pull request is eligible when it merged on or after its repository's watermark date, judged by its merge time alone: one merged before the watermark stays out however recently it was updated (commented on, labeled, or edited) after merging. Because merging updates a pull request, every one merged on or after the watermark was last updated on or after it, so enumeration may stop listing closed pull requests, read from the most recently updated, at the first page whose last pull request was updated before the watermark. A repository without a watermark lists its whole history once.
- Mutable state, flags, and review versions use separate short-lived locks; network and semantic review work occurs outside those locks. The configuration file takes none: nothing in the suite changes it in place, and `review_config.py write` replaces it whole through a uniquely named temporary file, so a reader sees the old file or the new one, and of two writes at once the later one stays.

## Reviewer behavior

- The bundled generic reviewer and a repository's specialists work under the same prompt contracts, and each writes a [specialist result](#specialist-result), which the core validates and assembles into one adapter result. A repository's entrypoint reviewer is given the versioned request and writes the adapter result itself.
- The bundled generic reviewer's instructions and the adapter result schema resolve from the installed `code-review-core` skill, not from a repository checkout or branch.
- Re-review requires exactly one disposition for every prior finding, and every open or unverified entry of the pull request's finding ledger is a prior finding until a review closes it.
- A finding that repeats another one is linked to it with `repeats` and counted once, never twice. A link must name an existing finding at least as severe that is not itself a repeat.
- The core assigns stable finding IDs, keeps the finding ledger, calculates verdicts from its open entries, renders reports, and owns durable writes. An initial review starts a fresh ledger; a record written before ledgers stays valid and is read as having no history.
- Repository reviewers are loaded only from a trusted commit: an explicitly configured trusted ref, else the pull request's base, else, for a configured review skill the base predates, the default branch's tip as origin reports it. When that tip lacks the skill too, or is the pull request's head, the suite's generic reviewer reviews the pull request instead. The record's `adapter.source` names which ran. Every loaded file is declared, materialized, and hashed.
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
- The suite-owned generic reviewer and result schema are resolved in the installed `code-review-core` skill's `references/` directory. They never depend on a configured repository checkout, its current branch, or a hard-coded main-worktree path.

When configuration selects `copilot-cli`, the pipeline runs the bounded host in `code-review-core/scripts/review_hosts.py`, never Copilot directly. The host requires Copilot CLI 1.0.88 or newer, verifies the exact file set and hashes of every materialized reviewer resource, requires the diff and source snapshot to be inside the isolated run directory, runs with isolated `HOME`, `USERPROFILE`, `COPILOT_HOME`, and working directory values, disables ambient instructions and MCP servers, and grants write access only to its attempt's staging file, which the host renames to the result path once it is a JSON object and the role has not been set aside. `dispatch` starts the host detached and `wait` follows it, so no runtime's command time limit stops a review. Authentication tokens inherited from the invoking environment remain available by design, so treat this as configuration isolation rather than credential isolation.

## Runtime roles

| Runtime | Role | Trust boundary |
| --- | --- | --- |
| Claude Code | Native skill host and native reviewer delegation. | Authoritative rendered skill plus immutable materialized repository reviewer. |
| Codex | Thin runtime adapter and native agent delegation. | The adapter loads the authoritative skill; durable outputs still pass core validation. |
| GitHub Copilot CLI | Personal skill host, inline reviewer of the generic reviewer's and specialists' roles, and bounded non-interactive driver of a repository entrypoint reviewer. | Inline: the session's own tools and permissions, with the run's files sealed and every result validated. Host: no custom instructions, shell, URL, memory, or interactive question tools; JSONL is diagnostics only. |
| Copilot cloud agent | Not a local suite host. | Cannot advance local state or commit local records without a separately approved transport. |
| Copilot code review | External evidence source. | PR-head instructions are untrusted for the suite protocol. |

## Threat model

The code-review bundle is the one component of this repository that reads input an adversary writes. The adversary is a pull request's author: they control everything the pull request carries, and through it what a reviewer model reads. The configuration, the trusted reviewer commit, the archive, a canary's fixture, and the session running `review-prs` are trusted. Each row names what the author controls, what the suite guarantees against it, and the tests that hold the guarantee. Every guarantee fails closed: a file left out is an exclusion entry, a change no reviewer could see in full is a coverage gap that makes the verdict `INCOMPLETE`, a reviewer's call outside its role is denied, and anything else ends the pull request as one `FAILED <selector> <reason>` line with nothing recorded, never a traceback. `test_adversarial_inputs.py` holds at least one test for each row; validation fails when the table names a test that does not exist.

| The author controls | The suite guarantees | Held by |
| --- | --- | --- |
| Diff text and headers: every content line, and the header lines git writes for their file names. | The diff is data in `diff.patch` and the work files, never prompt text. A file block starts only at a `diff --git` line, and lines split only at LF, so a carriage return, U+2028, or any other line break inside content cannot start a header or a file. A header path no prompt can carry (a control character, U+007F, a line break, a backslash, or an absolute, empty, `.`, or `..` segment) is never parsed into a prompt, work file, or note: it is an unavailable source, the review is `INCOMPLETE`, and a pull request that changes only such files fails. Undecodable bytes become U+FFFD. | `test_adversarial_inputs.py::test_hostile_diff_lines_and_a_newline_path_become_one_exclusion_never_a_prompt_line`, `test_review_specialists.py::test_a_quoted_newline_path_cannot_inject_a_prompt_line`, `test_review_specialists.py::test_safe_path_rejects_each_control_and_line_breaking_character`, `test_review_pipeline_prepare.py::test_a_path_with_a_control_character_reaches_no_prompt_and_is_a_coverage_gap`, `test_review_core.py::test_the_diff_replaces_and_counts_each_undecodable_byte` |
| Paths and file names in the head's tree. | A path the snapshot cannot hold on Windows as it is named is an `unsafe-path` exclusion, never written, and a coverage gap only for a pull request that changes it: that review is `INCOMPLETE`, and every other review of the repository runs in full. `UNSAFE_PATH_RULES` in `review_runtime.py` holds the rules, each with its reason: bytes that are not UTF-8, named with U+FFFD for each byte; a reserved character (`:<>"\|?*`) or a control character; a backslash, which Windows reads as a folder separator; a segment ending in a dot or a space, which Windows drops; a reserved device name, with or without an extension (`CON`, `PRN`, `AUX`, `NUL`, `CONIN$`, `CONOUT$`, and `COM` or `LPT` followed by a digit, `¹`, `²`, or `³`), in any case; and the two length rules in the next row. The diff names a changed file exactly as the snapshot does, a trailing space included, so its exclusion is found. `validate-reviewer` counts these exclusions rather than failing. A name that would leave the snapshot or name its root (absolute, or with an empty, `.`, or `..` segment) fails the snapshot, and so do two names a case-insensitive file system would merge. | `test_adversarial_inputs.py::test_names_windows_cannot_hold_are_unsafe_path_exclusions_and_a_coverage_gap_only_where_changed`, `test_adversarial_inputs.py::test_each_rule_names_why_windows_cannot_hold_a_path_and_names_just_inside_are_kept`, `test_adversarial_inputs.py::test_names_a_case_insensitive_file_system_would_merge_fail_the_snapshot`, `test_review_core.py::test_an_undecodable_name_is_an_unsafe_path_and_a_coverage_gap_only_where_changed`, `test_review_specialists.py::test_a_name_ending_in_a_space_keeps_it_and_loses_only_the_tab_git_ends_it_with`, `test_review_pipeline.py::test_validate_reviewer_counts_names_windows_cannot_hold_as_unsafe_paths`, `test_review_followups.py::test_case_collisions_fail_closed` |
| Path length. | A segment over 255 UTF-16 code units, which no Windows file system holds, is an `unsafe-path` exclusion, and so is a path longer than the room the snapshot's folder leaves it. The folder and the path together may hold 259 units for a file and 247 for the folder it is in without long-path support (MAX_PATH less its terminating NUL, and less the 12 CreateDirectory keeps for an 8.3 name), and 32,507 for either with it (the 32,767 of an NT path less MAX_PATH left for the volume's device name, which Windows counts in place of the drive). The room is computed from the snapshot's own folder, and `validate-reviewer` measures at a folder as long as the one `prepare` creates. A path exactly at each limit is written. | `test_adversarial_inputs.py::test_paths_at_the_room_the_root_leaves_are_kept_and_one_unit_longer_are_unsafe_path_exclusions`, `test_adversarial_inputs.py::test_a_path_at_this_machines_limit_is_written_and_one_unit_longer_is_an_unsafe_path_exclusion`, `test_adversarial_inputs.py::test_the_limits_are_windows_path_limits_with_and_without_long_paths`, `test_adversarial_inputs.py::test_each_rule_names_why_windows_cannot_hold_a_path_and_names_just_inside_are_kept`, `test_review_pipeline.py::test_validate_reviewer_measures_at_a_path_as_long_as_the_source_folder_of_prepares_run` |
| Blob content: bytes, encodings, line endings, and text addressed to a reviewer. | The snapshot holds each kept file's exact committed bytes, hashed as written. A file with a NUL byte in its first 8,000 bytes is a `binary` exclusion. A file over the per-file limit (1 MiB, or 16 MiB for a changed file) is a `file-size-limit` exclusion and, when changed, a coverage gap. Content reaches reviewers only as files they read, never as prompt text. | `test_adversarial_inputs.py::test_hostile_blob_content_reaches_reviewers_only_as_exact_bytes_or_an_exclusion`, `test_review_core.py::test_source_snapshot_excludes_binary_and_oversized_files_from_limits`, `test_review_pipeline_prepare.py::test_a_changed_file_over_the_source_limit_is_kept_under_the_changed_file_limit` |
| `.gitattributes`: `export-ignore`, `export-subst`, `eol`, filters, and `binary`. | With `checkout_path`, the snapshot comes from git objects, which no attribute or configuration touches, so a file hidden from the diff or an archive is still in it with its exact bytes. Without one, GitHub's tarball is checked against the commit's tree, and any difference fails the pull request, naming the paths. | `test_adversarial_inputs.py::test_attributes_that_hide_or_rewrite_a_file_leave_the_snapshot_exact_or_fail_the_tarball`, `test_review_core.py::test_source_snapshot_holds_the_exact_blobs_whatever_attributes_and_configuration_say`, `test_review_followups.py::test_a_file_the_attributes_dropped_or_rewrote_fails_closed` |
| A change to a file the repository's `snapshot_exclude` matches. | The file is a `configured` exclusion on every route, never written, fetched, or searched, and a changed one is an unavailable source, so its review is `INCOMPLETE` while its reviewers still get its diff: an exclusion the maintainer sets for files reviewers rarely open cannot hide a change. `validate-reviewer` refuses a pattern that matches a file the reviewer declares. | `test_review_pipeline_prepare.py::test_a_configured_exclusion_leaves_files_out_and_a_changed_one_is_an_unavailable_source`, `test_review_pipeline_prepare.py::test_a_configured_exclusion_applies_to_the_tarball_and_a_changed_file_stays_a_coverage_gap`, `test_review_source.py::test_a_fetch_or_a_search_of_a_configured_path_is_refused_with_its_reason`, `test_review_followups.py::test_tarball_snapshot_leaves_out_configured_paths_and_a_changed_one_is_unavailable`, `test_review_pipeline.py::test_validate_reviewer_refuses_a_configured_exclusion_over_a_file_the_reviewer_declares` |
| Symbolic links, hard links, and other entries that are not regular files. | A symbolic link is a `symbolic-link` exclusion and any other such entry `non-regular`; none is written or followed. A tarball entry whose name leaves the snapshot fails the pull request with nothing written outside it. A link at the snapshot's reserved manifest path fails it too, and a link among a repository reviewer's declared files is refused before anything is materialized. | `test_adversarial_inputs.py::test_links_are_excluded_and_a_traversing_entry_fails_with_nothing_written_outside`, `test_review_core.py::test_source_snapshot_excludes_links_and_writes_nothing_for_a_submodule`, `test_review_core.py::test_source_snapshot_still_refuses_a_link_at_the_reserved_manifest_path`, `test_review_followups.py::test_tarball_snapshot_excludes_a_symbolic_link_and_a_fifo`, `test_review_core.py::test_git_symlink_entries_are_rejected_before_materialization` |
| The head moving during a review, and the snapshot changing before its reviewer starts. | `prepare` reads the pull request again after its diff and fails if the head or base moved, so a diff is never recorded under another head. The snapshot is the exact commit that diff belongs to, read by its SHA: a push after that check leaves it unchanged, and a force push that makes the commit unavailable fails the pull request. The Copilot CLI host starts its reviewer in a process of its own, so it checks the snapshot's structure again, which refuses a file added, removed, or replaced by a link since `prepare`, and compares a stamp `prepare` took: one SHA-256 over the manifest's bytes and the path, size, and modification time of each file `prepare` wrote. While the stamp matches, it re-hashes the files dated no earlier than the manifest and, on a lazy snapshot, each fetched file against its blob id; once it differs, it re-hashes every file, so a file changed since `prepare` fails the host before Copilot starts. | `test_adversarial_inputs.py::test_a_head_pushed_after_the_diff_is_read_is_snapshotted_exactly_or_fails`, `test_review_pipeline_prepare.py::test_a_push_while_preparing_fails_before_the_diff_is_written`, `test_review_pipeline_prepare.py::test_a_base_that_moves_while_preparing_fails_too`, `test_review_runtime_validation.py::test_a_file_changed_after_the_stamp_is_refused_by_the_full_pass`, `test_review_runtime_validation.py::test_a_file_added_after_the_stamp_is_refused_before_anything_is_read`, `test_review_runtime_validation.py::test_a_file_written_in_the_manifests_clock_tick_is_read_though_the_stamp_matches`, `test_review_runtime_validation.py::test_a_lazy_snapshots_fetched_files_are_read_whenever_they_are_held`, `test_review_hosts_copilot.py::test_a_file_changed_or_added_after_prepare_is_refused_before_copilot_starts`, `test_review_pipeline.py::test_a_snapshot_file_changed_after_prepare_fails_the_host_before_copilot_starts` |
| The size of the tree. | The snapshot holds at most 50,000 files and writes at most 256 MiB, and `prepare` fails and leaves no run on a tree over either limit by the route it takes. Written whole (GitHub's tarball, and the checkout for the Copilot CLI host or a routed condition that declares no `reads`), every file it keeps counts against both. Written lazily (the checkout otherwise), every file it can hold counts against the file-count limit when `prepare` lists it, and only the files it writes (the changed files, the analyzer settings, and the paths declared `reads` match) count against the size limit, so files a pull request leaves unchanged never fail it by their size; a fetch that would pass the size limit is refused. `validate-reviewer` measures the route `prepare` would take, applies the same limits, names the route, and names the largest directories of a snapshot it refuses. | Whole: `test_review_pipeline_prepare.py::test_the_size_limit_holds_for_the_files_each_route_writes`, `test_review_core.py::test_source_snapshot_materialization_enforces_file_count_limit`, `test_review_pipeline.py::test_a_whole_snapshot_over_the_size_limit_fails_naming_the_largest_directories`. Lazy: `test_adversarial_inputs.py::test_a_tree_over_the_size_limit_fails_prepare_and_leaves_no_run`, `test_adversarial_inputs.py::test_large_files_the_pull_request_leaves_unchanged_are_listed_never_written_or_counted_by_size`, `test_review_pipeline.py::test_a_lazy_snapshot_counts_only_the_files_it_writes_against_the_size_limit`, `test_review_pipeline.py::test_a_lazy_snapshot_over_the_size_limit_by_its_changed_files_fails_naming_the_largest_directories`, `test_review_pipeline.py::test_a_snapshot_over_the_file_count_limit_fails`, `test_review_pipeline.py::test_the_lazy_measurement_matches_the_lazy_snapshot_prepare_writes`, `test_review_source.py::test_a_lazy_snapshot_lists_no_more_paths_than_the_file_count_limit`, `test_review_source.py::test_the_snapshot_size_limit_holds_for_fetches` |
| Paths a reviewer asks for on a lazy snapshot, and the patterns it searches the head with. | On the checkout route, unless the reviewers cannot run a command (the Copilot CLI host) or a condition script of a routed specialist declares no `reads`, `prepare` writes only the changed files, the analyzer settings, and the paths the `reads` of the conditions it runs match, which those scripts read in place and no other path, and the manifest lists every other path the snapshot would hold under the rows above, by name, kind, and size, with its blob id. `source-file` in `review_source.py` looks a path up in that manifest and never makes a file name of what the reviewer passes: a path the head does not hold as a listed file, in any spelling (`..`, absolute, another case, a folder, a file only the base holds), fails with nothing written, and an excluded path is named with its reason. The blob is read from the checkout's object store by its id and must hash to it; a NUL byte in its first 8,000 bytes makes it `binary`, never written; and it is written into a folder checked for reparse points and escape, where verification holds it to its id. `source-search` runs `git grep` at the head commit and prints only matches in paths the snapshot can hold, so an agent-instruction file stays out, and only in files the same NUL-byte test finds text, so no `.gitattributes`, `info/attributes`, or `core.attributesFile` entry in the checkout hides a file from it or opens a binary one to it. The guard allows the two commands only for the reviewer's own run and role, with a path or pattern that holds no quote, backtick, dollar sign, backslash, or line break, so it never leaves its quotes. | `test_adversarial_inputs.py::test_a_lazy_snapshot_reviewer_obtains_only_files_the_head_commit_holds`, `test_review_source.py::test_a_path_outside_the_commit_is_refused_with_nothing_written`, `test_review_source.py::test_an_excluded_or_binary_path_is_named_with_its_reason_and_never_written`, `test_review_source.py::test_a_blob_the_checkout_answers_wrongly_is_refused`, `test_review_source.py::test_a_fetched_file_that_changed_or_a_file_no_manifest_lists_fails`, `test_review_source.py::test_a_reparse_point_in_the_snapshot_refuses_every_fetch`, `test_review_source.py::test_the_search_covers_the_commit_and_leaves_out_what_the_snapshot_excludes`, `test_review_source.py::test_the_search_judges_each_file_by_its_bytes_whatever_the_checkout_sets`, `test_review_source.py::test_a_binary_file_the_manifest_lists_or_excludes_is_never_searched`, `test_review_guard.py::test_bash_runs_the_source_commands_of_its_own_role_and_run_with_a_path_or_pattern_kept_in_quotes`, `test_review_pipeline_prepare.py::test_a_condition_that_declares_no_reads_reads_the_whole_snapshot`, `test_review_pipeline_prepare.py::test_a_condition_that_declares_its_reads_keeps_the_snapshot_lazy_and_decides_as_on_the_whole_one`, `test_review_pipeline_prepare.py::test_a_declared_condition_is_not_given_a_path_it_does_not_declare`, `test_review_pipeline_prepare.py::test_a_declared_read_the_configured_exclusions_match_is_never_written`, `test_review_pipeline.py::test_a_condition_that_declares_its_reads_decides_on_the_snapshot_a_review_gives_it`, `test_review_pipeline_prepare.py::test_an_entrypoint_needs_no_agent_delegation` |
| Agent-configuration paths: instruction files, agent and skill folders, and settings, in any case and at any depth. | Each is an `agent-instruction` exclusion, never written, so no reviewer loads the pull request's own instructions; reviewers judge such a change from the diff. A repository reviewer's files come only from the trusted commit, never the head, and a trusted ref that resolves to the head is refused. | `test_adversarial_inputs.py::test_agent_configuration_in_the_head_reaches_no_reviewer_in_any_spelling`, `test_review_core.py::test_source_snapshot_uses_exact_head_and_excludes_agent_instructions`, `test_review_core.py::test_reviewer_is_loaded_from_trusted_commit_not_head`, `test_review_core.py::test_pull_ref_head_and_remote_mismatch_are_rejected` |
| A review skill or reviewer file the pull request adds or rewrites. | A repository reviewer is read from a trusted commit only: the configured `trusted_ref`; else the pull request's base; else, when a configured review skill is not at the base, the default branch's tip as origin reports it, which holds only what the repository merged. A tip that also lacks the skill, or that is the pull request's head, runs the suite's generic reviewer instead, so a skill file only the head holds never reviews it. The record's `adapter.source` and a `NOTE` name the fallback, and `validate-reviewer --pull` resolves the reviewer the same way and prints that source and note. With a `trusted_ref` or a `manifest_path`, a missing file fails the pull request as before. | `test_adversarial_inputs.py::test_a_review_skill_only_the_head_holds_never_reviews_the_pull_request`, `test_review_pipeline_prepare.py::test_a_skill_only_the_head_holds_is_never_the_reviewer`, `test_review_pipeline_prepare.py::test_a_skill_the_base_predates_is_read_from_the_default_branch_tip`, `test_review_pipeline_prepare.py::test_a_skill_neither_the_base_nor_the_tip_has_runs_the_generic_reviewer` |
| The reviewer's own behaviour under hostile content. | In Claude Code the reviewer guard binds each `code-review-reviewer` to one role of one run: it reads only that run and the suite's references, writes only that role's result under any spelling of another path, and runs only that role's self-check and, for a lazy snapshot, its two source commands, with no quoted script or run ending in a backslash, which would escape its closing quote. The Workflow script never reruns a role without the guard. The Copilot CLI host grants a write to its staging file only. The core validates every result against the request and computes the verdict itself, so a persuaded reviewer can omit a finding but cannot clear a coverage gap. | `test_adversarial_inputs.py::test_a_reviewer_cannot_write_or_run_outside_its_result_by_spelling_the_path_another_way`, `test_review_guard.py::test_a_reviewer_writes_only_its_own_roles_result_in_its_own_run`, `test_review_guard.py::test_reads_are_allowed_only_inside_its_own_run_or_the_references`, `test_review_guard.py::test_a_reviewer_reads_nothing_before_its_prompt`, `test_review_guard.py::test_without_an_agent_id_every_guarded_call_is_denied`, `test_review_guard.py::test_bash_runs_only_the_self_check_of_its_own_role_and_run_with_every_value_kept_in_quotes`, `test_review_pipeline.py::test_one_script_starts_every_role_of_several_runs`, `test_review_hosts_copilot.py::test_a_review_runs_version_then_review_then_promotes`, `test_review_core.py::test_adapter_result_rejects_unsafe_paths_and_mismatched_sha` |
| The reviewer's own behaviour under hostile content, when it runs inline. | Where the session cannot start subagents (Copilot CLI) or `prepare --inline` asks for it, the orchestrating session works each role itself, one at a time as `next-role` hands it out, from the same prompt, with the same exclusions, as a subagent would. No hook confines it, so the run is sealed instead: `prepare` records the SHA-256 of every run file it wrote except `run.json`, the source snapshot, and the results (the request, the diff, the plan, every prompt and work file, and the materialized reviewer), and `next-role` seals each result it moves past. `next-role`, `check`, and `finalize` fail the pull request, recording nothing, when a sealed file changed or is gone, so a later role cannot rewrite an earlier role's result, clear a coverage gap or prior finding from the request, or rewrite the next role's instructions unnoticed. The core validates every result and computes the verdict as in every mode. A specialists manifest that lists `agent-delegation` never runs inline. | `test_adversarial_inputs.py::test_an_inline_reviewer_cannot_clear_a_coverage_gap_or_rewrite_its_inputs_unnoticed`, `test_review_pipeline.py::test_a_later_role_that_rewrites_an_earlier_result_fails_the_pull_request`, `test_review_pipeline.py::test_check_sets_aside_an_invalid_inline_result_and_next_role_hands_it_out_again`, `test_review_pipeline.py::test_manifests_with_and_without_agent_delegation_run_as_the_dispatch_allows`, `test_review_core.py::test_an_inline_review_offers_only_reading_the_diff_and_writing_the_result` |
| Concurrent reviews of the same pull request. | `finalize` records only on top of the archive version `prepare` saw, and allocates that version under the pull request's lock, so of two sessions finalizing at once exactly one records and the other fails with the archive untouched and its run still unfinalized. | `test_adversarial_inputs.py::test_two_sessions_finalizing_the_same_pull_request_record_exactly_one_version`, `test_review_pipeline.py::test_finalize_fails_closed_when_another_version_was_recorded_after_prepare`, `test_review_core.py::test_versions_are_allocated_under_lock`, `test_review_core.py::test_recent_lock_of_live_owner_times_out_and_is_kept`, `test_review_core.py::test_release_succeeds_while_a_waiter_reads_the_owner_file` |
| A fixture directory given to `prepare --canary --fixture`, which no pull request's author writes. | A fixture is trusted input, as the configuration is: it lives under `tests/fixtures/`, which validation keeps from shipping. It takes no shortcut past the snapshot: `prepare` commits its two trees byte for byte to a throwaway repository whose origin names the fixture's repository, and snapshots the head from it as it snapshots a checkout's, so every exclusion above applies; that repository moves into the run, where a lazy snapshot's reviewers fetch from it, and goes with the run when `finalize` removes it; a link or another entry that is not a regular file or folder fails it. `pull.json` and a re-review's prior record are validated before anything is written. Nothing is read from GitHub, the suite's generic reviewer reviews it, and the pair is written only under a new canary root. | `test_adversarial_inputs.py::test_a_fixture_is_snapshotted_as_a_pull_requests_head_and_reads_nothing_from_github`, `test_review_pipeline.py::test_a_fixture_with_a_planted_defect_runs_through_every_step_with_no_gh_call`, `test_review_pipeline.py::test_a_prior_record_that_is_not_this_pull_requests_first_review_fails_with_nothing_prepared`, `test_review_canary.py::test_a_link_or_special_file_in_a_tree_is_refused`, `test_review_canary.py::test_both_trees_are_committed_byte_for_byte_whatever_their_attributes_say` |

The model does not cover these, by design:

- **A reviewer persuaded to miss a problem.** Hostile content can still talk a reviewer out of a finding; no check can tell an honest "no findings" from a persuaded one. The verdict, the ledger, and the coverage gaps stay the core's.
- **Reviewers outside the guard.** Codex reviewers, and the general-purpose subagent `review-prs` falls back to when a session lacks the `code-review-reviewer` type, run without a hook, so their prompt is their only boundary.
- **Inline reviewers.** An inline reviewer is the orchestrating session itself, so it keeps that session's tools, permissions, instruction files, and context: no hook confines what it reads, writes, or runs, and hostile content it reads reaches a session that can do whatever that session is allowed. Each role also sees the roles before it in its context. The seal catches a changed run file, but not one rewritten together with the digests in `run.json`, and the source snapshot is not hashed again between roles, because hashing a large one takes minutes. Run inline reviews from a directory outside the checkout, in a session allowed nothing beyond what the pipeline's commands need.
- **The guard's agent ID.** The guard tells parallel reviewers apart by the `agent_id` Claude Code puts in a subagent's hook event, which is observed rather than documented. Without it, every guarded call is denied and reviews fail visibly.
- **The same user.** Run folders and the guard's claims live in the per-user temporary directory, and authentication tokens stay available to reviewer hosts, so a process running as the user is trusted. The Copilot CLI host's stamp, for one, does not see a change that keeps a file's size and modification time, which takes setting the time back or a writer still holding the file open when the host lists its folder.

## Formats

These tables are the single statement of every structured file the suite keeps between operations or takes from a reviewer, but one. That one is `review-insights`' own versioned report, `insights.json`, with the synthesis files `report` writes beside it: that skill alone writes them, and `load_report` in `review_insights.py` alone reads the report back, upgrading a report of any earlier version (1 to 6) to the current one, 7. "Synthesis" in [Code-review operations](code-review-operations.md#synthesis) describes them. Each format names the function that validates it, and `tests/code-review/test_format_contract.py` fails when a table and that function disagree on the suite's fixtures. The adapter result, which a repository's entrypoint reviewer writes and the core assembles from specialist results, is stated instead by `skills/code-review-core/references/review-adapter.schema.json`, because an entrypoint's author reads that file; the same suite checks it against `validate_adapter_result` in `review_records.py`. A file one operation writes and reads before it ends, such as a run's `run.json`, plan, prompts, and work files, the batch file, and the tracker input, is not stated here: the script that writes it is the only reader. How to write and use each file is in [Code-review operations](code-review-operations.md).

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
| `snapshot_exclude` | array | no | Glob patterns of head files to leave out of the source snapshot, at most 100 distinct ones ignoring case, each 1 to 200 characters with no backslash, control character, or empty, `.`, or `..` segment. Each matches a whole path relative to the repository root, ignoring case: `*`, `?`, and `[...]` within one segment, and a `**` segment any number of segments, none included. A matching file is a `configured` exclusion on every route. Defaults to `[]`. |

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
| `status_overrides` | object | no | Keyed by `owner/repo#number`. Each value is a non-empty status shown for that pull request, and may not be a computed one (`to review`, `awaiting response`, `my prs`, `my pull requests`, `drafts`, `missing`, `current`, or `stale`, in any case). |
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
| `uncovered` | string | no | One of `review` or `ignore`. What happens to changed files no specialist matches when some do: `review`, the default, gives them to the generic reviewer, and `ignore` leaves them unreviewed and lists them in the record. |
| `finding_categories` | array | no | Distinct one-line names (matched without regard to case) of at most 60 characters, without backticks, quotes, or pipes; at least one. When given, every role names one per finding as its `category`, which the record keeps; without it, a finding's category is its specialist's `category`. |
| `fallback_finding_category` | string | no | A name that `finding_categories` lists, such as Other: the category reviewers are told to use only when no other fits. A finding must still name a listed category. |

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
| `reads` | array | no | The snapshot paths the script opens besides the changed files: at most 100 glob patterns, distinct ignoring case, each 1 to 200 characters, repository-relative, without a backslash, a control character, or an empty, `.`, or `..` segment, matched as a repository's `snapshot_exclude` is. When every condition a review runs declares it, the checkout route's snapshot stays lazy and also holds the paths they match that `snapshot_exclude` does not, and the script reads no other; without it, the snapshot is written whole. May be empty. |

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
| `manifest_path` | string | yes | Absolute path of the snapshot's manifest, which lists every file it holds and its hash and, for a lazy snapshot, every other file it can hold and its blob id. |
| `source_commit` | string | yes | The commit it holds: the head commit. |

The snapshot's manifest records each path it leaves out under `excluded_paths`, with one reason: `agent-instruction`, `binary`, `configured` (a regular file the repository's `snapshot_exclude` matches, changed or not), `file-size-limit`, `unsafe-path` (a path Windows cannot hold as it is named; the "Threat model" section lists the rules), `symbolic-link`, or `non-regular` (any other entry that is not a regular file or a directory, such as a hard link, FIFO, or device). Only `configured`, `file-size-limit`, and `unsafe-path` are coverage gaps, which put a changed file in `coverage.unavailable_sources`, so the author of a pull request cannot hide a change behind the maintainer's exclusion: the review is `INCOMPLETE`, and its reviewers still get the change's diff. The others are deliberate, and reviewers judge those files from the diff. A lazy snapshot lists no excluded path under `fetchable`, so `source-file` names it with its reason and writes nothing, and `source-search` leaves it out.

#### Request coverage (`request.coverage`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `unavailable_sources` | array | yes | Changed files the snapshot could not hold, for size, an unsafe name, or the repository's `snapshot_exclude`, and changed paths no reviewer prompt can carry safely. |

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
| `category` | string | when the specialists manifest declares `finding_categories` | One of those categories, matched ignoring case. Without them, the role's own category is recorded and this one is not read. |
| `body` | string | yes | The problem. Not blank. |
| `analyzer` | object | no | How a diagnostic analyzer could catch it instead. |
| `repeats` | integer or string | no | The finding it repeats: the 0-based index of another finding in this result, or the ID of a prior finding this result judges `still_present` or `partially_addressed`. At least as severe, and not itself a repeat. |

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
| `dispatch` | string | no | One of `subagents`, `copilot-host`, or `inline`. How the reviewers were worked: as subagents (or by a Claude Code Workflow), by the bounded Copilot CLI host, or inline by the orchestrating session. Absent from older records. |
| `snapshot` | object | no | The [source snapshot](#source-snapshot-measured-recordreviewsnapshot) the reviewers read: where it came from, how large it was, and how long `prepare` took around it. Absent from older records. |

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
| `seconds` | integer | no | Whole seconds from handing it the role to its accepted result, reruns included, and 0 for a result dated less than a second before it was handed out; absent when not timed or when the result is dated a second or more before. |
| `files_read` | integer or null | with `bytes_read` | How many distinct files of the source snapshot it opened, with Read or a Grep of one file, or fetched or found with `source-file`, as the reviewer guard logged them, reruns included. Null when no guard counted its reads (an inline, Copilot CLI host, Codex, or general-purpose fallback reviewer), which means unknown, not zero. Absent from older records. |
| `bytes_read` | integer or null | with `files_read` | Those files' total size in bytes, each counted whole whatever part of it was read; null together with `files_read`. |

#### Source snapshot measured (`record.review.snapshot`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `source` | string | yes | One of `checkout`, `checkout-lazy`, or `tarball`. Where the snapshot came from: the configured checkout's git objects (a fixture canary's throwaway repository counts as a checkout), written whole, or `checkout-lazy`, its changed files and analyzer settings written and every other file fetched when a reviewer asked for it; or GitHub's tarball. |
| `files` | integer | yes | How many files it held when `prepare` wrote it, its own manifest left out. Not negative, as for each count here. |
| `bytes` | integer | yes | Their total size in bytes. |
| `excluded` | object | no | Keyed by exclusion reason, a lowercase name of at most 40 letters and hyphens, as the snapshot's manifest records it under `excluded_paths`. Each value is the positive number of the head's paths left out for that reason, every path the head holds counted on every route. An empty object when nothing was left out. Absent from older records. |
| `seconds` | object | yes | How long each [phase](#snapshot-phase-seconds-recordreviewsnapshotseconds) of `prepare` took. |

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
| `name` | string | yes | The configured reviewer's ID. |
| `protocol_version` | integer | yes | One of `1`. |
| `scope` | string | yes | One of `generic` or `repository`. |
| `source` | string | no | One of `generic`, `trusted-ref`, `base`, `default-branch`, or `generic-fallback`. Where the reviewer came from: with scope `generic`, the suite's generic reviewer the repository is configured with (`generic`) or one that ran in place of a configured review skill the base predates and the default branch's tip could not supply (`generic-fallback`); with scope `repository`, the commit `source_commit` names, which is the configured trusted ref, the pull request's base, or the default branch's tip when the base predates the review skill. Absent from older records. |
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

#### Analyzer coverage (`record.findings[].analyzer`, `specialist-result.findings[].analyzer`)

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
| `body` | string | yes | The thread's first comment. |
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
| `finding_id` | string or null | yes | The finding it names in that review version, or null. `flag-review-finding add` writes only a finding ID that review has; its form is not checked when read, so an older flag's ID loads and links to nothing. |
| `category` | string | yes | Its category. Not blank. |
| `body` | string | yes | The observation. Not blank. |
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
| `merged_since` | string | no | The merged-pull watermark: pull requests merged on or after this date are eligible. Its first ten characters are a `YYYY-MM-DD` calendar date, and only they are read; `validate_state` refuses any other value, so a state that holds one fails every operation that reads it. Absent until a batch run first advances it. |
| `updated_at` | string | no | The date `advance` last moved the watermark. |

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

### Fixture pull request

A fixture canary, `prepare --canary --fixture <directory>`, reviews a fixture directory in place of a pull request: `base/` and `head/` hold the change as two trees, and `pull.json` the pull request around it, validated by `validate_fixture_pull` in `review_canary.py`. `prepare` commits each tree's files, byte for byte, to a throwaway repository, and takes the base and head commits and the diff from it; a fixture tree holds only regular files and folders, at paths git accepts in an index (none inside `.git`, for one). A re-review canary's prior record is a `record`, the pull request's first review. See "Fixture canaries" in [Code-review operations](code-review-operations.md#fixture-canaries).

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

#### Fixture thread (`fixture-pull.threads[]`)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `author` | string | yes | The person who started the thread. Not blank. |
| `path` | string | yes | The file it is on. Not blank. |
| `line` | integer or null | yes | Its line, from 1, or null when it has none. |
| `outdated` | boolean | yes | Whether the code it was on has changed since. |
| `body` | string | yes | The thread's first comment. Not blank. |
| `url` | string | yes | Its web address. Not blank. |
