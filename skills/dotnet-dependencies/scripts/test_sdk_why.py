"""Regression tests for the `dotnet nuget why` runner and parser, against output captured from the SDK."""

from __future__ import annotations

import os
import sys
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import sdk_why
from sdk_why import Completed, SdkPath, Services, parse_why, run_why, why_command

CYAN = "\x1b[38;5;14m"
RESET = "\x1b[0m"
# Two projects' blocks as the SDK prints them for a solution, with the heading wrapped at the console width, the
# tree lines padded to it, box-drawing branches, and the target colored.
SOLUTION_TEXT = (
    "Project 'Harbor.Web' has the following dependency graph(s) for \r\n"
    "'Lantern.Json':\r\n"
    "\r\n"
    "  [net8.0]                                                                      \r\n"
    "  \u2514\u2500\u2500 Harbor.Core (v1.0.0)                                                      \r\n"
    "      \u2514\u2500\u2500 Quill.Http (v1.2.0)                                                   \r\n"
    f"          \u251c\u2500\u2500 Quill.Abstractions (v1.2.0)                                       \r\n"
    f"          \u2502   \u2514\u2500\u2500 {CYAN}Lantern.Json (v12.0.3){RESET}\r\n"
    f"          \u2514\u2500\u2500 {CYAN}Lantern.Json (v12.0.3){RESET}                                   \r\n"
    "\r\n"
    "Project 'Harbor.Core' has the following dependency graph(s) for 'Lantern.Json':\r\n"
    "\r\n"
    "  [net8.0]                                                                      \r\n"
    "  [net472]                                                                      \r\n"
    f"  \u2514\u2500\u2500 {CYAN}Lantern.Json (v12.0.3){RESET}                                                 \r\n"
    "\r\n"
)
CORE_TEXT = SOLUTION_TEXT[SOLUTION_TEXT.index("Project 'Harbor.Core'") :]
ABSENT_TEXT = "Project 'Harbor.Core' does not have a dependency on 'Nothing.Here'.\r\n"
NO_ASSETS_TEXT = (
    "\x1b[38;5;9mNo assets file was found for \x1b[0m\r\n"
    "\x1b[38;5;9m`C:\\src\\Harbor.Core\\Harbor.Core.csproj`. Please run restore before running \x1b[0m\r\n"
    "\x1b[38;5;9mthis command.\x1b[0m\r\n"
)


class ParseTests(unittest.TestCase):
    def test_chains_are_read_per_project_with_the_depth_from_the_branch_column(self) -> None:
        answer = parse_why(SOLUTION_TEXT, "lantern.json")
        self.assertEqual({"Harbor.Web", "Harbor.Core"}, answer.answered)
        self.assertEqual(
            [
                SdkPath(
                    "Harbor.Web",
                    "net8.0",
                    (
                        ("Harbor.Core", "1.0.0"),
                        ("Quill.Http", "1.2.0"),
                        ("Quill.Abstractions", "1.2.0"),
                        ("Lantern.Json", "12.0.3"),
                    ),
                ),
                SdkPath(
                    "Harbor.Web",
                    "net8.0",
                    (("Harbor.Core", "1.0.0"), ("Quill.Http", "1.2.0"), ("Lantern.Json", "12.0.3")),
                ),
            ],
            answer.paths["Harbor.Web"],
        )
        # A framework with no chains is passed over; the chain belongs to the framework printed last before it.
        self.assertEqual([SdkPath("Harbor.Core", "net472", (("Lantern.Json", "12.0.3"),))], answer.paths["Harbor.Core"])

    def test_a_project_without_the_dependency_is_answered_with_no_chain(self) -> None:
        answer = parse_why(ABSENT_TEXT, "Nothing.Here")
        self.assertEqual({"Harbor.Core"}, answer.answered)
        self.assertEqual({"Harbor.Core": []}, answer.paths)

    def test_text_with_no_heading_answers_nothing(self) -> None:
        answer = parse_why(NO_ASSETS_TEXT, "Lantern.Json")
        self.assertEqual(set(), answer.answered)
        self.assertEqual({}, answer.paths)


