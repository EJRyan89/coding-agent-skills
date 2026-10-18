from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import run_dotnet_format as formatter

# Runs no test of its own; the test functions below check through its assertions, as the classes do.
CHECK = unittest.TestCase()

INSTALL_HINT = "dotnet-format is not installed; install it with: dotnet tool install -g dotnet-format"
SDK_INSTALL_HINT = (
    "dotnet is not installed; the solution is a .slnx solution, or a .sln of SDK-style projects, which the .NET SDK's"
    " dotnet format formats; install the .NET SDK 9.0.200 or newer"
)
TOOL = "dotnet-format"
SDK = "dotnet format"
SDK_PROJECT = '<Project Sdk="Microsoft.NET.Sdk"></Project>\n'
LEGACY_PROJECT = '<Project ToolsVersion="15.0" xmlns="http://schemas.microsoft.com/developer/msbuild/2003"></Project>\n'
WEB_PROJECT = '<Project Sdk="Microsoft.NET.Sdk.Web"></Project>\n'
CSHARP_PROJECT_TYPE = "{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}"
# A file outside the repository as dotnet-format names it on this platform, and as the formatter reports it.
OUTSIDE_FILE, OUTSIDE_REPORTED = (
    ("C:\\elsewhere\\Generated.cs", "C:/elsewhere/Generated.cs")
    if sys.platform == "win32"
    else ("/elsewhere/Generated.cs", "/elsewhere/Generated.cs")
)


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
            f"  {OUTSIDE_FILE}(1,1): warning CA1822: Mark members as static",
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
    ["DIAGNOSTIC", OUTSIDE_REPORTED, "1", "1", "warning", "CA1822"],
    ["SUMMARY", "5", "3", "CA1822,FINALNEWLINE,IDE0005,RCS0041,WHITESPACE"],
]


def stop_recorded_process(pid_file: Path) -> None:
    """End the process whose id a test program recorded in `pid_file`, if it recorded one and it still runs."""
    with contextlib.suppress(OSError, ValueError):
        os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGTERM)


class FakeRunner:
    def __init__(self, returncode: int, output: str, timed_out: bool = False) -> None:
        self.result = formatter.Completed(returncode, output.encode("utf-8"), timed_out)
        self.calls: list[tuple[list[str], Path, float]] = []

    def __call__(self, arguments: Sequence[str], cwd: Path, timeout: float) -> formatter.Completed:
        self.calls.append((list(arguments), cwd, timeout))
        return self.result


class ScriptedRunner:
    """Answers each call with the next of the results given."""

    def __init__(self, *results: formatter.Completed) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], Path, float]] = []

    def __call__(self, arguments: Sequence[str], cwd: Path, timeout: float) -> formatter.Completed:
        self.calls.append((list(arguments), cwd, timeout))
        return self.results[len(self.calls) - 1]


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
            formatter.command(TOOL, "check", "My App.sln", ["src/A.cs", "src/My Folder/B.cs"], "warn"),
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
            formatter.command(TOOL, "fix", "App.sln", ["A.cs"], "info"),
        )

    def test_include_path_changes_only_a_name_the_formatter_would_not_read_as_a_path(self) -> None:
        self.assertEqual("./@a.cs", formatter.include_path("@a.cs"))
        self.assertEqual("./-a.cs", formatter.include_path("-a.cs"))
        self.assertEqual("./--report.cs", formatter.include_path("--report.cs"))
        self.assertEqual("a/@b.cs", formatter.include_path("a/@b.cs"))
        self.assertEqual("a-b.cs", formatter.include_path("a-b.cs"))


