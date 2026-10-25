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
names no **Search for** form, names neither a mechanism, a guard, nor a reason, or names both a guard and a reason, and
each mechanism or guard that does not resolve, so one renamed or removed fails validation until its pattern follows it.

## Patterns

### A failure is never read as absence

- **Rule:** A reader that cannot tell whether something is there reports that it could not tell, never that it is
  absent or that nothing was wrong.
- **Why:** An unreadable path, a failed command, or a git error read as "not found" otherwise reads as a clean pass.
- **Use:** `file_presence:is_present`, which tells a file, nothing, and something else apart;
  `link_probe:is_link_or_junction`, which lets the error of an unreadable path through; and
  `deployer.source_ref:TagsUnreadable`, which keeps git's reason when the tags cannot be read. The found, absent, and
  could-not-tell lookups of git and the GitHub CLI come with #1881.
- **Guard:** `tests/validation/python_checks.py::PythonChecksPolicies::test_link_probe_is_the_one_link_test`,
  `tests/validation/python_checks.py::PythonChecksPolicies::test_no_process_runner_result_is_discarded_unchecked`,
  `tests/tools/test_mutation_gate.py::test_a_failed_or_timed_out_coverage_step_names_the_step_and_its_output`,
  `tests/deployer/test_deploy_at.py::DeployAtTests::test_a_pin_whose_tags_git_cannot_read_is_refused_with_gits_reason`
- **Search for:** `returncode == 0` or `returncode != 0` beside a `git` or `gh` command outside `git_client.py` and
  `github_client.py`; `except GitError:` followed by `return None` or `return False`; `except OSError:` followed by
  `return False`, `return None`, or `return []`; `rg -n "if not \w+(\.\w+)*\.(is_file|exists)\(\):\s*$" -A1`
  followed by a `return None`, `return []`, or `return False`, where `is_present` should probe; `if not result.stdout`
  read as nothing found.
- **Examples:** #1320, #1365, #1599, #1601, #1887, #1888

### Evidence that was never produced is never a pass

- **Rule:** A guard, gate, verdict, count, or measurement whose evidence never existed reports that case apart from a
  pass.
- **Why:** A step that never ran, a listing that failed, or an empty input otherwise reads as everything checked and
  nothing found.
- **Use:** `conditions:MissingEvidence`, `deployer.discovery:ListingError`
- **Guard:** `skills/ejryan89-code-review-core/scripts/test_review_pipeline.py::test_finalize_refuses_a_result_the_guard_never_claimed_with_nothing_recorded`,
  `skills/audit-ai-config/scripts/test_audit_ai_config.py::test_a_repository_whose_files_show_nothing_says_the_checks_did_not_run`,
  `tests/tools/test_hub_guard.py::test_an_event_that_names_no_command_or_file_is_allowed_with_a_note_never_unseen`,
  `.claude/skills/evaluate-skill/SKILL.md#5-read-the-sampled-transcript`
- **Search for:** `rg -n "os\.walk\(" | rg -v onerror`, a walk that skips what it cannot read; `if not <input>:`
  followed by `return True` or `return 0`; a count, verdict, or summary computed over a list that is empty because its
  step never ran; a skill-evaluation scenario that holds `no_finding_above` and no `verdict`.
- **Examples:** #1443, #1462, #1603, #1821, #1824, #1854

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
- **Use:** `console:run_main`, `console:failed_line`, `skill_roots:MissingSibling`; holding each entry point's exit
  codes to its docstring is the mechanism in #1910.
- **Guard:** `tests/malformed_input/test_malformed_input.py::test_failure_names_a_write_before_a_refusal_an_escape_and_an_undocumented_acceptance`,
  `tests/validation/skill_grants.py::SkillGrantsPolicies::test_skill_markdown_names_every_status_line_its_scripts_print`
