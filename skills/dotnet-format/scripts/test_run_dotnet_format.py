from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_dotnet_format as formatter

INSTALL_HINT = "dotnet-format is not installed; install it with: dotnet tool install -g dotnet-format"


def sample_log(root: Path) -> str:
    """Detailed dotnet-format output in its MSBuildIssueFormatter layout, as a check run prints it."""
    app = root / "src" / "App"
    project = f"[{app / 'App.csproj'}]"
    return "\r\n".join(
        (
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
        )
    )


SAMPLE_DIAGNOSTICS = [
    ["DIAGNOSTIC", "src/App/Program.cs", "12", "5", "error", "WHITESPACE"],
    ["DIAGNOSTIC", "src/App/Program.cs", "30", "2", "error", "FINALNEWLINE"],
    ["DIAGNOSTIC", "src/App/Program.cs", "14", "9", "warning", "RCS0041"],
    ["DIAGNOSTIC", "src/App/My Folder/Util.cs", "3", "1", "info", "IDE0005"],
    ["DIAGNOSTIC", "C:/elsewhere/Generated.cs", "1", "1", "warning", "CA1822"],
    ["SUMMARY", "5", "3", "CA1822,FINALNEWLINE,IDE0005,RCS0041,WHITESPACE"],
]


class FakeRunner:
    def __init__(self, returncode: int, output: str, timed_out: bool = False) -> None:
        self.result = formatter.Completed(returncode, output.encode("utf-8"), timed_out)
        self.calls: list[tuple[list[str], Path, float]] = []

    def __call__(self, arguments: Sequence[str], cwd: Path, timeout: float) -> formatter.Completed:
        self.calls.append((list(arguments), cwd, timeout))
        return self.result


class CommandTests(unittest.TestCase):
    def test_check_adds_check_and_keeps_each_file_one_argument(self) -> None:
        self.assertEqual(
            [
                "dotnet-format",
                "My App.sln",
                "--no-restore",
                "--fix-whitespace",
                "--fix-style",
                "warn",
                "--fix-analyzers",
                "warn",
                "--verbosity",
                "detailed",
                "--check",
                "--include",
                "src/A.cs",
                "src/My Folder/B.cs",
            ],
            formatter.command("check", "My App.sln", ["src/A.cs", "src/My Folder/B.cs"], "warn"),
        )

    def test_fix_saves_and_passes_the_severity_to_style_and_analyzers(self) -> None:
        self.assertEqual(
            [
                "dotnet-format",
                "App.sln",
                "--no-restore",
                "--fix-whitespace",
                "--fix-style",
                "info",
                "--fix-analyzers",
                "info",
                "--verbosity",
                "detailed",
                "--include",
                "A.cs",
            ],
            formatter.command("fix", "App.sln", ["A.cs"], "info"),
        )


class ParseTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def test_display_paths_are_repository_relative_when_inside_it(self) -> None:
        self.assertEqual("src/App/A.cs", formatter.display_path(str(self.root / "src" / "App" / "A.cs"), self.root))
        self.assertEqual("src/A.cs", formatter.display_path("src/A.cs", self.root))
        outside = Path(tempfile.gettempdir()).resolve().anchor + "elsewhere/B.cs"
        self.assertEqual(Path(outside).as_posix(), formatter.display_path(outside, self.root))

    def test_diagnostics_are_distinct_in_first_seen_order_and_only_from_issue_lines(self) -> None:
        log = "\n".join(
            (
                f"{self.root / 'A.cs'}(2,3): hidden IDE0055: Fix formatting [{self.root / 'A.csproj'}]",
                f"  {self.root / 'A.cs'}(1,1): warning Custom_Rule2: Underscore and digit rule",
                f"{self.root / 'A.cs'}(2,3): hidden IDE0055: Fix formatting again",
                f"{self.root / 'A.cs'}(4,1): notice IDE0001: not a severity dotnet-format prints",
                f"{self.root / 'A.cs'}(5,1): warning 1BAD: a rule never starts with a digit",
                f"{self.root / 'A.cs'}(6,1): warning CA1000 missing the colon",
                f"{self.root / 'A.cs'}(x,1): warning CA1000: not a line number",
                "Warning: A.cs(1,1) is not an issue line",
            )
        )
        self.assertEqual(
            [
                formatter.Diagnostic("A.cs", 2, 3, "hidden", "IDE0055"),
                formatter.Diagnostic("A.cs", 1, 1, "warning", "Custom_Rule2"),
            ],
            formatter.parse_diagnostics(log, self.root),
        )
        self.assertEqual([], formatter.parse_diagnostics("", self.root))

    def test_file_lists_skip_blank_lines_and_a_byte_order_mark(self) -> None:
        path = self.root / "files.txt"
        path.write_bytes(b"\xef\xbb\xbf  src/A.cs  \r\n\r\nsrc/My Folder/B.cs\n\n")
        self.assertEqual(["src/A.cs", "src/My Folder/B.cs"], formatter.read_file_list(path))
        path.write_text(" \n\n", encoding="utf-8")
        with self.assertRaisesRegex(formatter.Failed, "lists no files"):
            formatter.read_file_list(path)


