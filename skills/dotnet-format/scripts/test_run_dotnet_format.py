from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIRECTORY))

import run_dotnet_format as formatter  # noqa: E402


def sample_log(root: Path) -> str:
    """Detailed dotnet-format output in its MSBuildIssueFormatter layout, as a check run prints it."""
    app = root / "src" / "App"
    project = f"[{app / 'App.csproj'}]"
    return "\r\n".join((
        "  The dotnet runtime version is '5.0.17'.",
        f"  Formatting code files in workspace '{root / 'App.sln'}'.",
        "  Loading workspace.",
        f"  {app / 'Program.cs'}(12,5): error WHITESPACE: Fix whitespace formatting. Replace 6 characters "
        f"with '\\r\\n\\s\\s\\s\\s'. {project}",
        f"  {app / 'Program.cs'}(30,2): error FINALNEWLINE: Fix final newline. Insert '\\r\\n'. {project}",
        f"  {app / 'Program.cs'}(14,9): warning RCS0041: Remove new line between 'if' keyword and 'else' "
        f"keyword {project}",
        f"  {app / 'Program.cs'}(14,9): warning RCS0041: Remove new line between 'if' keyword and 'else' "
        f"keyword {project}",
        f"  {app / 'My Folder' / 'Util.cs'}(3,1): info IDE0005: Using directive is unnecessary. {project}",
        "  C:\\elsewhere\\Generated.cs(1,1): warning CA1822: Mark members as static",
        "  Warning: Program.cs(1,1) is not an issue line",
        "  Formatted code file 'Program.cs'.",
        "  Format complete in 5123ms.",
        "",
    ))


class FakeRunner:
    def __init__(self, returncode: int, output: str, timed_out: bool = False) -> None:
        self.result = formatter.Completed(returncode, output.encode("utf-8"), timed_out)
        self.calls: list[tuple[list[str], Path, float]] = []

    def __call__(self, arguments: Sequence[str], cwd: Path, timeout: float) -> formatter.Completed:
        self.calls.append((list(arguments), cwd, timeout))
        return self.result


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.files = ["src/App/Program.cs", "src/App/My Folder/Util.cs"]
        self.file_list = self.root / "files.txt"
        self.file_list.write_text("".join(f"{name}\n" for name in self.files), encoding="utf-8")

    def run_formatter(self, mode: str, runner: FakeRunner, installed: bool = True,
                      *extra: str) -> tuple[int, list[list[str]], str]:
        services = formatter.Services(run=runner, which=lambda name: "dotnet-format" if installed else None)
        output, errors = io.StringIO(), io.StringIO()
        arguments = [mode, "--repo-root", str(self.root), "--solution", "App.sln",
                     "--include-file", str(self.file_list), *extra]
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = formatter.main(arguments, services)
        lines = [line.split("\t") for line in output.getvalue().splitlines()]
        for fields in lines:
            if fields[0] == "LOG":
                self.addCleanup(os.remove, fields[1])
        return status, lines, errors.getvalue()

    def test_check_prints_distinct_diagnostics_summary_and_log(self) -> None:
        log = sample_log(self.root)
        runner = FakeRunner(formatter.CHECK_FAILED_EXIT_CODE, log)
        status, lines, errors = self.run_formatter("check", runner)

        self.assertEqual((0, ""), (status, errors))
        self.assertEqual([
            ["DIAGNOSTIC", "src/App/Program.cs", "12", "5", "error", "WHITESPACE"],
            ["DIAGNOSTIC", "src/App/Program.cs", "30", "2", "error", "FINALNEWLINE"],
            ["DIAGNOSTIC", "src/App/Program.cs", "14", "9", "warning", "RCS0041"],
            ["DIAGNOSTIC", "src/App/My Folder/Util.cs", "3", "1", "info", "IDE0005"],
            ["DIAGNOSTIC", "C:/elsewhere/Generated.cs", "1", "1", "warning", "CA1822"],
            ["SUMMARY", "5", "3", "CA1822,FINALNEWLINE,IDE0005,RCS0041,WHITESPACE"],
        ], lines[:-1])
        self.assertEqual("LOG", lines[-1][0])
        self.assertEqual(log.encode("utf-8"), Path(lines[-1][1]).read_bytes())
        ((arguments, cwd, timeout),) = runner.calls
        self.assertEqual(self.root, cwd)
        self.assertEqual(formatter.DEFAULT_TIMEOUT_SECONDS, timeout)
        self.assertEqual([
            "dotnet-format", "App.sln", "--no-restore", "--fix-whitespace", "--fix-style", "warn",
            "--fix-analyzers", "warn", "--verbosity", "detailed", "--check",
            "--include", "src/App/Program.cs", "src/App/My Folder/Util.cs",
        ], arguments)

    def test_clean_check_and_fix_runs(self) -> None:
        runner = FakeRunner(0, "  Format complete in 10ms.\n")
        status, lines, _ = self.run_formatter("check", runner)
        self.assertEqual(0, status)
        self.assertEqual(["SUMMARY", "0", "0", "-"], lines[0])
        status, lines, _ = self.run_formatter("fix", runner, True, "--severity", "error", "--timeout", "30")
        self.assertEqual(0, status)
        arguments, _, timeout = runner.calls[-1]
        self.assertNotIn("--check", arguments)
        self.assertEqual(["error", "error"], [arguments[5], arguments[7]])
        self.assertEqual(30.0, timeout)

    def test_missing_tool_stops_without_running(self) -> None:
        runner = FakeRunner(0, "")
        status, lines, _ = self.run_formatter("check", runner, False)
        self.assertEqual(0, status)
        self.assertEqual([["STOP", formatter.INSTALL_HINT]], lines)
        self.assertEqual([], runner.calls)

    def test_failures_keep_the_log_and_exit_2(self) -> None:
        cases = (
            (FakeRunner(1, "Unhandled exception: System.TypeInitializationException\n"), "check", "exited with code 1"),
            (FakeRunner(-1, "partial\n", timed_out=True), "fix", "did not finish within 570 seconds"),
            (FakeRunner(formatter.CHECK_FAILED_EXIT_CODE, "changed something\n"), "check", "did not itemize"),
            (FakeRunner(formatter.CHECK_FAILED_EXIT_CODE, sample_log(self.root)), "fix", "exited with code 2"),
        )
        for runner, mode, reason in cases:
            with self.subTest(reason=reason):
                status, lines, errors = self.run_formatter(mode, runner)
                self.assertEqual(2, status)
                self.assertTrue(errors.startswith("FAILED "), errors)
                self.assertIn(reason, errors)
                self.assertEqual("LOG", lines[-1][0])
                self.assertEqual(runner.result.output, Path(lines[-1][1]).read_bytes())

    def test_empty_file_list_fails(self) -> None:
        self.file_list.write_text("\n", encoding="utf-8")
        status, lines, errors = self.run_formatter("check", FakeRunner(0, ""))
        self.assertEqual((2, []), (status, lines))
        self.assertIn("lists no files", errors)


if __name__ == "__main__":
    unittest.main()
