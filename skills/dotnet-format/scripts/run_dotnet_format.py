"""Run the formatter on the resolved files and print only its diagnostics.

    check  --repo-root R --solution S --include-file LIST   report what dotnet-format would change; save nothing
    fix    --repo-root R --solution S --include-file LIST   apply the whitespace, code-style, and analyzer fixes

Both run whitespace formatting plus the code-style (IDE*) and analyzer (CA*, StyleCop, Roslynator, ...)
diagnostics the repository configures at --severity or above (default warn). The full detailed log goes to a
temporary file; standard output gets one fact per line, tab separated:

    REFUSED     <entry> <reason> a FILE_LIST entry outside the repository root, never passed to the formatter
    DIAGNOSTIC  <file> <line> <column> <severity> <rule>
    SUMMARY     <diagnostic count> <file count> <comma-separated rules, or ->
    SAVED       <file>          a failed fix only: a run that finished found this file's fixes and saved them
    UNFINISHED  <file>          a failed fix only: this file's run failed or never started, so its fixes are not saved
    LOG         <log file>

The formatter is the one dotnet_format_targets.py chooses: the .NET SDK's `dotnet format` for a .slnx solution and for a
.sln whose projects are all SDK-style, and the dotnet-format global tool for any other .sln; both print diagnostics in
one layout, and both exit 2 from a check that finds changes. The global tool is not run on a file of an ASP.NET project,
which `resolve` reports as SKIPPED_ASPNET; when it leaves no file to format, nothing runs and no LOG line is printed. An
entry is passed only when it lies inside the repository root, judged from its text before anything touches it and then
as resolved: an absolute entry, one whose `..` segments leave the root, one written as a network share or a device, and
one a symbolic link or junction leads out of the root is REFUSED. FILE paths are relative to the repository root when
they are inside it. A changed file whose name starts with `@` or `-` is passed to the formatter as `./<name>`, since
either formatter would read the bare name as a response file or an option. Files too many for one command line, which
Windows limits to 32,767 characters, are formatted in several runs within the one time limit, and the log holds every
run's output in order.

`check` exits 1 when it prints a DIAGNOSTIC or a REFUSED line (findings) and 0 when it prints neither. `fix` exits 1
when it prints a REFUSED line, and otherwise 0 once the formatter succeeds: its DIAGNOSTIC lines are what it found to
fix, because its log does not say which of them it could not fix, so rerun `check` to see what remains. A failure, such
as the formatter not being installed, exiting with an unexpected code, or not finishing in time, prints `FAILED
<reason>` as the last line, after the LOG line when the formatter ran, and exits 1. A `fix` that fails tells its runs
apart first: each run saves its fixes when it finishes, so SAVED names the files the runs before the failed one changed,
and UNFINISHED every file of the failed run and of the runs after it. A usage error exits 2.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bounded_process import run_bounded
from console import run_main
from dotnet_format_targets import (
    SDK_FORMATTER,
    SDK_SOLUTION,
    formatter_for,
    formatter_skips,
    inside_root,
    read_file_list,
)
from executable_locator import find_executable, located_command

# dotnet-format's MSBuildIssueFormatter: "<file>(<line>,<column>): <severity> <id>: <message> [<project>]".
ISSUE = re.compile(
    r"^\s*(?P<path>.+?)\((?P<line>\d+),(?P<column>\d+)\): (?P<severity>error|warning|info|hidden) "
    r"(?P<rule>[A-Za-z][A-Za-z0-9_]*): "
)
CHECK_FAILED_EXIT_CODE = 2
DEFAULT_TIMEOUT_SECONDS = 570
INSTALL_HINT = "dotnet-format is not installed; install it with: dotnet tool install -g dotnet-format"
SDK_INSTALL_HINT = f"dotnet is not installed; the solution {SDK_SOLUTION}; install the .NET SDK 9.0.200 or newer"
# The global tool's last release bundles a Roslyn older than the analyzers current .NET SDKs give an SDK-style project,
# and ends with this exception when it loads them.
ANALYZER_LOAD_FAILURE = b"System.Reflection.ReflectionTypeLoadException"
ANALYZER_LOAD_REASON = (
    "the global tool could not load the analyzers the installed .NET SDK gives an SDK-style project"
    " (ReflectionTypeLoadException); it runs because the solution holds a project that is not SDK-style, so convert"
    " that project to SDK-style, and the SDK's dotnet format formats the solution"
)
# Windows' CreateProcess takes a command line of at most 32,767 characters.
COMMAND_LINE_LIMIT = 32_000


@dataclass(frozen=True)
class Completed:
    returncode: int
    output: bytes
    timed_out: bool = False


Runner = Callable[[Sequence[str], Path, float], Completed]


# The functions that start a command whose program tests/run_validation.py cannot read here, with the reason.
COMMANDS_ALLOWED = {
    "subprocess_runner": "the default runner of Services, which runs the dotnet or dotnet-format command command()"
    " builds, and the dotnet msbuild command csharp_layout.evaluated_reference builds; the policy reads the programs"
    " there",
}


def subprocess_runner(arguments: Sequence[str], cwd: Path, timeout: float) -> Completed:
    """Run the formatter with no stdin and every prompt off, and return its exit code and output.

    Its stdout and stderr go together, in the order written, to a file rather than a pipe, so a build host it leaves
    running cannot hold the time limit, and what it wrote is kept when it does not finish. The program runs at the
    path ejryan89-skill-core's locator finds, the one `deploy.py check` reports and `Services.which` found.
    """
    with tempfile.TemporaryFile() as output:
        try:
            result = run_bounded(located_command(arguments), timeout, stdout=output, stderr=output, cwd=cwd)
        except subprocess.TimeoutExpired:
            output.seek(0)
            return Completed(-1, output.read(), timed_out=True)
        output.seek(0)
        return Completed(result.returncode, output.read())


@dataclass
class Services:
    """External effects, replaceable in tests."""

    run: Runner = subprocess_runner
    which: Callable[[str], str | None] = field(default=find_executable)
    clock: Callable[[], float] = field(default=time.monotonic)


class Failed(Exception):
    pass


@dataclass(frozen=True)
class Diagnostic:
    path: str
    line: int
    column: int
    severity: str
    rule: str


def include_path(name: str) -> str:
    """A changed file's name as the formatter's --include reads it as a path. Both formatters read an argument that
    starts with `@` as a response file of further arguments and one that starts with `-` as an option, wherever it
    stands, so such a name is passed as `./<name>`, which both match as the same file."""
    return f"./{name}" if name.startswith(("@", "-")) else name


def command(formatter: str, mode: str, solution: str, files: list[str], severity: str) -> list[str]:
    """The formatter's command for the files, each passed through `include_path`."""
    included = [include_path(name) for name in files]
    if formatter == SDK_FORMATTER:
        # The SDK command runs the whitespace, code-style, and analyzer passes together at one --severity.
        arguments = ["dotnet", "format", solution, "--no-restore", "--severity", severity, "--verbosity", "detailed"]
        if mode == "check":
            arguments.append("--verify-no-changes")
        return [*arguments, "--include", *included]
    arguments = [
        "dotnet-format",
        solution,
        "--no-restore",
        "--fix-whitespace",
        "--fix-style",
        severity,
        "--fix-analyzers",
        severity,
        "--verbosity",
        "detailed",
    ]
    if mode == "check":
        arguments.append("--check")
    return [*arguments, "--include", *included]


