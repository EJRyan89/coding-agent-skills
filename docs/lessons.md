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
  reads. The module is importable from the repository root, such as `deployer.config`, or from
  `skills/ejryan89-skill-core/scripts`, such as `json_reader`, the two directories every part of the repository puts
  on `sys.path`, so a mechanism is one the whole repository can call.
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
- **Why:** An unreadable path, a failed command, or evidence never produced otherwise reads as a clean pass.
- **Use:** `file_presence:is_present`, which tells a file, nothing, and something else apart, and
  `link_probe:is_link_or_junction`, which lets the error of an unreadable path through. The found, absent, and
  could-not-tell lookups of git and the GitHub CLI come with #1881.
- **Guard:** `tests/validation/python_checks.py::PythonChecksPolicies::test_link_probe_is_the_one_link_test`,
  `tests/validation/python_checks.py::PythonChecksPolicies::test_no_process_runner_result_is_discarded_unchecked`,
  `tests/tools/test_mutation_gate.py::test_a_failed_or_timed_out_coverage_step_names_the_step_and_its_output`,
  `skills/ejryan89-code-review-core/scripts/test_review_pipeline.py::test_finalize_refuses_a_result_the_guard_never_claimed_with_nothing_recorded`,
  `skills/audit-ai-config/scripts/test_audit_ai_config.py::test_a_repository_whose_files_show_nothing_says_the_checks_did_not_run`,
  `tests/tools/test_hub_guard.py::test_an_event_that_names_no_command_or_file_is_allowed_with_a_note_never_unseen`
- **Search for:** `returncode == 0` or `returncode != 0` beside a `git` or `gh` command outside `git_client.py` and
  `github_client.py`; `except OSError:` followed by `return False`, `return None`, or `return []`; `.is_file()` or
  `.exists()` before a read whose `False` is reported as no configuration; `if not result.stdout` read as nothing
  found; a count, verdict, or summary computed over a list that may be empty because its step never ran.
- **Examples:** #1320, #1365, #1443, #1462, #1599, #1601, #1603, #1887, #1888

### A number read from text is bounded

- **Rule:** A whole number read from text, an argument, a file, or another program's output is read with its bounds
  and its digit count limited, and a value outside them is the reader's documented refusal.
- **Why:** An unbounded `int()` turns a hostile or garbled value into a `ValueError` past the digit limit, a huge
  allocation, or a negative count the code never expected.
- **Use:** `json_reader:loads` for a JSON number, and `typed_fields:FieldReader.integer` for its bounds; the bounded
  reader of a number in other text comes with #1879.
- **Guard:** `skills/ejryan89-skill-core/scripts/test_json_reader.py::RefusalTests::test_an_integer_past_the_digit_limit_is_a_value_error`,
  `skills/ejryan89-skill-core/scripts/test_typed_fields.py::FieldReaderTests::test_an_integer_within_its_bounds`
- **Search for:** `int(` on a `.group(` of a regular expression; `.isdigit()` before an `int(`, which admits digits
  `int` reads past any bound; `int(` on a `.split(` of another program's output; `float(` on text from a file or an
  argument with no `math.isfinite`.
- **Examples:** #1611, #1729, #1886

### One fact has one owner

- **Rule:** A fact the repository states, a vocabulary, a set the code owns, a status line, a prompt list, or a
  document's wording a test holds, lives in one place, and every other place reads it from there or is compared with
  it.
- **Why:** A copy begins identical and then drifts, so a document, a test, or a second module keeps the old fact while
  the owner moves on.
- **Use:** `tests.validation.duplication:_shared_facts`, the registry of facts each read from its owner, and
  #1882 registers more. A module more than one part of the repository needs moves to skill-core.