- **Search for:** `rg -nU "except (OSError|ValueError|UnicodeDecodeError)[^\n]*:\n\s+(print\([^\n]*\)\n\s+)?return 1\b"`,
  a handler that ends every failure in one code; two probes under one `except` that print one message; an `except`
  around a `git` or `gh` call that drops the exception's text; `FAILED KeyError`, `FAILED TypeError`, or
  `FAILED AttributeError` expected by a test.
- **Examples:** #1511, #1840, #1855, #1910

### A caller catches the whole error family its callee raises on content

- **Rule:** A caller handles every error its callee raises for bad content, by the family the callee exports or a
  superclass of it, never by one member.
- **Why:** The member left out escapes as an unhandled error where the caller promised a refusal.
- **Use:** `json_reader:JsonRefused`, `review_io:CONTENT_ERRORS`, `file_presence:NotAFile`; a content-error family
  for each shared reader, and a policy that a handler names it, is the mechanism in #1907.
- **Guard:** `tests/malformed_input/test_malformed_input.py::test_each_reader_refuses_or_documents_accepting_each_case`,
  `tests/validation/python_checks.py::PythonChecksPolicies::test_json_reader_is_the_one_parse_of_json`
- **Search for:** `except json.JSONDecodeError`, `except UnicodeDecodeError:`, or `except tomllib.TOMLDecodeError:`
  around a reader that raises more; `except ValueError` near an `xml_reader`, `tomllib`, or `csv.` call; an `except X`
  around a helper that raises a class outside `X`.
- **Examples:** #1634, #1816, #1874

### A result cut short says it was cut

- **Rule:** A result a limit, budget, cap, or page size cut short says so, by a flag, a line, or a field its reader
  checks.
- **Why:** A listing cut at its limit otherwise reads as the whole set, and what lies past the cut is never judged.
- **Use:** `bounded_process:run_bounded`, `pr_status:_list_pulls`, `audit_repository:duplicates`,
  `review_guidance:overlaps`, `tools.mutation_gate:unfinished_line`; a capped GitHub CLI listing read outside the client is
  refused by the mechanism in #1881.
- **Guard:** `.claude/skills/audit-repository/scripts/test_audit_repository.py::DuplicateTests::test_a_search_that_finds_exactly_what_it_lists_is_not_cut`,
  `skills/review-insights/scripts/test_review_guidance.py::ReportTests::test_a_search_that_stops_at_its_budget_is_named_in_a_fact_line_and_the_input`,
  `tests/tools/test_mutation_gate.py::SummaryFixtures::test_a_run_the_cap_cut_short_says_so_on_its_first_line_naming_each_module_and_its_counts`
- **Search for:** `"--limit"` in a `gh` call whose result is never compared with the limit; a slice such as
  `[:MAX_RELATED_TESTS]` or `rg -n "\[:[A-Z_a-z]*(LIMIT|MAX|BUDGET|CAP)[A-Z_a-z]*\]"` with no flag beside it; a
  `break` on a counter in a loop that returns no sign it stopped.
- **Examples:** #1845, #1857, #1864, #1891

### A number read from text is bounded

- **Rule:** A whole number read from text, an argument, a file, or another program's output is read with its bounds
  and its digit count limited, and a value outside them is the reader's documented refusal.
- **Why:** An unbounded `int()` turns a hostile or garbled value into a `ValueError` past the digit limit, a huge
  allocation, or a negative count the code never expected.
- **Use:** `whole_number:read`, which raises the caller's named error, and `whole_number:parse`, which gives None, for a
  number in any other text, both capped at `whole_number:DIGIT_LIMIT`; `json_reader:loads` for a JSON number, which
  refuses one past the same cap, and `typed_fields:FieldReader.integer` for its bounds.
