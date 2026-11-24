"""Every place in a shipped skill's scripts that changes a file is held by a command the fault corpus drives under the
change faults, or names why it changes no file a refusal must leave as it was.

change_scan.py finds each function that calls a change step. A row of HELD names the commands, as their drivers name
them, whose drivers list the files the place changes in `changes`, so test_faults_unchangeable.py and
test_faults_directory.py run each with its last changed file read-only or a directory and hold it to refusing before
its first change. A row of NOT_HELD says why the place changes no file of the user's that way: a file of the run's
own, a throwaway one, a lock, a new file that replaces nothing, or one the harness cannot reach, with what holds it
instead. A place the scan finds that neither table holds, or a row whose place the scan no longer finds, fails, so a
command that starts changing files is listed in the pull request that makes it.
"""

from __future__ import annotations

import change_scan
import faults

MEMORY_AUDIT = "skills/curate-agent-memory/scripts/memory_audit.py"
CSHARP_LAYOUT = "skills/dotnet-format/scripts/csharp_layout.py"
FLAGS = "skills/flag-review-finding/scripts/flag_review_finding.py"
INSIGHTS = "skills/review-insights/scripts/review_insights.py"
DASHBOARD = "skills/update-pr-dashboard/scripts/dashboard_pipeline.py"
CORE = "skills/ejryan89-code-review-core/scripts"

# Each place, with the commands whose drivers list the files it changes.
HELD: dict[str, tuple[str, ...]] = {
    "memory_audit.delete": (f"{MEMORY_AUDIT} delete",),
    "memory_audit.write_index": (f"{MEMORY_AUDIT} reindex",),
    "dependency_graph.write_graph": ("skills/dotnet-dependencies/scripts/dependency_graph.py graph",),
    "csharp_layout.write_source": (f"{CSHARP_LAYOUT} check", f"{CSHARP_LAYOUT} config"),
    "review_config.write_config": (f"{CORE}/review_config.py write",),
    "review_flags._write_store": (
        f"{FLAGS} add",
        f"{FLAGS} edit",
        f"{FLAGS} resolve",
        f"{INSIGHTS} decide",
        f"{INSIGHTS} decide-custom",
    ),
    "review_state.update_state": (f"{CORE}/review_pipeline.py advance",),
    "review_insights.write_report": (
        f"{INSIGHTS} report",
        f"{INSIGHTS} decide",
        f"{INSIGHTS} decide-custom",
        f"{INSIGHTS} synthesize",
    ),
    # The synthesis files staged beside a report, written by write_report before the report itself.
    "review_synthesis.Staged.write": (f"{INSIGHTS} report", f"{INSIGHTS} synthesize"),
    "dashboard_pipeline.rewrite_config": (f"{DASHBOARD} override", f"{DASHBOARD} repository"),
    "update_pr_dashboard.update_dashboard_rows": (f"{DASHBOARD} update",),
}

