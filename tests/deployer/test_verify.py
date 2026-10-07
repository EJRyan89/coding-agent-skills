from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import textwrap
import time
import unittest
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from unittest import mock

from harness import DeployerTestCase, Result, forward

from deployer import cli, discovery, platform_support
from deployer.discovery import Listed, ListingError

EXECUTABLES = {"codex": "C:/tools/codex.cmd", "copilot": "C:/tools/copilot.exe"}
VERSIONS = {"codex": "codex-cli 0.160.0\n", "copilot": "GitHub Copilot CLI 1.0.92.\n"}


def codex_answer(*skills: tuple[str, str, bool], errors: list | None = None) -> str:
    """The app server's answer to skills/list, which names each skill by its SKILL.md file."""
    entries = [
        {"name": name, "description": "d", "path": path, "scope": "user", "enabled": enabled}
        for name, path, enabled in skills
    ]
    return json.dumps(
        {
            "id": discovery.CODEX_LIST_ID,
            "result": {"data": [{"cwd": "C:/work", "skills": entries, "errors": errors or []}]},
        }
    )


def copilot_answer(*skills: tuple[str, str, bool]) -> str:
    """`copilot skill list --json`, which names each skill by its directory."""
    return json.dumps(
        [
            {"name": name, "description": "d", "source": "inherited", "path": path, "enabled": enabled}
            for name, path, enabled in skills
        ]
    )


class Runtimes:
    """Stands in for discovery.converse: answers each runtime's listing and records how it was started."""

    def __init__(self) -> None:
        self.answers: dict[str, list[str] | Exception] = {}
        self.calls: list[tuple[list[str], Path, Mapping[str, str], list[str], bool]] = []
        self.cwd_was_empty: list[bool] = []

    def __call__(
        self,
        arguments: list[str],
        cwd: Path,
        environment: Mapping[str, str],
        requests: list[str],
        answered: Callable[[str], bool] | None,
        timeout: float,
    ) -> list[str]:
        runtime = "codex" if arguments[0] == EXECUTABLES["codex"] else "copilot"
        self.calls.append((arguments, cwd, environment, requests, answered is not None))
        self.cwd_was_empty.append(cwd.is_dir() and not any(cwd.iterdir()))
        answer = self.answers[runtime]
        if isinstance(answer, Exception):
            raise answer
        return answer