- **Guard:** `tests/validation/python_checks.py::PythonChecksPolicies::test_whole_number_is_the_one_reader_of_a_number_from_text`,
  `tests/validation/test_python_checks.py::test_int_parse_policy_refuses_a_group_or_a_digit_tested_value_outside_whole_number`,
  `skills/ejryan89-skill-core/scripts/test_whole_number.py::ReadTests`,
  `skills/ejryan89-skill-core/scripts/test_json_reader.py::RefusalTests::test_an_integer_past_the_shared_digit_cap_is_refused_whatever_its_sign`,
  `skills/ejryan89-skill-core/scripts/test_typed_fields.py::FieldReaderTests::test_an_integer_within_its_bounds`
- **Search for:** `rg -n "int\(\w+\.group\(|int\(match\["`, a number taken from a regular expression whose pattern
  has `\d+` rather than `\d{1,N}`; `.isdigit()` before an `int(`, which admits digits `int` reads past any bound;
  `int(` on a `.split(` of another program's output; `float(` on text from a file or an argument with no
  `math.isfinite`; `int(` on each item of a `.findall(`, or on text a `re.fullmatch(r"[0-9]+", ...)` admitted, which
  bounds no length; a refusal worded "more than N digits" for a `\d` group,
  which also admits digits that are not ASCII.
- **Examples:** #1611, #1729, #1879, #1886, #1895, #1897

### One fact has one owner

- **Rule:** A fact the repository states, a vocabulary, a set the code owns, a status line, a prompt list, or a
  normalizer, lives in one place, and every other place reads it from there or is compared with it.
- **Why:** A copy begins identical and then drifts, so a document, a test, or a second module keeps the old fact while
  the owner moves on.
- **Use:** `tests.validation.duplication:_shared_facts`, the registry of facts each read from its owner; where a fact
  is a behavior, the owner's function, such as `frontmatter:Frontmatter.user_only`, `frontmatter:user_only_value` for
  another reader's values, and `typed_text:read_typed`, the read of a title or body file without its byte-order mark.
  A module more than one part of the repository needs moves to skill-core.
- **Guard:** `tests/validation/duplication.py::DuplicationPolicies::test_repository_owners_hold_every_shared_fact`,
  `tests/validation/duplication.py::DuplicationPolicies::test_repository_copies_only_what_it_sanctions`,
  `tests/validation/test_duplication.py::SharedFactPolicy`,
  `tests/validation/python_checks.py::PythonChecksPolicies::test_json_reader_is_the_one_parse_of_json`,
  `tests/validation/skill_grants.py::SkillGrantsPolicies::test_skill_markdown_names_every_status_line_its_scripts_print`,
  `tests/validation/workflows.py::WorkflowsPolicies::test_deployable_workflow_answers_every_prompt_configure_declares_in_both_jobs`,
  `tests/tools/test_hub_guard.py::test_the_refused_subcommands_are_the_writing_subcommands`,
  `tests/tools/test_hub_guard.py::test_each_prefix_program_the_contract_names_is_read_as_the_command_it_starts`,
  `tests/code-review/test_format_contract.py::test_the_threat_model_rows_name_each_file_the_analyzer_inventory_parses`,
  `.claude/skills/review-repository/scripts/test_review_repository.py::test_the_trust_boundary_route_matches_its_scripts_and_each_choice_matches_a_file`
- **Search for:** a module-level `frozenset({` or tuple of strings whose members another module also spells; a
  document's list of names, flags, or modules that a module's constant holds; a `print(` of a status line no
  `SKILL.md` names; a second `json.loads(`, `os.lstat(`, or other helper beside the one skill-core owns;
  `rg -n "def (_?inside|strictly_inside|_lexically_inside)\b"` and other helpers of one job held in several modules.
- **Examples:** #1349, #1477, #1495, #1596, #1679

### A promise in prose is held where its reader reads it

- **Rule:** A promise a document, a `SKILL.md`, or a docstring makes is held by a test that reads it from there, and a
  universal claim names its exceptions.
- **Why:** A test that restates a document's wording pins the drift instead of the promise, and a claim of "never" or
  "every" with an unstated exception misleads the reader who relies on it.
