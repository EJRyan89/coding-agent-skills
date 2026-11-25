"""Regression tests for ask_sdk.py, the one entry point that starts `dotnet nuget why`, and its runner."""

from __future__ import annotations

import contextlib
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import ask_sdk
import frontmatter
from dotnet_solution import same_path
from sdk_why import Completed, Services
from test_dependency_graph import UNPLACED_ASSETS, TableSdk, restored_with, solution

SKILL = Path(__file__).resolve().parents[1] / "SKILL.md"


class RunnerTests(unittest.TestCase):
    def test_the_runner_joins_both_streams_and_reports_a_time_limit(self) -> None:
        result = ask_sdk.subprocess_runner(
            [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"], 60, Path()
        )
        self.assertEqual(3, result.returncode)
        self.assertIn("out", result.text)
        self.assertIn("err", result.text)
        self.assertFalse(result.timed_out)
        slow = ask_sdk.subprocess_runner([sys.executable, "-c", "import time; time.sleep(30)"], 0.5, Path())
        self.assertTrue(slow.timed_out)
        self.assertEqual(-1, slow.returncode)

    def test_the_runner_runs_the_cli_in_english_and_keeps_every_other_variable(self) -> None:
        # Whatever language the machine or a variable names, the CLI prints the English the parser reads; the
        # environment is otherwise the one every bounded command gets, with each prompt off.
        script = (
            "import os; print(os.environ.get('DOTNET_CLI_UI_LANGUAGE'), os.environ.get('VSLANG'),"
            " os.environ.get('DOTNET_DEPENDENCIES_KEPT'), os.environ.get('GIT_TERMINAL_PROMPT'), sep='|')"
        )
        named = {"DOTNET_CLI_UI_LANGUAGE": "de", "VSLANG": "1031", "DOTNET_DEPENDENCIES_KEPT": "a kept value"}
        with mock.patch.dict(os.environ, named):
            result = ask_sdk.subprocess_runner([sys.executable, "-c", script], 60, Path())
        self.assertEqual((0, "en|1031|a kept value|0"), (result.returncode, result.text.strip()))

    def test_the_runner_runs_in_the_folder_it_is_given(self) -> None:
        # The .NET host picks the SDK by the nearest global.json at or above the folder it runs in.
        with tempfile.TemporaryDirectory() as folder:
            result = ask_sdk.subprocess_runner(
                [sys.executable, "-c", "import os; print(os.getcwd())"], 60, Path(folder)
            )
            self.assertEqual(Path(folder).resolve(), Path(result.text.strip()).resolve())

    def test_the_runner_states_why_the_policy_cannot_read_its_program(self) -> None:
        self.assertIn("subprocess_runner", ask_sdk.COMMANDS_ALLOWED)


class EntryPointTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dotnet-dependencies-ask-")).resolve()
        self.addCleanup(self.remove_root)

    def remove_root(self) -> None:
        for path in sorted(self.root.rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        self.root.rmdir()

    def run_entry(self, *arguments: str, services: Services) -> tuple[int, list[list[str]], str]:
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                code = ask_sdk.main(list(arguments), services)
            except SystemExit as exited:
                code = int(exited.code or 0)
        return code, [line.split("\t") for line in output.getvalue().splitlines()], errors.getvalue()

    def test_it_asks_the_sdk_about_a_project_whose_restore_the_walk_cannot_place(self) -> None:
        project = self.root / "Moved" / "Moved.csproj"
        project.parent.mkdir()
        project.write_text(restored_with(UNPLACED_ASSETS), encoding="utf-8")
        (self.root / "X.sln").write_text(solution("Moved\\Moved.csproj"), encoding="utf-8")
        answer = (
            "Project 'Moved' has the following dependency graph(s) for 'Lantern.Json':\r\n\r\n  [net8.0]\r\n"
            "  └── Quill.Http (v1.2.0)\r\n      └── Lantern.Json (v12.0.3)\r\n\r\n"
        )
        sdk = TableSdk({(same_path(project), "lantern.json"): answer})
        services = Services(run=sdk, which=lambda name: "dotnet")
        code, rows, _ = self.run_entry("Lantern.Json", "--root", str(self.root), services=services)
        self.assertEqual(0, code)
        self.assertEqual([[str(project), "Lantern.Json"]], [command[3:] for command in sdk.commands])
        self.assertIn(
            [
                "PATH",
                "Moved/Moved.csproj",
                "net8.0",
                "Lantern.Json",
                "sdk",
                "Quill.Http (v1.2.0) > Lantern.Json (v12.0.3)",
            ],
            rows,
        )

    def test_it_refuses_all(self) -> None:
        services = Services(run=lambda arguments, timeout, folder: Completed(0, ""), which=lambda name: None)
        code, _, errors = self.run_entry("--all", "--project", "x.csproj", services=services)
        self.assertEqual(2, code)
        self.assertIn("ask_sdk.py takes names, not --all", errors)


class GrantTests(unittest.TestCase):
    def test_no_grant_of_the_skill_covers_the_entry_point_that_starts_the_sdk(self) -> None:
        # Claude Code asks before a command no grant matches, so the SDK route waits for the user's approval; a grant
        # pattern is the command's text with `*` for any text.
        grants = frontmatter.read(SKILL).value("allowed-tools")
        assert isinstance(grants, list)
        command = '{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/ask_sdk.py" "Lantern.Json"'
        for grant in grants:
            body = str(grant)[str(grant).index("(") + 1 : -1]
            pattern = "".join(".*" if part == "*" else re.escape(part) for part in re.split(r"(\*)", body))
            with self.subTest(grant=grant):
                self.assertIsNone(re.fullmatch(pattern, command, re.DOTALL))
        self.assertTrue(ask_sdk.RUNS_REPOSITORY_CODE)
