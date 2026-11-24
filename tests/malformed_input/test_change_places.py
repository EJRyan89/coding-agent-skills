"""Every place in a shipped skill's scripts that changes a file is held by a command the fault corpus drives under the
change faults, or names why it changes no file a refusal must leave as it was.

change_scan.py finds each function that calls a change step. A row of HELD names the commands, as their drivers name
them, whose drivers list the files the place changes in `changes`, so test_faults_unchangeable.py and
test_faults_directory.py run each with each changed file but its first read-only, in a read-only folder, or a directory,
and hold it to refusing before its first change. A row of NOT_HELD says why the place changes no file of the user's
that way: a file of the run's own, a throwaway one, a lock, a new file that replaces nothing, or one the harness cannot
reach, with what holds it instead. A place the scan finds that neither table holds, or a row whose place the scan no
longer finds, fails, so a script that starts changing files through one of the steps the scan reads is listed in the
pull request that makes it. A change a program the script starts makes, such as dotnet format, git, or a shell script,
is no call the scan reads: STARTED names each command that changes files that way, with why the harness does not
drive it, and a row naming no command a driver or NOT_RUN knows fails.
"""

from __future__ import annotations

import change_scan
import faults
from reader_scan import REPOSITORY

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
    "review_synthesis.Staged.write": (f"{INSIGHTS} report",),
    "dashboard_pipeline.rewrite_config": (f"{DASHBOARD} override", f"{DASHBOARD} repository"),
    "update_pr_dashboard.update_dashboard_rows": (f"{DASHBOARD} update",),
}

