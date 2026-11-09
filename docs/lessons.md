# Lessons

This catalogue records the design patterns the repository learned from the defects and drift found after a change
merged. Each pattern states a rule, the mechanism that applies it, the guard that refuses a violation, and the forms a
violation takes, so a contributor or agent learns which rule to follow, which helper to call, and what to search for,
not which issues happened.

## Using the catalogue

A defect or drift found after merge is an instance of a pattern. The pull request that fixes it cites the pattern it
violated and runs that pattern's **Search for** across the repository, fixing or filing each hit, or, when no pattern
covers it, adds one here, as [Validation](implementing-changes.md#validation) in the implementation profile and the
release procedure in [releasing.md](releasing.md) say. A fix that adds a mechanism or a guard adds it to its pattern.

Each pattern is a `###` heading under [Patterns](#patterns) that names it, followed by these fields, each a list item
of its own:

- **Rule:** the pattern, in one sentence.
- **Why:** the behavior it protects, in a line.
- **Use:** the mechanism that applies the rule, each in a code span as a module and a name it binds,
  `<module>:<name>`, or a name one of its classes binds, `<module>:<class>.<name>`, the form `pkgutil.resolve_name`
  reads. The module is importable from the repository root, such as `deployer.config`, or from one skill's or
  repository skill's `scripts`, such as `json_reader` in skill-core's, the directories a part of the repository puts
  on `sys.path`; a module name more than one of them holds is ambiguous and fails.
- **Guard:** each guard that refuses a violation, in a code span by its path from the repository root: a test, which a
  validation policy is too, by its test ID, `<path>::<test>`, `<path>::<class>`, or `<path>::<class>::<test>`; a step
  of a skill, `<path>/SKILL.md#<fragment>`, by the fragment GitHub gives its heading; or a workflow,
  `.github/workflows/<file>`.
- **No guard:** in place of **Guard**, the reason no guard refuses a violation yet, and what holds the rule instead.
- **Search for:** the forms a violation takes, each in a code span a reader can search for, with the context that
  tells a violation from an innocent use.
- **Examples:** optional, issues or pull requests that show the pattern. They illustrate it; nothing counts them.

`lessons_ledger_problems` in `tests/validation/markdown_links.py` fails a pattern that states no **Rule** or **Why**,
names no **Search for** form, names neither a **Guard** nor a **No guard** reason, even when it names a mechanism under
**Use**, or names both a guard and a reason, and each mechanism or guard that does not resolve, so one renamed or
removed fails validation until its pattern follows it.

## Patterns

### A failure is never read as absence

- **Rule:** A reader that cannot tell whether something is there reports that it could not tell, never that it is
  absent or that nothing was wrong.
- **Why:** An unreadable path, a failed command, or a git error read as "not found" otherwise reads as a clean pass.
- **Use:** `file_presence:is_present`, which tells a file, nothing, and something else apart, and lets a stat that
  fails with anything but "not found" through as the error the stat raised; `file_presence:is_directory`, which
  answers the same for a folder; `file_presence:is_occupied`, which answers whether anything is at a path, a link
  to nothing included, in place of lexists; `file_presence:matching`, which lists what a glob pattern matches but
  raises for a folder it cannot list;
  `link_probe:is_link_or_junction`, which lets the error of an unreadable path through; and
  `deployer.source_ref:TagsUnreadable`, which keeps git's reason when the tags cannot be read; and git's found,
  absent, and could-not-tell lookups, `git_client:GitClient.rev_parse_paths` for a path query,
  `git_client:GitClient.commit_of` for a ref, `git_client:GitClient.file_at` for a file at a commit, and
  `git_client:GitClient.quiet_output` for any command run quiet, each None only for what git reports as absent.
- **Guard:** `tests/validation/python_checks.py::PythonChecksPolicies::test_file_presence_is_the_one_reader_of_absence`,
  which refuses a presence predicate whose False is the answer, a glob, an os.walk with no onerror, and a removal
  that discards its errors, outside file_presence unless its module states why,
  `tests/deployer/test_invariants.py::TreeStateTests::test_a_folder_that_cannot_be_listed_fails_the_state_and_a_root_nothing_is_at_holds_nothing`,
  which holds the deployer harness's walk, in the tests that policy leaves out, to failing on a folder it cannot list,
  `skills/ejryan89-skill-core/scripts/test_file_presence.py::MatchingTests`,
  `tests/tools/test_hub_guard.py::UnstatablePlaceTests`,
  `tests/validation/python_checks.py::PythonChecksPolicies::test_no_git_exit_status_is_read_outside_the_client`,
  which refuses a git exit status read outside the git client unless its module states why,
  `skills/ejryan89-skill-core/scripts/test_git_client.py::ThreeAnswerTests`,
  `skills/audit-skill/scripts/test_skill_inventory.py::LocateTests::test_a_checkout_git_cannot_read_fails_naming_git_instead_of_searching_no_repository`,
  `tests/validation/python_checks.py::PythonChecksPolicies::test_link_probe_is_the_one_link_test`,
  `tests/validation/python_checks.py::PythonChecksPolicies::test_no_process_runner_result_is_discarded_unchecked`,
  `tests/tools/test_mutation_gate.py::test_a_failed_or_timed_out_coverage_step_names_the_step_and_its_output`,
  `tests/deployer/test_deploy_at.py::DeployAtTests::test_a_pin_whose_tags_git_cannot_read_is_refused_with_gits_reason`,
  `tests/deployer/test_deploy_at.py::RefValidationTests::test_only_gits_quiet_answer_is_a_ref_that_names_no_commit`,
  `tests/tools/test_hub_guard.py::UnreadableCheckoutFileTests`,
  `tests/tools/test_hub_guard.py::PlaceLookupTests`,
  `skills/ejryan89-skill-core/scripts/test_file_presence.py::FilePresenceTests::test_a_stat_that_fails_with_anything_but_not_found_reaches_the_caller`,
  `skills/ejryan89-skill-core/scripts/test_file_presence.py::EveryHelperTests`,
  `tests/deployer/test_presence_reads.py::PresenceReadTests`,
  `tests/deployer/test_state_files.py::ReaderTests::test_a_state_file_whose_stat_fails_is_refused_with_the_reason_not_read_as_absent`,
  `skills/ejryan89-code-review-core/scripts/test_review_source.py::SearchErrorTests`,
  `tests/malformed_input/test_faults_unreadable.py::test_each_command_refuses_a_file_it_reads_that_it_cannot_read`,
  `tests/malformed_input/test_faults_directory.py::test_each_command_refuses_a_directory_in_a_file_s_place`,
  `tests/malformed_input/test_faults_missing.py::test_each_command_refuses_a_missing_file_it_reads`,
  `tests/malformed_input/test_faults_stat.py::test_each_command_refuses_a_file_whose_stat_fails`, and
  `tests/malformed_input/test_faults_programs.py::test_each_command_refuses_a_git_or_gh_that_fails`,
  which run every command each area's driver table drives, with a file it reads unreadable, a directory, missing, or
  unstatable, and with git exiting 128, git timing out, and gh failing, and fail a run that exits 0 or never names
  what failed; a command a table names in NOT_RUN, with its reason, is not run, and
  `tests/malformed_input/test_faults.py::test_every_command_of_every_entry_point_is_driven_or_named_not_run` fails a
  command neither driven nor named
- **Search for:** `returncode == 0` or `returncode != 0` beside a `git` or `gh` command outside `git_client.py` and
  `github_client.py`, including a git the deployer runs through `platform_support.run_tool`, which reaches
  `GitClient` only through a runner over `run_tool`; `except GitError:` followed by `return None` or `return False`; `except OSError:` followed by
  `return False`, `return None`, or `return []`; `rg -n "if not \w+(\.\w+)*\.(is_file|exists)\(\):\s*$" -A1`
  followed by a `return None`, `return []`, or `return False`, where `is_present` should probe, and its expression
  form, `... if path.is_file() else None`, its loop forms, `if not path.is_file(): continue` and
  `while not path.is_dir():`, its return forms, `return path.exists()` and `if path.is_file(): return path`
  falling through to `return None` or the end of the function, its parenthesised form,
  `if not (path.is_file() and ...): continue`, its filter forms,
  `[e for e in entries if e.is_file()]`, `any(p.exists() for p in paths)`, and `filter(os.path.isfile, paths)`,
  and an emptiness check that passes a folder it cannot stat by, `path.exists() and any(path.iterdir())`; a
  `glob` or `rglob`, which matches nothing in a folder it cannot list from Python 3.15, an `os.walk` with no
  `onerror`, and an `rmtree(ignore_errors=True)` or `TemporaryDirectory(ignore_cleanup_errors=True)`, which reads a
  folder it could not remove as gone; `exists()`, `is_file()`, `is_dir()`, `os.path.exists`, `os.path.lexists`,
  `os.path.isfile`, or `os.path.isdir` anywhere a False must mean nothing is there, since from Python 3.14 each
  returns False on every `OSError`, and `lexists` on a `ValueError` too; `if not result.stdout`
  read as nothing found; `len(lines) != ` beside a successful git answer followed by `return None`; two branches
  that catch the same `GitError` and read its kinds differently; a function given a new `raise GitError` whose
  callers are not each read for where the error goes, one that catches it on one branch and lets it escape on
  another among them; `returncode != 0` followed by `return None` where git's error is an answer about a place
  other than absence, such as a start inside a git directory, which has no work tree to show; `.wait()` or a
  `returncode` of a command that reports a per-file error on stderr and goes on, such as `git grep`, read as the whole
  answer with its stderr unread, where `status not in {0, 1}` makes an exit 1 with an error "no match"; in a shipped
  `.sh`, `rg -n '\$\(git |git .*\| *tr ' skills --glob '*.sh'`, a git answer taken through a command substitution or a
  pipeline whose status no branch reads, which a script without `set -e` reads as an empty answer, and a
  `rev-parse --verify --quiet` whose exit 1 is not told from any other failure.
- **Examples:** #1320, #1365, #1599, #1601, #1887, #1888, #2090, #2091, #2131, #2133, #2182, #2185, #2205, #2218, #2236,
  #2245

### Evidence that was never produced is never a pass

- **Rule:** A guard, gate, verdict, count, or measurement whose evidence never existed reports that case apart from a
  pass.
- **Why:** A step that never ran, a listing that failed, or an empty input otherwise reads as everything checked and
  nothing found.
- **Use:** `conditions:MissingEvidence`, `deployer.discovery:ListingError`, `tools.hub_guard:Reading.time_left`, the
  hub guard's own reading time, below the hook timeout, so a command it cannot finish reading is refused with a reason
  rather than blocked by a timed-out hook, `tools.skill_evals_results:kept_models`, the models whose committed results
  row a write keeps because no record of theirs counted
- **Guard:** `skills/ejryan89-code-review-core/scripts/test_review_pipeline.py::test_finalize_refuses_a_result_the_guard_never_claimed_with_nothing_recorded`,
  `tests/tools/test_skill_evals.py::EvaluateTests::test_a_write_keeps_the_committed_row_of_a_model_no_record_of_which_counted`,
  `tests/tools/test_skill_evals.py::EvaluateTests::test_a_write_in_which_no_record_counted_keeps_every_committed_row`,
  `skills/audit-ai-config/scripts/test_audit_ai_config.py::test_a_repository_whose_files_show_nothing_says_the_checks_did_not_run`,
  `tests/tools/test_hub_guard.py::test_an_event_that_names_no_command_or_file_is_allowed_with_a_note_never_unseen`,
  `tests/tools/test_hub_guard.py::ReadingBoundTests`,
  `.claude/skills/evaluate-skill/SKILL.md#5-read-the-sampled-transcript`
- **Search for:** `rg -n "os\.walk\(" | rg -v onerror`, a walk that skips what it cannot read; `if not <input>:`
  followed by `return True` or `return 0`; a count, verdict, or summary computed over a list that is empty because its
  step never ran; a skill-evaluation scenario that holds `no_finding_above` and no `verdict`; `"timeout"` in a hook
  of `.claude/settings.json` whose program has no bound of its own below it, or reads a part of its input more than
  once per level of nesting, as `for _ in range(2)` over a child does.
- **Examples:** #1443, #1462, #1603, #1805, #1821, #1824, #1854, #2432, #2203

### Evidence a store records leaving out is withheld, not missing

- **Rule:** A reader of a store that records what it leaves out, and why, reads that record before calling an absent
  file missing: a file the record names is reported with its reason and judged another way, and only a file it does
  not name is missing.
- **Why:** Without the record, a deliberate exclusion either fails the run as missing evidence or, softened to skip
  every absent file, hides a file that should have been there.
- **Use:** `conditions:snapshot_exclusions`
- **Guard:** `.claude/skills/review-repository/scripts/test_review_repository.py::SnapshotTests::test_a_changed_script_the_snapshot_excludes_opens_the_route_with_its_reason_and_the_rest_still_run`,
  `.claude/skills/review-repository/scripts/test_untested_functions.py::ConditionTests::test_a_changed_script_the_snapshot_records_as_excluded_opens_the_route_for_review_from_the_diff`
- **Search for:** `rg -n "\.(is_file|exists)\(\)" .claude/skills/review-repository/scripts`, a condition testing a
  snapshot path without `snapshot_exclusions`; a glob over the snapshot for paths its exclusions always leave out, such
  as anything under `.claude/`.
- **Examples:** #1918

### A failure is reported as itself

- **Rule:** A failure names its own cause and the step that stopped, and ends with the exit code its contract gives
  it, never a neighbour's message, a traceback, or a generic code.
- **Why:** A failure reported as another one sends its reader to fix the wrong thing, and a caller that reads exit
  codes takes the wrong branch.
- **Use:** `console:run_main`, `console:failed_line`, `skill_roots:MissingSibling`; an entry point states its codes
  in its docstring's exit table, in the form "Script results" in the skill contract settles.
- **Guard:** `tests/malformed_input/test_malformed_input.py::test_failure_names_a_write_before_a_refusal_an_escape_and_an_undocumented_acceptance`,
  `tests/validation/skill_grants.py::SkillGrantsPolicies::test_skill_markdown_names_every_status_line_its_scripts_print`,
  `tests/tools/test_mutation_gate.py::CoverageReachFixtures::test_no_data_is_read_as_reaching_nothing_only_from_coverage_json_exiting_1_with_that_message_alone`,
  `skills/repo-cleanup/scripts/test_repo_cleanup.py::SyncTests::test_each_stop_is_returned_as_the_state_it_stopped_in_never_read_back_from_the_lines`,
  `skills/repo-cleanup/scripts/test_repo_cleanup_stops.py::StopAndRefusalTests::test_a_branch_a_stopped_apply_never_offered_is_refused_and_the_stop_outlasts_later_commands`,
  `tests/validation/exit_tables.py::ExitTablePolicies::test_each_entry_point_s_exit_table_names_exactly_the_codes_it_can_exit_with`,
  `tests/malformed_input/test_faults_programs.py::test_each_command_refuses_a_git_or_gh_that_fails`,
  `tests/deployer/test_verify.py::ConverseTests::test_a_codex_answer_json_reader_refuses_fails_the_listing_at_once_never_as_a_timeout`
- **Search for:** `rg -nU "except (OSError|ValueError|UnicodeDecodeError)[^\n]*:\n\s+(print\([^\n]*\)\n\s+)?return 1\b"`,
  a handler that ends every failure in one code; two probes under one `except` that print one message; an `except`
  around a `git` or `gh` call that drops the exception's text; `FAILED KeyError`, `FAILED TypeError`, or
  `FAILED AttributeError` expected by a test; a state decided from the lines a step printed, such as
  `split("\t")[0] ==`, instead of a value the step returned; a recorded failure a later, different command clears;
  a reader that returns None or `{}` for every fault, whose caller then refuses a field of it as missing; an entry
  point's message about where it runs, such as "Run this inside the repository", printed after any git failure;
  a wait for a response that passes over every line `json_reader` refuses, reporting a refused answer as a timeout.
- **Examples:** #1511, #1840, #1855, #1910, #2022, #2181, #2207, #2209, #2548

### A caller catches the whole error family its callee raises on content

- **Rule:** A caller handles every error its callee raises for bad content, by the family the callee exports or a
  superclass of it, never by one member.
- **Why:** The member left out escapes as an unhandled error where the caller promised a refusal.
- **Use:** each shared reader's family, `json_reader:CONTENT_ERRORS`, `json_writer:CONTENT_ERRORS`,
  `xml_reader:CONTENT_ERRORS`, `frontmatter:CONTENT_ERRORS`, `typed_text:CONTENT_ERRORS`,
  `guidance_record:CONTENT_ERRORS`, `check_provenance:CONTENT_ERRORS`, and `diff_paths:CONTENT_ERRORS`, named whole in
  the handler, and the readers each module names, such as `json_reader:CONTENT_READERS`; `review_io:CONTENT_ERRORS`
  and `file_presence:NotAFile`; `review_io:PersistenceError`, the family of `review_io:read_json`, which the
  content-errors policy's `tests.validation.python_checks:core_reader_families` names for that reader; and
  `deployer.journal:RECOVERY_FAILURES`, every failure a recovery step meets that a rerun would meet again.
- **Guard:** `tests/validation/python_checks.py::PythonChecksPolicies::test_each_handler_around_a_reader_catches_its_whole_content_family`,
  `tests/malformed_input/test_malformed_input.py::test_each_reader_refuses_or_documents_accepting_each_case`,
  `tests/validation/python_checks.py::PythonChecksPolicies::test_json_reader_is_the_one_parse_of_json`,
  `tests/validation/test_python_checks.py::test_content_errors_policy_reads_read_json_as_raising_persistence_error`,
  `tests/validation/test_python_checks.py::test_content_errors_policy_reads_the_family_named_for_a_core_reader`,
  `tests/deployer/test_recovery_migration.py::RecoveryFailureTests::test_each_failure_other_than_an_os_error_a_rerun_repeats_sends_a_committed_run_to_the_manual_steps`,
  `tests/deployer/test_recovery_migration.py::RecoveryFailureTests::test_each_failure_recovery_expects_outside_a_step_fails_its_run_and_the_next_still_recovers`
- **Search for:** `except json.JSONDecodeError`, `except UnicodeDecodeError:`, or `except tomllib.TOMLDecodeError:`
  around a reader that raises more; `except ValueError` near an `xml_reader`, `tomllib`, or `csv.` call; an `except X`
  around a helper that raises a class outside `X`; an `except (`, `OSError`, or `ValueError` around a
  `validate_record_pair` or `read_json` call with no `PersistenceError` beside it; `.encode("utf-8")` of a name read
  from disk with no handler, which raises `UnicodeEncodeError` for a lone surrogate or a surrogate escape; in the
  content-errors policy, a table merged into `self.families[` that a second reader fills too, replacing a family
  without a problem, or an entry stored with no class.
- **Examples:** #1634, #1816, #1874, #2221, #2294, #2351, #2358

### A result cut short says it was cut

- **Rule:** A result a limit, budget, cap, or page size cut short says so, by a flag, a line, or a field its reader
  checks.
- **Why:** A listing cut at its limit otherwise reads as the whole set, and what lies past the cut is never judged.
- **Use:** `github_client:GitHubClient.capped_list`, which asks gh for one more item than its limit and says whether
  GitHub held more; `bounded_process:run_bounded`, `pr_status:_list_pulls`, `audit_repository:duplicates`,
  `review_guidance:overlaps`, `tools.mutation_gate:unfinished_line`, `deployer.plan:reported_diff`, and
  `review_github:PullFiles.cut`, which says whether a pull request's file listing holds the files it changes.
- **Guard:** `tests/validation/python_checks.py::PythonChecksPolicies::test_no_gh_listing_is_capped_outside_the_client`,
  which refuses a limit passed to gh by hand outside the GitHub client unless its module states why,
  `skills/ejryan89-skill-core/scripts/test_github_client.py::CappedListTests`,
  `.claude/skills/audit-repository/scripts/test_audit_repository.py::DuplicateTests::test_a_search_that_finds_exactly_what_it_lists_is_not_cut`,
  `skills/review-insights/scripts/test_review_guidance.py::ReportTests::test_a_search_that_stops_at_its_budget_is_named_in_a_fact_line_and_the_input`,
  `tests/tools/test_mutation_gate.py::SummaryFixtures::test_a_run_the_cap_cut_short_says_so_on_its_first_line_naming_each_module_and_its_counts`,
  `skills/review-document/scripts/test_review_document.py::PullRequestTests::test_a_file_listing_shorter_than_the_files_the_pull_request_changes_fails_before_any_document_is_read`,
  `skills/ejryan89-code-review-core/scripts/test_adversarial_inputs.py::ChangeSizeTests::test_a_file_listing_cut_short_or_read_at_a_moved_head_is_refused_by_each_of_its_readers`,
  `tests/deployer/test_plan.py::UnmanagedDiffTests`,
  `tests/tools/test_hub_guard.py::UnderstoodSetTests::test_with_no_hub_in_view_named_paths_past_the_lookup_limit_are_refused_as_a_lookup_cut_short`
- **Search for:** `"--limit"` in a `gh` call whose result is never compared with the limit; a pull request's
  `pulls/<n>/files` pages read outside `review_github:GitHubClient.list_pull_files`, or its `PullFiles` used without
  comparing `files` with `changed`, since GitHub's files API lists at most 3,000; a slice such as
  `[:MAX_RELATED_TESTS]` or `rg -n "\[:[A-Z_a-z]*(LIMIT|MAX|BUDGET|CAP)[A-Z_a-z]*\]"` with no flag beside it; a
  `break` on a counter in a loop that returns no sign it stopped.
- **Examples:** #1845, #1857, #1864, #1891, #2213, #2220, #2482

### One source that cannot be read costs only its own findings

- **Rule:** A run over several independent sources, such as changelogs, pages, or repositories, lists a source it
  cannot read as a line of its own and still reads the others, and a request to a remote API asks for a page its
  server answers within its time.
- **Why:** One source's failure that stops the run hides every other source's findings, and a page too large for the
  server's time fails every run until someone reads the failure.
- **Use:** `tools.runtime_changelogs:watch`, which lists a changelog it cannot read and reads on, and
  `tools.runtime_changelogs:RELEASES_PER_PAGE`.
- **Guard:** `tests/tools/test_runtime_changelogs.py::MainTests::test_a_changelog_that_cannot_be_read_is_listed_and_the_others_and_the_pages_keep_their_watch`,
  `tests/tools/test_runtime_changelogs.py::MainTests::test_github_releases_are_asked_for_thirty_a_page_which_the_api_answers_in_its_time`
- **Search for:** a comprehension that calls a reader raising on one source across all of them, as
  `rg -n "\[\(\w+, \w+\([^)]*\)\) for \w+ in "`, with no `except` per source; `per_page=100` on an endpoint whose
  items carry long bodies, such as releases.
- **Examples:** #2286

### A number read from text is bounded

- **Rule:** A whole number read from text, an argument, a file, or another program's output is read with its bounds
  and its digit count limited, and a value outside them is the reader's documented refusal.
- **Why:** An unbounded `int()` turns a hostile or garbled value into a `ValueError` past the digit limit, a huge
  allocation, or a negative count the code never expected.
- **Use:** `whole_number:read`, which raises the caller's named error, and `whole_number:parse`, which gives None, for a
  number in any other text, both capped at `whole_number:DIGIT_LIMIT`; `whole_number:argument`, the argparse type
  of every option that takes one; `json_reader:loads` for a JSON number, which refuses one past the same cap, and
  `typed_fields:FieldReader.integer` for its bounds; `process_identity:read_pid` for a PID, bounded by the
  platform's range so none is truncated to another process's; `versions:find` and `versions:read` for a version, each
  number read through `whole_number:parse`; and `json_writer:dumps`, which refuses an integer past the same cap by name.
- **Guard:** `tests/validation/python_checks.py::PythonChecksPolicies::test_whole_number_is_the_one_reader_of_a_number_from_text`,
  `tests/validation/test_python_checks.py::test_int_parse_policy_refuses_a_group_or_a_digit_tested_value_outside_whole_number`,
  `tests/validation/test_python_checks.py::test_int_parse_policy_refuses_an_option_read_with_int_or_float`,
  `tests/validation/test_python_checks.py::test_each_policy_refuses_every_form_its_documentation_names`,
  `skills/ejryan89-skill-core/scripts/test_whole_number.py::ReadTests`,
  `skills/ejryan89-skill-core/scripts/test_whole_number.py::ArgumentTests`,
  `skills/ejryan89-skill-core/scripts/test_json_reader.py::RefusalTests::test_an_integer_past_the_shared_digit_cap_is_refused_whatever_its_sign`,
  `skills/ejryan89-skill-core/scripts/test_typed_fields.py::FieldReaderTests::test_an_integer_within_its_bounds`,
  `skills/ejryan89-skill-core/scripts/test_process_identity.py::PidRangeTests::test_a_pid_is_read_within_the_platforms_range_and_anything_else_is_refused_by_its_path`
- **Search for:** `rg -n "int\(\w+\.group\(|int\(match\["`, a number taken from a regular expression whose pattern
  has `\d+` or `[0-9]+` rather than a bounded count; `.isdigit()` before an `int(`, which admits digits `int` reads
  past any bound; `int(` on a `.split(` of another program's output; `float(` on text from a file or an argument with no
  `math.isfinite`; `int(` on each item of a `.findall(`, or on text a `re.fullmatch(r"[0-9]+", ...)` admitted, which
  bounds no length; a refusal worded "more than N digits" for a `\d` group,
  which also admits digits that are not ASCII; `type=int` or `type=float` in an `add_argument`, and an argparse type
  function that returns `int(value)`, which read `1_0` as 10 and an Arabic-Indic three as 3; an integer read from a
  file and handed to a `ctypes` call, whose declared type holds the kind and truncates the range.
- **Examples:** #1611, #1729, #1879, #1886, #1895, #1897, #2189, #2215, #2217

### One fact has one owner

- **Rule:** A fact the repository states, a vocabulary, a set the code owns, a status line, a prompt list, or a
  normalizer, lives in one place, and every other place reads it from there or is compared with it.
- **Why:** A copy begins identical and then drifts, so a document, a test, or a second module keeps the old fact while
  the owner moves on.
- **Use:** `tests.validation.duplication:_shared_facts`, the registry of facts each read from its owner; where a fact
  is a behavior, the owner's function, such as `frontmatter:Frontmatter.user_only`, `frontmatter:user_only_value` for
  another reader's values, `typed_text:read_typed`, the read of a title or body file without its byte-order mark, and
  `deployer.journal:finish_backup`, the one rule a committed run's finalization and its recovery settle a backup by,
  and `review_config:REPOSITORY`, the one rule of a repository identity the mute and pull-key patterns are built
  on, and the adapter schema's repository pattern is probed against.
  A module more than one part of the repository needs moves to skill-core.
- **Guard:** `tests/deployer/test_backup_settlement.py::SettlementTests`, which runs each backup through both paths,
  `skills/ejryan89-code-review-core/scripts/test_review_config.py::ConfigurationTests::test_a_repository_identity_holds_githubs_name_rule_and_every_reader_built_on_it_agrees`,
  `tests/validation/duplication.py::DuplicationPolicies::test_repository_owners_hold_every_shared_fact`,
  `tests/validation/duplication.py::DuplicationPolicies::test_repository_copies_only_what_it_sanctions`,
  `tests/validation/test_duplication.py::SharedFactPolicy`,
  `tests/validation/python_checks.py::PythonChecksPolicies::test_json_reader_is_the_one_parse_of_json`,
  `tests/validation/skill_grants.py::SkillGrantsPolicies::test_skill_markdown_names_every_status_line_its_scripts_print`,
  `tests/validation/workflows.py::WorkflowsPolicies::test_deployable_workflow_answers_every_prompt_configure_declares_in_both_jobs`,
  `tests/tools/test_hub_guard.py::ContractListTests::test_the_forms_the_hub_allows_are_the_read_only_forms`,
  `tests/tools/test_hub_guard.py::ContractListTests::test_the_runners_the_guard_refuses_are_the_ones_its_contract_names`,
  `tests/code-review/test_format_contract.py::test_the_threat_model_rows_name_each_file_the_analyzer_inventory_parses`,
  `.claude/skills/review-repository/scripts/test_review_repository.py::test_the_trust_boundary_route_matches_its_scripts_and_each_choice_matches_a_file`
- **Search for:** a module-level `frozenset({` or tuple of strings whose members another module also spells; a
  document's list of names, flags, or modules that a module's constant holds; a `print(` of a status line no
  `SKILL.md` names; a second `json.loads(`, `os.lstat(`, or other helper beside the one skill-core owns;
  `rg -n "def (_?inside|strictly_inside|_lexically_inside)\b"` and other helpers of one job held in several modules;
  a live step and its recovery, such as a run's finalization and the completion of an interrupted one, each deciding
  in its own code what becomes of the same state on disk.
- **Examples:** #1349, #1477, #1495, #1596, #1679, #2219, #2295, #2296

### A promise in prose is held where its reader reads it

- **Rule:** A promise a document, a `SKILL.md`, or a docstring makes is held by a test that reads it from there, and a
  universal claim names its exceptions.
- **Why:** A test that restates a document's wording pins the drift instead of the promise, and a claim of "never" or
  "every" with an unstated exception misleads the reader who relies on it.
- **Use:** `tests.validation.skill_layout:runtime_page_problems`, `tests.validation.workflows:token_reach_problems`
- **Guard:** `tests/validation/markdown_links.py::MarkdownLinksPolicies::test_every_test_a_document_cites_exists`,
  `tests/validation/test_skill_layout.py::SkillLayoutFixtures::test_runtime_page_policy_holds_each_support_statement_to_the_metadata`,
  `tests/validation/workflows.py::WorkflowsPolicies::test_a_token_step_that_runs_the_pull_requests_own_code_is_described_as_doing_so`,
  `tests/deployer/test_backup_settlement.py::RecoveryStopsTests::test_when_recovery_fails_names_every_stop_recovery_makes`,
  which reads each stop from the code and fails one the guide's list leaves out,
  `skills/dotnet-format/scripts/test_csharp_layout.py::test_each_status_word_a_scripts_docstring_names_is_one_it_prints`,
  which fails a dotnet-format script's docstring naming a status word the script never prints
- **Search for:** an `assertEqual(` or `assertIn(` on a literal sentence a document states, where the test could read
  the document or the shared constant; `never`, `always`, `nothing`, `every`, or `only` in a `SKILL.md` or a
  docstring, each checked for a test of its exception; a docstring's exit codes against the function's `return` and
  `sys.exit(` values; a statement of which code a credential or the run's token reaches, checked for the code a
  pull request can change at those paths; a document's list of the cases a step stops or refuses on, checked against
  each `return False` or `raise` of that step.
- **Examples:** #1317, #1584, #1629, #1861, #1864, #1869, #2224, #2237, #2296

### A reader refuses a shape it does not know

- **Rule:** A reader of input or of another program's output accepts the shapes it was written and tested for and
  refuses or reports as unknown any other, never passing over it and judging what is left.
- **Why:** A skipped entry, an ignored field, or a top-level value of the wrong type lets the reader judge a partial
  input as whole.
- **Use:** `typed_fields:FieldReader.json_object`, which refuses an unknown key, `xml_reader:read_xml`, which refuses
  a document type or entity declaration, `deployer.discovery:ListingError`, `skill_inventory:hooks_file_hooks`,
  which refuses a hooks file member of another kind at every level, by name, `msbuild_reader:mapping`, which reads an
  absent section as empty and refuses one of another kind, and the shepherd's `shepherd_pull_requests:objects` and
  `shepherd_pull_requests:member`, which refuse a gh listing or field of another kind than the shepherd acts on, and
  `shepherd_pull_requests:reported` and `shepherd_pull_requests:present`, which refuse a member that decides a write
  when the listing leaves it out, so an absent field is never read as the answer that permits the write, and
  `tools.hub_guard:judge_option`, which refuses a shell option neither table of the guard's names.
- **Guard:** `skills/ejryan89-skill-core/scripts/test_typed_fields.py::FieldReaderTests::test_an_object_holds_its_required_fields_and_no_unknown_one`,
  `tests/malformed_input/test_malformed_input.py::test_each_reader_refuses_or_documents_accepting_each_case`, which
  fails a reader that answers on a field or a list item of another type it does not document taking,
  `tests/malformed_input/test_malformed_input.py::test_a_field_case_changes_one_field_of_the_readers_own_document`,
  `.claude/skills/shepherd-pull-requests/scripts/test_shepherd_pull_requests.py::ShapeTest::test_a_pull_request_member_the_listing_leaves_out_or_nulls_is_malformed_never_a_default`,
  which holds a written-out row for every member the shepherd lists, absent and null, and fails when the listing
  gains a member without one, and
  `.claude/skills/shepherd-pull-requests/scripts/test_shepherd_pull_requests.py::ShapeTest::test_a_check_run_member_the_rollup_leaves_out_is_malformed_never_a_default`,
  the same table for each check run in a rollup, and
  `tests/deployer/test_state_files.py::ReaderTests::test_a_journal_entry_with_any_key_of_another_type_is_malformed_never_an_error`,
  which gives every key of each journal entry the deployer writes a list, an object, and a number, and
  `tests/malformed_input/test_malformed_input.py::test_every_function_that_calls_a_shared_reader_is_held_reached_pending_or_named_no_reader`,
  which fails a function calling a shared reader that no table holds, no held reader of its own module reaches, and
  neither the pending list nor NOT_READERS, the functions named as no reader with the reason, names, passes a helper
  a held reader reaches, and fails a pending or NOT_READERS name the scan no longer finds unheld
- **Search for:** `if not isinstance(` followed by `continue` in a loop over another program's entries; `.get(` with a
  default, or `["key"]`, on `json_reader` output with no `isinstance` or `FieldReader` check; a top-level value used
  without `isinstance(value, dict)`; a regular expression over another program's output with no branch for no match;
  `member(` or `.get(` followed by `or "<default>"`, `is not None`, or `is None` where the member decides a write,
  such as arming, holding, or a rerun, and an absent member would read as the answer that allows it;
  `rg -n "\.get\([^)]*\)\)? (not )?in [A-Z_{(]"`, a field read from a file tested against a set, a frozenset, or a
  dictionary before its type is checked, which a list or an object fails with `TypeError` rather than as malformed.
- **Examples:** #1890, #1892, #2054, #2227, #2235, #2292

### A reader of another program's language reads it as that program does

- **Rule:** A reader of another program's language or output reads it as that program does, held by a differential
  test against the program itself or by output captured from it with its provenance. At a trust boundary, where
  reading every construct as the program does has not converged, the reader fails closed instead: it understands an
  explicit table of constructs, refuses every other one, and holds each table to the catalogue the program lists of
  itself, so a construct it never met is a refusal rather than a pass.
- **Why:** A hand-written reader that misses a construct the program reads lets a command or an entry pass unjudged.
  The hub guard's modelling reader closed one spelling per fix and every review found the next, until it was rebuilt
  to refuse what it does not understand.
- **Use:** `tools.hub_guard:read_powershell`, `tools.hub_guard:runner_refusal`, `option_reader:option_refusal`, and
  `tools.hub_guard:expansion_form`
- **Guard:** `tests/tools/test_bash_syntax.py::ArrayAssignmentTests::test_an_array_assignment_to_a_subscript_is_read_where_bash_reads_an_array`,
  `tests/tools/test_captured_samples.py::test_every_sample_is_named_by_a_suite_that_reads_it`, which fails a captured
  sample no suite names, so real output no reader is held to is found,
  `tests/validation/test_skill_scripts.py::SkillScriptsFixtures::test_shell_command_extraction_runs_a_program_a_quoted_function_name_never_defined`,
  `skills/ejryan89-skill-core/scripts/test_git_client.py::IsolatedClientTests::test_a_plain_client_refuses_a_command_line_setting_that_can_define_an_alias`,
  `skills/ejryan89-skill-core/scripts/test_git_client.py::IsolatedClientTests::test_a_plain_client_refuses_every_carrier_of_another_command_in_every_spelling`,
  `tests/tools/test_hub_guard_bash.py::BuiltinTests::test_every_builtin_bash_lists_is_understood_or_not_read_and_never_both`,
  `tests/tools/test_hub_guard_bash.py::ShellOptionTests::test_each_set_option_is_read_by_its_table_from_every_source`,
  `tests/tools/test_hub_guard_bash.py::ExpansionTests::test_every_expansion_form_is_understood_or_not_read_and_has_a_sample`,
  `tests/tools/test_hub_guard_pwsh.py::NodeCatalogueTests::test_every_node_type_powershell_has_is_understood_or_not_read_and_never_both`,
  `tests/tools/test_prefix_programs.py::RunnerFormTests::test_timeout`,
  `tests/tools/test_hub_guard.py::UnderstoodSetTests::test_each_class_of_construct_the_guard_does_not_read_is_refused_while_a_hub_is_in_view`,
  `tests/tools/test_hub_guard.py::ReadOnlyFormTests::test_a_dash_word_after_a_valued_option_written_alone_is_refused`,
  which refuses a dash word git would take as a lone valued option's value,
  `tests/tools/test_hub_guard.py::UnderstoodSetTests::test_a_gh_flag_before_its_subcommand_is_not_read_past`, which
  refuses a flag cobra may take the next word for before gh's subcommand,
  `tests/tools/test_captured_samples.py::test_every_sample_has_its_provenance_and_every_provenance_its_sample`,
  `tests/tools/test_runtime_changelogs.py::test_a_term_matches_only_where_a_real_entry_names_it`,
  `.claude/skills/shepherd-pull-requests/scripts/test_shepherd_pull_requests.py::ShapeTest::test_the_recorded_rollup_reads_its_newest_validation_run`,
  which reads a check rollup recorded from gh's pull request listing, and
  `.claude/skills/shepherd-pull-requests/scripts/test_shepherd_pull_requests.py::ShapeTest::test_a_check_no_workflow_reports_neither_satisfies_nor_blocks_the_required_check`,
  `skills/dotnet-dependencies/scripts/test_msbuild_conditions.py::test_a_number_is_read_as_msbuild_reads_one_and_any_other_spelling_leaves_the_comparison_unsettled`
  and
  `skills/dotnet-dependencies/scripts/test_msbuild_conditions.py::test_equality_compares_numbers_booleans_and_text_as_msbuild_does`,
  which hold the condition reader to MSBuild's verdicts captured from the .NET SDK's MSBuild, and
  `skills/ejryan89-skill-core/scripts/test_github_client.py::test_a_shorthand_cluster_is_read_as_gh_reads_it`, which
  holds the client's reading of a gh api command to gh's flag parser, a letter it does not read counting as a change
- **Search for:** a Python parser or folding, `float(`, `int(` with or without a base, `casefold()`, `lower()`,
  `strip()`, or a pattern with `re.IGNORECASE` and no `re.ASCII`, applied to a value another program reads, each
  checked against that program's own reading of it, as MSBuild reads a number with .NET's invariant `Double.TryParse`
  and compares text with .NET's ordinal comparison ignoring case; a POSIX `shlex` lexer over shell text whose
  words are compared with an operator or keyword, which reads a quoted `';'` or an escaped `\;` as the separator it
  spells, rather than `ShellLexer` in `tests/validation/skill_scripts.py`, or a function name read from such a word
  rather than from its syntax, which is None for a quoted or escaped name Bash refuses; in `tools/hub_guard.py`, a construct a
  table neither understands nor lists in `NOT_READ`, a table whose test types its rows rather than taking them from the shell's own
  catalogue, a word settled from what an earlier command assigns rather than the assignment refused, a program added
  to `RUNNERS` with no row, a program that runs a command its words name left out of it, a runner allowed around a command that may be git, a cmdlet added to
  `POWERSHELL_COMMANDS` that starts a command its words name, and a refusal that waits
  for the command to name git; a `re.compile(` in `tools/runtime_*.py` or `deployer/discovery.py` with no sample
  under `tests/fixtures/captured-output/`; a test whose sample output was typed rather than captured; a refusal of an
  empty or absent member of another program's entries, such as `reported(..., empty=False)`, whose fixture rows give
  every kind of entry the member typed in, never read from what the program lists for each kind it reports, as gh
  lists a check run no workflow reports with an empty `workflowName`; a refusal of a git command by its subcommand's
  name that a `-c alias.<name>`, `-c include.path`, or `-c includeIf.<condition>.path` on the same command line can
  rename, a `-c help.autocorrect` can turn a misspelled name into, or an `--exec-path=<folder>` can replace; a list
  of the subcommands that take an option, read against `git <command> -h` for every command git lists, that leaves
  one out, as `difftool --no-index` was; a git option table that leaves out one git.c reads a value after, as
  `--shallow-file` was; a failure labelled with `arguments[0]` rather than `git_client.command_name`; a refusal that
  reads the subcommand a client runs but not the command a carrier starts for it, such as `submodule foreach`,
  `rebase --exec` or `-x`, `bisect run`, a transport's `--upload-pack`, or a `-c` setting that names a program, rather
  than an allow-list of the subcommands, actions, and settings that start none; a reader of a gh flag word that takes
  its first two characters for the flag, rather than `gh_shorthands.shorthands`, which reads a cluster such as
  `-ifbody=x` letter by letter as gh does.
- **Examples:** #1412, #1429, #1430, #1592, #1606, #1843, #1851, #1865, #2032, #2088, #2089, #2134, #2135, #2136,
  #2137, #2138, #2139, #2140, #2188, #2225, #2232, #2233, #2234, #2251, #2262, #2274, #2276, #2316, #2411,
  #2418, #2203, #2450, #2476, #2481

### A recursive parser of input has a depth bound

- **Rule:** A parser that recurses into input it does not control stops at a stated depth and ends in its documented
  refusal, never in a `RecursionError`.
- **Why:** Nesting past Python's recursion limit escapes the reader as an unhandled error instead of its refusal, and
  the C JSON scanner and the XML parser nest far deeper than a Python walk over what they read can recurse.
- **Use:** `nesting_depth:deeper`, the check a recursive parser makes at each level, raising its own error, against
  `nesting_depth:DEPTH_LIMIT`; `json_reader:loads` and `xml_reader:read_xml`, which refuse a value nested past the
  same limit, so a walk over what they return, such as MSBuild's Choose evaluation, needs no bound of its own;
  `csharp_layout:_brace_level` and `msbuild_conditions:Parser.nested`, a hand-written parser's own check; and
  `python_reader:parse`, `python_reader:parse_expression`, and `python_reader:literal`, the one parse of Python source
  and of a literal, which refuse the parser's overflow, a memory or recursion error, as the
  `python_reader:PythonRefused` their callers handle. A handler around such a reader alone names its family and no
  RecursionError; one that also guards a parser that recurses, such as tomllib, keeps it and says which call
  raises it.
- **Guard:** `skills/ejryan89-skill-core/scripts/test_nesting_depth.py::DeeperTests`,
  `tests/validation/test_python_checks.py::test_content_errors_policy_refuses_a_recursion_handler_around_readers_that_never_raise_it`,
  `skills/ejryan89-skill-core/scripts/test_json_reader.py::RefusalTests::test_nesting_one_level_past_the_shared_limit_is_refused_though_the_scanner_reads_it`,
  `skills/ejryan89-skill-core/scripts/test_json_reader.py::RefusalTests::test_a_scalar_is_no_level_so_one_at_the_limit_reads_as_an_empty_container_does`,
  `skills/ejryan89-skill-core/scripts/test_xml_reader.py::DepthTests`,
  `skills/ejryan89-skill-core/scripts/test_python_reader.py::ParseTests`,
  `tests/validation/test_python_checks.py::test_owner_only_policy_refuses_each_reference_outside_its_owners`,
  `skills/dotnet-format/scripts/test_csharp_layout.py::test_braces_nested_past_the_shared_depth_limit_fail_by_name`,
  `skills/dotnet-dependencies/scripts/test_msbuild_conditions.py::ConditionTests::test_negations_and_parentheses_nested_past_the_shared_depth_limit_are_not_settled`,
  `skills/ejryan89-skill-core/scripts/test_json_reader.py::RefusalTests::test_nesting_past_the_recursion_limit_is_refused`,
  `tests/tools/test_hub_guard.py::EndToEndTests::test_bash_nested_deeper_than_the_reader_follows_is_refused_alike_in_its_own_process`,
  `tests/validation/python_checks.py::PythonChecksPolicies::test_json_reader_is_the_one_parse_of_json`,
  `tests/malformed_input/test_malformed_input.py::test_each_reader_refuses_or_documents_accepting_each_case`, whose
  table holds each parser of text by its own grammar's deep nesting
- **Search for:** a function that calls itself on a child of its input with no depth parameter and no
  `except RecursionError`; `literal_eval(` or `ast.parse(` outside `python_reader.py`, whose handler would have to
  name `MemoryError` and `RecursionError` beside `SyntaxError`; `json.loads(` outside `json_reader.py`.
- **Examples:** #1613, #1617, #1729, #1886, #1893, #2170, #2190, #2390

### A path taken from input is judged inside its tree before anything opens it

- **Rule:** A path taken from input is judged inside its tree lexically, then resolved, then probed for a link, before
  anything opens it.
- **Why:** A parent segment, a link, or an absolute path otherwise leads a reader or a writer outside the tree it was
  given.
- **Use:** `path_containment:refusal`, which judges an entry from input as written, as resolved, and through the link
  probe, and names why; `path_containment:within`, the one comparison of a path with its tree, which every other
  containment test calls on paths it has made absolute or resolved; `path_containment:relative_within`, the path
  relative to its tree from the forms `path_containment:within` compares, never a relative_to of the paths as given; `path_containment:posix_within` for a repository
  path a listing gives; `diff_paths:snapshot_path` and `diff_paths:unsafe_snapshot_name`, which hold a snapshot's
  readers and its writer to the same written rules; `dotnet_solution:refusal` and
  `dotnet_format_targets:listed_refusal`, which compare through it a
  path a solution or a file list names; `link_probe:is_plain_file`, `tools.checkout:checkout_root`,
  `memory_audit:bare_name`.
- **Guard:** `tests/validation/duplication.py::DuplicationPolicies::test_path_containment_is_the_one_containment_test`,
  `skills/ejryan89-skill-core/scripts/test_path_containment.py::RefusalTests`,
  `skills/ejryan89-skill-core/scripts/test_path_containment.py::ExtendedPrefixTests`,
  `skills/ejryan89-skill-core/scripts/test_diff_paths.py::SnapshotPathTests`,
  `skills/ejryan89-code-review-core/scripts/test_adversarial_inputs.py::test_the_writer_leaves_out_exactly_the_names_a_snapshot_reader_refuses`,
  `skills/dotnet-format/scripts/test_dotnet_format_targets.py::ListedRefusalTests`,
  `skills/curate-agent-memory/scripts/test_memory_audit.py::ReindexTests::test_a_target_with_a_parent_segment_is_never_read`,
  `tests/tools/test_plugin_canary.py::test_an_installed_plugin_the_canary_cannot_place_in_the_home_is_refused_by_name`,
  `tests/tools/test_checkout.py::test_every_tool_that_takes_root_parses_it_with_checkout_root_and_the_module_names_it`
- **Search for:** `root / name` or `joinpath(*` where the operand comes from input and no `refusal` judged it first;
  `within(` on a path from input with no lexical check before it; `is_relative_to(`, `commonpath(`, `in` a path's
  `.parents`, a `startswith(`, `find(`, `removeprefix(`, slice, regular expression, or glob of a folder built to end
  in a separator, a `relpath(` or a `relative_to(..., walk_up=True)` checked for `..`, or a `relative_to(` inside a
  `try` or a `contextlib.suppress` that turns its `ValueError` into an answer, outside `path_containment.py`, which
  the guard refuses unless a `CONTAINMENT_ALLOWED` entry states why; a second judge of a path from input, such as a
  snapshot reader's own segment rules, that leaves out a rule `written_refusal` holds, a reserved device name or a
  trailing dot or space, or a writer and its readers whose rules differ in either direction; a `.resolve()`
  compared as text with a path that was not resolved, where Windows may add its extended-length prefix to one.
- **Examples:** #1780, #1782, #1818, #1905, #2050, #2195, #2196, #2226

### Every check comes before the first write

- **Rule:** A step validates everything it can before it writes anything, and a refusal leaves nothing behind.
- **Why:** A refusal after a write leaves a half-made output or a changed home the next run trips over.
- **Use:** `atomic_file:replace_bytes`, `review_io:atomic_write_json`.
- **Guard:** `tests/deployer/test_invariants.py::RefusalBeforeMutationGuardTests::test_a_refusal_raised_after_the_deployment_changed_the_home_fails_the_test`,
  `tests/malformed_input/test_malformed_input.py::test_failure_names_a_write_before_a_refusal_an_escape_and_an_undocumented_acceptance`,
  `tests/deployer/test_invariants.py::RefusalBeforeTheFirstWriteTests::test_every_root_refuses_a_backups_folder_it_could_not_keep_a_copy_in`,
  `tests/deployer/test_rendering_locking.py::LockTests::test_a_read_only_manifest_is_refused_before_the_first_change_and_by_the_dry_run`,
  `skills/curate-agent-memory/scripts/test_memory_audit.py::DeleteTests::test_a_read_only_memory_among_several_is_refused_before_any_is_deleted`,
  `tests/malformed_input/test_faults_directory.py::test_each_command_refuses_a_directory_in_a_file_s_place`, which
  compares the home, the work folder, and each output a driver names before and after every refused run of every entry point
- **Search for:** a `mkdir(parents=True` or `mkdtemp(` in `tools/` before the arguments are checked; a write before a
  `raise` of the step's refusal class; a check that runs only where the path is first used, after earlier writes, such
  as a destination judged in the step that creates it; a refusal added to a write the commit makes, such as
  `fsops.write_atomic`'s, with no check of the same target in the plan; an `unlink(`, `rename(`, or `replace_text(` in
  a loop over several files whose refusals do not ask `atomic_file:is_read_only`, which Windows refuses to delete or
  replace.
- **Examples:** #1396, #1855, #1876, #1911, #2184, #2210, #2223

### A preview reads what the run refuses on before it answers

- **Rule:** A dry run or other preview reads every state its run refuses on, in the run's order, before it gives any
  answer, so it refuses what the run would refuse.
- **Why:** A preview that answers before the read that refuses, such as reporting a pending recovery, promises a run
  that stops, and sends the user to the step that fails.
- **Use:** `deployer.lock:check_holder`, which judges a lock already there as `deployer.lock:acquire` does and takes nothing,
  `deployer.pipeline:_reject_other_checkout`, which reads the manifest and refuses another checkout, and
  `deployer.journal:check_manifest_beside_runs`, which refuses a run's journal beside no manifest: the deployment's
  checks before it recovers, which `deployer.pipeline:_dry_run` makes first too, in the deployment's order, beginning,
  for a preview that takes the source over, with `deployer.source:reject_linked_worktree`, which the deployment runs
  before its lock whatever the flag.
- **Guard:** `tests/deployer/test_state_files.py::StateFileTests::test_a_dry_run_refuses_each_state_the_deployment_refuses_before_recovery_pending_run_or_not`,
  a table of each state the deployment refuses before it recovers, each with a pending recovery beside it and without,
  and `tests/deployer/test_source_validation.py::OtherCheckoutTests::test_a_dry_run_with_take_over_source_from_a_linked_worktree_is_refused_as_the_deployment_refuses_it`
- **Search for:** a `return` in `_dry_run` or another preview before a check its run makes first, such as
  `_stop_for_pending_recovery(` before `check_holder(` or `_reject_other_checkout(`; a `--dry-run` branch taken before
  a check the full run takes, such as the lock a preview never acquires but must still judge; an option a preview
  newly accepts, such as `--take-over-source`, whose run makes a check the preview skips, such as `_claim`'s.
- **Examples:** #2293, #2313

### What one step accepts, every later step can read and write

- **Rule:** A value a step accepts is one every later step can write and read back, so a reader refuses what the
  writer cannot hold, such as a non-finite number or text UTF-8 cannot encode.
- **Why:** A value the first step accepted and a later one cannot write ends the run part-way, after state has
  changed.
- **Use:** `json_writer:dumps` for JSON text and `json_writer:write` for a JSON file, which refuse a non-finite
  number, a lone surrogate, and what `json_reader:loads` refuses on the read-back, by name, at the write;
  `review_io:atomic_write_json`, which writes through them, `review_io:read_json`, `typed_fields:FieldReader`, and
  `json_reader:loads`, which refuses a number too large for a float before it becomes an infinity the writer refuses,
  and a lone surrogate the writer cannot encode.
- **Guard:** `skills/ejryan89-skill-core/scripts/test_json_writer.py::RefusalTests`,
  `skills/ejryan89-skill-core/scripts/test_json_reader.py::RefusalTests::test_a_number_that_overflows_to_an_infinity_is_refused_whatever_its_sign_or_nesting`,
  `skills/ejryan89-skill-core/scripts/test_json_reader.py::AcceptanceTests::test_what_the_reader_accepts_the_writer_writes`,
  `tests/validation/python_checks.py::PythonChecksPolicies::test_json_writer_is_the_one_writer_of_json`,
  `tests/validation/test_python_checks.py::test_json_write_policy_refuses_a_json_write_outside_json_writer`,
  `skills/ejryan89-code-review-core/scripts/test_review_io.py::ReadJsonTests::test_content_the_parser_refuses_or_utf8_cannot_hold_is_a_persistence_error`
- **Search for:** `rg -n "json\.dumps?\(" | rg -v allow_nan=False`; `write_text(json.dumps(`; input written back
  without the later reader's pass.
- **Examples:** #1552, #1634, #1874, #1906, #2006, #2039

### A record separator is one the data cannot hold

- **Rule:** A listing of names or records another program writes is split on a separator the data cannot hold, NUL
  for git, a script never splits a line it printed to recover a value it already holds, and a fact line a script
  prints holds each value flattened, so no value adds a line or a field.
- **Why:** A name holding the separator, or a path git quotes, is split or misread, and the listing names a file that
  is not there.
- **Use:** `git_client:GitClient.names`, which runs a name listing with -z and splits it on NUL;
  `pr_status:branch_tips`, `dotnet_format_targets:git_paths`; a for-each-ref format joined by %00 and a log format
  joined by %x00; and `git_client:output_lines`, which splits git's output on a newline alone, the one byte that ends
  a line of it, where splitlines() also breaks on U+0085, U+2028, and other characters a ref name or a path holds;
  `git_client:output_line`, which takes off only the newline ending a one-line answer, where strip() also drops
  U+00A0 and U+2028;
  `skill_inventory:delegation_lines`, which returns each runtime prompt's line number beside the lines it prints;
  `run_dotnet_format:parse_diagnostics`, which splits a formatter's log on a newline alone for the same reason;
  `deployer.discovery:output_lines`, which splits a runtime's output, its skill catalog, and the canary's records on a
  newline alone, taking off a carriage return before it; `flat_text:fact_line`, which flattens every field of a
  tab-separated fact line; and the choke points every stdout line of a shipped skill's scripts passes,
  `flat_text:flat_text`, `flat_text:fact_line`, `console:failed_line`, and `flat_text:single_line`, given a whole
  line or each line of a newline join, as `review_pipeline:_run_enumerate` and `memory_audit:main` print it, and
  `repo_cleanup:fields_line`, which flattens each field of a line it keeps tab-separated.
- **Guard:** `tests/validation/python_checks.py::PythonChecksPolicies::test_every_git_listing_and_format_separates_on_nul`,
  which refuses a git name listing without -z, a for-each-ref or log format whose fields a byte but NUL joins, and a
  split of git's output with splitlines(), or a strip() or split() of it given no argument, unless its module
  states why,
  `tests/validation/test_python_checks.py::test_each_policy_refuses_every_form_its_documentation_names`,
  `skills/ejryan89-skill-core/scripts/test_release_tags.py::LastReleaseTests::test_a_tag_whose_name_holds_a_character_splitlines_breaks_on_is_one_name`,
  `skills/ejryan89-skill-core/scripts/test_git_client.py::ThreeAnswerTests::test_a_path_holding_a_character_splitlines_breaks_on_is_one_path`,
  `tests/tools/test_worktrees.py::CheckoutTests::test_a_checkout_whose_path_holds_a_character_splitlines_breaks_on_is_found`,
  `tests/tools/test_hub_guard.py::FileGuardTests::test_a_guarded_hub_whose_path_holds_a_character_splitlines_breaks_on_is_protected`,
  `skills/ejryan89-skill-core/scripts/test_git_client.py::NamesTests::test_names_reads_a_name_holding_a_non_ascii_character_or_a_newline_exactly`,
  `tests/tools/test_mutation_gate.py::test_the_gate_reads_a_changed_module_whose_path_holds_a_non_ascii_character_or_a_newline`,
  `skills/repo-cleanup/scripts/test_pr_status.py::BranchTipTests::test_an_upstream_whose_name_holds_a_bar_is_read_whole`,
  `tests/tools/test_release_notes.py::RangeEntriesTests::test_a_message_holding_any_byte_but_nul_is_read_whole`,
  `skills/dotnet-format/scripts/test_dotnet_format_targets.py::FileListTests::test_every_name_reads_back_as_resolve_writes_it`,
  `skills/audit-skill/scripts/test_skill_inventory.py::ScanTests::test_a_runtime_prompt_in_a_folder_whose_path_holds_a_space_keeps_its_line`,
  `skills/ejryan89-skill-core/scripts/test_flat_text.py::FactLineTests::test_a_field_holding_any_line_or_field_break_stays_one_field_on_one_line`,
  `skills/dotnet-format/scripts/test_run_dotnet_format.py::OneLineFactTests::test_a_saved_file_holding_a_line_separator_is_named_on_one_line`,
  `skills/dotnet-format/scripts/test_csharp_layout.py::OneLineFactTests::test_violations_and_fixes_in_a_name_holding_a_line_break_print_one_line_each`,
  `tests/deployer/test_verify.py::ParsingTests::test_a_codex_catalog_description_holding_a_line_break_other_than_newline_is_part_of_its_line`,
  `tests/deployer/test_verify.py::ConverseTests::test_a_last_error_line_holding_a_line_break_other_than_newline_is_named_whole`,
  `tests/tools/test_runtime_canary.py::RunTests::test_a_line_holding_a_line_break_other_than_newline_is_read_whole`,
  `tests/validation/skill_scripts.py::SkillScriptsPolicies::test_skill_scripts_print_each_line_through_a_choke_point`,
  which refuses a stdout print in a shipped skill's scripts whose text may hold a line break, unless its module's
  allowance states why,
  `tests/validation/test_skill_scripts.py::SkillScriptsFixtures::test_print_choke_point_policy_refuses_a_stdout_line_that_may_hold_a_break`,
  `skills/ejryan89-code-review-core/scripts/test_review_source.py::CommandTests::test_a_match_or_a_failure_holding_a_line_break_prints_on_one_line`,
  `skills/ejryan89-code-review-core/scripts/test_review_pipeline.py::OneLineFactTests`,
  `skills/review-document/scripts/test_review_document.py::test_a_document_line_holding_a_line_break_prints_on_one_line`,
  `skills/curate-agent-memory/scripts/test_memory_audit.py::test_a_skill_file_holding_a_line_break_prints_on_one_line`;
  one refusing splitlines() of any program's output, not git's alone, is #2329's
- **Search for:** a `"log"` or `"show"` git call with `--name-only` and no `-z`, which the policy does not read; a
  name listing whose arguments a function builds from names rather than string literals; `split("|")` or
  `splitlines()` on another program's output, git's included where the policy cannot trace it to the call, such as
  output passed into a function that splits it; `.split()[` or `.partition(" ")` on a line the same module wrote,
  beside a `startswith("<FACT> ")`, where a field before the one read is a path or other text that can hold a space;
  `emit(f"` in a script whose output is one fact per line, formatting a path, a name, a title, an exception, or
  another program's output without `fact_line` or `flat_text`, and a `print` the policy does not read: one reached
  through another name, such as `emit = print`, in a script outside a shipped skill, or under a `PRINT_ALLOWED`
  reason; `.strip()` or `.split()` with no
  argument on git's output, a ref name read from `symbolic-ref` or `config` most of all; a git subcommand left out of
  the policy's catalogue and run through a wrapper whose name does not say git.
- **Examples:** #1816, #1848, #1862, #1908, #2034, #2187, #2206, #2211, #2214, #2248

### Two spellings are compared as their consumer compares them

- **Rule:** Two spellings of one thing are compared as the program that consumes them compares them: versions by
  number, URLs and paths in canonical form, and environment names folded as the platform folds them.
- **Why:** A text comparison takes `1.10` for older than `1.9`, or two spellings of one path for two paths.
- **Use:** `versions:find`, `versions:read`, and `versions:latest`, the one version reader, which reads a version's
  numbers and orders by them; `release_tags:release_order` and `release_tags:highest_release`, a release tag through
  it; `path_keys:path_key`, the one form a path is compared in, which the deployer reaches as
  `deployer.platform_support:path_key`; `guidance_record:canonical_url`; `deployer.source:any_case` and
  `deployer.source:refuse_other_spellings`, which find a name the deployer reads spelled in any case on every
  platform and refuse one spelled otherwise.
- **Guard:** `tests/validation/duplication.py::DuplicationPolicies::test_repository_copies_only_what_it_sanctions`,
  which fails a version's numbers read by hand outside the version reader, and a path case-folded by hand outside
  the path key;
  `skills/ejryan89-skill-core/scripts/test_versions.py::OrderTests::test_versions_order_by_number_never_as_text`,
  `skills/ejryan89-skill-core/scripts/test_path_keys.py::PathKeyTests::test_two_spellings_of_one_path_have_one_key`,
  `skills/ejryan89-skill-core/scripts/test_release_tags.py::ReleaseTagTests::test_releases_are_ordered_by_their_numbers_not_their_text`,
  `skills/ejryan89-skill-core/scripts/test_guidance_record.py::test_the_canonical_form_folds_only_raw_github_s_refs_heads_and_the_scheme_and_host_case`,
  `tests/deployer/test_source_validation.py::MetadataValidationTests::test_a_name_the_deployer_reads_spelled_in_another_case_is_refused_by_name_on_every_platform`
- **Search for:** `version in`, `.startswith(version`, `sorted(tags`, or `tags[-1]`; `int(part) for part in` or
  `map(int,` over a `.split(".")`; in a workflow, `grep` or `contains(` on a version; `==` between two paths with no
  normalization, or `os.path.normcase` outside `path_keys.py`; a glob, `fnmatch.fnmatch(`, or `.is_file()` of a
  fixed file name, which folds case on Windows, beside a dictionary keyed by each file's spelling on disk.
- **Examples:** #1556, #1855, #1867, #1909, #2299

### A child process starts from an environment built for it

- **Rule:** A child process starts from an environment built for it, never the one its parent inherited.
- **Why:** A variable the parent carries, such as git's repository selection or a signed-in token, otherwise reaches
  a child that must not see it. A checker also finds settings on its own, in an options variable, a file above what
  it checks, or the user's configuration folder, and one of them can turn its findings off.
- **Use:** `bounded_process:run_bounded`, `bounded_process:without_repository_variables`,
  `tools.mutation_gate:suite_environment`, a checker's one runner: `deployer.render:run_shellcheck`,
  `deployer.render:run_bash_syntax_check`, `tests.validation.toolchain:shellcheck_job`,
  `tests.validation.toolchain:bash_syntax_job`, `tests.validation.toolchain:ruff_job`, and
  `tests.validation.toolchain:mypy_job`, `tests.validation.suite_discovery:PYTEST_ENVIRONMENT` for every pytest the
  runner and the mutation gate start, and for a git that reads content it did not write,
  `bounded_process:isolated_git_environment` with `git_client:NO_REPOSITORY`, which a client its caller makes
  isolated starts every command with, taking no runner that could leave the environment out, so no repository around
  the content and no configuration shapes what git prints; a client that is not isolated refuses a search or
  comparison any of whose arguments spells the no-index option or an abbreviation of it
  (`git_client:reads_no_repository`), reading no option grammar, and takes a search's pattern apart from its arguments
- **Guard:** `tests/validation/python_checks.py::PythonChecksPolicies::test_every_process_starts_without_git_repository_variables`,
  `tests/validation/skill_scripts.py::SkillScriptsPolicies::test_each_checker_starts_only_from_its_runner_and_every_repository_command_is_read`,
  which reads a process start, a mypy or ruff import, and the names it is written with, so a name built at run time
  from pieces is outside what it reads; `skills/ejryan89-skill-core/scripts/test_git_client.py::IsolatedClientTests`,
  `skills/ejryan89-code-review-core/scripts/test_review_source.py::SnapshotIsolationTests`,
  `tests/validation/test_suite_discovery.py::PytestEnvironmentFixtures`,
  `tests/tools/test_mutation_gate.py::RunSuiteFixtures::test_a_test_the_users_environment_would_leave_out_still_kills_the_mutant`
- **Search for:** `{**os.environ`, `os.environ.copy()`, or `dict(os.environ)` passed as `env=`; a measured or
  sandboxed child started with no `env=`; a checker started anywhere but its runner, or run with no option that stops
  it reading settings outside the repository: ShellCheck without `--norc` and an empty `SHELLCHECK_OPTS`,
  `Invoke-ScriptAnalyzer` without `-Settings`, ruff given a folder without `--no-respect-gitignore`, mypy with
  `MYPYPATH` left set, and a `pytest_command` started without `PYTEST_ENVIRONMENT`, which leaves `PYTEST_ADDOPTS`,
  `PYTEST_PLUGINS`, and plugin autoloading to the user; a git command run with its working directory, or `-C`, inside content a pull request or a
  reviewer wrote, such as a review snapshot, through a client that is not isolated.
- **Examples:** #1366, #1396, #1598, #1777, #1861, #2208, #2241, #2246

### An action on something not proven ours stops

- **Rule:** An action that changes something, such as a merge, a tag move, or a deletion, stops unless the thing is
  proven ours, and unknown ownership is not ours.
- **Why:** An action taken on a guess acts on another person's pull request, branch, or pin.
- **Use:** `deployer.source_ref:is_release_pin`, `deployer.source_ref:replaceable_release_pin`
- **Guard:** `tests/deployer/test_deploy_at.py::DeployAtTests::test_a_pin_whose_tags_git_cannot_read_is_refused_with_gits_reason`,
  `.claude/skills/shepherd-pull-requests/scripts/test_shepherd_pull_requests.py::AdmissionTest`,
  `skills/repo-cleanup/scripts/test_repo_cleanup_stops.py::StopAndRefusalTests::test_no_planned_action_lets_a_deleting_command_act_without_the_offer_the_apply_wrote`
- **Search for:** `"pr", "merge"`, `"--auto"`, `"pr", "edit"`, or `"pr", "close"` with no check of who owns the pull
  request; an `is_<owner>` function that returns `False` from an `except`; a confirmed deletion admitted by what a
  plan planned to offer rather than by the offer it recorded, such as `branch["action"] ==` in an eligibility set.
- **Examples:** #1686, #1719, #1778, #2209

### A decision about merged, retired, or owned is made by identity

- **Rule:** Whether something is merged, retired, or owned is decided by its identity, a commit, never by a name that
  can be reused.
- **Why:** A branch name or a path outlives what it named, so a new branch with an old name is judged merged.
- **Use:** `pull_carried:carried`
- **Guard:** `skills/ejryan89-skill-core/scripts/test_pull_carried.py::CarriedTests`,
  `.claude/skills/shepherd-pull-requests/scripts/test_shepherd_pull_requests.py::MergedTest::test_a_worktree_at_main_that_reuses_a_merged_branch_s_name_is_not_named`,
  `.claude/skills/shepherd-pull-requests/scripts/test_shepherd_pull_requests.py::MergedTest::test_a_worktree_with_its_own_commit_that_reuses_a_merged_branch_s_name_is_not_named`,
  `.claude/skills/shepherd-pull-requests/scripts/test_shepherd_pull_requests.py::MergedTest::test_a_local_branch_that_reuses_a_merged_branch_s_name_is_not_named_for_deletion`,
  `.claude/skills/shepherd-pull-requests/scripts/test_shepherd_pull_requests.py::MergedTest::test_only_an_open_pull_request_of_this_repository_holds_a_merged_worktree_s_line_back`
- **Search for:** a branch, a ref, or a path compared by name, `== branch` or `in branches`, in a decision that a
  pull request is merged, a worktree retired, or a file owned; a `headRefName` read where a `headRefOid` or a commit
  is meant; a name or a head read from another repository's pull request, `isCrossRepository` true, in a decision
  about this repository's own branches.
- **Examples:** #1901, #2238

### A time option is a positive finite number of seconds

- **Rule:** An option or setting that gives a time limit accepts only a positive finite number of seconds, and refuses
  zero, a negative number, `inf`, and `nan` when it is read.
- **Why:** `float` accepts `inf`, `nan`, and negatives, so a limit can silently become no limit or an immediate
  timeout.
- **Use:** `seconds_option:positive_seconds`, the argparse type every time option takes
- **Guard:** `tests/validation/python_checks.py::PythonChecksPolicies::test_every_time_option_refuses_what_is_no_limit`,
  `tests/validation/test_python_checks.py::test_time_option_policy_refuses_a_float_time_option`,
  `skills/ejryan89-skill-core/scripts/test_seconds_option.py::test_anything_else_is_refused_naming_the_value`
- **Search for:** `rg -n "type=float"` in an `add_argument(` for a `--timeout`, `--wait`, or other seconds option;
  `type=int` on such an option; `timeout=` given a value read from an argument or a file with no check that it is
  above zero and finite.
- **Examples:** #1756, #1837, #1883, #1899

### A deadline holds every step its promise names

- **Rule:** A deadline a document promises holds every step the promise names, each process a step starts given no
  more than the time left, never only the steps that check the clock themselves.
- **Why:** A step that keeps its own fixed timeout, or that runs between two looks at the clock, can outlast the
  deadline by as much as the input asks of it, and the promise past it, such as the hook's own timeout, is broken
  unseen.
- **Use:** `tools.hub_guard:lookups_within`, which holds every hub lookup made inside it to the reading deadline, and
  `tools.hub_guard:lookup_timeout`, the timeout each of their git calls takes.
- **Guard:** `tests/tools/test_hub_guard.py::ReadingBoundTests::test_the_hub_lookups_after_reading_end_with_the_reading_time`,
  `tests/tools/test_hub_guard.py::ReadingBoundTests::test_a_lookups_git_call_gets_what_is_left_of_the_reading_time`,
  and one test for each step the deadline holds, each failing with that step's bound removed:
  `tests/tools/test_hub_guard.py::ReadingBoundTests::test_a_lookup_while_reading_ends_with_the_reading_time`,
  `tests/tools/test_hub_guard.py::ReadingBoundTests::test_a_lookup_while_judging_ends_with_the_reading_time`, and
  `tests/tools/test_hub_guard.py::ReadingBoundTests::test_a_lookup_the_reading_time_cuts_short_is_refused_with_no_hub_in_view_of_the_session`,
  `tests/tools/test_hub_guard.py::PlaceLookupTests::test_a_lookup_git_call_the_reading_time_cuts_short_refuses_a_command_naming_git`,
  `tests/deployer/test_verify.py::ConverseTests::test_a_program_whose_kill_is_never_reported_ends_in_a_listing_error_within_the_waits_bounds`,
  `skills/ejryan89-skill-core/scripts/test_bounded_process.py::StreamingTests::test_closing_kills_a_command_that_never_reads_while_another_thread_is_blocked_writing`,
  and `skills/audit-ai-config/scripts/test_mcp_handshake.py::test_a_request_the_server_never_reads_fails_within_the_timeout_and_the_server_is_stopped`
- **Search for:** `deadline` or `time_left` in a module whose functions start a process with a fixed `timeout=`, such
  as `GUARD_GIT_TIMEOUT_SECONDS`, while a caller holds a deadline; a loop that looks at the clock once per item while
  each item's work starts processes; a deadline set after work it promises to bound has begun; a loop that gathers
  what it finds into a local and returns it only at the end, while a step inside it can raise the deadline's error;
  a handler that counts the deadline's own error as cut short but not a process's timeout error, which a step given
  the time left raises when the deadline ends inside it; a test that moves the clock only between steps, never
  inside one; a `.kill()` followed by a `.wait()` with no `timeout=`, which waits for as long as the kill goes
  unreported; a write to, flush of, or close of a child's stdin outside the limit its reads keep, since a child that
  stops reading holds a blocking write, and a buffered pipe's close waits for a write another thread has under way.
- **Examples:** #2031, #2087, #2092, #2132, #2184, #2462

### A test's time-out is injected, never raced

- **Rule:** A test that expects a step to run out of time makes it run out, by a stub that raises the time-out or a
  clock the test moves, and a test of anything else reads with a clock that moves only when the test moves it and
  hands each real process it starts a limit of the test's own, through the same seam as the code's real limit.
- **Why:** A real process given a limit it can beat, or a real clock a loaded runner can run out, decides the result
  by the runner's speed, so the test fails at random on a change that touches nothing it reads.
- **Guard:** `tests/tools/test_hub_guard.py::ReadingBoundTests::test_a_reading_in_this_process_takes_its_time_from_the_tests_clock_and_never_from_the_wall`,
  which fails while the guard's tests read with the wall clock or hand pwsh or git a limit other than the test's
  own, and
  `tests/tools/test_hub_guard.py::PlaceLookupTests::test_a_lookup_git_call_the_reading_time_cuts_short_refuses_a_command_naming_git`,
  whose git stub raises the time-out of the call the reading time cuts short.
- **Search for:** a stub that hands a real git or other process a timeout under a second and expects it to expire,
  such as `READING_SECONDS - 0.0005`; `time.monotonic() - started` asserted below a bound when the process timed
  can finish on its own, rather than one that sleeps far past the bound to show it was stopped; a test of a reason
  other than the time-out that reads a command through a real clock while it starts pwsh, bash, or git; a test that
  holds the clock still but lets the code hand a real process what is left of its time, as the guard handed pwsh
  what was left of its reading time.
- **Examples:** #2256, #2355

### A check reaches its whole rule

- **Rule:** A policy, an audit brief, or a gate reaches every place its rule covers, in every language the fact is
  written in and through every indirection that carries it, never a narrower set its author swept by area. Where the
  indirection is a language's open set of spellings, as Python's ways of reaching a name by a value are, the first
  remedy is to restrict the language: refuse the indirect forms at the source, so the check reads only direct ones,
  rather than widen its reader to the next spelling an issue names.
- **Why:** A rule held only where a sweep reached keeps failing in the places it missed, and passes them as checked,
  and a reader widened one spelling at a time never reaches the end of an open set.
- **Use:** `tests.validation.python_checks:indirection_problems`, `tests.validation.python_checks:owner_only_problems`,
  and `tests.validation.python_checks:policed_call_problems`, which refuse dynamic name lookup, an owner-only callable
  referenced outside its owners, and an argument a policed call takes that its readers cannot follow, over the one
  catalogue of lookups by name, `tests.validation.validation_support:NAME_LOOKUPS`;
  `tests.validation.validation_support:scope_problems`, which holds a policy's scope, named as data, to every
  non-test module ruff checks, each part it leaves out stated with its reason, as
  `tests.validation.python_checks:claim_omissions` and `tests.validation.duplication:duplication_omissions` state them;
  `powershell_startup:startup_command`, the one reading of PowerShell's start-up parameters, through which the
  commands policy reads the script pwsh or Windows PowerShell is started to run as it reads a shell's -c script.
- **Guard:** `tests/validation/python_checks.py::PythonChecksPolicies::test_no_name_is_looked_up_dynamically`,
  `tests/validation/python_checks.py::PythonChecksPolicies::test_owner_only_names_are_referenced_only_by_their_owners`,
  `tests/validation/python_checks.py::PythonChecksPolicies::test_policed_calls_take_literal_arguments`,
  `tests/validation/test_python_checks.py::test_each_spelling_the_resolver_did_not_follow_is_refused_at_the_source`,
  `tests/validation/test_shell_readers.py::test_the_command_policy_reads_the_script_a_powershell_start_up_runs`,
  `tests/validation/python_checks.py::PythonChecksPolicies::test_claim_policies_read_every_module_their_rules_speak_of`,
  `tests/validation/duplication.py::DuplicationPolicies::test_the_duplication_policy_reads_every_module_its_rule_speaks_of`,
  `tests/validation/test_python_checks.py::test_a_policy_scope_fails_for_each_module_it_skips_and_each_stale_omission`,
  `tests/validation/test_skill_layout.py::SkillLayoutFixtures::test_an_install_path_is_refused_under_every_spelling_of_the_home`,
  `tests/validation/python_checks.py::PythonChecksPolicies::test_every_process_starts_without_git_repository_variables`,
  `tests/tools/test_mutation_gate.py::test_a_mutant_on_a_continuation_line_runs_against_the_suite_that_executed_its_statement`,
  `.claude/skills/audit-repository/scripts/test_audit_repository.py::ReleaseTests::test_every_brief_carries_the_kinds_of_drift_and_the_report_template`,
  `.claude/skills/review-repository/scripts/test_review_repository.py::ManifestTests::test_every_file_the_design_trust_model_names_reaches_the_trust_boundary_reviewer`
- **Search for:** a policy fix that adds one more spelling of a lookup, a binding, or an unpacking to a reader, where
  an indirection policy could refuse the construct instead; a `*_ALLOWED` entry in `INDIRECTION_ALLOWED`,
  `OWNER_ONLY_ALLOWED`, or `POLICED_CALL_ALLOWED` whose site a direct form could replace;
  a review route whose `include` patterns name files by hand where a document owns the list, such as
  the boundaries of the trust model in `docs/design.md`, and a row of that list naming only a suite or a document, not
  the code that holds it; a policy whose file list is a hand-kept tuple of directories, such as a `*_ROOTS`, that no
  `scope_problems` test holds, or a `glob(`
  narrower than the rule's statement in `CLAUDE.md` or `docs/`; `rg -n "glob\(\"\*\.sh\"\)|suffix == \"\.sh\"" tests/validation/`,
  a policy that reads one shell, or a shell's `-c` script but not PowerShell's `-Command`; a fixture suite whose violating inputs cover one of the forms the rule names; a
  policy that refuses a sentence as an exact substring, which a capital, a reflowed line, or an optional word walks
  past; an audit brief that asks only for drift; a `scope_problems` call whose modules are filtered by the roots it
  is held to, which compares a list with itself.
- **Examples:** #1366, #1598, #1678, #1722, #2260, #2352, #1884, which adds the check that holds a policy's scope
  to its rule, #2315, whose resolver spellings the indirection policies refuse at the source, and #2388

### A check reads its subject through its consumer's reader

- **Rule:** A check reads what it holds through the reader or builder its consumer uses, never a re-derivation of the
  same fact.
- **Why:** A check that derives the set itself agrees with its own copy and passes what the consumer reads otherwise.
- **Use:** `deployer.render:shipped_files`, `suite_names:is_suite_name`,
  `runtime_support:carried_needs`, `runtime_support:python_git_subcommands`
- **Guard:** `tests/validation/test_suite_discovery.py::test_a_parametrized_test_is_never_recorded_since_the_sharded_run_collects_only_its_cases`
- **Search for:** `rglob("*")` in `tests/validation/` where `shipped_files` is meant; a `["claude"`, `["codex"`, or
  `["copilot"` command line in `tools/` built outside the function real runs use; a policy that reads test names from
  source where the runner collects cases.
- **Examples:** #1368, #1508, #1853, #1859

### A statement about another program cites its current source

- **Rule:** A statement about another program cites that program's current source, and its command lines and the
  changelog entries that touch a term the code uses are exercised or read against its latest release on a schedule.
- **Why:** A release that drops a flag or changes a behavior otherwise breaks every run, or leaves a document wrong,
  until someone happens to probe it.
- **Use:** `check_provenance:load`, `guidance_record:canonical_url`, and `tools.runtime_changelogs:watch`, which
  the Runtime Changelogs workflow runs weekly as an advisory watch that refuses nothing: it lists the
  changelog entries and moved guidance pages for a reader to act on; a policy that each runtime statement in the
  support pages links a page the guidance record lists is the mechanism in #1912.
- **Guard:** `.github/workflows/runtime-command-lines.yml`,
  `tests/tools/test_runtime_command_lines.py::CapturedTests::test_each_runtimes_real_refusal_of_a_flag_is_read_as_one`,
  `tests/tools/test_captured_samples.py::test_a_sample_older_than_its_runtimes_version_is_behind_and_one_at_it_is_not`
- **Search for:** a sentence about a runtime's behavior in `docs/*-support.md` or `docs/installation.md` with no
  link; `always`, `never`, `only`, `cannot`, or `does not` in a support page; a `"--` flag literal in a list passed to
  `copilot`, `codex`, or `claude` that `tools/runtime_command_lines.py` does not build.
- **Examples:** #1362, #1409, #1538, #1573, #1581, #1583, #1842, #1856

### A fix reaches every sibling of its construct

- **Rule:** A defect's fix starts by searching for the same construct elsewhere, the other callers of the same reader
  or writer, and its twin in the other shell or runtime, and fixes each sibling or files it.
- **Why:** A fix that reaches only the reported instance leaves the rest to be found again; the twin sweep before each
  tag searches every fix since the last one again, for the twins its own search missed.
- **Guard:** `.claude/skills/implement-change/SKILL.md#3-bug-class`,
  `.claude/skills/audit-repository/SKILL.md#4-confirm-before-filing`, the fix body's Forms considered section,
  `tests/validation/pull_request_body.py::PullRequestBodyPolicies::test_a_fix_body_fills_its_forms_considered_section`,
  and the fix reviewer given the closing issue,
  `.claude/skills/review-repository/scripts/test_review_repository.py::SiblingSpellingsTests`
- **Search for:** the name of the function, call, or literal the fix changed, across `skills/`, `deployer/`, `tools/`,
  and `.claude/skills/`; its Bash twin in `*.sh` and its PowerShell twin in `*.ps1`; the **Search for** of the
  pattern above that the defect is an instance of.
- **Examples:** #1369, #1521, #2184, #2248, #2297

### A foreign function declares its types

- **Rule:** Every foreign function a module calls through `ctypes` has its `argtypes` and `restype` set in that module.
- **Why:** An undeclared function passes and returns a C `int`, which truncates a handle on a 64-bit process.
- **Guard:** `tests/validation/python_checks.py::PythonChecksPolicies::test_every_ctypes_foreign_function_called_is_declared`,
  `tests/validation/test_python_checks.py::test_ctypes_policy_refuses_a_library_a_base_class_binds_in_another_module`
- **Search for:** `ctypes.windll.` or `WinDLL(` in a module, with a function of it called and no `.argtypes =` or
  `.restype =` for that function; a `self.` or `cls.` read in a class whose base is imported from a module that binds
  a library, which the policy reads in its own module only unless the base is followed.
- **Examples:** #1363, #1600, #2369

### A pipe's reader never ends before its writer under `pipefail`

- **Rule:** A Bash suite that sets `pipefail` reads text it matches from a here-string, and a first line by parameter
  expansion, never through a pipe into a reader that exits early: `grep -q`, `-m`, `-l`, or `-L`, `rg -q`, `-m`, or
  `-l`, or `head`.
- **Why:** `grep -q` exits at its match and `head` at its count, the writer then fails with `SIGPIPE`, and `pipefail`
  makes that an intermittent failure.
- **Guard:** `tests/validation/suite_discovery.py::SuiteDiscoveryPolicies::test_no_bash_suite_pipes_into_a_reader_that_ends_early`
- **Search for:** `| grep -q`, `| grep -m`, `| grep -l`, `| grep -L`, `| rg -q`, or `| head` in any form, `-n`, `-c`,
  `-1`, or alone, in a `.sh` file that turns `pipefail` on by `set`, `shopt -so`, `builtin set`, `bash -o pipefail`, or
  `SHELLOPTS`; the reader also in a `{ ...; }` or `( ... )` group or an `if`, `while`, `until`, `for`, `select`, or
  `case` after the pipe, in a function the pipe runs, in a `bash -c` or `eval` script, in a `$(...)` or backtick
  substitution, whether the pipe stands inside it or feeds the command, function, or `bash -c` or `eval` script that
  holds it, as in `seq 9 | echo $(head -n 1)` or `f() { echo $(head -n 1); }; seq 9 | f`, by a path such as `/usr/bin/head`, or behind `env` (and `env -S`), `exec`, `timeout`, `command`, or `sudo`, each by a
  path or not, and `head` whose last count is positive, as in `head -n -1 -n 1`.
- **Examples:** #1557, #1602, #2151, #2417

### A program whose status is the verdict starts with no shell above it

- **Rule:** A check whose verdict is a program's exit status starts that program as its own process, judged by the
  runner, never as one link of a shell's command chain or loop.
- **Why:** On Windows, an MSYS Bash reads a child that Windows ends as exit 0, so `set -e` and `&&` go on and the
  ended check passes; only the last command's low byte reaches the runner.
- **Use:** `tests.validation.shell_targets:run_bash_suite`, `tests.validation.job_pool:run_process`, which names a
  termination as one.
- **Guard:** `tests/validation/test_shell_targets.py::ProgramEndingFixtures`
- **Search for:** `" && ".join(` building a command line a shell runs; `"-c"` beside `find_git_bash()` or
  `find_bash()` whose script starts several programs and whose one status is read as their verdict; a `( ... )`
  subshell's `$?` in a Bash loop read as a child's verdict.
- **Examples:** #654, #1355, #1798, #1963

### A file handed to another process appears whole

- **Rule:** A file one process writes for another is written to a temporary name beside it and renamed into place.
- **Why:** A reader that runs before its writer finishes reads an empty or partial file.
- **Use:** `atomic_file:replace_text`, `atomic_file:replace_bytes`
- **No guard:** Whether a reader can run before its writer finishes depends on the processes' timing, which reading
  the code cannot settle. The canary's own case is held by
  `tests/tools/test_runtime_canary.py::test_an_empty_pid_file_is_a_named_wait_rather_than_a_parse_error`, and review
  holds the rest.
- **Search for:** `.write_text(` or `open(` with `"w"` on a pid, lock, status, or record file another process polls;
  a reader that loops until `.exists()` and then reads at once.
- **Examples:** #1354

### A deletion stopped partway is told from a change

- **Rule:** A copy whose content a later step judges, such as by a recorded hash, is renamed to a name only its deleter
  uses before it is deleted, and the step that meets that name finishes the deletion.
- **Why:** A deletion a kill or an error stops partway leaves a remainder that matches nothing, which a recovery that
  reads it where the copy stood takes for someone's change and keeps or refuses.
- **Use:** `deployer.journal:_delete_copy`, which renames the copy to `deployer.names:deletion_name` before deleting
  it, or to `deployer.names:spare_deletion_name` while a held leftover stays there, and
  `deployer.journal:_finish_deletion`, which finishes what a stopped deletion left under either name and only names
  what it cannot delete, so a held leftover fails no later run.
- **Guard:** `tests/deployer/test_backup_settlement.py::SettlementTests::test_finalization_and_recovery_settle_each_backup_alike`,
  whose rows include a deletion killed partway and one failing partway, and
  `tests/deployer/test_recovery_migration.py::RecoveryTests::test_a_rollback_killed_partway_through_deleting_the_installed_copy_is_finished_by_the_next`,
  and, for a leftover that cannot be deleted, `tests/deployer/test_recovery_migration.py::RecoveryFailureTests::test_a_held_leftover_at_the_deletion_name_fails_no_later_rollback_or_finalization`
  and the tests of a deletion stopped partway beside one, such as
  `tests/deployer/test_recovery_migration.py::RecoveryFailureTests::test_a_rollback_killed_partway_through_its_deletion_beside_a_held_leftover_is_finished_by_the_next`
- **Search for:** `fsops.remove(` or `shutil.rmtree(` of a path a later step reads with `hash_path(`, `_hash_existing(`,
  or `backup_unchanged(`; a retry that compares a tree it may have been deleting when it stopped.
- **Examples:** #2314, #2449

### A missing record decides nothing it would have decided

- **Rule:** A decision a record makes, such as whether a run was committed, is never taken from the default a loader
  gives a missing record while evidence written after the record remains, and the writer writes the record before
  that evidence, so a missing record beside it can only mean the record was moved or lost.
- **Why:** A missing record read as its default answers in its place, as a manifest moved aside once read as "no run
  committed" and rolled back a run it had committed.
- **Use:** `deployer.manifest:save_if_missing`, which `deployer.journal:prepare_roots` runs before a run's journal;
  `deployer.journal:check_manifest_beside_runs`, which refuses a journal beside no manifest before recovery; and
  `deployer.journal:_committed_run_id`, which answers None, undecided, for a missing manifest.
- **Guard:** `tests/deployer/test_state_files.py::StateFileTests::test_a_dry_run_refuses_each_state_the_deployment_refuses_before_recovery_pending_run_or_not`,
  whose "manifest missing beside a pending run" row holds the deployment and the dry run to one refusal, and
  `tests/deployer/test_recovery_migration.py::ManifestStateTests`
- **Search for:** `manifest.load(` or another loader that returns a default for a missing file, read to decide
  something a journal, a lock, or a staging folder written after that file still depends on; `.get("last_run_id", "")`
  read where the file may be missing.
- **Examples:** #2312

### A grant is earned by a step that names its tool

- **Rule:** A skill's `allowed-tools` grant counts only for a tool one of its steps names.
- **Why:** A grant a step only implies widens what the skill may do with nothing in the skill that needs it.
- **Guard:** `tests/validation/test_skill_grants.py::test_grant_policy_fails_a_grant_whose_tool_a_step_only_implies`
- **Search for:** an entry of `allowed-tools` in a `SKILL.md` frontmatter whose tool name no step's text names.
- **Examples:** #1482, #1579

### The installed release's update runs against the current tree

- **Rule:** The update an installed release runs, which reaches into the fast-forwarded clone, is tested against the
  current tree, so a move of a path it reads fails before it ships.
- **Why:** A rename the release's own update did not expect breaks the one path every installation takes.
- **Guard:** `tests/deployer/test_installed_update.py::test_the_release_names_only_clone_paths_the_current_tree_keeps`,
  `tests/deployer/test_installed_update.py::test_the_update_fails_when_a_commit_moves_a_path_the_release_reads`
- **Search for:** `"$CLONE/` in the installed `update.sh`, and a rename or removal of the path it names.
- **Examples:** #1507, #1607

### A grader is read against the runs it passes

- **Rule:** A script that judges a run is itself checked by reading a sampled passing run against its transcript.
- **Why:** A grader that passes a wrong run fails nothing, and no passing run was ever read to show it.
- **Guard:** `.claude/skills/evaluate-skill/SKILL.md#5-read-the-sampled-transcript`
- **Search for:** a judge in `tools/skill_evals.py` or a scenario's check that sets a pass from a field's presence
  alone; a scenario whose expectation reads only the report the run wrote.
- **Examples:** #1738, #1774

### A literal says what its test means

- **Rule:** A string literal meant as a path or a pattern is raw, or escapes each backslash, so no control escape
  hides in it.
- **Why:** `"\bin"` is a backspace and `in`, and `"C:\temp"` holds a tab, so a test of a rooted path passes without
  ever trying one, and neither ruff nor the interpreter warns.
- **Guard:** `tests/validation/literal_escapes.py::LiteralEscapePolicies::test_no_literal_writes_a_control_escape_where_it_reads_as_a_path_or_pattern`
- **Search for:** `rg -n '"[^"]*\\[abfnrtv][A-Za-z]'` in non-raw strings, where the escape follows a drive letter
  and colon or a slash, or `\a`, `\b`, or `\v` sits beside a word; a non-raw pattern given to `re` holding `\b`; a
  non-raw literal given to `Path` holding any control escape.
- **Examples:** #1904, #1924

### A gate runs the check that owns what it mutates

- **Rule:** A gate that tests a change against checks runs, for each part it changes, the check that reads that part,
  or leaves the part to that check and names it, never a check that cannot see it.
- **Why:** A mutant no run check reads survives every time, costs run time, and is ruled equivalent by hand on every
  pull request that touches it.
- **Use:** `tools.mutation_gate:sanction_table`, `tools.mutation_gate:policy_tables`, which find a module-level
  sanction table and the validation policy that reads it.
- **Guard:** `tests/tools/test_mutation_gate.py::SanctionTableFixtures`,
  `tests/tools/test_mutation_gate.py::test_every_validation_policy_is_mutated_and_its_fixture_suite_reaches_it`,
  `tests/tools/test_mutation_gate.py::test_every_validation_program_is_mutated_and_its_own_suite_reaches_it`
- **Search for:** a mutant of a module-level `*_ALLOWED` table in the `Mutation gate` step summary; a gate or audit
  that runs a module's suites on what only a policy under `tests/validation/` reads; a new `*_ALLOWED` name with no
  `*_ALLOWANCE` constant naming it in a policy; a folder of checked code, such as the policies under
  `tests/validation/`, that `is_gate_module` leaves out; a "Mutation survivors" comment that applied mutants by hand
  rather than through the gate's `unrun` step.
- **Examples:** #1933, #1938, #2174

### A gate runs where the behavior it judges differs

- **Rule:** A gate that judges code whose behavior differs by platform runs that code on each platform where it
  differs, chosen by a rule that reads the code, never a list a contributor keeps, and counts a fault caught on any of
  them as caught.
- **Why:** A mutant whose effect shows only on Windows, such as a path compared by its text, a name NTFS folds, an
  8.3 name, or a junction, survives a Linux-only gate however well it is tested, and each pull request then rules it
  equivalent by hand where the gate's record cannot show it.
- **Use:** `tools.mutation_gate:runs_on_windows`, the rule that sends a module's mutants to the Windows leg, and
  `tools.mutation_gate:verdict`, which counts a mutant either leg killed as killed.
- **Guard:** `tests/tools/test_mutation_gate.py::test_the_windows_leg_runs_the_platform_seam_each_module_that_imports_it_or_holds_its_table_and_each_that_branches`,
  `tests/tools/test_mutation_gate.py::test_the_windows_leg_runs_each_seam_module_claude_md_names_and_each_module_that_branches_on_the_platform`,
  `tests/tools/test_mutation_gate.py::test_the_windows_leg_runs_a_module_that_names_any_token_of_the_platform_catalogue`,
  `tests/tools/test_mutation_gate.py::test_the_windows_leg_runs_an_importer_of_each_seam_module_claude_md_names`,
  `tests/tools/test_mutation_gate.py::SplitRunFixtures::test_a_mutant_that_survives_the_linux_leg_and_the_windows_leg_kills_reads_as_killed`,
  `tests/validation/test_workflows.py::test_a_mutation_job_off_its_shape_fails`
- **Search for:** a survivor named "Linux-invisible" or "Windows-only" in a pull request's "Mutation survivors"
  comment; a gate or canary job with one `runs-on` that judges a module importing `platform_support`, `path_keys`,
  `link_probe`, or `device_names`; a hand-kept list of modules or suites a platform leg runs.
- **Examples:** #1932, #1948, #1960

### A fixture derives what it copies from the source

- **Rule:** A suite that builds a throwaway copy of ejryan89-skill-core's modules copies a module with every module it
  imports, read from the source, and leaves one out only by removing it from the built tree by name.
- **Why:** A hand-kept list of modules to copy is one short as soon as a copied module imports another, and the
  fixture's script then fails with `ModuleNotFoundError` in a suite the change never touched.
- **Use:** `module_closure:copy_with_imports`, given the modules a fixture's script needs, or
  `module_closure:imports_of` for a script of another skill.
- **Guard:** `skills/ejryan89-skill-core/scripts/test_module_closure.py::ModuleClosureTests::test_a_new_import_of_a_copied_module_is_copied_without_any_list_edited`
- **Search for:** a tuple or list of `"<module>.py"` names in a suite next to `write_bytes`, `write_text`, or
  `copyfile` under a path naming `ejryan89-skill-core`; `with_name(module)` in a loop over such names.
- **Examples:** #1939

### Pin a site's output before routing it through a shared helper

- **Rule:** A change that routes existing call sites through a shared helper first pins each site's current output
  against a literal, the bytes it writes, the key it compares, or the line it prints, then converts the site and keeps
  the pin, saying beside that site's test when the conversion changes its output on purpose.
- **Why:** Without a pin from before the conversion, a converted site whose output changed passes, and each site's
  arguments survive the mutation gate in bulk.
- **No guard:** Whether a test pins a site's output is a judgment reading the code cannot settle, so no validation
  policy refuses it. `review-repository`'s `untested-changes` reviewer reports a converted call site whose output no
  test pins, and "Shared code" in [implementing-changes.md](implementing-changes.md#shared-code) states the rule.
- **Search for:** a pull request that replaces `json.dump(`, `json.dumps(`, `.write_text(`, or a `normcase(` or
  `casefold()` comparison with a call to `json_writer` or `path_key` across many sites; a `textual=`, `indent=`,
  `ensure_ascii=`, `sort_keys=`, or `mode=` argument at a converted site that the `Mutation gate` step summary lists
  as a survivor.
- **Examples:** #1948, #1949, #1953, #1957, #1961