- **Guard:** `tests/validation/duplication.py::DuplicationPolicies::test_repository_owners_hold_every_shared_fact`,
  `tests/validation/duplication.py::DuplicationPolicies::test_repository_copies_only_what_it_sanctions`,
  `tests/validation/python_checks.py::PythonChecksPolicies::test_json_reader_is_the_one_parse_of_json`,
  `tests/validation/skill_grants.py::SkillGrantsPolicies::test_skill_markdown_names_every_status_line_its_scripts_print`,
  `tests/validation/markdown_links.py::MarkdownLinksPolicies::test_every_test_a_document_cites_exists`,
  `tests/validation/workflows.py::WorkflowsPolicies::test_deployable_workflow_answers_every_prompt_configure_declares_in_both_jobs`,
  `tests/tools/test_hub_guard.py::test_the_refused_subcommands_are_the_writing_subcommands`,
  `tests/tools/test_hub_guard.py::test_each_prefix_program_the_contract_names_is_read_as_the_command_it_starts`,
  `tests/code-review/test_format_contract.py::test_the_threat_model_rows_name_each_file_the_analyzer_inventory_parses`,
  `.claude/skills/review-repository/scripts/test_review_repository.py::test_the_trust_boundary_route_matches_its_scripts_and_each_choice_matches_a_file`
- **Search for:** a module-level `frozenset({` or tuple of strings whose members another module also spells; a
  document's list of names, flags, or modules that a module's constant holds; an `assertEqual(` or `assertIn(` on a
  literal sentence a document states, where the test could read the document or the shared constant; a `print(` of a
  status line no `SKILL.md` names; a second `json.loads(`, `os.lstat(`, or other helper beside the one skill-core
  owns.
- **Examples:** #1317, #1349, #1477, #1495, #1596, #1679

### A reader refuses a shape it does not know

- **Rule:** A reader of input or of another program's output accepts the shapes it was written and tested for, held
  to captured real output, and refuses or reports as unknown any other, never passing over it and judging what is
  left.
- **Why:** A skipped entry, an ignored field, or an unread construct lets the reader judge a partial input as whole.
- **Use:** `typed_fields:FieldReader.json_object`, which refuses an unknown key, and `xml_reader:read_xml`,
  which refuses a document type or entity declaration.
- **Guard:** `skills/ejryan89-skill-core/scripts/test_typed_fields.py::FieldReaderTests::test_an_object_holds_its_required_fields_and_no_unknown_one`,
  `tests/malformed_input/test_malformed_input.py::test_each_reader_refuses_or_documents_accepting_each_case`,
  `tests/tools/test_captured_samples.py::test_every_sample_has_its_provenance_and_every_provenance_its_sample`,
  `tests/tools/test_runtime_changelogs.py::test_a_term_matches_only_where_a_real_entry_names_it`,
  `tests/tools/test_bash_syntax.py::ArrayAssignmentTests::test_an_array_assignment_to_a_subscript_is_read_where_bash_reads_an_array`,
  `tests/tools/test_hub_guard.py::test_a_git_call_after_any_assignment_bash_reads_before_it_is_judged_as_the_bare_call`
- **Search for:** `if not isinstance(` followed by `continue` in a loop over another program's entries; `.get(` with a
  default on a field the reader requires; a regular expression over another program's output with no branch for no
  match; a hand-written tokenizer of Bash or PowerShell with no case for a construct `bash -n` reads; a test whose
  sample output was typed rather than captured.
- **Examples:** #1412, #1429, #1430, #1592, #1606, #1890, #1892

### A recursive parser of input has a depth bound

- **Rule:** A parser that recurses into input it does not control stops at a stated depth and ends in its documented
  refusal, never in a `RecursionError`.
- **Why:** Nesting past Python's recursion limit escapes the reader as an unhandled error instead of its refusal.
- **Use:** `json_reader:loads`; the depth bound for the other recursive parsers comes with #1880.
- **Guard:** `skills/ejryan89-skill-core/scripts/test_json_reader.py::RefusalTests::test_nesting_past_the_recursion_limit_is_refused`,
  `tests/validation/python_checks.py::PythonChecksPolicies::test_json_reader_is_the_one_parse_of_json`