- **Use:** `tests.validation.skill_layout:runtime_page_problems`
- **Guard:** `tests/validation/markdown_links.py::MarkdownLinksPolicies::test_every_test_a_document_cites_exists`,
  `tests/validation/test_skill_layout.py::SkillLayoutFixtures::test_runtime_page_policy_holds_each_support_statement_to_the_metadata`
- **Search for:** an `assertEqual(` or `assertIn(` on a literal sentence a document states, where the test could read
  the document or the shared constant; `never`, `always`, `nothing`, `every`, or `only` in a `SKILL.md` or a
  docstring, each checked for a test of its exception; a docstring's exit codes against the function's `return` and
  `sys.exit(` values.
- **Examples:** #1317, #1584, #1629, #1861, #1864, #1869

### A reader refuses a shape it does not know

- **Rule:** A reader of input or of another program's output accepts the shapes it was written and tested for and
  refuses or reports as unknown any other, never passing over it and judging what is left.
- **Why:** A skipped entry, an ignored field, or a top-level value of the wrong type lets the reader judge a partial
  input as whole.
- **Use:** `typed_fields:FieldReader.json_object`, which refuses an unknown key, `xml_reader:read_xml`, which refuses
  a document type or entity declaration, and `deployer.discovery:ListingError`.
- **Guard:** `skills/ejryan89-skill-core/scripts/test_typed_fields.py::FieldReaderTests::test_an_object_holds_its_required_fields_and_no_unknown_one`,
  `tests/malformed_input/test_malformed_input.py::test_each_reader_refuses_or_documents_accepting_each_case`,
  `tests/malformed_input/test_malformed_input.py::test_a_field_case_changes_one_field_of_the_readers_own_document`
- **Search for:** `if not isinstance(` followed by `continue` in a loop over another program's entries; `.get(` with a
  default, or `["key"]`, on `json_reader` output with no `isinstance` or `FieldReader` check; a top-level value used
  without `isinstance(value, dict)`; a regular expression over another program's output with no branch for no match.
- **Examples:** #1890, #1892

### A reader of another program's language reads it as that program does

- **Rule:** A reader of another program's language or output reads it as that program does, held by a differential
  test against the program itself or by output captured from it with its provenance.
- **Why:** A hand-written reader that misses a construct the program reads lets a command or an entry pass unjudged.
- **Use:** `tools.hub_guard:powershell_reads_input`
- **Guard:** `tests/tools/test_bash_syntax.py::ArrayAssignmentTests::test_an_array_assignment_to_a_subscript_is_read_where_bash_reads_an_array`,
  `tests/tools/test_hub_guard.py::test_a_git_call_after_any_assignment_bash_reads_before_it_is_judged_as_the_bare_call`,
  `tests/tools/test_captured_samples.py::test_every_sample_has_its_provenance_and_every_provenance_its_sample`,
  `tests/tools/test_runtime_changelogs.py::test_a_term_matches_only_where_a_real_entry_names_it`
- **Search for:** an option table in `tools/hub_guard.py` with no differential case; a `re.compile(` in
  `tools/runtime_*.py` or `deployer/discovery.py` with no sample under `tests/fixtures/captured-output/`; each
  interpreter's form for a script on its standard input, `bash -s`, `pwsh -File -`, `python -`, `node -`, against the
  forms the guard names; a test whose sample output was typed rather than captured.
- **Examples:** #1412, #1429, #1430, #1592, #1606, #1843, #1851, #1865

### A recursive parser of input has a depth bound

- **Rule:** A parser that recurses into input it does not control stops at a stated depth and ends in its documented
  refusal, never in a `RecursionError`.
- **Why:** Nesting past Python's recursion limit escapes the reader as an unhandled error instead of its refusal.
- **Use:** `json_reader:loads`, `msbuild_evaluation:MAX_CHOOSE_DEPTH`; the depth bound for the other recursive parsers
  comes with #1880.
