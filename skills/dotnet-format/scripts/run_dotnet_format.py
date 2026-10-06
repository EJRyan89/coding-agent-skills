"""Run the dotnet-format global tool on the resolved files and print only its diagnostics.

    check  --repo-root R --solution S --include-file LIST   report what dotnet-format would change; save nothing
    fix    --repo-root R --solution S --include-file LIST   apply the whitespace, code-style, and analyzer fixes

Both run whitespace formatting plus the code-style (IDE*) and analyzer (CA*, StyleCop, Roslynator, ...)
diagnostics the repository configures at --severity or above (default warn). The full detailed log goes to a
temporary file; standard output gets one fact per line, tab separated:

    DIAGNOSTIC  <file> <line> <column> <severity> <rule>
    SUMMARY     <diagnostic count> <file count> <comma-separated rules, or ->
    LOG         <log file>

FILE paths are relative to the repository root when they are inside it.

`check` exits 1 when it prints a DIAGNOSTIC (findings) and 0 when it prints none. `fix` exits 0 once dotnet-format
succeeds: its DIAGNOSTIC lines are what it found to fix, because its log does not say which of them it could not
fix, so rerun `check` to see what remains. A failure, such as dotnet-format not being installed, exiting with an
unexpected code, or not finishing in time, prints `FAILED <reason>` as the last line, after the LOG line when
dotnet-format ran, and exits 1. A usage error exits 2.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

# dotnet-format's MSBuildIssueFormatter: "<file>(<line>,<column>): <severity> <id>: <message> [<project>]".
ISSUE = re.compile(
    r"^\s*(?P<path>.+?)\((?P<line>\d+),(?P<column>\d+)\): (?P<severity>error|warning|info|hidden) "
    r"(?P<rule>[A-Za-z][A-Za-z0-9_]*): "
)
CHECK_FAILED_EXIT_CODE = 2
DEFAULT_TIMEOUT_SECONDS = 570
INSTALL_HINT = "dotnet-format is not installed; install it with: dotnet tool install -g dotnet-format"


@dataclass(frozen=True)
class Completed:
    returncode: int
    output: bytes
    timed_out: bool = False


Runner = Callable[[Sequence[str], Path, float], Completed]


def subprocess_runner(arguments: Sequence[str], cwd: Path, timeout: float) -> Completed:
    try:
        result = subprocess.run(
            list(arguments),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as expired:
        return Completed(-1, expired.output or b"", timed_out=True)
    return Completed(result.returncode, result.stdout)


@dataclass
class Services:
    """External effects, replaceable in tests."""

    run: Runner = subprocess_runner
    which: Callable[[str], str | None] = field(default=shutil.which)


class Failed(Exception):
    pass


@dataclass(frozen=True)
class Diagnostic:
    path: str
    line: int
    column: int
    severity: str
    rule: str


def command(mode: str, solution: str, files: list[str], severity: str) -> list[str]:
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
    return [*arguments, "--include", *files]


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


def read_file_list(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise Failed(f"{path} is not UTF-8 text") from None
    files = [line.strip() for line in text.splitlines() if line.strip()]
    if not files:
        raise Failed(f"{path} lists no files")
    return files


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
    """Run dotnet-format and print its diagnostics; return whether a check found any."""
    if not services.which("dotnet-format"):
        raise Failed(INSTALL_HINT)
    files = read_file_list(file_list)
    result = services.run(command(mode, solution, files, severity), root, timeout)
    descriptor, log_path = tempfile.mkstemp(prefix=f"dotnet-format-{mode}-", suffix=".log")
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(result.output)
    diagnostics = parse_diagnostics(result.output.decode("utf-8", errors="replace"), root)
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
    emit(f"LOG\t{log_path}")
    if result.timed_out:
        raise Failed(f"dotnet-format did not finish within {timeout:g} seconds; see the log")
    if result.returncode == CHECK_FAILED_EXIT_CODE and mode == "check":
        if not diagnostics:
            raise Failed("dotnet-format reported changes it did not itemize; see the log")
        return True
    if result.returncode != 0:
        raise Failed(f"dotnet-format exited with code {result.returncode}; see the log")
    # A fix run's log lists what it found before fixing, not what is left; the check rerun reports that.
    return mode == "check" and bool(diagnostics)


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
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
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
    sys.exit(main())
