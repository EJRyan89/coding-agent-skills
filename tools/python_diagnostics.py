"""Run chosen suites with Python's development mode and warnings as errors, in every Python process they start.

Usage:
  PYTHONDEVMODE=1 PYTHONWARNINGS=error python -B tools/python_diagnostics.py [--summary FILE] [SUITE ...]

The `Python Diagnostics` workflow runs it weekly. It refuses to start unless its own environment sets PYTHONDEVMODE=1
and PYTHONWARNINGS=error and its interpreter took them, so the settings it reports are the ones every process of the
run inherits. Development mode shows the ResourceWarning of a file, socket, or process left open and the
DeprecationWarning of a call a later Python removes, and warnings as errors turn each into an exception. In a suite's
own pytest process that fails the test: pytest raises the warning where it is issued, and reports an unraisable
exception, such as a warning raised in a destructor, as a PytestUnraisableExceptionWarning, which the same setting makes
an error. In a process a suite starts, a warning raised where it is issued ends the process as any exception does, but
one raised in a destructor is unraisable: Python prints it and carries on, and the process's exit status never shows
it. So the run copies tools/unraisable_record.py, as sitecustomize.py, into a folder of its own that it puts first on
PYTHONPATH, and every Python process the suites start records each unraisable exception it meets in a folder the run
reads after each suite. A process started with -I, -E, or an environment built without this one takes neither the
settings nor the recorder, and the run cannot see it.

Before the suites it starts one Python process with the suites' environment, which leaves a file open, and checks that
the process ran in development mode with warnings as errors and recorded the unraisable exception that left. Then it
runs each SUITE, or each of DIAGNOSTIC_SUITES when none is given, as the validation runner runs a suite: `python -B -m
pytest <suite>` from the top of the repository, with the runner's PYTEST_ENVIRONMENT.

It prints one fact per line, its fields tab-separated:

  INTERPRETER <path> <version>       the interpreter every process of the run is
  SETTINGS <variable>=<value> ...    the two settings, as its environment and its children's hold them
  CHILD <dev_mode> <warning options> the probe's development mode and sys.warnoptions, and that it recorded its
                                     unraisable exception
  CHILD_UNSET <reason>               the probe did not run in development mode with warnings as errors, or recorded
                                     nothing; no suite runs; exit 1
  PASSED <suite> <seconds>           pytest passed and no process recorded an unraisable exception
  FAILED <suite> <reason>            pytest failed or ran past SUITE_SECONDS; pytest's output follows, indented;
                                     exit 1
  UNRAISABLE <suite> <record>        a process the suite started recorded an unraisable exception, named by its
                                     arguments, what Python was finalizing, and the exception; exit 1

With --summary FILE it also appends the interpreter, the settings, and a table of the suites to FILE, as the workflow
does with its step summary.

Exit status:
  0  the probe and every suite passed, and no process recorded an unraisable exception
  1  CHILD_UNSET, FAILED, or UNRAISABLE above, or a last line FAILED <reason>
  2  a usage error: a setting missing from its environment, or a suite that is not a file
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "validation"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

import file_presence
import json_reader
import private_folder
from bounded_process import run_bounded
from console import run_main
from flat_text import fact_line, flat_text
from job_tree import process_tree
from suite_discovery import PYTEST_ENVIRONMENT, pytest_command
from text_lines import program_lines

from tools.unraisable_record import RECORD_VARIABLE, SITE_MODULE

COMMANDS_ALLOWED = {"run_suite": "starts pytest as the runner does, through suite_discovery.pytest_command"}
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RECORDER = Path(__file__).resolve().with_name("unraisable_record.py")
# The settings the run needs in its own environment, which every process it starts inherits.
SETTINGS = {"PYTHONDEVMODE": "1", "PYTHONWARNINGS": "error"}
SKILL_CORE = "skills/ejryan89-skill-core/scripts"
# The suites the scheduled run exercises, chosen for the code that opens and writes files, starts and ends processes,
# and parses text it did not write, where a resource left open or a call a later Python removes would hide, and because
# most of them start Python programs of their own, so the settings reach a child. Each runs in seconds.
DIAGNOSTIC_SUITES = (
    # Files: the atomic writer, the JSON writer, the file lock, the tree-bound reader, the folder walk, and the
    # deployer's one write point and state files.
    f"{SKILL_CORE}/test_atomic_file.py",
    f"{SKILL_CORE}/test_json_writer.py",
    f"{SKILL_CORE}/test_file_lock.py",
    f"{SKILL_CORE}/test_tree_reader.py",
    f"{SKILL_CORE}/test_folder_walk.py",
    "tests/deployer/test_fsops.py",
    "tests/deployer/test_state_files.py",
    # Processes: the bounded runner, the process tree and probe, the git and GitHub clients, the entry-point runner, and
    # the deployer's tool deadlines, its command line, and its recovery from an abrupt end.
    f"{SKILL_CORE}/test_bounded_process.py",
    f"{SKILL_CORE}/test_job_tree.py",
    f"{SKILL_CORE}/test_process_identity.py",
    f"{SKILL_CORE}/test_git_client.py",
    f"{SKILL_CORE}/test_github_client.py",
    f"{SKILL_CORE}/test_console.py",
    "tests/deployer/test_tool_deadlines.py",
    "tests/deployer/test_cli.py",
    "tests/deployer/test_abrupt_termination.py",
    # Parsing: the JSON, XML, and Python readers, and the frontmatter reader.
    f"{SKILL_CORE}/test_json_reader.py",
    f"{SKILL_CORE}/test_xml_reader.py",
    f"{SKILL_CORE}/test_python_reader.py",
    "tests/deployer/test_frontmatter.py",
)
# How long one suite may run before it is ended with every process it started and failed.
SUITE_SECONDS = 600.0
PROBE_SECONDS = 60.0
# The probe leaves a file open, so development mode warns as its destructor runs and the warning, raised as an error
# there, is the unraisable exception the recorder must record; then it says how it ran.
PROBE = (
    "import json, os, sys\n"
    "open(os.devnull, encoding='utf-8')\n"
    "print(json.dumps({'dev_mode': sys.flags.dev_mode, 'warnings': sys.warnoptions}))\n"
)


class UsageError(Exception):
    """The run cannot start as asked."""


@dataclass(frozen=True)
class Outcome:
    """One suite's verdict: its failure's reason and pytest's output, or none, and the records its processes left."""

    suite: str
    seconds: float
    failure: str | None
    output: str
    records: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.failure is None and not self.records