RUN = "writes the review run's own files, in the run folder prepare makes and the run's end removes"
LOCK = "makes or removes review_io's lock folder, which every writer under the lock refuses an unchangeable file before"
STEP = "a change step of code-review-core's over ejryan89-skill-core's atomic_file, held through the places calling it"
THROWAWAY = "a throwaway file or folder of its own under the temporary directory, removed when it is done"
AFTER_GITHUB = (
    "replaces the one file it names only after GitHub answers, which no fault but gh's lets a run of the harness "
    "reach; atomic_file refuses a read-only file before a copy exists, and the command's suite holds it"
)
# Each place that changes no file a refused command must leave as it was, with why.
NOT_HELD: dict[str, str] = {
    "review_io.atomic_write_text": STEP,
    "review_io.atomic_write_bytes": STEP,
    "review_io.atomic_write_json": STEP,
    "review_io.ResourceLock._take_file_lock": LOCK,
    "review_io.ResourceLock._claim": LOCK,
    "review_io.ResourceLock._reclaim_stale": LOCK,
    "review_io.ResourceLock._remove_directory": LOCK,
    "review_batches.enumerate_batch": AFTER_GITHUB,
    "dashboard_pipeline.collect": AFTER_GITHUB,
    "github_activity_report.compare": AFTER_GITHUB,
    "review_dispatch.count_reads": RUN,
    "review_dispatch.next_role": RUN,
    "review_dispatch._check_roles": RUN,
    "review_dispatch.workflow_script": RUN,
    "review_guard._prune_claims": RUN,
    "review_guard._prune": RUN,
    "review_hosts.new_claim": RUN,
    "review_hosts.record_host_process": RUN,
    "review_hosts.write_outcome": RUN,
    "review_hosts._write_diagnostic": RUN,
    "review_hosts.isolated_copilot_environment": RUN,
    "review_pipeline._write_request": RUN,
    "review_pipeline._write_roles": RUN,
    "review_pipeline._run_fixture": RUN,
    "review_pipeline._prepare_run": RUN,
    "review_pipeline._run_prepare": RUN,
    "review_pipeline.finalize": RUN + ", and the review record pair commit_record creates new",
    "review_pipeline.remove_recorded_run": RUN + ", and the run list naming it",
    "review_pipeline._save_run_list": "the list of runs a batch prepared, the pipeline's own hand-off between commands",
    "review_reviewer_setup._pull_changes": RUN,
    "review_run.remove_run": RUN,
    "review_run._write_run": RUN,
    "review_run.mark_dispatched": RUN,
    "review_runtime.materialize_reviewer": RUN,
    "review_runtime._populate_snapshot": RUN,
    "review_runtime.materialize_source_snapshot": RUN,
    "review_runtime.fetch_source_file": RUN,
    "review_runtime.materialize_source_snapshot_from_github": RUN,
    "review_runtime.write_adapter_request": RUN,
    "review_snapshot._write_diff": RUN,
    "review_snapshot._retake_snapshot": RUN,
    "review_snapshot._document_diffs": RUN,
    "review_specialists._give_condition_files": RUN,
    "review_specialists._write_shared": RUN,
    "review_specialists._write_role": RUN,
    "review_specialists.build_plan": RUN,
    "review_records.write_record_pair": (
        "creates a new record pair, which commit_record refuses where either name is taken, and removes the Markdown "
        "it wrote when the JSON write fails; it replaces nothing"
    ),
    "review_documents._pdftotext": THROWAWAY,
    "powershell_commands.read": THROWAWAY,
    "review_document.output_root": THROWAWAY,
    "review_document.write_file": "writes a fixture into the --output folder, which must be new or empty, or a new "
    "temporary one",
    "review_synthesis.Staged.stage_run": "makes the synthesis agent's run folder, new for each report",
    "review_synthesis.Staged.discard_run": "removes the synthesis run folder the report it stages for made",
    "review_synthesis.remove_run": "removes a synthesis run folder once its result is recorded or a fresh one replaces "
    "it",
    "hub_state.write_record": "the record repo-cleanup's report keeps of a clone's hub guard in the clone's git "
    "directory, written whole through json_writer.write; a failure is a problem line beside the report, never a "
    "refusal",
    "json_writer.write": "ejryan89-skill-core's JSON write over atomic_file, held through the places calling it",
    "repo_cleanup.save_plan": "a plan or sweep record in the folder a sweep made for them",
    "repo_cleanup.prune_empty_parents": "removes, by rmdir, only the empty folders a confirmed worktree removal left",
    "repo_cleanup.delete_tree": (
        "deletes one orphan directory the user confirmed; each confirmation is its own step, ending in its own "
        "DELETED_DIR or PRESERVED line, as the exit table says, never one refusal for several"
    ),
}


def test_every_place_that_changes_a_file_is_held_or_named_not_held_and_every_row_a_place() -> None:
    places = set(change_scan.skill_places())
    rows = set(HELD) | set(NOT_HELD)
    assert sorted(places - rows) == [], (
        "list each place in HELD with the commands whose drivers list the files it changes, or in NOT_HELD with why"
    )
    assert sorted(rows - places) == [], "remove each row whose place no longer changes a file"
    assert sorted(set(HELD) & set(NOT_HELD)) == [], "a place is held or not held, never both"


def test_each_command_a_place_is_held_by_lists_the_files_it_changes_and_each_such_driver_holds_a_place() -> None:
    changing = {driver.name for driver in faults.DRIVERS if driver.changes}
    named = {command for commands in HELD.values() for command in commands}
    assert sorted(named - changing) == [], "a command HELD names has a driver listing the files it changes"
    assert sorted(changing - named) == [], "a driver that lists the files its command changes holds a place in HELD"


def test_the_scan_finds_each_spelling_of_a_change_and_nothing_else() -> None:
    source = (
        "import os, shutil, json_writer\n"
        "from atomic_file import replace_text as put\n"
        "def unlinked(path):\n    path.unlink()\n"
        "def replaced(a, b):\n    os.replace(a, b)\n"
        "def moved(a, b):\n    shutil.move(a, b)\n"
        "def written(path):\n    json_writer.write(path, {})\n"
        "def stepped(path):\n    replace_text(path, '')\n"
        "def outer(path):\n    def inner():\n        path.write_text('')\n    return inner()\n"
        "class Store:\n    def save(self, path):\n        atomic_write_json(path, {})\n"
        "def text(value):\n    return value.replace('a', 'b')\n"
        "def listed(items):\n    items.remove(1)\n"
        "def streamed(stream):\n    stream.write('x')\n"
        "def created(path):\n    create_bytes(path, b'')\n"
        "def made(path):\n    path.mkdir()\n"
    )
    assert change_scan.changing_places("module", source) == [
        "module.unlinked",
        "module.replaced",
        "module.moved",
        "module.written",
        "module.stepped",
        "module.outer",
        "module.Store.save",
    ]