- **Guard:** `skills/ejryan89-skill-core/scripts/test_json_reader.py::RefusalTests::test_nesting_past_the_recursion_limit_is_refused`,
  `tests/tools/test_hub_guard.py::EndToEndTests::test_bash_nested_deeper_than_the_reader_follows_is_refused_alike_in_its_own_process`,
  `tests/validation/python_checks.py::PythonChecksPolicies::test_json_reader_is_the_one_parse_of_json`
- **Search for:** a function that calls itself on a child of its input with no depth parameter and no
  `except RecursionError`; `literal_eval(` or `ast.parse(` on text from a file or another program; `json.loads(`
  outside `json_reader.py`.
- **Examples:** #1613, #1617, #1729, #1886, #1893

### A path taken from input is judged inside its tree before anything opens it

- **Rule:** A path taken from input is judged inside its tree lexically, then resolved, then probed for a link, before
  anything opens it.
- **Why:** A parent segment, a link, or an absolute path otherwise leads a reader or a writer outside the tree it was
  given.
- **Use:** `path_containment:refusal`, which judges an entry from input as written, as resolved, and through the link
  probe, and names why; `path_containment:within`, the one comparison of a path with its tree, which every other
  containment test calls on paths it has made absolute or resolved; `path_containment:posix_within` for a repository
  path a listing gives; `dotnet_solution:refusal` and `dotnet_format_targets:listed_refusal`, which compare through it a
  path a solution or a file list names; `link_probe:is_plain_file`, `tools.checkout:checkout_root`,
  `memory_audit:bare_name`.
- **Guard:** `tests/validation/duplication.py::DuplicationPolicies::test_path_containment_is_the_one_containment_test`,
  `skills/ejryan89-skill-core/scripts/test_path_containment.py::RefusalTests`,
  `skills/dotnet-format/scripts/test_dotnet_format_targets.py::ListedRefusalTests`,
  `skills/curate-agent-memory/scripts/test_memory_audit.py::ReindexTests::test_a_target_with_a_parent_segment_is_never_read`,
  `tests/tools/test_plugin_canary.py::test_an_installed_plugin_the_canary_cannot_place_in_the_home_is_refused_by_name`,
  `tests/tools/test_checkout.py::test_every_tool_that_takes_root_parses_it_with_checkout_root_and_the_module_names_it`
- **Search for:** `root / name` or `joinpath(*` where the operand comes from input and no `refusal` judged it first;
  `within(` on a path from input with no lexical check before it; `is_relative_to(`, `commonpath(`, or
  `startswith(f"{` with a trailing `/` outside `path_containment.py`, which the guard refuses unless a
  `CONTAINMENT_ALLOWED` entry states why.
- **Examples:** #1780, #1782, #1818, #1905

### Every check comes before the first write

- **Rule:** A step validates everything it can before it writes anything, and a refusal leaves nothing behind.
- **Why:** A refusal after a write leaves a half-made output or a changed home the next run trips over.
- **Use:** `atomic_file:replace_bytes`, `review_io:atomic_write_json`; the same check for each entry point under tools
  that writes an output is the mechanism in #1911.
- **Guard:** `tests/deployer/test_invariants.py::RefusalBeforeMutationGuardTests::test_a_refusal_raised_after_the_deployment_changed_the_home_fails_the_test`,
  `tests/malformed_input/test_malformed_input.py::test_failure_names_a_write_before_a_refusal_an_escape_and_an_undocumented_acceptance`
- **Search for:** a `mkdir(parents=True` or `mkdtemp(` in `tools/` before the arguments are checked; a write before a
  `raise` of the step's refusal class.
- **Examples:** #1396, #1855, #1876, #1911

### What one step accepts, every later step can read and write

- **Rule:** A value a step accepts is one every later step can write and read back, so a reader refuses what the
  writer cannot hold, such as a non-finite number or text UTF-8 cannot encode.