def settings_problem(environment: Mapping[str, str], dev_mode: bool, warning_options: Sequence[str]) -> str | None:
    """Why this process cannot run the diagnostics: a setting its environment lacks, or one its interpreter ignored,
    as -I or -E would make it."""
    missing = [f"{name}={value}" for name, value in SETTINGS.items() if environment.get(name) != value]
    if missing:
        return f"set {' and '.join(missing)} in the environment that starts the run"
    if not dev_mode or "error" not in warning_options:
        return "the interpreter ignored PYTHONDEVMODE or PYTHONWARNINGS; start it without -I or -E"
    return None


def child_environment(environment: Mapping[str, str], site: Path, record: Path) -> dict[str, str]:
    """The environment every process of the run starts with: this one, the runner's pytest settings, the recorder's
    folder first on PYTHONPATH, and the record folder."""
    search = [str(site), *([environment["PYTHONPATH"]] if environment.get("PYTHONPATH") else [])]
    return {
        **environment,
        **PYTEST_ENVIRONMENT,
        "PYTHONPATH": os.pathsep.join(search),
        RECORD_VARIABLE: str(record),
    }


def taken_records(record: Path) -> tuple[str, ...]:
    """Each line every process wrote to the record folder, in the order of its file's name, and the files removed, so
    the next suite starts with none."""
    lines: list[str] = []
    for path in file_presence.matching(record, "*.txt"):
        lines += program_lines(path.read_text(encoding="utf-8", errors="replace"))
        path.unlink()
    return tuple(line for line in lines if line.strip())


def probe_problem(environment: Mapping[str, str], record: Path) -> tuple[str, str | None]:
    """The probe's CHILD fields, and why it shows the settings or the recorder did not reach a child, or None."""
    finished = run_bounded([sys.executable, "-B", "-c", PROBE], PROBE_SECONDS, env=environment)
    recorded = taken_records(record)
    if finished.returncode != 0:
        return "", f"the probe exited {finished.returncode}: {flat_text(finished.stderr.decode(errors='replace'))}"
    try:
        shown = json_reader.loads(finished.stdout)
    except ValueError as error:
        return "", f"the probe printed no settings: {error}"
    dev_mode, options = shown.get("dev_mode"), shown.get("warnings")
    fields = f"{dev_mode}\t{','.join(map(str, options)) if isinstance(options, list) else options}"
    if dev_mode is not True or not isinstance(options, list) or "error" not in options:
        return fields, "the probe did not run in development mode with warnings as errors"
    if not any("ResourceWarning" in line for line in recorded):
        return fields, "the probe recorded no unraisable exception, so the recorder did not reach it"
    return fields, None