class ParsingTests(unittest.TestCase):
    def test_copilot_paths_are_skill_directories_with_forward_slashes(self) -> None:
        found = discovery.parse_copilot(
            copilot_answer(
                ("alpha", "C:\\Users\\YourName\\a dir\\.agents\\skills\\alpha", True),
                ("beta", "C:\\Users\\YourName\\a dir\\.agents\\skills\\beta", False),
            )
        )
        self.assertEqual(
            {
                "alpha": [Listed("C:/Users/YourName/a dir/.agents/skills/alpha", True)],
                "beta": [Listed("C:/Users/YourName/a dir/.agents/skills/beta", False)],
            },
            found,
        )

    def test_copilot_entries_without_a_name_or_path_are_ignored_and_a_skills_object_is_accepted(self) -> None:
        output = json.dumps(
            {
                "skills": [
                    {"name": "alpha"},
                    {"path": "C:/x"},
                    "text",
                    {"name": "beta", "path": "C:/b", "enabled": "yes"},
                ]
            }
        )
        self.assertEqual({"beta": [Listed("C:/b", False)]}, discovery.parse_copilot(output))

    def test_copilot_output_that_is_not_a_json_list_is_a_listing_error(self) -> None:
        for output in ("", "Error: not signed in", json.dumps({"skills": "none"})):
            with self.subTest(output=output), self.assertRaises(ListingError):
                discovery.parse_copilot(output)

    def test_codex_paths_are_the_directories_of_their_skill_files_and_every_copy_is_kept(self) -> None:
        found = discovery.parse_codex(
            [
                json.dumps({"id": 0, "result": {"codexHome": "C:/x"}}),
                json.dumps({"method": "skills/changed"}),
                codex_answer(
                    ("alpha", "C:\\Users\\YourName\\a dir\\.agents\\skills\\alpha\\SKILL.md", True),
                    ("alpha", "C:\\Users\\YourName\\a dir\\.codex\\skills\\alpha\\SKILL.md", True),
                    ("beta", "C:/home/.agents/skills/beta/SKILL.md", False),
                ),
            ]
        )
        self.assertEqual(
            {
                "alpha": [
                    Listed("C:/Users/YourName/a dir/.agents/skills/alpha", True),
                    Listed("C:/Users/YourName/a dir/.codex/skills/alpha", True),
                ],
                "beta": [Listed("C:/home/.agents/skills/beta", False)],
            },
            found,
        )

    def test_a_codex_error_or_missing_answer_is_a_listing_error(self) -> None:
        cases = {
            "refused the skills/list request: unknown method": [
                json.dumps({"id": discovery.CODEX_LIST_ID, "error": {"code": -32601, "message": "unknown method"}})
            ],
            "did not answer": [json.dumps({"id": 0, "result": {}}), "not json"],
            "has no data": [json.dumps({"id": discovery.CODEX_LIST_ID, "result": {}})],
        }
        for reason, lines in cases.items():
            with self.subTest(reason=reason), self.assertRaisesRegex(ListingError, reason):
                discovery.parse_codex(lines)

    def test_codex_is_asked_for_the_skills_of_the_working_directory_after_its_handshake(self) -> None:
        cwd = Path("C:/a dir/work")
        requests = [json.loads(line) for line in discovery.codex_requests(cwd)]
        self.assertEqual(["initialize", "initialized", "skills/list"], [request["method"] for request in requests])
        self.assertEqual({"cwds": [str(cwd)]}, requests[2]["params"])
        self.assertTrue(discovery.codex_answered(json.dumps({"id": discovery.CODEX_LIST_ID, "result": {}})))
        self.assertFalse(discovery.codex_answered(json.dumps({"id": 0, "result": {}})))
        self.assertFalse(discovery.codex_answered("not json"))

    def test_each_runtime_is_started_with_its_listing_command(self) -> None:
        runtimes = Runtimes()
        runtimes.answers = {"codex": [codex_answer()], "copilot": [copilot_answer()]}
        cwd = Path("C:/work")
        for runtime in discovery.RUNTIMES:
            discovery.list_skills(runtime, EXECUTABLES[runtime], cwd, {"HOME": "C:/h"}, talk=runtimes)
        codex, copilot = runtimes.calls
        self.assertEqual(
            ([EXECUTABLES["codex"], "app-server"], cwd, {"HOME": "C:/h"}, discovery.codex_requests(cwd), True), codex
        )
        self.assertEqual(
            ([EXECUTABLES["copilot"], "skill", "list", "--json"], cwd, {"HOME": "C:/h"}, [], False), copilot
        )