RUN = "writes the review run's own files, in the run folder prepare makes and the run's end removes"
LOCK = "makes or removes review_io's lock folder, which every writer under the lock refuses an unchangeable file before"
STEP = "a change step of code-review-core's over ejryan89-skill-core's atomic_file, held through the places calling it"
THROWAWAY = "a throwaway file or folder of its own under the temporary directory, removed when it is done"
AFTER_GITHUB = (
    "replaces the one file it names only after gh answers, and the harness never lets gh answer: it stops a run that "
    "starts any program but git, and its gh fault fails every gh command, which the command refuses before the write, "
    "so no fault run reaches the file; atomic_file refuses a read-only one before a copy exists, and the command's "
    "suite holds its write; a stated limit"
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
    "review_dispatch.run_host": RUN,
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


CLEANUP = "skills/repo-cleanup/scripts/repo_cleanup.py"
GIT_REMOVES = (
    "git deletes the branches and removes the worktrees the user confirmed, each its own step ending in its own line, "
    "never one refusal for several, as its exit table says; the harness starts no program but git, and its git faults "
    "fail every git command"
)
# Each command that changes files through a program it starts, which the scan does not read, named as its driver is or
# by its script, with why the harness does not drive it under the change faults.
STARTED: dict[str, str] = {
    "skills/dotnet-format/scripts/run_dotnet_format.py": (
        "fix runs dotnet format, which rewrites the listed files itself; the harness starts no program but git, and a "
        "fix that fails part way names each file the formatter saved and each it did not on SAVED and UNFINISHED lines"
    ),
    f"{CLEANUP} delete-local": GIT_REMOVES,
    f"{CLEANUP} force-delete": GIT_REMOVES,
    f"{CLEANUP} remove-directory": GIT_REMOVES,
    f"{CLEANUP} sweep": GIT_REMOVES + ", and git fast-forwards each main worktree a sweep syncs",
    "skills/update-coding-agent-skills/scripts/update.sh": (
        "a Bash script, which the scan does not read: it runs git and the deployer, whose own changes the deployer "
        "journals and rolls back"
    ),
}


def problems(
    places: set[str], held: dict[str, tuple[str, ...]], not_held: dict[str, str], changing: set[str]
) -> list[str]:
    """What is wrong with the tables against the places the scan found and the commands whose drivers list the files
    they change: a place neither table holds, a row whose place the scan no longer finds, a place in both, a command a
    row names that no such driver runs, a reason left blank, and such a driver whose command no row names."""
    rows = set(held) | set(not_held)
    named = {command for commands in held.values() for command in commands}
    found = [f"{place}: no row holds it; list it in HELD or NOT_HELD" for place in sorted(places - rows)]
    found += [f"{row}: the scan no longer finds it; remove the row" for row in sorted(rows - places)]
    found += [f"{row}: in HELD and NOT_HELD" for row in sorted(set(held) & set(not_held))]
    found += [f"{command}: no driver lists the files it changes" for command in sorted(named - changing)]
    found += [f"{row}: names no reason" for row, reason in sorted(not_held.items()) if not reason.strip()]
    found += [
        f"{command}: its driver lists the files it changes, but no row names it" for command in sorted(changing - named)
    ]
    return found


def test_each_command_named_as_changing_files_through_a_program_it_starts_is_one_there_is() -> None:
    drivers = {driver.name for driver in faults.DRIVERS}
    unknown = [name for name in STARTED if name not in drivers and not (REPOSITORY / name).is_file()]
    assert unknown == [], "a STARTED row names a driver's command or a shipped skill's script"
    assert [name for name, reason in STARTED.items() if not reason.strip()] == []


def test_every_place_that_changes_a_file_is_held_or_named_not_held_and_every_row_a_place() -> None:
    changing = {driver.name for driver in faults.DRIVERS if driver.changes}
    assert problems(set(change_scan.skill_places()), HELD, NOT_HELD, changing) == []


def test_an_unlisted_place_fails_and_a_listed_one_passes() -> None:
    held: dict[str, tuple[str, ...]] = {"module.write": ("entry.py write",)}
    not_held = {"module.cache": "a throwaway file of its own"}
    changing = {"entry.py write"}
    assert problems({"module.write", "module.cache"}, held, not_held, changing) == []
    assert problems({"module.write", "module.cache", "module.delete"}, held, not_held, changing) == [
        "module.delete: no row holds it; list it in HELD or NOT_HELD"
    ]
    assert problems({"module.write"}, held, not_held, changing) == [
        "module.cache: the scan no longer finds it; remove the row"
    ]
    assert problems({"module.write", "module.cache"}, held, {"module.cache": " "}, changing) == [
        "module.cache: names no reason"
    ]
    assert problems({"module.write", "module.cache"}, held, not_held, {"other.py move"}) == [
        "entry.py write: no driver lists the files it changes",
        "other.py move: its driver lists the files it changes, but no row names it",
    ]
    assert problems({"module.write"}, held, {"module.write": "why"}, changing) == ["module.write: in HELD and NOT_HELD"]


def test_the_scan_finds_each_spelling_of_a_change_and_nothing_else() -> None:
    source = (
        "import os, shutil as sh, json_writer\n"
        "from atomic_file import replace_text as put, create_bytes\n"
        "from review_io import atomic_write_json\n"
        "def unlinked(path):\n    path.unlink()\n"
        "def replaced(a, b):\n    os.replace(a, b)\n"
        "def moved(a, b):\n    sh.move(a, b)\n"
        "def written(path):\n    json_writer.write(path, {})\n"
        "def stepped(path):\n    put(path, '')\n"
        "def outer(path):\n    def inner():\n        path.write_text('')\n    return inner()\n"
        "class Store:\n    def save(self, path):\n        atomic_write_json(path, {})\n"
        "def promoted(staging, result):\n    staging.replace(result)\n"
        "def text(value):\n    return value.replace('a', 'b')\n"
        "def listed(items):\n    items.remove(1)\n"
        "def streamed(stream):\n    stream.write('x')\n"
        "def created(path):\n    create_bytes(path, b'')\n"
        "def made(path):\n    path.mkdir()\n"
        "def unimported(path):\n    replace_text(path, '')\n"
        "def os_path(path):\n    return os.path.exists(path)\n"
    )
    assert change_scan.changing_places("module", source) == [
        "module.unlinked",
        "module.replaced",
        "module.moved",
        "module.written",
        "module.stepped",
        "module.outer",
        "module.Store.save",
        "module.promoted",
    ]


def test_a_module_s_own_step_resolves_by_its_name_and_its_stated_limits_are_not_read() -> None:
    own = (
        "def atomic_write_text(path, text):\n    pass\n"
        "def atomic_write_json(path, value):\n    atomic_write_text(path, '')\n"
    )
    assert change_scan.changing_places("review_io", own) == ["review_io.atomic_write_json"]
    limits = (
        "import os\n"
        "def handed_on(paths):\n    list(map(os.remove, paths))\n"
        "remove = os.remove\n"
        "def rebound(path):\n    remove(path)\n"
        "def a_file(handle):\n    handle.write('x')\n"
    )
    assert change_scan.changing_places("module", limits) == []