- **Search for:** a function that calls itself on a child of its input; `literal_eval(` or `ast.parse(` on text from a
  file or another program; `except RecursionError`, which marks a parser that met the limit; `json.loads(` outside
  `json_reader.py`.
- **Examples:** #1613, #1617, #1729, #1886, #1893

### A time option is a positive finite number of seconds

- **Rule:** An option or setting that gives a time limit accepts only a positive finite number of seconds, and refuses
  zero, a negative number, `inf`, and `nan` when it is read.
- **Why:** `float` accepts `inf`, `nan`, and negatives, so a limit can silently become no limit or an immediate
  timeout.
- **No guard:** The mechanism, one argument type for a time option and a policy refusing `type=float` on one, is in
  #1883; until it lands, review holds the rule.
- **Search for:** `type=float` in an `add_argument(` for a `--timeout`, `--wait`, or other seconds option; `type=int`
  on such an option; `timeout=` given a value read from an argument or a file with no check that it is above zero and
  finite.
- **Examples:** #1883

### A check reaches its whole rule

- **Rule:** A policy, an audit brief, or a gate reaches every place its rule covers, and reads what the thing it holds
  reads, never a narrower set its author swept by area.
- **Why:** A rule held only where a sweep reached keeps failing in the places it missed, and passes them as checked.
- **Guard:** `tests/validation/python_checks.py::PythonChecksPolicies::test_every_process_starts_without_git_repository_variables`,
  `tests/validation/test_suite_discovery.py::test_a_parametrized_test_is_never_recorded_since_the_sharded_run_collects_only_its_cases`,
  `tests/tools/test_mutation_gate.py::test_a_mutant_on_a_continuation_line_runs_against_the_suite_that_executed_its_statement`,
  `.claude/skills/audit-repository/scripts/test_audit_repository.py::ReleaseTests::test_every_brief_carries_the_kinds_of_drift_and_the_report_template`
- **Search for:** a policy whose file list is a hand-kept tuple of directories or a `glob(` narrower than the rule's
  statement in `CLAUDE.md` or `docs/`; a policy that reads test names from source where the runner collects cases; a
  fixture suite whose violating inputs cover one of the forms the rule names; an audit brief that asks only for
  drift.
- **Examples:** #1366, #1368, #1598, #1678, #1722, and #1884, which adds the check that holds a policy's scope to
  its rule

### A fix reaches every sibling of its construct

- **Rule:** A defect's fix starts by searching for the same construct elsewhere, the other callers of the same reader
  or writer, and its twin in the other shell or runtime, and fixes each sibling or files it.
- **Why:** A fix that reaches only the reported instance leaves the rest to be found again.
- **Guard:** `.claude/skills/implement-change/SKILL.md#3-bug-class`
- **Search for:** the name of the function, call, or literal the fix changed, across `skills/`, `deployer/`, `tools/`,
  and `.claude/skills/`; its Bash twin in `*.sh` and its PowerShell twin in `*.ps1`; the pattern's own **Search
  for** when the defect is an instance of one above.
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

### Another program's contract is exercised on its latest release

- **Rule:** Each command line the repository starts on another program, and each changelog entry that mentions a term
  the code uses, is exercised or read against that program's latest release on a schedule.
- **Why:** A release that drops a flag or changes a behavior otherwise breaks every run until someone happens to probe
  it.
- **Guard:** `.github/workflows/runtime-command-lines.yml`, `.github/workflows/runtime-changelogs.yml`,
  `tests/tools/test_runtime_command_lines.py::CapturedTests::test_each_runtimes_real_refusal_of_a_flag_is_read_as_one`
- **Search for:** a `"--` flag literal in a list passed to `copilot`, `codex`, or `claude` that
  `tools/runtime_command_lines.py` does not build; a runtime behavior a document states with no
  term in the changelog watch.
- **Examples:** #1362, #1538, #1573, #1581, #1583

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