class ConverseTests(DeployerTestCase):
    """discovery.converse against real child processes."""

    def program(self, body: str) -> list[str]:
        script = self.root / "program with spaces.py"
        script.write_text(textwrap.dedent(body), encoding="utf-8")
        return [sys.executable, str(script)]

    def talk(
        self,
        arguments: list[str],
        requests: Sequence[str] = (),
        answered: Callable[[str], bool] | None = None,
        timeout: float = 30,
    ) -> list[str]:
        return discovery.converse(
            arguments, self.root, {**os.environ, "PROBE": "a b"}, list(requests), answered, timeout
        )

    def test_stdin_stays_open_until_the_awaited_answer_and_later_output_is_not_read(self) -> None:
        # Like the Codex app server, the program stops at the end of stdin, so it answers only while it stays open.
        arguments = self.program("""
            import json, os, sys
            for line in sys.stdin:
                message = json.loads(line)
                if message.get("id") == 1:
                    print(json.dumps({"id": 1, "cwd": os.getcwd(), "probe": os.environ["PROBE"]}), flush=True)
                    print("after the answer", flush=True)
        """)
        lines = self.talk(
            arguments, ['{"id": 0}', '{"method": "initialized"}', '{"id": 1}'], answered=discovery.codex_answered
        )
        self.assertEqual([{"id": 1, "cwd": str(self.root), "probe": "a b"}], [json.loads(line) for line in lines])

    def test_a_program_that_ends_before_answering_names_its_exit_code_and_last_error_line(self) -> None:
        arguments = self.program("""
            import sys
            sys.stdin.readline()
            print("first", file=sys.stderr)
            print("Error: sign in first", file=sys.stderr)
            sys.exit(3)
        """)
        expected = r"^it exited before answering with exit code 3: Error: sign in first$"
        with self.assertRaisesRegex(ListingError, expected):
            self.talk(arguments, ['{"id": 0}'], answered=discovery.codex_answered)

    def test_without_an_awaited_answer_all_output_is_read_and_a_failure_is_an_error(self) -> None:
        self.assertEqual(["one", "two"], self.talk(self.program('print("one")\nprint("two")\n')))
        with self.assertRaisesRegex(ListingError, r"^it failed with exit code 2$"):
            self.talk(self.program("import sys\nsys.exit(2)\n"))

    def test_a_program_that_never_answers_is_stopped_at_the_timeout(self) -> None:
        arguments = self.program("import time\ntime.sleep(60)\n")
        started = time.monotonic()
        with self.assertRaisesRegex(ListingError, r"^no answer within 1 seconds$"):
            self.talk(arguments, ['{"id": 0}'], answered=discovery.codex_answered, timeout=1)
        self.assertLess(time.monotonic() - started, discovery.CLOSE_GRACE)

    def test_a_program_that_cannot_start_is_a_listing_error(self) -> None:
        with self.assertRaisesRegex(ListingError, "^cannot start it: "):
            self.talk([str(self.root / "missing.exe")])

    def test_a_program_started_without_its_pipes_is_stopped_and_a_listing_error(self) -> None:
        for missing in ("stdin", "stdout"):
            with self.subTest(missing=missing):
                process = mock.MagicMock(spec=subprocess.Popen)
                for pipe in ("stdin", "stdout"):
                    setattr(process, pipe, None if pipe == missing else mock.MagicMock())
                with (
                    mock.patch.object(discovery.subprocess, "Popen", return_value=process),
                    self.assertRaisesRegex(ListingError, r"^its pipes did not open$"),
                ):
                    self.talk([sys.executable], ['{"id": 0}'], answered=discovery.codex_answered)
                process.kill.assert_called_once_with()
                process.wait.assert_called_once_with()


