"""Regression tests for the reviewer boundary hook."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIRECTORY))

import review_guard as guard  # noqa: E402
import review_pipeline as rp  # noqa: E402


class ReviewGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.run = self.root / "code-review-run-abc"
        for folder in ("work", "source/app", "reviewer/.claude/agents"):
            (self.run / folder).mkdir(parents=True)
        (self.run / "run.json").write_text("{}", encoding="utf-8")
        self.checkout = self.root / "GitHub" / "product"
        (self.checkout / "app").mkdir(parents=True)
        self.outside_cwd = self.root / "GitHub"

    def decide(self, tool: str, **tool_input: object) -> str | None:
        return guard.decide({"tool_name": tool, "tool_input": tool_input, "cwd": str(self.outside_cwd)})

    def test_reads_are_allowed_only_inside_a_review_run_or_the_references(self) -> None:
        allowed = [
            ("Read", {"file_path": str(self.run / "work" / "csharp-review.prompt.md")}),
            ("Read", {"file_path": str(self.run / "source" / "app" / "Service.cs")}),
            ("Read", {"file_path": str(self.run / "reviewer" / ".claude" / "agents" / "csharp-review.md")}),
            ("Read", {"file_path": str(guard.REFERENCES / "generic-reviewer.md")}),
            ("Grep", {"pattern": "class Role", "path": str(self.run / "source")}),
            ("Glob", {"pattern": "**/*.cs", "path": str(self.run / "source")}),
        ]
        for tool, tool_input in allowed:
            with self.subTest(tool=tool, input=tool_input):
                self.assertIsNone(self.decide(tool, **tool_input))
        denied = [
            # The reads a profiled canary made: the live checkout and the whole workspace.
            ("Read", {"file_path": str(self.checkout / "app" / "Collections.cs")}),
            ("Grep", {"pattern": "GetAssignments", "path": str(self.checkout)}),
            ("Grep", {"pattern": "GetAssignments", "path": str(self.outside_cwd)}),
            ("Grep", {"pattern": "anything"}),  # defaults to the session's working directory
            ("Glob", {"pattern": "**/*.cs"}),
            ("Read", {"file_path": "app/Collections.cs"}),  # relative to the session's working directory
            ("Read", {"file_path": str(self.run / ".." / "GitHub" / "product" / "app" / "Collections.cs")}),
            ("Glob", {"pattern": "../GitHub/**", "path": str(self.run / "source")}),
            ("Glob", {"pattern": str(self.checkout / "**"), "path": str(self.run / "source")}),
            ("Read", {"file_path": str(self.root / "code-review-run-fake" / "x.cs")}),  # no run.json there
        ]
        for tool, tool_input in denied:
            with self.subTest(tool=tool, input=tool_input):
                self.assertIn("Code-review reviewer boundary", self.decide(tool, **tool_input) or "")

    def test_a_shell_style_drive_path_is_understood(self) -> None:
        drive, rest = str(self.checkout).replace("\\", "/").split(":", 1)
        self.assertIsNotNone(self.decide("Read", file_path=f"/{drive.lower()}{rest}/app/Collections.cs"))

    def test_writes_are_allowed_only_on_a_result_file(self) -> None:
        for tool in ("Write", "Edit"):
            with self.subTest(tool=tool):
                self.assertIsNone(self.decide(tool, file_path=str(self.run / "work" / "csharp-review.result.json")))
                self.assertIsNone(self.decide(tool, file_path=str(self.run / "result.json")))
                for path in (
                    self.run / "work" / "notes.txt",
                    self.run / "source" / "app" / "x.result.json",
                    self.run / "work" / "csharp-review.prompt.md",
                    self.checkout / "result.json",
                ):
                    self.assertIsNotNone(self.decide(tool, file_path=str(path)), path)

    def test_bash_runs_only_the_pipelines_own_self_check(self) -> None:
        command = rp.self_check_command(self.run, "csharp-review")
        self.assertIsNone(self.decide("Bash", command=command))
        for bad in (
            "cat source/app/Service.cs",
            f"{command} && git log",
            f"{command}; rm -rf /",
            command.replace("validate-result", "finalize"),
            command.replace(str(rp.Path(rp.__file__).resolve()), str(self.root / "review_pipeline.py")),
            rp.self_check_command(self.checkout, "csharp-review"),
            rp.self_check_command(self.run / "work", "csharp-review"),
            command.replace('"csharp-review"', '"$(whoami)"'),
        ):
            with self.subTest(command=bad):
                self.assertIsNotNone(self.decide("Bash", command=bad))

    def test_other_tools_pass_and_unreadable_events_are_denied(self) -> None:
        self.assertIsNone(guard.decide({"tool_name": "SubagentHandback", "tool_input": {}}))
        self.assertIsNotNone(guard.decide({"tool_name": "Read", "tool_input": "not an object"}))
        self.assertIsNotNone(guard.decide({"tool_name": "Read", "tool_input": {"file_path": 3}}))

    def test_the_hook_prints_a_deny_decision_and_fails_closed(self) -> None:
        def hook(stdin: str) -> str:
            return subprocess.run(
                [sys.executable, "-B", str(SCRIPT_DIRECTORY / "review_guard.py")],
                input=stdin,
                capture_output=True,
                text=True,
                check=True,
            ).stdout

        event = {
            "tool_name": "Read",
            "tool_input": {"file_path": str(self.checkout / "app" / "x.cs")},
            "cwd": str(self.outside_cwd),
        }
        decision = json.loads(hook(json.dumps(event)))["hookSpecificOutput"]
        self.assertEqual(("PreToolUse", "deny"), (decision["hookEventName"], decision["permissionDecision"]))
        self.assertIn("may only look inside the review run folder", decision["permissionDecisionReason"])
        allowed = {**event, "tool_input": {"file_path": str(self.run / "source" / "app" / "x.cs")}}
        self.assertEqual("", hook(json.dumps(allowed)), "an allowed call prints nothing")
        self.assertEqual("deny", json.loads(hook("not json"))["hookSpecificOutput"]["permissionDecision"])


if __name__ == "__main__":
    unittest.main()