class SubprocessRunnerTests(unittest.TestCase):
    def test_merges_standard_error_into_the_output_and_keeps_the_exit_code(self) -> None:
        script = "import sys; print('out', flush=True); print('err', file=sys.stderr, flush=True); sys.exit(3)"
        result = formatter.subprocess_runner([sys.executable, "-c", script], Path.cwd(), 60)
        self.assertEqual(3, result.returncode)
        self.assertFalse(result.timed_out)
        self.assertEqual(["out", "err"], result.output.decode().split())

    def test_a_run_past_the_timeout_is_stopped_and_marked(self) -> None:
        script = "import time; print('partial', flush=True); time.sleep(60)"
        result = formatter.subprocess_runner([sys.executable, "-c", script], Path.cwd(), 2)
        self.assertEqual((-1, True), (result.returncode, result.timed_out))
        self.assertIn(b"partial", result.output)


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.files = ["src/App/Program.cs", "src/App/My Folder/Util.cs"]
        self.file_list = self.root / "files.txt"
        self.file_list.write_text("".join(f"{name}\n" for name in self.files), encoding="utf-8")

    def run_formatter(
        self, mode: str, runner: formatter.Runner, installed: bool = True, *extra: str
    ) -> tuple[int, list[list[str]], str]:
        services = formatter.Services(run=runner, which=lambda name: "dotnet-format" if installed else None)
        output, errors = io.StringIO(), io.StringIO()
        arguments = [
            mode,
            "--repo-root",
            str(self.root),
            "--solution",
            "App.sln",
            "--include-file",
            str(self.file_list),
            *extra,
        ]
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = formatter.main(arguments, services)
        lines = [line.split("\t") for line in output.getvalue().splitlines()]
        for fields in lines:
            if fields[0] == "LOG":
                self.addCleanup(os.remove, fields[1])
        return status, lines, errors.getvalue()

    def assert_failed(self, outcome: tuple[int, list[list[str]], str], reason: str) -> list[list[str]]:
        """Check a run exited 1 with FAILED as its only FAILED line and its last, on stdout; return the lines."""
        status, lines, errors = outcome
        self.assertEqual((1, ""), (status, errors))
        self.assertTrue(lines and lines[-1][0].startswith("FAILED "), lines)
        self.assertEqual(f"FAILED {reason}", "\t".join(lines[-1]))
        self.assertEqual(1, sum(1 for fields in lines if fields[0].startswith("FAILED")))
        return lines

    def test_check_with_diagnostics_prints_them_and_exits_1(self) -> None:
        log = sample_log(self.root)
        runner = FakeRunner(2, log)
        status, lines, errors = self.run_formatter("check", runner)

        self.assertEqual((1, ""), (status, errors))
        self.assertEqual(SAMPLE_DIAGNOSTICS, lines[:-1])
        self.assertEqual("LOG", lines[-1][0])
        log_path = Path(lines[-1][1])
        self.assertEqual(log.encode("utf-8"), log_path.read_bytes())
        self.assertEqual(Path(tempfile.gettempdir()).resolve(), log_path.parent.resolve())
        self.assertTrue(log_path.name.startswith("dotnet-format-check-") and log_path.name.endswith(".log"))
        ((arguments, cwd, timeout),) = runner.calls
        self.assertEqual(self.root, cwd)
        self.assertEqual(570.0, timeout)
        self.assertEqual(
            [
                "dotnet-format",
                "App.sln",
                "--no-restore",
                "--fix-whitespace",
                "--fix-style",
                "warn",
                "--fix-analyzers",
                "warn",
                "--verbosity",
                "detailed",
                "--check",
                "--include",
                "src/App/Program.cs",
                "src/App/My Folder/Util.cs",
            ],
            arguments,
        )

    def test_diagnostics_from_a_check_that_exits_0_are_still_findings(self) -> None:
        status, lines, _ = self.run_formatter("check", FakeRunner(0, sample_log(self.root)))
        self.assertEqual(1, status)
        self.assertEqual(SAMPLE_DIAGNOSTICS, lines[:-1])

    def test_a_clean_check_exits_0(self) -> None:
        runner = FakeRunner(0, "  Format complete in 10ms.\n")
        status, lines, errors = self.run_formatter("check", runner)
        self.assertEqual((0, ""), (status, errors))
        self.assertEqual(["SUMMARY", "0", "0", "-"], lines[0])
        self.assertEqual(["LOG"], [fields[0] for fields in lines[1:]])

    def test_a_successful_fix_exits_0_and_reports_what_it_found(self) -> None:
        log = sample_log(self.root)
        runner = FakeRunner(0, log)
        status, lines, errors = self.run_formatter("fix", runner, True, "--severity", "error", "--timeout", "30")
        self.assertEqual((0, ""), (status, errors))
        self.assertEqual(SAMPLE_DIAGNOSTICS, lines[:-1])
        log_path = Path(lines[-1][1])
        self.assertTrue(log_path.name.startswith("dotnet-format-fix-"))
        self.assertEqual(log.encode("utf-8"), log_path.read_bytes())
        ((arguments, _, timeout),) = runner.calls
        self.assertNotIn("--check", arguments)
        self.assertEqual(["--fix-style", "error", "--fix-analyzers", "error"], arguments[4:8])
        self.assertEqual(30.0, timeout)

        status, lines, _ = self.run_formatter("fix", FakeRunner(0, ""))
        self.assertEqual(0, status)
        self.assertEqual(["SUMMARY", "0", "0", "-"], lines[0])

    def test_failures_keep_the_log_and_exit_1(self) -> None:
        cases = (
            (
                "check",
                FakeRunner(1, "Unhandled exception: System.TypeInitializationException\n"),
                "dotnet-format exited with code 1; see the log",
            ),
            ("fix", FakeRunner(1, sample_log(self.root)), "dotnet-format exited with code 1; see the log"),
            (
                "check",
                FakeRunner(-1, "partial\n", timed_out=True),
                "dotnet-format did not finish within 570 seconds; see the log",
            ),
            (
                "fix",
                FakeRunner(-1, sample_log(self.root), timed_out=True),
                "dotnet-format did not finish within 570 seconds; see the log",
            ),
            (
                "check",
                FakeRunner(2, "changed something\n"),
                "dotnet-format reported changes it did not itemize; see the log",
            ),
            ("fix", FakeRunner(2, sample_log(self.root)), "dotnet-format exited with code 2; see the log"),
            ("check", FakeRunner(3, ""), "dotnet-format exited with code 3; see the log"),
            ("fix", FakeRunner(-1073741819, ""), "dotnet-format exited with code -1073741819; see the log"),
        )
        for mode, runner, reason in cases:
            with self.subTest(mode=mode, reason=reason):
                lines = self.assert_failed(self.run_formatter(mode, runner), reason)
                self.assertEqual("LOG", lines[-2][0])
                self.assertEqual(runner.result.output, Path(lines[-2][1]).read_bytes())

    def test_a_timeout_names_the_timeout_given(self) -> None:
        runner = FakeRunner(-1, "", timed_out=True)
        self.assert_failed(
            self.run_formatter("check", runner, True, "--timeout", "12.5"),
            "dotnet-format did not finish within 12.5 seconds; see the log",
        )

    def test_a_missing_tool_fails_without_running(self) -> None:
        runner = FakeRunner(0, "")
        lines = self.assert_failed(self.run_formatter("check", runner, False), INSTALL_HINT)
        self.assertEqual(1, len(lines))
        self.assertEqual([], runner.calls)

    def test_an_unusable_file_list_fails_without_running(self) -> None:
        cases = (
            (b"\n  \n", f"{self.file_list} lists no files"),
            (b"caf\xe9.cs\n", f"{self.file_list} is not UTF-8 text"),
        )
        for content, reason in cases:
            with self.subTest(reason=reason):
                self.file_list.write_bytes(content)
                runner = FakeRunner(0, "")
                self.assertEqual(
                    [[f"FAILED {reason}"]], self.assert_failed(self.run_formatter("check", runner), reason)
                )
                self.assertEqual([], runner.calls)
        self.file_list.unlink()
        runner = FakeRunner(0, "")
        status, lines, errors = self.run_formatter("check", runner)
        self.assertEqual((1, ""), (status, errors))
        self.assertEqual(1, len(lines))
        self.assertTrue(lines[0][0].startswith("FAILED [Errno 2] "), lines)
        self.assertEqual([], runner.calls)

    def test_operating_system_errors_fail(self) -> None:
        def launch_fails(arguments: Sequence[str], cwd: Path, timeout: float) -> formatter.Completed:
            raise FileNotFoundError(2, "The system cannot find the file specified")

        status, lines, errors = self.run_formatter("check", launch_fails)
        self.assertEqual(
            (1, "", [["FAILED [Errno 2] The system cannot find the file specified"]]), (status, errors, lines)
        )

        with mock.patch.object(tempfile, "mkstemp", side_effect=OSError(28, "No space left on device")):
            self.assert_failed(self.run_formatter("check", FakeRunner(0, "")), "[Errno 28] No space left on device")

    def test_usage_errors_exit_2_with_usage_on_stderr(self) -> None:
        cases = (
            ["format", "--repo-root", ".", "--solution", "A.sln", "--include-file", "f"],
            ["check", "--repo-root", ".", "--include-file", "f"],
            ["check", "--repo-root", ".", "--solution", "A.sln", "--include-file", "f", "--severity", "none"],
            ["check", "--repo-root", ".", "--solution", "A.sln", "--include-file", "f", "--timeout", "soon"],
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                runner = FakeRunner(0, "")
                output, errors = io.StringIO(), io.StringIO()
                with (
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(errors),
                    self.assertRaises(SystemExit) as raised,
                ):
                    formatter.main(arguments, formatter.Services(run=runner, which=lambda name: name))
                self.assertEqual(2, raised.exception.code)
                self.assertEqual("", output.getvalue())
                self.assertIn("usage:", errors.getvalue())
                self.assertEqual([], runner.calls)


if __name__ == "__main__":
    unittest.main()