class BatchTests(unittest.TestCase):
    def test_a_list_that_fits_is_one_run(self) -> None:
        self.assertEqual([["A.cs", "My Folder/B.cs"]], formatter.batches(["dotnet-format"], ["A.cs", "My Folder/B.cs"]))

    def test_runs_keep_the_order_and_each_fills_but_never_passes_the_limit(self) -> None:
        prefix = ["dotnet-format", "App.sln", "--include"]  # 31 characters
        files = ["A 1.cs", "B.cs", "C.cs", "D 2.cs"]  # 9, 5, 5, and 9 characters with the space before each
        self.assertEqual([["A 1.cs", "B.cs"], ["C.cs", "D 2.cs"]], formatter.batches(prefix, files, limit=45))
        self.assertEqual([["A 1.cs", "B.cs", "C.cs"], ["D 2.cs"]], formatter.batches(prefix, files, limit=50))
        for limit in (45, 50, 60):
            for run in formatter.batches(prefix, files, limit=limit):
                self.assertLessEqual(len(subprocess.list2cmdline([*prefix, *run])), limit)

    def test_a_run_fits_its_files_as_the_command_passes_them(self) -> None:
        prefix = ["dotnet-format", "App.sln", "--include"]  # 31 characters
        files = ["@A.cs", "B.cs"]  # 8 and 5 characters as `./@A.cs` and `B.cs` with the space before each
        self.assertEqual([["@A.cs"], ["B.cs"]], formatter.batches(prefix, files, limit=42))
        self.assertEqual([["@A.cs", "B.cs"]], formatter.batches(prefix, files, limit=44))

    def test_a_file_that_fits_no_command_line_fails(self) -> None:
        with self.assertRaisesRegex(formatter.Failed, "^Long Name.cs does not fit a command line of 40 characters$"):
            formatter.batches(["dotnet-format", "App.sln", "--include"], ["A.cs", "Long Name.cs"], limit=40)


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


