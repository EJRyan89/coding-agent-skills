"""Regression tests for the reviewer boundary hook."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import review_guard as guard
import review_pipeline as rp

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
AGENT = "a71dab35ebc1b97eb"  # the form Claude Code gives an agent ID
OTHER_AGENT = "a73176509870d6114"


def make_run(folder: Path, roles: list[str], *, entrypoint: bool = False) -> Path:
    """A prepared review run as the guard sees it: run.json naming each role's prompt and result file."""
    for sub in ("work", "source/app", "reviewer/.claude/agents"):
        (folder / sub).mkdir(parents=True, exist_ok=True)
    entries = (
        [
            {
                "id": roles[0],
                "prompt_file": str(folder / "reviewer.prompt.md"),
                "result_file": str(folder / "result.json"),
            }
        ]
        if entrypoint
        else [
            {
                "id": role,
                "prompt_file": str(folder / "work" / f"{role}.prompt.md"),
                "result_file": str(folder / "work" / f"{role}.result.json"),
            }
            for role in roles
        ]
    )
    (folder / "run.json").write_text(json.dumps({"roles": entries}), encoding="utf-8")
    return folder


class GuardFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.run_directory = make_run(self.root / "code-review-run-abc", ["csharp-review", "sql-review"])
        self.other_run = make_run(self.root / "code-review-run-def", ["csharp-review"])
        self.checkout = self.root / "GitHub" / "product"
        (self.checkout / "app").mkdir(parents=True)
        self.outside_cwd = self.root / "GitHub"
        self.claims = self.root / "claims"
        patcher = mock.patch.object(guard, "CLAIMS", self.claims)
        patcher.start()
        self.addCleanup(patcher.stop)

    def decide(self, tool: str, agent: str | None = AGENT, **tool_input: object) -> str | None:
        event: dict[str, Any] = {"tool_name": tool, "tool_input": tool_input, "cwd": str(self.outside_cwd)}
        if agent is not None:
            event["agent_id"] = agent
        return guard.decide(event)

    def prompt(self, role: str = "csharp-review", run: Path | None = None) -> str:
        return str((run or self.run_directory) / "work" / f"{role}.prompt.md")

    def result(self, role: str = "csharp-review", run: Path | None = None) -> str:
        return str((run or self.run_directory) / "work" / f"{role}.result.json")

    def claim(self, role: str = "csharp-review", run: Path | None = None, agent: str = AGENT) -> None:
        """The reviewer's first call: reading the prompt its task names, which binds it to that role."""
        self.assertIsNone(self.decide("Read", agent=agent, file_path=self.prompt(role, run)))

    def assert_denied(self, reason: str | None, text: str = "Code-review reviewer boundary") -> None:
        self.assertIsNotNone(reason)
        self.assertIn(text, reason or "")


class ReadTests(GuardFixture):
    def test_a_reviewer_reads_nothing_before_its_prompt(self) -> None:
        source = str(self.run_directory / "source" / "app" / "Service.cs")
        for tool, tool_input in (
            ("Read", {"file_path": source}),
            ("Read", {"file_path": str(self.run_directory / "work" / "csharp-review.diff")}),
            ("Read", {"file_path": str(guard.REFERENCES / "generic-reviewer.md")}),
            ("Grep", {"pattern": "x", "path": str(self.run_directory / "work")}),
            ("Glob", {"pattern": "*.prompt.md", "path": str(self.run_directory / "work")}),
        ):
            with self.subTest(tool=tool, input=tool_input):
                self.assert_denied(self.decide(tool, **tool_input), "read the prompt file your task names first")
        self.assertFalse(self.claims.exists(), "a denied read claims nothing")
        self.claim()
        self.assertIsNone(self.decide("Read", file_path=source))

    def test_reads_are_allowed_only_inside_its_own_run_or_the_references(self) -> None:
        self.claim()
        allowed = [
            ("Read", {"file_path": self.prompt()}),
            ("Read", {"file_path": str(self.run_directory / "source" / "app" / "Service.cs")}),
            ("Read", {"file_path": str(self.run_directory / "reviewer" / ".claude" / "agents" / "csharp-review.md")}),
            ("Read", {"file_path": str(guard.REFERENCES / "generic-reviewer.md")}),
            ("Grep", {"pattern": "class Role", "path": str(self.run_directory / "source")}),
            ("Glob", {"pattern": "**/*.cs", "path": str(self.run_directory / "source")}),
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
            ("Read", {"file_path": str(self.run_directory / ".." / "GitHub" / "product" / "app" / "Collections.cs")}),
            ("Glob", {"pattern": "../GitHub/**", "path": str(self.run_directory / "source")}),
            ("Glob", {"pattern": str(self.checkout / "**"), "path": str(self.run_directory / "source")}),
            ("Read", {"file_path": str(self.root / "code-review-run-fake" / "x.cs")}),  # no run.json there
            # Another pull request's run: its untrusted diff and its prompt.
            ("Read", {"file_path": str(self.other_run / "source" / "app" / "Service.cs")}),
            ("Read", {"file_path": self.prompt(run=self.other_run)}),
            ("Grep", {"pattern": "x", "path": str(self.other_run)}),
        ]
        for tool, tool_input in denied:
            with self.subTest(tool=tool, input=tool_input):
                self.assert_denied(self.decide(tool, **tool_input))

    def test_a_shell_style_drive_path_is_understood(self) -> None:
        self.claim()
        drive, rest = str(self.checkout).replace("\\", "/").split(":", 1)
        self.assertIsNotNone(self.decide("Read", file_path=f"/{drive.lower()}{rest}/app/Collections.cs"))