- **Why:** A value the first step accepted and a later one cannot write ends the run part-way, after state has
  changed.
- **Use:** `review_io:atomic_write_json`, `review_io:read_json`, `typed_fields:FieldReader`; one JSON writer and a
  policy refusing a direct write are the mechanism in #1906.
- **Guard:** `skills/ejryan89-code-review-core/scripts/test_review_io.py::ReadJsonTests::test_content_the_parser_refuses_or_utf8_cannot_hold_is_a_persistence_error`
- **Search for:** `rg -n "json\.dumps?\(" | rg -v allow_nan=False`; `write_text(json.dumps(`; input written back
  without the later reader's pass.
- **Examples:** #1552, #1634, #1874, #1906

### A record separator is one the data cannot hold

- **Rule:** A listing of names or records another program writes is split on a separator the data cannot hold, NUL
  for git.
- **Why:** A name holding the separator, or a path git quotes, is split or misread, and the listing names a file that
  is not there.
- **Use:** `pr_status:branch_tips`, `dotnet_format_targets:git_paths`; a git listing that asks for NUL separators itself, and a
  policy requiring it, are the mechanism in #1908.
- **Guard:** `skills/repo-cleanup/scripts/test_pr_status.py::BranchTipTests::test_an_upstream_whose_name_holds_a_bar_is_read_whole`,
  `tests/tools/test_release_notes.py::RangeEntriesTests::test_a_message_holding_any_byte_but_nul_is_read_whole`,
  `skills/dotnet-format/scripts/test_dotnet_format_targets.py::FileListTests::test_a_line_holding_a_nul_is_refused_naming_it`
- **Search for:** a `"diff"`, `"ls-files"`, `"ls-tree"`, `"log"`, or `"status"` git call with no `-z`; a git format
  such as `%(refname)|%(upstream)` joined by a bar, a tab, or a space; `split("|")` on another program's output.
- **Examples:** #1816, #1848, #1862, #1908

### Two spellings are compared as their consumer compares them

- **Rule:** Two spellings of one thing are compared as the program that consumes them compares them: versions by
  number, URLs and paths in canonical form, and environment names folded as the platform folds them.
- **Why:** A text comparison takes `1.10` for older than `1.9`, or two spellings of one path for two paths.
- **Use:** `versions:find`, `versions:read`, and `versions:latest`, the one version reader, which reads a version's
  numbers and orders by them; `release_tags:release_order` and `release_tags:highest_release`, a release tag through
  it; `path_keys:path_key`, the one form a path is compared in, which the deployer reaches as
  `deployer.platform_support:path_key`; `guidance_record:canonical_url`.
- **Guard:** `tests/validation/duplication.py::DuplicationPolicies::test_repository_copies_only_what_it_sanctions`,
  which fails a version's numbers read by hand outside the version reader;
  `skills/ejryan89-skill-core/scripts/test_versions.py::OrderTests::test_versions_order_by_number_never_as_text`,
  `skills/ejryan89-skill-core/scripts/test_path_keys.py::PathKeyTests::test_two_spellings_of_one_path_have_one_key`,
  `skills/ejryan89-skill-core/scripts/test_release_tags.py::ReleaseTagTests::test_releases_are_ordered_by_their_numbers_not_their_text`,
  `skills/ejryan89-skill-core/scripts/test_guidance_record.py::test_the_canonical_form_folds_only_raw_github_s_refs_heads_and_the_scheme_and_host_case`
- **Search for:** `version in`, `.startswith(version`, `sorted(tags`, or `tags[-1]`; `int(part) for part in` or
  `map(int,` over a `.split(".")`; in a workflow, `grep` or `contains(` on a version; `==` between two paths with no
  normalization, or `os.path.normcase` outside `path_keys.py`.
- **Examples:** #1556, #1855, #1867, #1909

### A child process starts from an environment built for it