class Runs:
    """A runner that answers each project's command from a table, keyed by the project path as given, and records
    every command."""

    def __init__(self, answers: dict[str, Completed]) -> None:
        self.answers = answers
        self.commands: list[list[str]] = []

    def __call__(self, arguments: Sequence[str], timeout: float) -> Completed:
        self.commands.append(list(arguments))
        project = Path(arguments[-2]).as_posix()
        answer = self.answers.get(project)
        if answer is None:
            raise FileNotFoundError(f"no answer for {project}")
        return answer


class RunTests(unittest.TestCase):
    def test_the_command_names_dotnet_the_project_and_the_package(self) -> None:
        self.assertEqual(
            ["dotnet", "nuget", "why", str(Path("a/Harbor.Core.csproj")), "Lantern.Json"],
            why_command(Path("a/Harbor.Core.csproj"), "Lantern.Json"),
        )

    def test_each_project_runs_alone_and_keeps_its_answer_when_another_fails(self) -> None:
        projects = [
            Path(f"src/{name}/{name}.csproj") for name in ("Harbor.Core", "Harbor.Tests", "Harbor.Slow", "Harbor.Gone")
        ]
        runs = Runs(
            {
                projects[0].as_posix(): Completed(0, CORE_TEXT),
                projects[1].as_posix(): Completed(0, NO_ASSETS_TEXT),
                projects[2].as_posix(): Completed(-1, "", timed_out=True),
            }
        )
        answer = run_why(projects, "Lantern.Json", Services(run=runs, which=lambda name: None), 30)
        self.assertEqual(4, len(runs.commands))
        self.assertTrue(all(command[:3] == ["dotnet", "nuget", "why"] for command in runs.commands))
        self.assertEqual({projects[0]}, answer.answered)
        self.assertEqual({projects[0]}, set(answer.paths))
        self.assertEqual(1, len(answer.paths[projects[0]]))
        self.assertEqual(
            [
                (projects[1], "dotnet nuget why answered nothing (no restored assets file)"),
                (projects[2], "dotnet nuget why did not finish within 30 seconds"),
            ],
            [(project, reason) for project, reason in answer.failures.items() if project != projects[3]],
        )
        self.assertTrue(answer.failures[projects[3]].startswith("dotnet nuget why could not start: "))

    def test_two_projects_with_one_file_name_keep_their_own_answers(self) -> None:
        api, tools = Path("src/Api/Shop.csproj"), Path("tools/Api/Shop.csproj")
        runs = Runs(
            {
                api.as_posix(): Completed(0, CORE_TEXT.replace("Harbor.Core", "Shop")),
                tools.as_posix(): Completed(0, ABSENT_TEXT.replace("Harbor.Core", "Shop")),
            }
        )
        answer = run_why([api, tools], "Lantern.Json", Services(run=runs), 30)
        self.assertEqual({api, tools}, answer.answered)
        self.assertEqual([SdkPath("Shop", "net472", (("Lantern.Json", "12.0.3"),))], answer.paths[api])
        self.assertEqual([], answer.paths[tools])

    def test_failures_name_the_reason_and_leave_the_project_unanswered(self) -> None:
        # A run with no heading names the first line it printed, which the parser could not read, such as an answer
        # in a language other than English.
        unread = "; the first line it printed, which the script cannot read: "
        localized = (
            f'\r\n  {CYAN}Projekt "Harbor.Core" weist keine Abh\u00e4ngigkeit von "Lantern.Json" auf.{RESET}\r\n'
        )
        for text, code, reason in (
            (NO_ASSETS_TEXT, 0, "(no restored assets file)"),
            ("boom", 1, f"(exit code 1){unread}boom"),
            ("", 1, "(exit code 1)"),
            (
                localized,
                0,
                f'(exit code 0){unread}Projekt "Harbor.Core" weist keine Abh\u00e4ngigkeit von "Lantern.Json" auf.',
            ),
        ):
            with self.subTest(reason=reason):
                runs = Runs({"Harbor.Core.csproj": Completed(code, text)})
                answer = run_why([Path("Harbor.Core.csproj")], "Lantern.Json", Services(run=runs), 30)
                self.assertEqual(
                    {Path("Harbor.Core.csproj"): f"dotnet nuget why answered nothing {reason}"}, answer.failures
                )
                self.assertEqual(set(), answer.answered)
        # A run that timed out after printing part of an answer is not taken as an answer.
        runs = Runs({"Harbor.Core.csproj": Completed(-1, CORE_TEXT, timed_out=True)})
        answer = run_why([Path("Harbor.Core.csproj")], "Lantern.Json", Services(run=runs), 7)
        self.assertEqual(
            {Path("Harbor.Core.csproj"): "dotnet nuget why did not finish within 7 seconds"}, answer.failures
        )
        self.assertEqual(({}, set()), (answer.paths, answer.answered))

    def test_a_run_that_exits_nonzero_after_printing_a_heading_is_a_failure_not_an_answer(self) -> None:
        # Part of a package tree, or a heading saying there is no dependency, followed by a failed exit, whichever
        # code other than 0 it exits with.
        partial = CORE_TEXT[: CORE_TEXT.index("  [net472]")]
        for text, code in ((partial, 1), (ABSENT_TEXT, 1), (partial, 2), (ABSENT_TEXT, 2)):
            with self.subTest(text=text[:40], code=code):
                runs = Runs({"Harbor.Core.csproj": Completed(code, text)})
                answer = run_why([Path("Harbor.Core.csproj")], "Lantern.Json", Services(run=runs), 30)
                self.assertEqual(
                    {
                        Path(
                            "Harbor.Core.csproj"
                        ): f"dotnet nuget why exited with code {code} after printing part of an answer"
                    },
                    answer.failures,
                )
                self.assertEqual(({}, set()), (answer.paths, answer.answered))

    def test_the_default_runner_joins_both_streams_and_reports_a_time_limit(self) -> None:
        result = sdk_why.subprocess_runner(
            [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"], 60
        )
        self.assertEqual(3, result.returncode)
        self.assertIn("out", result.text)
        self.assertIn("err", result.text)
        self.assertFalse(result.timed_out)
        slow = sdk_why.subprocess_runner([sys.executable, "-c", "import time; time.sleep(30)"], 0.5)
        self.assertTrue(slow.timed_out)
        self.assertEqual(-1, slow.returncode)

    def test_the_default_runner_runs_the_cli_in_english_and_keeps_every_other_variable(self) -> None:
        # Whatever language the machine or a variable names, the CLI prints the English the parser reads; the
        # environment is otherwise the one every bounded command gets, with each prompt off.
        script = (
            "import os; print(os.environ.get('DOTNET_CLI_UI_LANGUAGE'), os.environ.get('VSLANG'),"
            " os.environ.get('DOTNET_DEPENDENCIES_KEPT'), os.environ.get('GIT_TERMINAL_PROMPT'), sep='|')"
        )
        named = {"DOTNET_CLI_UI_LANGUAGE": "de", "VSLANG": "1031", "DOTNET_DEPENDENCIES_KEPT": "a kept value"}
        with mock.patch.dict(os.environ, named):
            result = sdk_why.subprocess_runner([sys.executable, "-c", script], 60)
        self.assertEqual((0, "en|1031|a kept value|0"), (result.returncode, result.text.strip()))

    def test_the_runner_states_why_the_policy_cannot_read_its_program(self) -> None:
        self.assertIn("subprocess_runner", sdk_why.COMMANDS_ALLOWED)


if __name__ == "__main__":
    unittest.main()