def command_line_length(arguments: Sequence[str]) -> int:
    """The length of the command line Windows builds from these arguments, quoting included."""
    return len(subprocess.list2cmdline(list(arguments)))


def batches(prefix: Sequence[str], files: list[str], limit: int = COMMAND_LINE_LIMIT) -> list[list[str]]:
    """The files in order, split into runs whose command line, `prefix` and then the run's files, fits `limit`."""
    runs: list[list[str]] = [[]]
    length = command_line_length(prefix)
    for name in files:
        size = 1 + command_line_length([include_path(name)])
        if runs[-1] and length + size > limit:
            runs.append([])
            length = command_line_length(prefix)
        if length + size > limit:
            raise Failed(f"{name} does not fit a command line of {limit} characters")
        runs[-1].append(name)
        length += size
    return runs


def display_path(path: str, root: Path) -> str:
    candidate = Path(path)
    if not candidate.is_absolute():
        return candidate.as_posix()
    try:
        return candidate.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return candidate.as_posix()


def parse_diagnostics(log: str, root: Path) -> list[Diagnostic]:
    """Distinct issues in a dotnet-format log, in first-seen order."""
    found: dict[Diagnostic, None] = {}
    for line in log.splitlines():
        match = ISSUE.match(line)
        if match:
            found.setdefault(
                Diagnostic(
                    display_path(match["path"], root),
                    int(match["line"]),
                    int(match["column"]),
                    match["severity"],
                    match["rule"],
                )
            )
    return list(found)


@dataclass(frozen=True)
class Runs:
    """What the formatter's runs did: the files of each run in order, the output of each run that started, how many
    of them finished, and the reason the runs stopped early, or None when each one finished."""

    batches: list[list[str]]
    outputs: list[bytes]
    finished: int
    failure: str | None

    @property
    def log(self) -> bytes:
        return b"".join(self.outputs)


def run_failure(formatter: str, mode: str, root: Path, result: Completed) -> str | None:
    """Why one run failed, or None when it finished: a check's exit 2 is findings once its output itemizes them."""
    if result.returncode == CHECK_FAILED_EXIT_CODE and mode == "check":
        if parse_diagnostics(result.output.decode("utf-8", errors="replace"), root):
            return None
        return f"{formatter} reported changes it did not itemize; see the log"
    if result.returncode == 0:
        return None
    if formatter != SDK_FORMATTER and ANALYZER_LOAD_FAILURE in result.output:
        return f"{formatter} exited with code {result.returncode}: {ANALYZER_LOAD_REASON}; see the log"
    return f"{formatter} exited with code {result.returncode}; see the log"