- **Rule:** A child process starts from an environment built for it, never the one its parent inherited.
- **Why:** A variable the parent carries, such as git's repository selection or a signed-in token, otherwise reaches
  a child that must not see it.
- **Use:** `bounded_process:run_bounded`, `bounded_process:without_repository_variables`,
  `tools.mutation_gate:suite_environment`
- **Guard:** `tests/validation/python_checks.py::PythonChecksPolicies::test_every_process_starts_without_git_repository_variables`
- **Search for:** `{**os.environ`, `os.environ.copy()`, or `dict(os.environ)` passed as `env=`; a measured or
  sandboxed child started with no `env=`.
- **Examples:** #1366, #1396, #1598, #1777, #1861

### An action on something not proven ours stops

- **Rule:** An action that changes something, such as a merge, a tag move, or a deletion, stops unless the thing is
  proven ours, and unknown ownership is not ours.
- **Why:** An action taken on a guess acts on another person's pull request, branch, or pin.
- **Use:** `deployer.source_ref:is_release_pin`, `deployer.source_ref:replaceable_release_pin`
- **Guard:** `tests/deployer/test_deploy_at.py::DeployAtTests::test_a_pin_whose_tags_git_cannot_read_is_refused_with_gits_reason`,
  `.claude/skills/shepherd-pull-requests/scripts/test_shepherd_pull_requests.py::AdmissionTest`
- **Search for:** `"pr", "merge"`, `"--auto"`, `"pr", "edit"`, or `"pr", "close"` with no check of who owns the pull
  request; an `is_<owner>` function that returns `False` from an `except`.
- **Examples:** #1686, #1719, #1778

### A decision about merged, retired, or owned is made by identity

- **Rule:** Whether something is merged, retired, or owned is decided by its identity, a commit, never by a name that
  can be reused.
- **Why:** A branch name or a path outlives what it named, so a new branch with an old name is judged merged.
- **No guard:** The mechanism is in #1901; until it lands, review holds the rule.
- **Search for:** a branch, a ref, or a path compared by name, `== branch` or `in branches`, in a decision that a
  pull request is merged, a worktree retired, or a file owned; a `headRefName` read where a `headRefOid` or a commit
  is meant.
- **Examples:** #1901

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

### A check reaches its whole rule

- **Rule:** A policy, an audit brief, or a gate reaches every place its rule covers, in every language the fact is
  written in and through every indirection that carries it, never a narrower set its author swept by area.
- **Why:** A rule held only where a sweep reached keeps failing in the places it missed, and passes them as checked.
- **Use:** `tests.validation.validation_support:scope_problems`, which holds a policy's scope, named as data, to every
  non-test module ruff checks, each part it leaves out stated with its reason, as
  `tests.validation.python_checks:claim_omissions` and `tests.validation.duplication:duplication_omissions` state them.
- **Guard:** `tests/validation/python_checks.py::PythonChecksPolicies::test_claim_policies_read_every_module_their_rules_speak_of`,
  `tests/validation/duplication.py::DuplicationPolicies::test_the_duplication_policy_reads_every_module_its_rule_speaks_of`,
  `tests/validation/test_python_checks.py::test_a_policy_scope_fails_for_each_module_it_skips_and_each_stale_omission`,
  `tests/validation/test_skill_layout.py::SkillLayoutFixtures::test_an_install_path_is_refused_under_every_spelling_of_the_home`,
  `tests/validation/python_checks.py::PythonChecksPolicies::test_every_process_starts_without_git_repository_variables`,
  `tests/tools/test_mutation_gate.py::test_a_mutant_on_a_continuation_line_runs_against_the_suite_that_executed_its_statement`,
  `.claude/skills/audit-repository/scripts/test_audit_repository.py::ReleaseTests::test_every_brief_carries_the_kinds_of_drift_and_the_report_template`