class WriteTests(GuardFixture):
    def test_a_reviewer_writes_only_its_own_roles_result_in_its_own_run(self) -> None:
        self.claim()
        for tool in ("Write", "Edit"):
            with self.subTest(tool=tool):
                self.assertIsNone(self.decide(tool, file_path=self.result()))
                for path in (
                    self.result("sql-review"),  # a sibling role's result
                    self.result(run=self.other_run),  # the same role in another pull request's run
                    str(self.run_directory / "result.json"),  # an entrypoint's result, which is not this role's
                    str(self.run_directory / "work" / "notes.txt"),
                    str(self.run_directory / "source" / "app" / "x.result.json"),
                    self.prompt(),
                    str(self.run_directory / "run.json"),
                    str(self.checkout / "result.json"),
                ):
                    with self.subTest(path=path):
                        self.assert_denied(self.decide(tool, file_path=path), "write only your own result file")

    def test_a_reviewer_cannot_write_before_it_reads_its_prompt(self) -> None:
        self.assert_denied(self.decide("Write", file_path=self.result()), "read the prompt file your task names first")

    def test_reading_a_siblings_prompt_does_not_move_the_claim(self) -> None:
        self.claim()
        # A sibling's prompt is trusted text in the reviewer's own run, but reading it binds nothing.
        self.assertIsNone(self.decide("Read", file_path=self.prompt("sql-review")))
        self.assert_denied(self.decide("Write", file_path=self.result("sql-review")), "write only your own result file")
        self.assertIsNone(self.decide("Write", file_path=self.result()))

    def test_a_retry_claims_the_same_role_with_its_own_agent(self) -> None:
        self.claim()
        self.claim(agent=OTHER_AGENT)
        self.assertIsNone(self.decide("Write", agent=OTHER_AGENT, file_path=self.result()))
        self.assertIsNone(self.decide("Write", file_path=self.result()))

    def test_an_entrypoint_reviewer_writes_the_runs_result(self) -> None:
        run = make_run(self.root / "code-review-run-ghi", ["repository-review"], entrypoint=True)
        self.assertIsNone(self.decide("Read", file_path=str(run / "reviewer.prompt.md")))
        self.assertIsNone(self.decide("Write", file_path=str(run / "result.json")))
        self.assert_denied(self.decide("Write", file_path=str(run / "work" / "repository-review.result.json")))


class BashTests(GuardFixture):
    def test_bash_runs_only_the_self_check_of_its_own_role_and_run(self) -> None:
        command = rp.self_check_command(self.run_directory, "csharp-review")
        self.assert_denied(self.decide("Bash", command=command), "read the prompt file your task names first")
        self.claim()
        self.assertIsNone(self.decide("Bash", command=command))
        for bad in (
            "cat source/app/Service.cs",
            f"{command} && git log",
            f"{command}; rm -rf /",
            command.replace("validate-result", "finalize"),
            command.replace(str(rp.Path(rp.__file__).resolve()), str(self.root / "review_pipeline.py")),
            rp.self_check_command(self.checkout, "csharp-review"),
            rp.self_check_command(self.run_directory / "work", "csharp-review"),
            command.replace('"csharp-review"', '"$(whoami)"'),
            rp.self_check_command(self.run_directory, "sql-review"),
            rp.self_check_command(self.other_run, "csharp-review"),
        ):
            with self.subTest(command=bad):
                self.assert_denied(self.decide("Bash", command=bad))


