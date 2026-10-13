"""Regression tests for the `dotnet nuget why` runner and parser, against output captured from the SDK."""

from __future__ import annotations

import sys
import unittest
from collections.abc import Sequence
from pathlib import Path

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
        self.assertIsNone(answer.failure)
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
    """A runner that answers each project's command from a table and records every command."""

    def __init__(self, answers: dict[str, Completed]) -> None:
        self.answers = answers
        self.commands: list[list[str]] = []

    def __call__(self, arguments: Sequence[str], timeout: float) -> Completed:
        self.commands.append(list(arguments))
        project = Path(arguments[-2]).stem
        answer = self.answers.get(project)
        if answer is None:
            raise FileNotFoundError(f"no answer for {project}")
        return answer


class RunTests(unittest.TestCase):
    def test_the_command_names_dotnet_the_project_and_the_package(self) -> None:
        self.assertEqual(
            ["dotnet", "nuget", "why", str(Path("a/Harbor.Core.csproj")), "Lantern.Json"],
            why_command([Path("a/Harbor.Core.csproj")], "Lantern.Json"),
        )

    def test_each_project_runs_alone_and_keeps_its_answer_when_another_fails(self) -> None:
        runs = Runs(
            {
                "Harbor.Core": Completed(0, SOLUTION_TEXT),
                "Harbor.Tests": Completed(0, NO_ASSETS_TEXT),
                "Harbor.Slow": Completed(-1, "", timed_out=True),
            }
        )
        projects = [
            Path(f"src/{name}/{name}.csproj") for name in ("Harbor.Core", "Harbor.Tests", "Harbor.Slow", "Harbor.Gone")
        ]
        answer = run_why(projects, "Lantern.Json", Services(run=runs, which=lambda name: None), 30)
        self.assertEqual(4, len(runs.commands))
        self.assertTrue(all(command[:3] == ["dotnet", "nuget", "why"] for command in runs.commands))
        self.assertEqual({"Harbor.Web", "Harbor.Core"}, answer.answered)
        self.assertEqual(1, len(answer.paths["Harbor.Core"]))
        self.assertIn("could not start", answer.failure or "")

    def test_failures_name_the_project_and_the_reason(self) -> None:
        for text, code, reason in ((NO_ASSETS_TEXT, 0, "no restored assets file"), ("boom", 1, "exit code 1")):
            with self.subTest(reason=reason):
                runs = Runs({"Harbor.Core": Completed(code, text)})
                answer = run_why([Path("Harbor.Core.csproj")], "Lantern.Json", Services(run=runs), 30)
                self.assertEqual(f"dotnet nuget why answered nothing for Harbor.Core.csproj ({reason})", answer.failure)
        runs = Runs({"Harbor.Core": Completed(-1, "", timed_out=True)})
        answer = run_why([Path("Harbor.Core.csproj")], "Lantern.Json", Services(run=runs), 7)
        self.assertEqual("dotnet nuget why did not finish within 7 seconds", answer.failure)

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

    def test_the_runner_states_why_the_policy_cannot_read_its_program(self) -> None:
        self.assertIn("subprocess_runner", sdk_why.COMMANDS_ALLOWED)


if __name__ == "__main__":
    unittest.main()