def run_suite(suite: str, environment: Mapping[str, str], record: Path) -> Outcome:
    """Run one suite as the validation runner does, with every process it starts ended after it, and read what its
    processes recorded."""
    started = time.monotonic()
    failure: str | None = None
    with tempfile.TemporaryFile() as written:
        try:
            finished = run_bounded(
                pytest_command(suite),
                SUITE_SECONDS,
                stdout=written,
                stderr=written,
                cwd=REPOSITORY_ROOT,
                env=environment,
                tree=process_tree(),
            )
        except subprocess.TimeoutExpired:
            failure = f"it ran past {SUITE_SECONDS:.0f} seconds"
        else:
            if finished.returncode != 0:
                failure = f"pytest exited {finished.returncode}"
        written.seek(0)
        output = written.read().decode("utf-8", errors="replace")
    return Outcome(suite, time.monotonic() - started, failure, output, taken_records(record))


def outcome_lines(outcome: Outcome) -> list[str]:
    """The fact lines one suite's outcome prints, pytest's output indented below a failure."""
    if outcome.passed:
        return [fact_line("PASSED", outcome.suite, f"{outcome.seconds:.0f}")]
    lines: list[str] = []
    if outcome.failure is not None:
        lines.append(fact_line("FAILED", outcome.suite, outcome.failure))
        lines += [f"    {line}" for line in program_lines(outcome.output)]
    lines += [fact_line("UNRAISABLE", outcome.suite, record) for record in outcome.records]
    return lines


def summary(header: Sequence[str], outcomes: Sequence[Outcome], problem: str | None) -> str:
    """The Markdown the run appends to the step summary: what it ran under, and each suite's verdict."""
    lines = ["## Python diagnostics", "", *(f"- `{flat_text(line)}`" for line in header), ""]
    if problem is not None:
        return "\n".join([*lines, f"No suite ran: {flat_text(problem)}", ""]) + "\n"
    lines += ["| Suite | Verdict | Seconds |", "|---|---|---|"]
    for outcome in outcomes:
        verdict = "passed" if outcome.passed else (outcome.failure or f"{len(outcome.records)} unraisable recorded")
        lines.append(f"| `{outcome.suite}` | {flat_text(verdict)} | {outcome.seconds:.0f} |")
    return "\n".join(lines) + "\n\n"


def chosen_suites(given: Sequence[str]) -> list[str]:
    """The suites to run, each a file under the top of the repository."""
    suites = list(given) or list(DIAGNOSTIC_SUITES)
    missing = [suite for suite in suites if not (REPOSITORY_ROOT / suite).is_file()]
    if missing:
        raise UsageError(f"not a suite file: {', '.join(missing)}")
    return suites


def run(suites: Sequence[str], environment: Mapping[str, str], summary_file: Path | None) -> int:
    """Run the probe and the suites, print their facts, and return the exit status."""
    header = [
        fact_line("INTERPRETER", sys.executable, sys.version),
        fact_line("SETTINGS", *(f"{name}={environment[name]}" for name in SETTINGS)),
    ]
    print("\n".join(header), flush=True)
    outcomes: list[Outcome] = []
    with tempfile.TemporaryDirectory(prefix="python-diagnostics-", dir=private_folder.temporary_directory()) as made:
        site, record = Path(made) / "site", Path(made) / "record"
        site.mkdir()
        record.mkdir()
        shutil.copyfile(RECORDER, site / f"{SITE_MODULE}.py")
        children = child_environment(environment, site, record)
        fields, problem = probe_problem(children, record)
        if problem is None:
            header.append(f"CHILD\t{fields}")
            print(header[-1], flush=True)
            for suite in suites:
                outcomes.append(run_suite(suite, children, record))
                print("\n".join(outcome_lines(outcomes[-1])), flush=True)
        else:
            print(fact_line("CHILD_UNSET", problem), flush=True)
    if summary_file is not None:
        with summary_file.open("a", encoding="utf-8") as stream:
            stream.write(summary(header, outcomes, problem))
    return 0 if problem is None and all(outcome.passed for outcome in outcomes) else 1


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    parser.add_argument("--summary", type=Path, metavar="FILE", help="append a Markdown summary of the run to FILE")
    parser.add_argument("suites", nargs="*", metavar="SUITE", help="a suite to run instead of the chosen ones")
    namespace = parser.parse_args(arguments)
    problem = settings_problem(os.environ, bool(sys.flags.dev_mode), sys.warnoptions)
    try:
        if problem is not None:
            raise UsageError(problem)
        suites = chosen_suites(namespace.suites)
    except UsageError as error:
        parser.error(str(error))
    return run(suites, os.environ, namespace.summary)


if __name__ == "__main__":
    run_main(main)