class ClaimTests(GuardFixture):
    def test_without_an_agent_id_every_guarded_call_is_denied(self) -> None:
        for agent in (None, "", "../escape", "a b", 7):
            with self.subTest(agent=agent):
                event: dict[str, Any] = {
                    "tool_name": "Read",
                    "tool_input": {"file_path": self.prompt()},
                    "cwd": str(self.outside_cwd),
                }
                if agent is not None:
                    event["agent_id"] = agent
                self.assert_denied(guard.decide(event), "agent ID")
                for tool, tool_input in (
                    ("Write", {"file_path": self.result()}),
                    ("Bash", {"command": rp.self_check_command(self.run_directory, "csharp-review")}),
                ):
                    self.assert_denied(guard.decide({**event, "tool_name": tool, "tool_input": tool_input}))
        self.assertFalse(self.claims.exists())

    def test_a_claim_is_one_file_per_agent_naming_its_run_and_role(self) -> None:
        self.claim()
        claim = json.loads((self.claims / f"{AGENT}.json").read_text(encoding="utf-8"))
        self.assertEqual({"run": str(self.run_directory), "role": "csharp-review"}, claim)

    def test_an_unreadable_claim_denies(self) -> None:
        self.claims.mkdir()
        (self.claims / f"{AGENT}.json").write_text("not json", encoding="utf-8")
        self.assert_denied(self.decide("Read", file_path=self.prompt()))
        (self.claims / f"{AGENT}.json").write_text(json.dumps({"run": str(self.run_directory)}), encoding="utf-8")
        self.assert_denied(self.decide("Write", file_path=self.result()))

    def test_a_claim_on_a_role_the_run_no_longer_has_denies(self) -> None:
        self.claim()
        make_run(self.run_directory, ["sql-review"])
        self.assert_denied(self.decide("Write", file_path=self.result()))

    def test_a_new_claim_removes_claims_whose_run_is_gone(self) -> None:
        self.claims.mkdir()
        stale = self.claims / f"{OTHER_AGENT}.json"
        stale.write_text(json.dumps({"run": str(self.root / "code-review-run-gone"), "role": "x"}), encoding="utf-8")
        live = self.claims / "a000000000000000f.json"
        live.write_text(json.dumps({"run": str(self.other_run), "role": "csharp-review"}), encoding="utf-8")
        self.claim()
        self.assertEqual(sorted([f"{AGENT}.json", live.name]), sorted(path.name for path in self.claims.iterdir()))


class HookTests(GuardFixture):
    def hook(self, stdin: str | bytes, **environment: str) -> subprocess.CompletedProcess[Any]:
        # The hook process finds its claims under the system temporary directory, here this test's own.
        temporary = str(self.root / "tmp")
        Path(temporary).mkdir(exist_ok=True)
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT_DIRECTORY / "review_guard.py")],
            input=stdin.encode("utf-8") if isinstance(stdin, str) else stdin,
            capture_output=True,
            env={**os.environ, "TMPDIR": temporary, "TEMP": temporary, "TMP": temporary, **environment},
            check=False,
        )

    def test_other_tools_pass_and_unreadable_events_are_denied(self) -> None:
        self.assertIsNone(guard.decide({"tool_name": "SubagentHandback", "tool_input": {}}))
        self.assertIsNotNone(guard.decide({"tool_name": "Read", "tool_input": "not an object", "agent_id": AGENT}))
        self.assertIsNotNone(guard.decide({"tool_name": "Read", "tool_input": {"file_path": 3}, "agent_id": AGENT}))

    def test_the_hook_prints_a_deny_decision_and_fails_closed(self) -> None:
        event = {
            "tool_name": "Read",
            "tool_input": {"file_path": self.prompt()},
            "cwd": str(self.outside_cwd),
            "agent_id": AGENT,
        }
        self.assertEqual("", self.hook(json.dumps(event), PYTHONIOENCODING="utf-8").stdout.decode(), "allowed")
        self.assertTrue((self.root / "tmp" / "code-review-reviewer-claims" / f"{AGENT}.json").is_file())
        outside = {**event, "tool_input": {"file_path": str(self.checkout / "app" / "x.cs")}}
        decision = json.loads(self.hook(json.dumps(outside)).stdout)["hookSpecificOutput"]
        self.assertEqual(("PreToolUse", "deny"), (decision["hookEventName"], decision["permissionDecision"]))
        self.assertIn("may only look inside your own review run", decision["permissionDecisionReason"])
        self.assertEqual("deny", json.loads(self.hook("not json").stdout)["hookSpecificOutput"]["permissionDecision"])

    def test_a_decision_survives_a_console_that_cannot_encode_it(self) -> None:
        # A Windows pipe defaults to a legacy code page, and a deny reason names the path the reviewer asked for.
        outside = self.root / "repo → ✓" / "x.cs"
        event = {"tool_name": "Read", "tool_input": {"file_path": str(outside)}, "cwd": str(self.outside_cwd)}
        event["agent_id"] = AGENT
        result = self.hook(json.dumps(event).encode("utf-8"), PYTHONIOENCODING="cp1252")
        self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", "replace"))
        decision = json.loads(result.stdout.decode("utf-8"))["hookSpecificOutput"]
        self.assertEqual("deny", decision["permissionDecision"])
        self.assertIn(f"{outside}", decision["permissionDecisionReason"])


if __name__ == "__main__":
    unittest.main()