- **Search for:** a policy whose file list is a hand-kept tuple of directories, such as a `*_ROOTS`, that no
  `scope_problems` test holds, or a `glob(`
  narrower than the rule's statement in `CLAUDE.md` or `docs/`; `rg -n "glob\(\"\*\.sh\"\)|suffix == \"\.sh\"" tests/validation/`,
  a policy that reads one shell; a fixture suite whose violating inputs cover one of the forms the rule names; an
  audit brief that asks only for drift.
- **Examples:** #1366, #1598, #1678, #1722, and #1884, which adds the check that holds a policy's scope to its rule

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
- **Use:** `check_provenance:load`, `guidance_record:canonical_url`; a policy that each runtime statement in the
  support pages links a page the guidance record lists is the mechanism in #1912.
- **Guard:** `.github/workflows/runtime-command-lines.yml`, `.github/workflows/runtime-changelogs.yml`,
  `tests/tools/test_runtime_command_lines.py::CapturedTests::test_each_runtimes_real_refusal_of_a_flag_is_read_as_one`,
  `tests/tools/test_captured_samples.py::test_a_sample_older_than_its_runtimes_version_is_behind_and_one_at_it_is_not`
- **Search for:** a sentence about a runtime's behavior in `docs/*-support.md` or `docs/installation.md` with no
  link; `always`, `never`, `only`, `cannot`, or `does not` in a support page; a `"--` flag literal in a list passed to
  `copilot`, `codex`, or `claude` that `tools/runtime_command_lines.py` does not build.
- **Examples:** #1362, #1409, #1538, #1573, #1581, #1583, #1842, #1856

### A fix reaches every sibling of its construct

- **Rule:** A defect's fix starts by searching for the same construct elsewhere, the other callers of the same reader
  or writer, and its twin in the other shell or runtime, and fixes each sibling or files it.
- **Why:** A fix that reaches only the reported instance leaves the rest to be found again.
- **Guard:** `.claude/skills/implement-change/SKILL.md#3-bug-class`
- **Search for:** the name of the function, call, or literal the fix changed, across `skills/`, `deployer/`, `tools/`,
  and `.claude/skills/`; its Bash twin in `*.sh` and its PowerShell twin in `*.ps1`; the **Search for** of the
  pattern above that the defect is an instance of.
- **Examples:** #1369, #1521

### A foreign function declares its types

- **Rule:** Every foreign function a module calls through `ctypes` has its `argtypes` and `restype` set in that module.
- **Why:** An undeclared function passes and returns a C `int`, which truncates a handle on a 64-bit process.
- **Guard:** `tests/validation/python_checks.py::PythonChecksPolicies::test_every_ctypes_foreign_function_called_is_declared`
- **Search for:** `ctypes.windll.` or `WinDLL(` in a module, with a function of it called and no `.argtypes =` or
  `.restype =` for that function.
- **Examples:** #1363, #1600

### A pipe's reader never ends before its writer under `pipefail`

- **Rule:** A Bash suite that sets `pipefail` reads text it matches from a here-string, never through a pipe into a
  reader that exits at its first match.
- **Why:** `grep -q` exits at its match, the writer then fails with `SIGPIPE`, and `pipefail` makes that an
  intermittent failure.
- **Guard:** `tests/validation/suite_discovery.py::SuiteDiscoveryPolicies::test_no_bash_suite_pipes_into_a_grep_that_ends_at_its_match`
- **Search for:** `| grep -q` or `| head -n` in a `.sh` file that runs `set -o pipefail` or `set -euo pipefail`.
- **Examples:** #1557, #1602

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
- **Guard:** `tests/tools/test_mutation_gate.py::SanctionTableFixtures`
- **Search for:** a mutant of a module-level `*_ALLOWED` table in the `Mutation gate` step summary; a gate or audit
  that runs a module's suites on what only a policy under `tests/validation/` reads; a new `*_ALLOWED` name with no
  `*_ALLOWANCE` constant naming it in a policy.
- **Examples:** #1933, #1938

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
