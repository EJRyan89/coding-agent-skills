# Lessons

This ledger records what the repository learned from each defect or drift found after its change merged: the
lesson, the issues it was found in, and the guard that now refuses the mistake, or the reason no guard is possible.
A contributor or agent reads it to learn which mistakes are already refused, and which are held only by care.

## Adding a lesson

A defect or drift found after merge ends in an entry here, in the pull request that fixes it or adds its guard, as
[Validation](implementing-changes.md#validation) in the implementation profile and the release procedure in
[releasing.md](releasing.md) say. A finding of a lesson already here adds its issue to that entry's **Found in**, and
its guard when the fix adds one.

Each lesson is a `###` heading under [Lessons](#lessons-1) that states the lesson, followed by these fields, each a
list item of its own:

- **Found in:** the issues the defect or drift was found in, each as `#<number>`.
- **Lesson:** what went wrong and what holds now.
- **Guard:** each guard that refuses the mistake, in a code span by its path from the repository root: a test, which
  a validation policy is too, by its test ID, `<path>::<test>`, `<path>::<class>`, or `<path>::<class>::<test>`; a
  step of a skill, `<path>/SKILL.md#<fragment>`, by the fragment GitHub gives its heading; or a workflow,
  `.github/workflows/<file>`.
- **No guard:** in place of **Guard**, the reason no guard can refuse the mistake, and what holds it instead.

`lessons_ledger_problems` in `tests/validation/markdown_links.py` fails a lesson that names no issue, one that names
neither a guard nor a reason or names both, and each guard that does not resolve, so a guard renamed or removed fails
validation until its lesson follows it.

## Lessons

### A rule swept in by area holds only in the areas the sweep reached

- **Found in:** #1366, #1598
- **Lesson:** The rule that no process starts with git's repository-selecting variables landed in several pull
  requests, each sweeping one area, and the hub guard's PowerShell reader was never swept. A rule stated for every
  process holds only where a policy refuses the next process that breaks it.
- **Guard:** `tests/validation/python_checks.py::PythonChecksPolicies::test_every_process_starts_without_git_repository_variables`

### A consolidation into the one implementation leaves a twin that fails open

- **Found in:** #1320, #1599
- **Lesson:** `link_probe` became the one link test, and the deployer kept its own pair, which read an unreadable path
  as no link. A module named the one home of a test needs a policy that refuses the test anywhere else.
- **Guard:** `tests/validation/python_checks.py::PythonChecksPolicies::test_link_probe_is_the_one_link_test`

### Every reader of JSON ended the parser's limits its own way

- **Found in:** #1611, #1613, #1617, #1729
- **Lesson:** A document past Python's integer-digit or nesting limit escaped reader after reader as an unhandled
  `ValueError` or `RecursionError`, each found and fixed on its own. JSON is now parsed in one place, which ends both
  limits in the reader's documented failure, and a direct parse elsewhere is refused.
- **Guard:** `tests/validation/python_checks.py::PythonChecksPolicies::test_json_reader_is_the_one_parse_of_json`

### A foreign function called without declared types

- **Found in:** #1363, #1600
- **Lesson:** Some `kernel32` calls set no `argtypes` or `restype`, unlike the module that set the pattern, so a handle
  could be truncated to a C `int`. Every foreign function a module calls declares both in that module.
- **Guard:** `tests/validation/python_checks.py::PythonChecksPolicies::test_every_ctypes_foreign_function_called_is_declared`

### A runner's result discarded reads a failure as success

- **Found in:** #1365, #1601
- **Lesson:** The mutation gate ignored a failed coverage step, so every mutant read as not reached. A process
  runner that returns its result whatever the exit status has that result checked.
- **Guard:** `tests/validation/python_checks.py::PythonChecksPolicies::test_no_process_runner_result_is_discarded_unchecked`,
  `tests/tools/test_mutation_gate.py::test_a_failed_or_timed_out_coverage_step_names_the_step_and_its_output`

### Evidence that was never produced reads as a pass

- **Found in:** #1443, #1462, #1603
- **Lesson:** A reviewer whose guard never claimed it was counted, and an audit with no generator manifest skipped its
  layering checks and reported the repository compliant. A guard, gate, verdict, count, or measurement has a test in
  which its evidence never existed, and reports that case apart from a pass.
- **Guard:** `skills/ejryan89-code-review-core/scripts/test_review_pipeline.py::test_finalize_refuses_a_result_the_guard_never_claimed_with_nothing_recorded`,
  `skills/audit-ai-config/scripts/test_audit_ai_config.py::test_a_repository_whose_files_show_nothing_says_the_checks_did_not_run`,
  `tests/tools/test_hub_guard.py::test_an_event_that_names_no_command_or_file_is_allowed_with_a_note_never_unseen`

### A fix reaches the reported instance and leaves its siblings

- **Found in:** #1369, #1521
- **Lesson:** A rollback added to one scaffold left its twin without one, and a reader made strict for most fields of
  a record left two open. A defect's fix starts by searching for the same construct elsewhere and fixes each sibling
  or files it.
- **Guard:** `.claude/skills/implement-change/SKILL.md#3-bug-class`

### Audit briefs that ask only for drift miss the defect classes that keep shipping

- **Found in:** #1678
- **Lesson:** Defects with no guard, absent evidence, broken universal claims, malformed records, and unfixed
  siblings, kept passing release and rotation audits whose briefs asked only for the kinds of drift. Every such brief
  asks those questions of every file, and a finding names the question that found it.
- **Guard:** `.claude/skills/audit-repository/scripts/test_audit_repository.py::ReleaseTests::test_every_brief_carries_the_kinds_of_drift_and_the_report_template`

### `grep -q` reading a pipe under `pipefail` races its writer

- **Found in:** #1557, #1602
- **Lesson:** `grep -q` exits at its first match, the writer can then fail with `SIGPIPE`, and `pipefail` turns that
  into a failure that came and went on unrelated pull requests. A Bash suite that sets `pipefail` reads such text
  from a here-string.
- **Guard:** `tests/validation/suite_discovery.py::SuiteDiscoveryPolicies::test_no_bash_suite_pipes_into_a_grep_that_ends_at_its_match`

### A file a test reads before its writer has finished it

- **Found in:** #1354
- **Lesson:** The canary test read its child's pid file before the child had written it. A file one process hands
  another is written to a temporary name and renamed into place.
- **No guard:** Whether a reader can run before its writer finishes depends on the processes' timing, which reading
  the code cannot settle. The canary's own case is held by
  `tests/tools/test_runtime_canary.py::test_an_empty_pid_file_is_a_named_wait_rather_than_a_parse_error`, and review
  holds the rest.

### A skill's Markdown and its scripts' status lines drift apart

- **Found in:** #1349, #1350, #1351, #1579
- **Lesson:** Status lines a script printed went unnamed in its skill's Markdown, and the comparison that found them
  ran only when someone ran `audit-skill`. Validation now runs it over every skill.
- **Guard:** `tests/validation/skill_grants.py::SkillGrantsPolicies::test_skill_markdown_names_every_status_line_its_scripts_print`

### A grant that no step uses

- **Found in:** #1482, #1579
- **Lesson:** A skill kept a tool grant that a step only implied and none named. A grant counts only for a tool a step
  names.
- **Guard:** `tests/validation/test_skill_grants.py::test_grant_policy_fails_a_grant_whose_tool_a_step_only_implies`

### A document cites a test that was renamed

- **Found in:** #1495, #1580
- **Lesson:** A decision record named a test class a refactor had replaced, and a rename of a test the contract cites
  would have passed. Every test a document cites resolves to the file that defines it.
- **Guard:** `tests/validation/markdown_links.py::MarkdownLinksPolicies::test_every_test_a_document_cites_exists`

### A document's list of a code-owned set drifts from the code

- **Found in:** #1477, #1478, #1492, #1514, #1582
- **Lesson:** The hub guard's contract, a review route's module list, and the threat model's analyzer inventory each
  listed a set the code owns, and each fell behind it. A list that must stay in a document is compared with the code
  that owns the set.
- **Guard:** `tests/tools/test_hub_guard.py::test_the_refused_subcommands_are_the_writing_subcommands`,
  `tests/tools/test_hub_guard.py::test_each_prefix_program_the_contract_names_is_read_as_the_command_it_starts`,
  `tests/code-review/test_format_contract.py::test_the_threat_model_rows_name_each_file_the_analyzer_inventory_parses`,
  `.claude/skills/review-repository/scripts/test_review_repository.py::test_the_trust_boundary_route_matches_its_scripts_and_each_choice_matches_a_file`

### A test that restates a document's wording pins the drift

- **Found in:** #1317, #1318, #1584, #1629
- **Lesson:** Tests asserted the drifted wording of a warning and an error line instead of the wording the documents
  promised, so the drift passed. A test takes its expected wording from the document it holds, or from the one
  constant the code and the document share.
- **No guard:** A literal a test restates from a document cannot be told from one the test needs for itself, so no
  check can refuse it; review holds the rule [Validation](implementing-changes.md#validation) states.

### A runtime release drops a flag the repository passes

- **Found in:** #1573, #1581
- **Lesson:** A Copilot CLI release removed a flag every Copilot review passed, and nothing started the repository's
  command lines on the latest release before a probe found it. Each command line is now started weekly on the latest
  release of every runtime.
- **Guard:** `.github/workflows/runtime-command-lines.yml`,
  `tests/tools/test_runtime_command_lines.py::CapturedTests::test_each_runtimes_real_refusal_of_a_flag_is_read_as_one`

### A runtime's changelog states a change nobody read

- **Found in:** #1362, #1538, #1539, #1574, #1583
- **Lesson:** Changelog entries, some older than the deployer's own floor, had changed behavior the documents still
  described the old way. A weekly watch lists each entry that mentions a term the code uses, for the guidance audit
  to read.
- **Guard:** `.github/workflows/runtime-changelogs.yml`

### A matcher tested only on invented text misses the real one

- **Found in:** #1412, #1592, #1606
- **Lesson:** The changelog watch missed a real entry that named a flag the code passes, and a pattern over a
  runtime's error output was held only to text the tests made up. A reader of another program's output is tested
  against captured real output with its provenance, and reports a shape it does not know as unknown.
- **Guard:** `tests/tools/test_runtime_changelogs.py::test_a_term_matches_only_where_a_real_entry_names_it`,
  `tests/tools/test_captured_samples.py::test_every_sample_has_its_provenance_and_every_provenance_its_sample`

### A hand-written reader of Bash misses a construct Bash reads

- **Found in:** #1429, #1430
- **Lesson:** The hub guard did not see git after an array element assignment, which Bash reads, so the call went
  unjudged. The guard's Bash reader is held to what `bash -n` reads, and each construct the comparison found has a
  case of its own.
- **Guard:** `tests/tools/test_bash_syntax.py::ArrayAssignmentTests::test_an_array_assignment_to_a_subscript_is_read_where_bash_reads_an_array`,
  `tests/tools/test_hub_guard.py::test_a_git_call_after_any_assignment_bash_reads_before_it_is_judged_as_the_bare_call`

### The installed release's update breaks when a later tree moves what it reads

- **Found in:** #1507, #1607
- **Lesson:** An installed update read a helper from the fast-forwarded clone by a path a later rename moved, which
  broke the one path every installation takes across releases. The latest release's installed update now runs against
  the current tree.
- **Guard:** `tests/deployer/test_installed_update.py::test_the_release_names_only_clone_paths_the_current_tree_keeps`,
  `tests/deployer/test_installed_update.py::test_the_update_fails_when_a_commit_moves_a_path_the_release_reads`

### A workflow answers fewer prompts than configure asks

- **Found in:** #1596
- **Lesson:** `configure` gained prompts the deployment workflow's piped answers did not cover, and the job hung until
  cancelled. Each job's answers are held to the prompts `configure` declares.
- **Guard:** `tests/validation/workflows.py::WorkflowsPolicies::test_deployable_workflow_answers_every_prompt_configure_declares_in_both_jobs`

### A recorded cost names a test the sharded run never collects

- **Found in:** #1368
- **Lesson:** A recorded cost for a parametrized test passed the policy, which read test names from the source, and
  failed the sharded run, which collects only its cases. A policy reads what the runner reads.
- **Guard:** `tests/validation/test_suite_discovery.py::test_a_parametrized_test_is_never_recorded_since_the_sharded_run_collects_only_its_cases`

### A script grader that passes a wrong run fails nothing

- **Found in:** #1738, #1774
- **Lesson:** A skill-evaluation scenario passed a review that named no verdict, and no run that passed was ever read.
  Every recorded evaluation samples a passing run for the author to read against its transcript.
- **Guard:** `.claude/skills/evaluate-skill/SKILL.md#5-read-the-sampled-transcript`

### A mutant on a continuation line was never run

- **Found in:** #1722
- **Lesson:** The mutation gate read a mutant on a continuation line of a multi-line statement as not reached, so such
  a change was never mutation-tested. A mutant's reach is the statement that holds its line.
- **Guard:** `tests/tools/test_mutation_gate.py::test_a_mutant_on_a_continuation_line_runs_against_the_suite_that_executed_its_statement`