def run_batches(
    formatter: str,
    mode: str,
    root: Path,
    solution: str,
    files: list[str],
    severity: str,
    timeout: float,
    services: Services,
) -> Runs:
    """Run the formatter on the files, as many times as the command-line limit needs, within one time limit, and stop
    at the first run that fails."""
    runs = batches(command(formatter, mode, solution, [], severity), files)
    outputs: list[bytes] = []
    timed_out = f"{formatter} did not finish within {timeout:g} seconds; see the log"
    start = services.clock()
    spent = 0.0
    for run_files in runs:
        if spent >= timeout:
            return Runs(runs, outputs, len(outputs), timed_out)
        result = services.run(command(formatter, mode, solution, run_files, severity), root, timeout - spent)
        spent = services.clock() - start
        outputs.append(result.output)
        failure = timed_out if result.timed_out else run_failure(formatter, mode, root, result)
        if failure:
            return Runs(runs, outputs, len(outputs) - 1, failure)
    return Runs(runs, outputs, len(outputs), None)


def unfinished_fix_lines(runs: Runs, root: Path) -> list[str]:
    """The SAVED and UNFINISHED lines of a fix whose runs stopped early: a run saves its fixes when it finishes, so
    the files the finished runs found something to fix in hold them, and no file of a later run does."""
    lines: list[str] = []
    for run_files, output in zip(runs.batches[: runs.finished], runs.outputs, strict=False):
        found = {diagnostic.path for diagnostic in parse_diagnostics(output.decode("utf-8", errors="replace"), root)}
        lines += [f"SAVED\t{name}" for name in run_files if Path(name).as_posix() in found]
    for run_files in runs.batches[runs.finished :]:
        lines += [f"UNFINISHED\t{name}" for name in run_files]
    return lines


def run(
    mode: str,
    root: Path,
    solution: str,
    file_list: Path,
    severity: str,
    timeout: float,
    services: Services,
    emit: Callable[[str], None],
) -> bool:
    """Run the formatter and print its diagnostics; return whether a check found any or an entry was refused. An
    entry `inside_root` refuses is never passed to the formatter."""
    formatter = formatter_for(root, root / solution)
    if formatter == SDK_FORMATTER:
        if not services.which("dotnet"):
            raise Failed(SDK_INSTALL_HINT)
    elif not services.which("dotnet-format"):
        raise Failed(INSTALL_HINT)
    try:
        listed = read_file_list(file_list)
    except ValueError as exc:
        raise Failed(str(exc)) from None
    inside = inside_root(root, listed, emit)
    refused = len(inside) < len(listed)
    skipped = formatter_skips(root, formatter, inside)
    files = [name for name in inside if name not in skipped]
    if not files:
        emit("SUMMARY\t0\t0\t-")
        return refused
    runs = run_batches(formatter, mode, root, solution, files, severity, timeout, services)
    descriptor, log_path = tempfile.mkstemp(prefix=f"dotnet-format-{mode}-", suffix=".log")
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(runs.log)
    diagnostics = parse_diagnostics(runs.log.decode("utf-8", errors="replace"), root)
    for diagnostic in diagnostics:
        emit(
            "\t".join(
                (
                    "DIAGNOSTIC",
                    diagnostic.path,
                    str(diagnostic.line),
                    str(diagnostic.column),
                    diagnostic.severity,
                    diagnostic.rule,
                )
            )
        )
    rules = sorted({diagnostic.rule for diagnostic in diagnostics})
    file_count = len({diagnostic.path for diagnostic in diagnostics})
    emit(f"SUMMARY\t{len(diagnostics)}\t{file_count}\t{','.join(rules) or '-'}")
    if runs.failure and mode == "fix":
        for line in unfinished_fix_lines(runs, root):
            emit(line)
    emit(f"LOG\t{log_path}")
    if runs.failure:
        raise Failed(runs.failure)
    # A fix run's log lists what it found before fixing, not what is left; the check rerun reports that.
    return (mode == "check" and bool(diagnostics)) or refused


def main(argv: Sequence[str] | None = None, services: Services | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("check", "fix"))
    parser.add_argument("--repo-root", required=True, type=Path, help="REPO_ROOT from dotnet_format_targets.py")
    parser.add_argument("--solution", required=True, help="SOLUTION path, relative to the repository root")
    parser.add_argument("--include-file", required=True, type=Path, help="FILE_LIST from dotnet_format_targets.py")
    parser.add_argument(
        "--severity",
        choices=("info", "warn", "error"),
        default="warn",
        help="lowest code-style and analyzer severity to report or fix (default: warn)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"seconds to wait for dotnet-format (default: {DEFAULT_TIMEOUT_SECONDS})",
    )
    arguments = parser.parse_args(argv)
    try:
        findings = run(
            arguments.mode,
            arguments.repo_root,
            arguments.solution,
            arguments.include_file,
            arguments.severity,
            arguments.timeout,
            services or Services(),
            print,
        )
    except (Failed, OSError) as error:
        print(f"FAILED {error}")
        return 1
    return 1 if findings else 0


if __name__ == "__main__":
    run_main(main, errors="replace")