class SubprocessRunnerTests(unittest.TestCase):
    def test_merges_standard_error_into_the_output_and_keeps_the_exit_code(self) -> None:
        script = "import sys; print('out', flush=True); print('err', file=sys.stderr, flush=True); sys.exit(3)"
        result = formatter.subprocess_runner([sys.executable, "-c", script], Path.cwd(), 60)
        self.assertEqual(3, result.returncode)
        self.assertFalse(result.timed_out)
        self.assertEqual(["out", "err"], result.output.decode().split())

    def test_runs_in_the_repository_with_no_stdin_and_every_prompt_off(self) -> None:
        script = (
            "import json, os, sys; print(json.dumps([os.getcwd(), sys.stdin.read(), "
            "[os.environ.get(name) for name in ('GIT_TERMINAL_PROMPT', 'GCM_INTERACTIVE', 'GH_PROMPT_DISABLED')]]))"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "a repository"
            root.mkdir()
            result = formatter.subprocess_runner([sys.executable, "-c", script], root, 60)
            directory, stdin, prompts = json.loads(result.output)
            self.assertEqual(root.resolve(), Path(directory).resolve())
        self.assertEqual((0, "", ["0", "never", "1"]), (result.returncode, stdin, prompts))

    def test_a_run_past_the_timeout_is_stopped_and_marked(self) -> None:
        script = "import time; print('partial', flush=True); time.sleep(60)"
        result = formatter.subprocess_runner([sys.executable, "-c", script], Path.cwd(), 2)
        self.assertEqual((-1, True), (result.returncode, result.timed_out))
        self.assertIn(b"partial", result.output)

    def test_a_build_host_left_holding_the_output_does_not_hold_the_timeout(self) -> None:
        # The formatter starts a long-lived child that inherits its output, as an MSBuild node or build host can.
        with tempfile.TemporaryDirectory() as temporary:
            holder_pid = Path(temporary) / "holder.pid"
            holder = "import time; time.sleep(30)"
            script = (
                "import subprocess, sys, time; "
                f"holder = subprocess.Popen([sys.executable, '-c', {holder!r}]); "
                f"open({str(holder_pid)!r}, 'w').write(str(holder.pid)); "
                "print('loading', flush=True); time.sleep(60)"
            )
            self.addCleanup(stop_recorded_process, holder_pid)
            started = time.monotonic()
            result = formatter.subprocess_runner([sys.executable, "-c", script], Path.cwd(), 2)
            self.assertLess(time.monotonic() - started, 15)
        self.assertEqual((-1, True), (result.returncode, result.timed_out))
        self.assertIn(b"loading", result.output)


def install(folder: Path, name: str) -> Path:
    """A program as its installer leaves it: `<name>.exe` on Windows, and an executable `<name>` elsewhere."""
    program = folder / (f"{name}.exe" if sys.platform == "win32" else name)
    program.write_bytes(b"fixture")
    program.chmod(0o755)
    return program


class LocatedFormatterTests(unittest.TestCase):
    """The formatter is found by skill-core's locator, as `deploy.py check` finds it, and run by that absolute path.
    The environment is sanitized: PATH is one empty folder and the local application data folder a fresh one, so the
    only programs found are the ones a test puts there."""

    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="formatter locator ")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.path_folder = root / "on path"
        if sys.platform == "win32":
            self.links = root / "local app data" / "Microsoft" / "WinGet" / "Links"
            environment = {
                "PATH": str(self.path_folder),
                "PATHEXT": ".com;.exe",
                "LOCALAPPDATA": str(root / "local app data"),
            }
        else:
            # No package manager links commands into a folder of its own off Windows, so the folder PATH names
            # after the first stands in for one: the place the locator looks once the first has no such program.
            self.links = root / "later on path"
            environment = {"PATH": os.pathsep.join((str(self.path_folder), str(self.links)))}
        self.path_folder.mkdir()
        self.links.mkdir(parents=True)
        patch = mock.patch.dict(os.environ, environment)
        patch.start()
        self.addCleanup(patch.stop)

    def started(self, arguments: list[str]) -> str:
        """The program subprocess_runner starts for these arguments."""
        finished = mock.Mock(returncode=0)
        with mock.patch.object(formatter, "run_bounded", return_value=finished) as run:
            formatter.subprocess_runner(arguments, Path.cwd(), 60)
        self.assertEqual(arguments[1:], run.call_args.args[0][1:])
        return str(run.call_args.args[0][0])

    def test_each_formatter_only_in_wingets_links_folder_is_found_and_run_from_there(self) -> None:
        services = formatter.Services()
        for name, arguments in (
            ("dotnet-format", formatter.command(TOOL, "check", "App.sln", ["a b.cs"], "warn")),
            ("dotnet", formatter.command(SDK, "check", "App.slnx", ["a b.cs"], "warn")),
        ):
            with self.subTest(name=name):
                self.assertIsNone(services.which(name))
                program = install(self.links, name)
                self.assertEqual(str(program), services.which(name))
                self.assertEqual(str(program), self.started(arguments))
                self.assertTrue(Path(self.started(arguments)).is_absolute())

    def test_a_formatter_on_path_comes_before_the_links_folder(self) -> None:
        install(self.links, "dotnet-format")
        on_path = install(self.path_folder, "dotnet-format")
        self.assertEqual(str(on_path), formatter.Services().which("dotnet-format"))
        self.assertEqual(str(on_path), self.started(["dotnet-format", "App.sln"]))

    def test_a_missing_formatter_is_refused_before_anything_runs(self) -> None:
        file_list = self.path_folder.parent / "files.txt"
        file_list.write_text("a.cs\n", encoding="utf-8")
        arguments = ["check", "--repo-root", str(Path.cwd()), "--solution", "App.sln", "--include-file", str(file_list)]
        output = io.StringIO()
        with mock.patch.object(formatter, "run_bounded") as run, contextlib.redirect_stdout(output):
            status = formatter.main(arguments, formatter.Services())
        run.assert_not_called()
        self.assertEqual((1, f"FAILED {INSTALL_HINT}\n"), (status, output.getvalue()))


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.files = ["src/App/Program.cs", "src/App/My Folder/Util.cs"]
        self.file_list = self.root / "files.txt"
        self.file_list.write_text("".join(f"{name}\n" for name in self.files), encoding="utf-8")

    def run_formatter(
        self, mode: str, runner: formatter.Runner | formatter.Services, installed: bool = True, *extra: str
    ) -> tuple[int, list[list[str]], str]:
        if isinstance(runner, formatter.Services):
            services = runner
        else:
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

    def sdk_services(self, runner: formatter.Runner, installed: bool = True) -> formatter.Services:
        """Only the .NET SDK's dotnet is installed, or nothing is."""
        return formatter.Services(run=runner, which=lambda name: name if installed and name == "dotnet" else None)

    def test_an_slnx_check_runs_the_sdks_dotnet_format_and_exit_2_means_findings(self) -> None:
        log = sample_log(self.root)
        runner = FakeRunner(2, log)
        status, lines, errors = self.run_formatter("check", self.sdk_services(runner), True, "--solution", "App.slnx")
        self.assertEqual((1, ""), (status, errors))
        self.assertEqual(SAMPLE_DIAGNOSTICS, lines[:-1])
        ((arguments, cwd, timeout),) = runner.calls
        self.assertEqual((self.root, 570.0), (cwd, timeout))
        self.assertEqual(
            [
                "dotnet",
                "format",
                "App.slnx",
                "--no-restore",
                "--severity",
                "warn",
                "--verbosity",
                "detailed",
                "--verify-no-changes",
                "--include",
                "src/App/Program.cs",
                "src/App/My Folder/Util.cs",
            ],
            arguments,
        )

    def test_an_slnx_fix_and_clean_check_exit_0(self) -> None:
        runner = FakeRunner(0, "")
        status, lines, _ = self.run_formatter(
            "fix", self.sdk_services(runner), True, "--solution", "Src/App.SLNX", "--severity", "info"
        )
        self.assertEqual(0, status)
        self.assertEqual(["SUMMARY", "0", "0", "-"], lines[0])
        ((arguments, _, _),) = runner.calls
        self.assertEqual(["dotnet", "format", "Src/App.SLNX", "--no-restore", "--severity", "info"], arguments[:6])
        self.assertNotIn("--verify-no-changes", arguments)
        status, _, _ = self.run_formatter("check", self.sdk_services(FakeRunner(0, "")), True, "--solution", "A.slnx")
        self.assertEqual(0, status)

    def test_slnx_failures_name_dotnet_format(self) -> None:
        cases = (
            (FakeRunner(2, "changed\n"), "dotnet format reported changes it did not itemize; see the log"),
            (FakeRunner(1, "error\n"), "dotnet format exited with code 1; see the log"),
            (FakeRunner(-1, "", timed_out=True), "dotnet format did not finish within 570 seconds; see the log"),
        )
        for runner, reason in cases:
            with self.subTest(reason=reason):
                self.assert_failed(
                    self.run_formatter("check", self.sdk_services(runner), True, "--solution", "App.slnx"), reason
                )

    def test_an_slnx_without_dotnet_fails_without_running(self) -> None:
        runner = FakeRunner(0, "")
        services = formatter.Services(run=runner, which=lambda name: name if name == "dotnet-format" else None)
        lines = self.assert_failed(
            self.run_formatter("check", services, True, "--solution", "App.slnx"),
            SDK_INSTALL_HINT,
        )
        self.assertEqual(1, len(lines))
        self.assertEqual([], runner.calls)
        # The other way round: the SDK alone does not format a .sln.
        self.assert_failed(self.run_formatter("check", self.sdk_services(runner)), INSTALL_HINT)
        self.assertEqual([], runner.calls)

    def many_files(self) -> list[str]:
        """2,000 changed files, about 82,000 characters of command line: three runs' worth."""
        files = [f"src/Project {number // 100}/Generated File {number:04}.cs" for number in range(2000)]
        self.file_list.write_text("".join(f"{name}\n" for name in files), encoding="utf-8")
        return files

    def test_a_long_file_list_runs_in_batches_that_each_fit_the_command_line(self) -> None:
        files = self.many_files()
        first = f"{self.root / files[0]}(1,1): error WHITESPACE: Fix whitespace formatting.\n"
        last = f"{self.root / files[-1]}(2,3): warning IDE0005: Using directive is unnecessary.\n"
        runner = ScriptedRunner(
            formatter.Completed(2, first.encode()),
            formatter.Completed(0, b"clean\n"),
            formatter.Completed(2, last.encode()),
        )
        status, lines, errors = self.run_formatter("check", runner)

        self.assertEqual((1, ""), (status, errors))
        self.assertEqual(3, len(runner.calls))
        included = []
        for arguments, cwd, _ in runner.calls:
            self.assertEqual(self.root, cwd)
            self.assertLessEqual(len(subprocess.list2cmdline(arguments)), formatter.COMMAND_LINE_LIMIT)
            self.assertEqual(
                formatter.command(TOOL, "check", "App.sln", [], "warn"), arguments[: arguments.index("--include") + 1]
            )
            included += arguments[arguments.index("--include") + 1 :]
        self.assertEqual(files, included)
        self.assertEqual(
            [
                ["DIAGNOSTIC", files[0], "1", "1", "error", "WHITESPACE"],
                ["DIAGNOSTIC", files[-1], "2", "3", "warning", "IDE0005"],
                ["SUMMARY", "2", "2", "IDE0005,WHITESPACE"],
            ],
            lines[:-1],
        )
        self.assertEqual(f"{first}clean\n{last}".encode(), Path(lines[-1][1]).read_bytes())

    def test_batches_share_one_time_limit(self) -> None:
        self.many_files()
        clock = iter((100.0, 300.0, 520.0, 600.0))
        runner = ScriptedRunner(*[formatter.Completed(0, b"")] * 3)
        services = formatter.Services(run=runner, which=lambda name: name, clock=lambda: next(clock))
        status, _, _ = self.run_formatter("fix", services)
        self.assertEqual(0, status)
        self.assertEqual([570.0, 370.0, 150.0], [timeout for _, _, timeout in runner.calls])

    def test_a_batch_that_fails_stops_the_rest_and_keeps_the_log_so_far(self) -> None:
        cases = (
            (formatter.Completed(3, b"crash\n"), "dotnet-format exited with code 3; see the log"),
            (
                formatter.Completed(-1, b"crash\n", timed_out=True),
                "dotnet-format did not finish within 570 seconds; see the log",
            ),
            (formatter.Completed(2, b"crash\n"), "dotnet-format reported changes it did not itemize; see the log"),
        )
        self.many_files()
        for second, reason in cases:
            with self.subTest(reason=reason):
                runner = ScriptedRunner(formatter.Completed(0, b"first\n"), second, formatter.Completed(0, b""))
                lines = self.assert_failed(self.run_formatter("check", runner), reason)
                self.assertEqual(2, len(runner.calls))
                self.assertEqual(b"first\ncrash\n", Path(lines[-2][1]).read_bytes())
                self.assertEqual([], [fields for fields in lines if fields[0] in ("SAVED", "UNFINISHED")])

    def test_a_fix_that_fails_in_a_later_run_names_the_files_it_saved_and_those_it_did_not_finish(self) -> None:
        # Each run saves its fixes as it finishes, so the first run's are in the working tree and the second's and
        # third's are not; the report has to say which files are which.
        files = self.many_files()
        runs = formatter.batches(formatter.command(TOOL, "fix", "App.sln", [], "warn"), files)
        self.assertEqual(3, len(runs))
        first_run, later = runs[0], runs[1] + runs[2]
        found = (
            f"{self.root / first_run[0]}(1,1): warning IDE0055: Fix formatting\n"
            f"{self.root / first_run[5]}(2,1): warning IDE0055: Fix formatting\n"
            f"{self.root / first_run[0]}(3,1): error WHITESPACE: Fix whitespace formatting.\n"
        )
        crashed = f"{self.root / later[0]}(1,1): warning IDE0055: Fix formatting\nUnhandled exception\n"
        for timed_out in (False, True):
            with self.subTest(timed_out=timed_out):
                runner = ScriptedRunner(
                    formatter.Completed(0, found.encode()), formatter.Completed(1, crashed.encode(), timed_out)
                )
                reason = (
                    "dotnet-format did not finish within 570 seconds; see the log"
                    if timed_out
                    else "dotnet-format exited with code 1; see the log"
                )
                lines = self.assert_failed(self.run_formatter("fix", runner), reason)
                self.assertEqual(2, len(runner.calls))
                self.assertEqual(
                    [["SAVED", first_run[0]], ["SAVED", first_run[5]], *(["UNFINISHED", name] for name in later)],
                    [fields for fields in lines if fields[0] in ("SAVED", "UNFINISHED")],
                )
                kinds = [fields[0] for fields in lines]
                self.assertLess(kinds.index("SUMMARY"), kinds.index("SAVED"))
                self.assertEqual(["UNFINISHED", "LOG"], kinds[-3:-1])

    def test_a_fix_that_fails_in_its_only_run_saved_nothing(self) -> None:
        lines = self.assert_failed(
            self.run_formatter("fix", FakeRunner(1, sample_log(self.root))),
            "dotnet-format exited with code 1; see the log",
        )
        self.assertEqual(
            [["UNFINISHED", name] for name in self.files],
            [fields for fields in lines if fields[0] in ("SAVED", "UNFINISHED")],
        )
        status, lines, _ = self.run_formatter("fix", FakeRunner(0, sample_log(self.root)))
        self.assertEqual(0, status)
        self.assertEqual([], [fields for fields in lines if fields[0] in ("SAVED", "UNFINISHED")])

    def write_solution(self, *projects: tuple[str, str]) -> None:
        """App.sln at the repository root listing each project, written with the given text."""
        lines = ["Microsoft Visual Studio Solution File, Format Version 12.00"]
        for number, (project, text) in enumerate(projects):
            path = self.root / project.replace("\\", "/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            lines += [f'Project("{CSHARP_PROJECT_TYPE}") = "P{number}", "{project}", "{{{number}}}"', "EndProject"]
        (self.root / "App.sln").write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")

    def test_a_sln_of_sdk_style_projects_runs_the_sdks_dotnet_format_and_any_other_the_global_tool(self) -> None:
        # The global tool cannot load the analyzers current SDKs give an SDK-style project, so only a solution with a
        # project in the older format, which the SDK's command can fail on, keeps it.
        both = formatter.Services(run=FakeRunner(0, ""), which=lambda name: name)
        cases = (
            ("dotnet", [("src\\App\\App.csproj", SDK_PROJECT)]),
            ("dotnet", [("src\\App\\App.csproj", SDK_PROJECT), ("native\\Native.vcxproj", LEGACY_PROJECT)]),
            ("dotnet", [("src\\App\\App.csproj", '<Project><Sdk Name="Microsoft.NET.Sdk" /></Project>\n')]),
            (
                "dotnet",
                [("src\\App\\App.csproj", '<Project><Import Project="Sdk.props" Sdk="Microsoft.NET.Sdk" /></Project>')],
            ),
            ("dotnet-format", [("src\\App\\App.csproj", SDK_PROJECT), ("old\\Old.csproj", LEGACY_PROJECT)]),
            ("dotnet-format", [("src\\App\\App.csproj", SDK_PROJECT), ("old\\Old.vbproj", LEGACY_PROJECT)]),
            ("dotnet-format", [("src\\App\\App.csproj", SDK_PROJECT), ("old\\Broken.csproj", "<Project>")]),
            ("dotnet-format", [("native\\Native.vcxproj", SDK_PROJECT)]),
        )
        for program, projects in cases:
            with self.subTest(projects=projects):
                self.write_solution(*projects)
                runner = FakeRunner(2, sample_log(self.root))
                both.run = runner
                status, lines, _ = self.run_formatter("check", both)
                self.assertEqual(1, status)
                self.assertEqual(SAMPLE_DIAGNOSTICS, lines[:-1])
                ((arguments, _, _),) = runner.calls
                self.assertEqual(program, arguments[0])
                if program == "dotnet":
                    self.assertEqual(["dotnet", "format", "App.sln", "--no-restore", "--severity"], arguments[:5])
                    self.assertEqual("--verify-no-changes", arguments[arguments.index("--include") - 1])
        self.write_solution(("src\\App\\App.csproj", SDK_PROJECT))
        runner = FakeRunner(0, "")
        services = formatter.Services(run=runner, which=lambda name: name if name == "dotnet-format" else None)
        self.assert_failed(self.run_formatter("check", services), SDK_INSTALL_HINT)
        self.assertEqual([], runner.calls)

    def test_a_relative_repository_root_chooses_as_an_absolute_one_does(self) -> None:
        self.write_solution(("src\\App\\App.csproj", SDK_PROJECT))
        runner = FakeRunner(0, "")
        services = formatter.Services(run=runner, which=lambda name: name)
        arguments = ["check", "--repo-root", ".", "--solution", "App.sln", "--include-file", str(self.file_list)]
        with contextlib.chdir(self.root), contextlib.redirect_stdout(io.StringIO()) as output:
            status = formatter.main(arguments, services)
        for line in output.getvalue().splitlines():
            if line.startswith("LOG\t"):
                self.addCleanup(os.remove, line.split("\t")[1])
        self.assertEqual(0, status)
        self.assertEqual("dotnet", runner.calls[0][0][0])

    def test_a_solution_project_outside_the_repository_is_never_read_and_keeps_the_global_tool(self) -> None:
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (Path(outside.name) / "Out.csproj").write_text(SDK_PROJECT, encoding="utf-8")
        self.write_solution(("src\\App\\App.csproj", SDK_PROJECT))
        listed = f'Project("{CSHARP_PROJECT_TYPE}") = "Out", "{Path(outside.name) / "Out.csproj"}", "{{9}}"\r\n'
        with (self.root / "App.sln").open("a", encoding="utf-8") as handle:
            handle.write(listed)
        runner = FakeRunner(0, "")
        status, _, _ = self.run_formatter("check", runner)
        self.assertEqual(0, status)
        self.assertEqual("dotnet-format", runner.calls[0][0][0])

    def test_the_global_tools_analyzer_load_failure_is_named_with_its_remedy(self) -> None:
        crash = (
            "Unhandled exception: System.Reflection.ReflectionTypeLoadException: Unable to load one or more of the "
            "requested types.\nCould not load file or assembly 'Microsoft.CodeAnalysis, Version=4.14.0.0'\n"
        )
        lines = self.assert_failed(
            self.run_formatter("check", FakeRunner(1, crash)),
            "dotnet-format exited with code 1: the global tool could not load the analyzers the installed .NET SDK "
            "gives an SDK-style project (ReflectionTypeLoadException); it runs because the solution holds a project "
            "that is not SDK-style, so convert that project to SDK-style, and the SDK's dotnet format formats the "
            "solution; see the log",
        )
        self.assertEqual(crash.encode(), Path(lines[-2][1]).read_bytes())
        self.assert_failed(
            self.run_formatter("check", self.sdk_services(FakeRunner(1, crash)), True, "--solution", "App.slnx"),
            "dotnet format exited with code 1; see the log",
        )

    def test_the_global_tool_skips_an_aspnet_projects_files_and_the_sdks_command_formats_them(self) -> None:
        self.files = ["src/App/Program.cs", "src/Web/Startup.cs", "src/Web/Pages/Index.cs"]
        self.file_list.write_text("".join(f"{name}\n" for name in self.files), encoding="utf-8")
        self.write_solution(("src\\App\\App.csproj", LEGACY_PROJECT), ("src\\Web\\Web.csproj", WEB_PROJECT))
        runner = FakeRunner(0, "")
        self.assertEqual(0, self.run_formatter("check", runner)[0])
        ((arguments, _, _),) = runner.calls
        self.assertEqual(("dotnet-format", ["src/App/Program.cs"]), (arguments[0], arguments[-1:]))

        self.write_solution(("src\\App\\App.csproj", SDK_PROJECT), ("src\\Web\\Web.csproj", WEB_PROJECT))
        runner = FakeRunner(0, "")
        self.assertEqual(0, self.run_formatter("check", formatter.Services(run=runner, which=lambda name: name))[0])
        ((arguments, _, _),) = runner.calls
        self.assertEqual("dotnet", arguments[0])
        self.assertEqual(self.files, arguments[arguments.index("--include") + 1 :])

    def test_nothing_runs_when_the_global_tool_skips_every_file(self) -> None:
        self.files = ["src/Web/Startup.cs"]
        self.file_list.write_text("src/Web/Startup.cs\n", encoding="utf-8")
        self.write_solution(("src\\Web\\Web.csproj", WEB_PROJECT), ("old\\Old.csproj", LEGACY_PROJECT))
        runner = FakeRunner(1, "")
        for mode in ("check", "fix"):
            with self.subTest(mode=mode):
                self.assertEqual((0, [["SUMMARY", "0", "0", "-"]], ""), self.run_formatter(mode, runner))
        self.assertEqual([], runner.calls)

    def test_no_batch_starts_once_the_time_limit_is_spent(self) -> None:
        self.many_files()
        clock = iter((0.0, 570.0))
        runner = ScriptedRunner(formatter.Completed(0, b"first\n"))
        services = formatter.Services(run=runner, which=lambda name: name, clock=lambda: next(clock))
        lines = self.assert_failed(
            self.run_formatter("check", services), "dotnet-format did not finish within 570 seconds; see the log"
        )
        self.assertEqual(1, len(runner.calls))
        self.assertEqual(b"first\n", Path(lines[-2][1]).read_bytes())

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


class ConsoleTests(unittest.TestCase):
    def test_output_a_cp1252_console_cannot_encode_is_written_as_utf_8(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            # A stub the tool lookup finds, so the run reaches the file list, whose path it then prints.
            tools = Path(temporary) / "tools"
            tools.mkdir()
            for name in ("dotnet-format.cmd", "dotnet-format"):
                (tools / name).write_text("exit 1\n", encoding="utf-8")
                (tools / name).chmod(0o755)
            file_list = Path(temporary) / "files → ✓.txt"
            file_list.write_text("\n", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    formatter.__file__,
                    "check",
                    "--repo-root",
                    temporary,
                    "--solution",
                    "App.sln",
                    "--include-file",
                    str(file_list),
                ],
                capture_output=True,
                env={**os.environ, "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}", "PYTHONIOENCODING": "cp1252"},
                check=False,
            )
        self.assertEqual(1, result.returncode, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(f"FAILED {file_list} lists no files\n", result.stdout.decode("utf-8").replace("\r\n", "\n"))


@pytest.mark.parametrize("mode", ["check", "fix"])
@pytest.mark.parametrize(("tool", "solution"), [(TOOL, "App.sln"), (SDK, "App.slnx"), (SDK, "App.sln")])
def test_a_name_starting_with_at_or_dash_is_passed_as_a_path_to_either_formatter(
    tool: str, solution: str, mode: str
) -> None:
    # Both formatters read an argument starting with `@` as a response file and one starting with `-` as an
    # option, anywhere on the command line; `./` makes each a path both match as the same file.
    names = ["@Program.cs", "-Option.cs", "src/@Kept.cs", "src/-Kept.cs", "A@B.cs", "Plain.cs"]
    included = ["./@Program.cs", "./-Option.cs", "src/@Kept.cs", "src/-Kept.cs", "A@B.cs", "Plain.cs"]
    arguments = formatter.command(tool, mode, solution, names, "warn")
    CHECK.assertEqual(included, arguments[arguments.index("--include") + 1 :])
    CHECK.assertEqual(
        formatter.command(tool, mode, solution, [], "warn"),
        arguments[: arguments.index("--include") + 1],
    )
    CHECK.assertFalse(
        [argument for argument in arguments[1:] if argument.startswith("@")],
        "no argument is read as a response file",
    )
    options = [argument for argument in arguments if argument.startswith("-")]
    CHECK.assertEqual(options, [argument for argument in options if argument.startswith("--")])


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["format", "--repo-root", ".", "--solution", "A.sln", "--include-file", "f"], id="unknown-mode"),
        pytest.param(["check", "--repo-root", ".", "--include-file", "f"], id="no-solution"),
        pytest.param(
            ["check", "--repo-root", ".", "--solution", "A.sln", "--include-file", "f", "--severity", "none"],
            id="unknown-severity",
        ),
        pytest.param(
            ["check", "--repo-root", ".", "--solution", "A.sln", "--include-file", "f", "--timeout", "soon"],
            id="unreadable-timeout",
        ),
    ],
)
def test_usage_errors_exit_2_with_usage_on_stderr(arguments: list[str]) -> None:
    runner = FakeRunner(0, "")
    output, errors = io.StringIO(), io.StringIO()
    with (
        contextlib.redirect_stdout(output),
        contextlib.redirect_stderr(errors),
        CHECK.assertRaises(SystemExit) as raised,
    ):
        formatter.main(arguments, formatter.Services(run=runner, which=lambda name: name))
    CHECK.assertEqual(2, raised.exception.code)
    CHECK.assertEqual("", output.getvalue())
    CHECK.assertIn("usage:", errors.getvalue())
    CHECK.assertEqual([], runner.calls)