class VerifyCommandTests(DeployerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_source_json()
        for name in ("alpha", "beta"):
            self.make_skill(name, name.title())
        self.make_skill("core", "Core", selectable=False)
        self.make_config()
        self.deploy_ok("--all")
        self.runtimes = Runtimes()
        self.installed = dict(EXECUTABLES)
        self.versions = dict(VERSIONS)

    def adapter(self, name: str) -> str:
        return forward(self.agents_dir / name)

    def answer(
        self, codex: list[tuple[str, str, bool]] | None = None, copilot: list[tuple[str, str, bool]] | None = None
    ) -> None:
        everything = [(name, self.adapter(name), True) for name in ("alpha", "beta")]
        codex = everything if codex is None else codex
        self.runtimes.answers["codex"] = [codex_answer(*((name, f"{path}/SKILL.md", on) for name, path, on in codex))]
        self.runtimes.answers["copilot"] = [copilot_answer(*(everything if copilot is None else copilot))]

    def groups(self, output: str, label: str) -> dict[str, list[str]]:
        """One runtime's report: report_groups reads to the end of the output, so cut it at the next section."""
        start = output.index(f"=== {label} ===")
        end = output.find("\n=== ", start)
        return self.report_groups(output[start:] if end < 0 else output[start:end], label)

    def version(
        self, arguments: list[str], environment: Mapping[str, str] | None = None
    ) -> platform_support.ToolResult:
        """A runtime's --version output, the only command verify runs besides each listing."""
        runtime = next(name for name, path in self.installed.items() if path == arguments[0])
        self.assertEqual([self.installed[runtime], "--version"], arguments)
        return platform_support.ToolResult(0, self.versions[runtime])

    def verify(self, *arguments: str) -> Result:
        captured = io.StringIO()
        with (
            mock.patch("deployer.discovery.converse", self.runtimes),
            mock.patch("deployer.platform_support.find_executable", side_effect=self.installed.get),
            mock.patch("deployer.platform_support.run_tool", side_effect=self.version),
            contextlib.redirect_stdout(captured),
            contextlib.redirect_stderr(captured),
        ):
            code = cli.main(["verify", *arguments], self.paths)
        return Result(code, captured.getvalue())

    def test_both_runtimes_finding_every_adapter_passes(self) -> None:
        self.answer()
        result = self.verify()
        self.assertEqual(0, result.code, result.output)
        self.assertEqual(
            "\n"
            f"Adapters: 2 in {forward(self.agents_dir)}\n"
            "\n=== CODEX CLI ===\n\nFOUND (2):\n  alpha\n  beta\n\n"
            "\n=== COPILOT CLI ===\n\nFOUND (2):\n  alpha\n  beta\n\n"
            "Verified: Codex CLI and Copilot CLI find every adapter.\n\n",
            result.output,
        )

    def test_each_runtime_lists_from_an_empty_directory_it_removes_afterwards(self) -> None:
        self.answer()
        self.verify()
        self.assertEqual([True, True], self.runtimes.cwd_was_empty)
        directories = {call[1] for call in self.runtimes.calls}
        self.assertEqual(1, len(directories))
        self.assertFalse(directories.pop().exists())

    def test_a_missing_disabled_or_shadowed_adapter_fails_and_names_the_runtime_documentation(self) -> None:
        other = "C:/Users/YourName/.codex/skills/alpha"
        self.answer(
            codex=[("alpha", self.adapter("alpha"), True), ("alpha", other, True)],
            copilot=[("beta", self.adapter("beta"), False)],
        )
        result = self.verify()
        self.assertEqual(1, result.code, result.output)
        self.assertEqual(
            {"SHADOWED": [f"alpha (also {other})"], "NOT FOUND": ["beta"]}, self.groups(result.output, "CODEX CLI")
        )
        self.assertEqual(
            {"NOT FOUND": ["alpha"], "DISABLED": ["beta (turned off in the runtime's settings)"]},
            self.groups(result.output, "COPILOT CLI"),
        )
        self.assertTrue(
            result.output.endswith(
                "Verification failed for Codex CLI and Copilot CLI.\n"
                "See docs/codex-support.md and docs/copilot-support.md.\n\n"
            ),
            result.output,
        )

    def test_copilot_listing_another_copy_in_place_of_the_adapter_is_shadowed(self) -> None:
        personal = forward(self.home / ".copilot" / "skills" / "alpha")
        self.answer(copilot=[("alpha", personal, True), ("beta", self.adapter("beta").upper(), True)])
        result = self.verify()
        self.assertEqual(1, result.code, result.output)
        self.assertEqual(
            {"SHADOWED": [f"alpha (also {personal})"], "FOUND": ["beta"]}, self.groups(result.output, "COPILOT CLI")
        )
        self.assertIn("Verification failed for Copilot CLI.\nSee docs/copilot-support.md.\n", result.output)

    def test_a_runtime_listing_the_adapters_under_another_spelling_of_the_home_finds_them(self) -> None:
        # Copilot CLI lists a profile folder by its 8.3 short name on some machines (RUNNER~1 for
        # runneradmin). A junction is the same directory under another name, which a textual comparison misses.
        alias = self.home.parent / "Alias Of Home"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(self.home)], check=True, capture_output=True)
        try:
            through_alias = [(name, forward(alias / ".agents" / "skills" / name), True) for name in ("alpha", "beta")]
            self.answer(copilot=through_alias)
            result = self.verify()
        finally:
            # Removes the junction only, never the directory it points to.
            alias.rmdir()
        self.assertEqual(0, result.code, result.output)
        self.assertEqual({"FOUND": ["alpha", "beta"]}, self.groups(result.output, "COPILOT CLI"))

    def test_a_runtime_that_is_not_installed_is_skipped(self) -> None:
        del self.installed["codex"]
        self.answer()
        result = self.verify()
        self.assertEqual(0, result.code, result.output)
        self.assertIn("\n=== CODEX CLI ===\n\nNot installed; skipped.\n\n", result.output)
        self.assertEqual({"FOUND": ["alpha", "beta"]}, self.groups(result.output, "COPILOT CLI"))
        self.assertTrue(result.output.endswith("Verified: Copilot CLI finds every adapter.\n\n"), result.output)
        self.assertEqual(1, len(self.runtimes.calls))

    def test_a_runtime_older_than_its_floor_is_outdated_not_started_and_fails(self) -> None:
        # Copilot CLI before 1.0.88 and Codex CLI before 0.88.0 cannot give the listing verify reads.
        self.answer()
        self.versions["copilot"] = "GitHub Copilot CLI 1.0.87.\n"
        result = self.verify()
        self.assertEqual(1, result.code, result.output)
        self.assertIn(
            "\n=== COPILOT CLI ===\n\n"
            "OUTDATED: Copilot CLI 1.0.87 is older than 1.0.88, the oldest version verify can read. "
            "Update it, then rerun.\n\n",
            result.output,
        )
        self.assertEqual({"FOUND": ["alpha", "beta"]}, self.groups(result.output, "CODEX CLI"))
        self.assertEqual([EXECUTABLES["codex"]], [call[0][0] for call in self.runtimes.calls])
        self.assertTrue(
            result.output.endswith("Verification failed for Copilot CLI.\nSee docs/copilot-support.md.\n\n"),
            result.output,
        )

        self.runtimes.calls.clear()
        self.versions.update(codex="codex-cli 0.87.0\n", copilot="GitHub Copilot CLI 1.0.88.\n")
        result = self.verify()
        self.assertEqual(1, result.code, result.output)
        self.assertIn(
            "\n=== CODEX CLI ===\n\n"
            "OUTDATED: Codex CLI 0.87.0 is older than 0.88.0, the oldest version verify can read. "
            "Update it, then rerun.\n\n",
            result.output,
        )
        self.assertEqual({"FOUND": ["alpha", "beta"]}, self.groups(result.output, "COPILOT CLI"))
        self.assertEqual([EXECUTABLES["copilot"]], [call[0][0] for call in self.runtimes.calls])

    def test_a_runtime_at_its_floor_or_with_an_unreadable_version_is_listed(self) -> None:
        self.answer()
        self.versions.update(codex="codex-cli 0.88.0\n", copilot="no version here\n")
        result = self.verify()
        self.assertEqual(0, result.code, result.output)
        self.assertEqual(2, len(self.runtimes.calls))

    def test_nothing_verified_is_a_failure(self) -> None:
        self.installed.clear()
        result = self.verify()
        self.assertEqual(1, result.code, result.output)
        self.assertTrue(
            result.output.endswith("Neither Codex CLI nor Copilot CLI is installed, so nothing was verified.\n\n"),
            result.output,
        )

    def test_a_runtime_that_cannot_list_its_skills_fails_and_the_other_is_still_checked(self) -> None:
        self.answer()
        self.runtimes.answers["codex"] = ListingError("no answer within 120 seconds")
        result = self.verify()
        self.assertEqual(1, result.code, result.output)
        self.assertIn("\n=== CODEX CLI ===\n\nCannot list its skills: no answer within 120 seconds\n\n", result.output)
        self.assertEqual({"FOUND": ["alpha", "beta"]}, self.groups(result.output, "COPILOT CLI"))
        self.assertIn("Verification failed for Codex CLI.\nSee docs/codex-support.md.\n", result.output)

    def test_adapters_of_every_deployed_source_are_checked(self) -> None:
        data = self.manifest()
        adapter = data["sources"]["test/skills"]["wrappers"]["alpha"]
        data["sources"]["test/other"] = {"wrappers": {"other-alpha": adapter}}
        self.write_manifest(data)
        self.answer()
        result = self.verify()
        self.assertEqual(1, result.code, result.output)
        self.assertEqual(["other-alpha"], self.groups(result.output, "CODEX CLI")["NOT FOUND"])

    def test_without_deployed_adapters_nothing_is_listed(self) -> None:
        self.manifest_file.unlink()
        result = self.verify()
        self.assertEqual(1, result.code, result.output)
        self.assertIn(
            f"ERROR: No runtime adapters are deployed in {forward(self.agents_dir)}.\n"
            "Deploy first with 'python deploy.py'.\n",
            result.output,
        )
        self.assertEqual([], self.runtimes.calls)

    def test_help_describes_the_command_without_running_it(self) -> None:
        result = self.verify("--help")
        self.assertEqual(0, result.code, result.output)
        self.assertIn("No model is started; nothing is changed.", result.output)
        self.assertEqual([], self.runtimes.calls)
        self.assertEqual(2, self.verify("--extra").code)


if __name__ == "__main__":
    unittest.main()
