"""Regression tests for the reviewer boundary hook."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import json_writer
import review_dispatch
import review_guard as guard
import review_pipeline as rp
import review_run
import review_runtime
from executable_locator import PYTHON
from review_source import source_commands
from test_review_io import UNPARSEABLE_JSON
from test_review_runtime_validation import directory_link

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
            ("Grep", {"pattern": "repeats", "path": str(guard.REFERENCES)}),
            ("Glob", {"pattern": "*.md", "path": str(guard.REFERENCES)}),
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
            ("Grep", {"pattern": "repeats", "path": str(guard.REFERENCES)}),
            ("Glob", {"pattern": "*.md", "path": str(guard.REFERENCES)}),
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
            ("Read", {"file_path": str(guard.SCRIPT_DIRECTORY / "review_guard.py")}),
            ("Grep", {"pattern": "anything", "path": str(guard.REFERENCES.parent)}),
            ("Glob", {"pattern": "*.md", "path": str(guard.REFERENCES.parent.parent / "review-prs")}),
            ("Read", {"file_path": str(guard.REFERENCES / ".." / "SKILL.md")}),
            ("Glob", {"pattern": "../scripts/*", "path": str(guard.REFERENCES)}),
        ]
        for tool, tool_input in denied:
            with self.subTest(tool=tool, input=tool_input):
                self.assert_denied(self.decide(tool, **tool_input))

    if sys.platform == "win32":

        def test_a_shell_style_drive_path_is_understood(self) -> None:
            self.claim()
            drive, rest = str(self.checkout).replace("\\", "/").split(":", 1)
            self.assertIsNotNone(self.decide("Read", file_path=f"/{drive.lower()}{rest}/app/Collections.cs"))

    else:

        def test_a_drive_letter_path_is_a_relative_one_and_denied(self) -> None:
            # Off Windows no shell spells a drive, so `C:/...` names a folder `C:` below the working directory.
            self.claim()
            source = self.run_directory / "source" / "app" / "Service.cs"
            self.assertIsNone(self.decide("Read", file_path=str(source)))
            self.assertIsNotNone(self.decide("Read", file_path=f"C:{source}"))

    def test_nested_run_like_source_folders_do_not_redefine_the_claimed_root(self) -> None:
        nested = self.run_directory / "source" / "code-review-run-authored"
        nested.mkdir()
        metadata = nested / "run.json"
        metadata.write_text("not reviewer metadata", encoding="utf-8")
        source = nested / "Service.cs"
        source.write_text("class Service {}\n", encoding="utf-8")
        self.claim()
        claim = (self.claims / f"{AGENT}.json").read_bytes()
        for tool, tool_input in (
            ("Read", {"file_path": str(metadata)}),
            ("Read", {"file_path": str(source)}),
            ("Read", {"file_path": str(nested / ".." / nested.name / source.name)}),
            ("Grep", {"pattern": "class", "path": str(source)}),
            ("Grep", {"pattern": "class", "path": str(nested)}),
            ("Grep", {"pattern": "class", "path": str(self.run_directory / "source")}),
            ("Glob", {"pattern": "**/*.cs", "path": str(nested)}),
            ("Glob", {"pattern": "**/*.cs", "path": str(self.run_directory / "source")}),
        ):
            with self.subTest(tool=tool, input=tool_input):
                self.assertIsNone(self.decide(tool, **tool_input))
        self.assertEqual(claim, (self.claims / f"{AGENT}.json").read_bytes())
        logged = [
            json.loads(line) for line in guard.read_log(self.run_directory, "csharp-review").read_text().splitlines()
        ]
        self.assertEqual(
            [
                *[
                    Path(os.path.normcase(f"{nested.name}/{name}")).as_posix()
                    for name in ("run.json", "Service.cs", "Service.cs", "Service.cs")
                ],
                *[guard.search_entry(tool) for tool in ("Grep", "Grep", "Glob", "Glob")],
            ],
            logged,
        )

    def test_a_run_name_prefix_or_a_junction_does_not_allow_an_outside_read(self) -> None:
        self.claim()
        outside = make_run(self.root / f"{self.run_directory.name}-outside", ["csharp-review"])
        (outside / "Service.cs").write_text("class Outside {}\n", encoding="utf-8")
        link = self.run_directory / "source" / "code-review-run-linked"
        directory_link(link, outside)
        for path in (outside, link, self.run_directory / "source" / ".." / ".." / outside.name):
            for tool, tool_input in (
                ("Read", {"file_path": str(path / "Service.cs")}),
                ("Grep", {"pattern": "class", "path": str(path)}),
                ("Glob", {"pattern": "*.cs", "path": str(path)}),
            ):
                with self.subTest(tool=tool, path=path):
                    self.assert_denied(self.decide(tool, **tool_input))


class LocalPathTests(GuardFixture):
    """A path is held to the temporary directory and the skill's folder by its text, before anything resolves it."""

    @contextlib.contextmanager
    def untouched(self, marker: str) -> Any:
        """Fail on any resolve or file test of a path holding `marker`: resolving a path to a host that does not
        answer blocks past the hook's timeout, and Claude Code lets a call through when its hook times out."""
        touched: list[str] = []
        resolve, is_file = Path.resolve, Path.is_file

        def spy(real: Any) -> Any:
            def call(path: Path, *args: Any, **kwargs: Any) -> Any:
                if marker in os.path.normcase(str(path)):
                    touched.append(str(path))
                return real(path, *args, **kwargs)

            return call

        with mock.patch.object(Path, "resolve", spy(resolve)), mock.patch.object(Path, "is_file", spy(is_file)):
            yield
        self.assertEqual([], touched, "a path the guard refuses by its text is never resolved or tested")

    def calls(self, path: str) -> list[tuple[str, dict[str, str]]]:
        """Every guarded call that names `path`, as a file, a folder, or a command's run."""
        return [
            ("Read", {"file_path": path}),
            ("Grep", {"pattern": "class", "path": path}),
            ("Glob", {"pattern": "*.cs", "path": path}),
            ("Write", {"file_path": path}),
            ("Edit", {"file_path": path}),
            ("Bash", {"command": review_run.self_check_command(Path(path), "csharp-review")}),
        ]

    def test_network_and_device_paths_are_denied_by_their_text(self) -> None:
        marker = "unreachable.invalid"
        paths = [f"//{marker}/share/code-review-run-abc/work/csharp-review.prompt.md"]
        if sys.platform == "win32":
            paths += [
                f"\\\\{marker}\\share\\code-review-run-abc\\work\\csharp-review.prompt.md",
                f"\\\\?\\UNC\\{marker}\\share\\x.cs",
                f"//?/UNC/{marker}/share/x.cs",
                f"\\\\.\\{marker}\\x.cs",
                f"\\\\?\\{self.run_directory / marker}",  # a device spelling of a path in the run
            ]
        with self.untouched(marker):
            for path in paths:
                with self.subTest(path=path, claimed=False):
                    self.assert_denied(self.decide("Read", file_path=path), "not a network or device path")
            self.assertFalse(self.claims.exists(), "a denied first read claims nothing")
            self.claim()
            for path in paths:
                for tool, tool_input in self.calls(path):
                    with self.subTest(path=path, tool=tool):
                        self.assert_denied(self.decide(tool, **tool_input), "not a network or device path")
            # A relative path names the session's working directory, which a hook event may give as a network one.
            event = {"tool_name": "Read", "tool_input": {"file_path": "x.cs"}, "agent_id": AGENT}
            reason = guard.decide({**event, "cwd": f"//{marker}/share"})
            self.assert_denied(reason, "not a network or device path")

    def test_a_path_outside_the_temporary_directory_and_the_skill_is_denied_unresolved(self) -> None:
        marker = "unreachable-mount"
        outside = Path(self.root.anchor) / marker
        paths = [str(outside / "code-review-run-abc" / "work" / "csharp-review.prompt.md"), str(outside / "x.cs")]
        if sys.platform == "win32":
            drive = next(f"{letter}:" for letter in "ZYXWVUTSRQ" if not self.root.anchor.upper().startswith(letter))
            paths += [f"{drive}\\{marker}\\x.cs", f"{drive}{marker}\\x.cs"]  # another drive, and drive-relative
        with self.untouched(marker):
            for path in paths:
                with self.subTest(path=path, claimed=False):
                    self.assert_denied(self.decide("Read", file_path=path), "is outside every folder it may use")
            self.claim()
            for path in paths:
                for tool, tool_input in self.calls(path):
                    with self.subTest(path=path, tool=tool):
                        self.assert_denied(self.decide(tool, **tool_input), "is outside every folder it may use")
            event = {"tool_name": "Read", "tool_input": {"file_path": "x.cs"}, "agent_id": AGENT}
            self.assert_denied(guard.decide({**event, "cwd": str(outside)}), "is outside every folder it may use")

    def test_local_paths_under_the_run_and_the_skill_are_resolved_and_allowed(self) -> None:
        roundabout = self.run_directory / "work" / ".." / "work" / "csharp-review.prompt.md"
        self.assertIsNone(self.decide("Read", file_path=str(roundabout)))
        self.assertIsNone(self.decide("Read", file_path=str(self.run_directory / "source" / "app" / "Service.cs")))
        self.assertIsNone(self.decide("Read", file_path=str(guard.REFERENCES / "generic-reviewer.md")))
        self.assertIsNone(self.decide("Grep", pattern="class", path=str(self.run_directory / "source")))
        self.assertIsNone(self.decide("Write", file_path=self.result()))
        self.assertIsNone(
            self.decide("Bash", command=review_run.self_check_command(self.run_directory, "csharp-review"))
        )
        event = {"tool_name": "Read", "tool_input": {"file_path": "app/Service.cs"}, "agent_id": AGENT}
        self.assertIsNone(guard.decide({**event, "cwd": str(self.run_directory / "source")}))
        # Inside the roots, the resolved path is still held to the claimed run.
        self.assert_denied(self.decide("Read", file_path=str(self.checkout / "app" / "x.cs")), "may only look inside")


class ReadLogTests(GuardFixture):
    """The guard logs each snapshot file a reviewer reads to its role's read log, which only `check` reads."""

    def setUp(self) -> None:
        super().setUp()
        self.source = self.run_directory / "source"
        (self.source / "app" / "Service.cs").write_text("class Service {}\n", encoding="utf-8")
        (self.source / "app" / "Role.cs").write_text("class Role {}\n", encoding="utf-8")

    def logged(self, role: str = "csharp-review") -> list[str]:
        return [json.loads(line) for line in guard.read_log(self.run_directory, role).read_text("utf-8").splitlines()]

    @staticmethod
    def entry(relative: str) -> str:
        """A path as the log names it: relative to the snapshot, in the case the file system compares."""
        return Path(os.path.normcase(relative)).as_posix()

    def test_check_skips_a_log_line_the_parser_refuses_and_counts_the_others(self) -> None:
        log = guard.read_log(self.run_directory, "csharp-review")
        lines = [json.dumps("app/Service.cs").encode(), *UNPARSEABLE_JSON.values(), json.dumps("app/Role.cs").encode()]
        log.write_bytes(b"\n".join(lines) + b"\n")
        state: Any = {"reads": {}}
        self.assertTrue(review_dispatch.count_reads(self.run_directory, state, ["csharp-review"]))
        size = sum((self.source / "app" / name).stat().st_size for name in ("Service.cs", "Role.cs"))
        self.assertEqual({"csharp-review": {"files": 2, "bytes": size, "searches": 0}}, state["reads"])
        self.assertFalse(log.exists(), "a counted log is removed")

    def test_a_claim_records_its_run_and_role_and_a_refused_one_leaves_no_claim(self) -> None:
        # A claim the JSON writer refuses fails the guard's evaluation, which the hook turns into a denial, and leaves
        # no empty claim file a later call would read as unreadable.
        refused = json_writer.JsonWriteRefused("the value holds a lone surrogate U+DCFF")
        with (
            mock.patch.object(json_writer, "dumps", side_effect=refused),
            self.assertRaises(json_writer.JsonWriteRefused),
        ):
            self.decide("Read", file_path=self.prompt("csharp-review"))
        self.assertFalse((self.claims / f"{AGENT}.json").exists())
        self.claim()
        claim = json.loads((self.claims / f"{AGENT}.json").read_text(encoding="utf-8"))
        self.assertEqual("csharp-review", claim["role"])
        self.assertEqual(guard.run_root(Path(self.prompt("csharp-review"))), Path(claim["run"]))

    def test_a_claim_starts_the_log_and_each_snapshot_file_read_is_logged(self) -> None:
        log = guard.read_log(self.run_directory, "csharp-review")
        self.assertEqual(self.run_directory / "work" / "reads-csharp-review.log", log)
        self.assertFalse(log.exists())
        self.claim()
        self.assertEqual([], self.logged(), "a claim starts an empty log, so a guarded reviewer is told from others")
        service = str(self.source / "app" / "Service.cs")
        for tool, tool_input in (
            ("Read", {"file_path": service}),
            ("Read", {"file_path": service, "offset": 10}),  # read again: check counts it once
            ("Read", {"file_path": str(self.source / "app" / ".." / "app" / "Service.cs")}),
            ("Grep", {"pattern": "class", "path": str(self.source / "app" / "Role.cs")}),  # a search of one file
            # Searches, each logged as one: of the snapshot, a folder in it, and the run folder holding it.
            ("Grep", {"pattern": "class", "path": str(self.source)}),
            ("Grep", {"pattern": "class", "path": str(self.source / "app")}),
            ("Glob", {"pattern": "**/*.cs", "path": str(self.source)}),
            ("Grep", {"pattern": "class", "path": str(self.run_directory)}),
            ("Glob", {"pattern": "**/*.cs", "path": str(self.run_directory)}),
            # Allowed, but neither a read of a snapshot file nor a search of the snapshot: a pattern under one file, a
            # missing file or folder, the work folder, and the work, reviewer, and reference files.
            ("Glob", {"pattern": "*", "path": service}),
            ("Read", {"file_path": str(self.source / "app")}),  # a Read of a folder reads no file and is no search
            ("Read", {"file_path": str(self.source / "app" / "Missing.cs")}),
            ("Grep", {"pattern": "class", "path": str(self.source / "missing")}),
            ("Grep", {"pattern": "class", "path": str(self.run_directory / "work")}),
            ("Glob", {"pattern": "*", "path": str(self.run_directory / "work")}),
            ("Read", {"file_path": str(self.run_directory / "work" / "csharp-review.diff")}),
            ("Grep", {"pattern": "class", "path": str(self.run_directory / "work" / "csharp-review.diff")}),
            ("Read", {"file_path": str(self.run_directory / "reviewer" / ".claude" / "agents" / "csharp-review.md")}),
            ("Read", {"file_path": str(guard.REFERENCES / "generic-reviewer.md")}),
            ("Grep", {"pattern": "class", "path": str(guard.REFERENCES)}),
        ):
            with self.subTest(tool=tool, input=tool_input):
                self.assertIsNone(self.decide(tool, **tool_input))
        # A denied read or search is not logged, and a reviewer cannot write its log.
        self.assert_denied(self.decide("Read", file_path=str(self.other_run / "source" / "app" / "Service.cs")))
        self.assert_denied(self.decide("Grep", pattern="class", path=str(self.other_run / "source")))
        self.assert_denied(self.decide("Write", file_path=str(log), content="[]"))
        self.assertEqual(
            [
                *[self.entry("app/Service.cs")] * 3,
                self.entry("app/Role.cs"),
                *[guard.search_entry(tool) for tool in ("Grep", "Grep", "Glob", "Grep", "Glob")],
            ],
            self.logged(),
        )
        state: Any = {"reads": {}}
        self.assertTrue(review_dispatch.count_reads(self.run_directory, state, ["csharp-review"]))
        size = sum((self.source / "app" / name).stat().st_size for name in ("Service.cs", "Role.cs"))
        self.assertEqual({"csharp-review": {"files": 2, "bytes": size, "searches": 5}}, state["reads"])

    def test_check_adds_a_rerun_s_searches_and_keeps_unknown_the_searches_of_a_role_counted_before_them(self) -> None:
        log = guard.read_log(self.run_directory, "csharp-review")
        entries = [guard.search_entry("Grep"), "app/Service.cs", guard.search_entry("source-search")]
        # Not a search: an object with another key, or another value, or more keys.
        others: list[object] = [{"search": 1}, {"searched": "Grep"}, {"search": "Grep", "path": "app/Role.cs"}, None]
        for counted in ({"files": 1, "bytes": 5, "searches": 2}, {"files": 1, "bytes": 5}):
            with self.subTest(counted=counted):
                log.write_text("".join(json.dumps(entry) + "\n" for entry in [*entries, *others]), encoding="utf-8")
                state: Any = {"reads": {"csharp-review": counted}}
                self.assertTrue(review_dispatch.count_reads(self.run_directory, state, ["csharp-review"]))
                size = (self.source / "app" / "Service.cs").stat().st_size
                expected = {"files": 2, "bytes": 5 + size, **({"searches": 4} if "searches" in counted else {})}
                self.assertEqual({"csharp-review": expected}, state["reads"])

    def test_each_role_has_its_own_log(self) -> None:
        self.claim()
        self.claim("sql-review", agent=OTHER_AGENT)
        self.assertIsNone(self.decide("Read", file_path=str(self.source / "app" / "Role.cs")))
        self.assertIsNone(self.decide("Read", agent=OTHER_AGENT, file_path=str(self.source / "app" / "Service.cs")))
        self.assertEqual([self.entry("app/Role.cs")], self.logged())
        self.assertEqual([self.entry("app/Service.cs")], self.logged("sql-review"))

    def test_a_log_that_cannot_be_written_never_denies_a_read(self) -> None:
        guard.read_log(self.run_directory, "csharp-review").mkdir(parents=True)  # a folder where the log should be
        self.claim()
        self.assertIsNone(self.decide("Read", file_path=str(self.source / "app" / "Service.cs")))

    def test_a_read_the_log_cannot_hold_is_allowed_uncounted_and_the_boundary_still_holds(self) -> None:
        # A path holding a lone surrogate, which a file name on Linux may, is a line no UTF-8 log can hold.
        self.claim()
        refused = json_writer.JsonWriteRefused("the value holds a lone surrogate U+DC80, which UTF-8 cannot encode")
        with mock.patch.object(json_writer, "dumps", side_effect=refused):
            self.assertIsNone(self.decide("Read", file_path=str(self.source / "app" / "Service.cs")))
            self.assert_denied(self.decide("Read", file_path=str(self.checkout / "app" / "Service.cs")))
        self.assertEqual([], self.logged())


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
    def test_smart_double_quotes_are_rejected_in_every_interpolated_value(self) -> None:
        self.claim()
        check = review_run.self_check_command(self.run_directory, "csharp-review")
        fetch, search = source_commands(self.run_directory, "csharp-review")
        for quote in ("\u201c", "\u201d", "\u201e"):
            for command, script in (
                (check, guard.PIPELINE),
                (fetch, guard.SOURCE_SCRIPT),
                (search, guard.SOURCE_SCRIPT),
            ):
                for bad in (
                    command.replace(f'"{script}"', f'"{script.parent / quote / ".." / script.name}"'),
                    command.replace(f'--run "{self.run_directory}"', f'--run "{self.run_directory / quote / ".."}"'),
                    command.replace("<path>", f"Service{quote}; Write-Output marker; #"),
                    command.replace("<pattern>", f"Service{quote}; Write-Output marker; #"),
                ):
                    if bad != command:
                        with self.subTest(quote=quote, command=bad):
                            self.assert_denied(self.decide("Bash", command=bad))

    def test_bash_runs_only_the_self_check_of_its_own_role_and_run_with_every_value_kept_in_quotes(self) -> None:
        command = review_run.self_check_command(self.run_directory, "csharp-review")
        self.assert_denied(self.decide("Bash", command=command), "read the prompt file your task names first")
        self.claim()
        self.assertIsNone(self.decide("Bash", command=command))
        script = str(Path(rp.__file__).resolve())
        for bad in (
            # The run or the script ending in a backslash, which would shift every quote after it.
            command.replace(f'--run "{self.run_directory}"', f'--run "{self.run_directory}\\"'),
            command.replace(f'"{script}"', f'"{script}\\"'),
            command.replace(f'"{script}"', f'"{script}\\"').replace(
                f'--run "{self.run_directory}"', f'--run "{self.run_directory}\\x & whoami & \\..\\"'
            ),
            "cat source/app/Service.cs",
            f"{command} && git log",
            f"{command}; rm -rf /",
            command.replace("validate-result", "finalize"),
            command.replace(script, str(self.root / "review_pipeline.py")),
            review_run.self_check_command(self.checkout, "csharp-review"),
            review_run.self_check_command(self.run_directory / "work", "csharp-review"),
            command.replace('"csharp-review"', '"$(whoami)"'),
            review_run.self_check_command(self.run_directory, "sql-review"),
            review_run.self_check_command(self.other_run, "csharp-review"),
        ):
            with self.subTest(command=bad):
                self.assert_denied(self.decide("Bash", command=bad))

    def test_a_role_that_declares_its_self_check_runs_that_command_alone(self) -> None:
        # The review-insights synthesis run: one role whose run.json entry names the one command it may run.
        run = self.root / "code-review-run-synthesis"
        run.mkdir()
        script, report = self.root / "review_insights.py", self.root / "insights.json"
        check = f'{PYTHON} -B "{script}" synthesize --report "{report}" --check'
        role = {
            "id": "synthesis",
            "prompt_file": str(run / "synthesis-prompt.md"),
            "result_file": str(run / "synthesis-result.json"),
            "self_check": check,
        }
        (run / "run.json").write_text(json.dumps({"roles": [role]}), encoding="utf-8")
        self.assertIsNone(self.decide("Read", agent=OTHER_AGENT, file_path=role["prompt_file"]))
        self.assertIsNone(self.decide("Bash", agent=OTHER_AGENT, command=f"  {check}  "))
        for bad in (
            f"{check}; whoami",
            check.replace(" --check", ""),
            review_run.self_check_command(run, "synthesis"),  # the reviewer forms are not the declared role's
            "git status",
        ):
            with self.subTest(command=bad):
                self.assert_denied(self.decide("Bash", agent=OTHER_AGENT, command=bad))
        # A reviewer role declares none, so it keeps its own self-check and never runs another role's command.
        self.claim()
        self.assertIsNone(
            self.decide("Bash", command=review_run.self_check_command(self.run_directory, "csharp-review"))
        )
        self.assert_denied(self.decide("Bash", command=check))

    def test_bash_runs_the_source_commands_of_its_own_role_and_run_with_a_path_or_pattern_kept_in_quotes(
        self,
    ) -> None:
        fetch, search = source_commands(self.run_directory, "csharp-review")
        file_command = fetch.replace("<path>", "app/Old Service.cs")
        search_command = search.replace("<pattern>", "class [A-Z]+Service;|GetAssignments( |[(])")
        self.assert_denied(self.decide("Bash", command=file_command), "read the prompt file your task names first")
        self.claim()
        for allowed in (file_command, search_command, fetch, search):
            with self.subTest(command=allowed):
                self.assertIsNone(self.decide("Bash", command=allowed))
        other_fetch, other_search = source_commands(self.other_run, "csharp-review")
        sibling_fetch, _ = source_commands(self.run_directory, "sql-review")
        script = str(Path(rp.__file__).resolve().parent / "review_source.py")
        for bad in (
            # A value that leaves its quotes, or runs something itself.
            fetch.replace("<path>", 'a.cs" && whoami && echo "'),
            fetch.replace("<path>", "$(whoami)"),
            fetch.replace("<path>", "`whoami`"),
            search.replace("<pattern>", "a\\"),  # escapes its closing quote
            search.replace("<pattern>", "a\\.b"),
            fetch.replace("<path>", "a\nb"),
            fetch.replace("<path>", ""),
            f"{file_command}; rm -rf /",
            f"{file_command} | sh",
            # The run or the script ending in a backslash, which would shift every quote after it.
            file_command.replace(f'--run "{self.run_directory}"', f'--run "{self.run_directory}\\"'),
            file_command.replace(f'"{script}"', f'"{script}\\"'),
            # The wrong option for the command, another script, another run or role.
            file_command.replace("source-file", "source-search"),
            search_command.replace("source-search", "source-file"),
            file_command.replace(script, str(self.root / "review_source.py")),
            file_command.replace(script, str(Path(rp.__file__).resolve())),
            other_fetch.replace("<path>", "app/Service.cs"),
            other_search.replace("<pattern>", "x"),
            sibling_fetch.replace("<path>", "app/Service.cs"),
            source_commands(self.checkout, "csharp-review")[0].replace("<path>", "a.cs"),
        ):
            with self.subTest(command=bad):
                self.assert_denied(self.decide("Bash", command=bad))


class ClaimTests(GuardFixture):
    def test_a_racing_first_read_keeps_the_first_written_run_and_role(self) -> None:
        for winner, allowed in ((self.run_directory, True), (self.other_run, False)):
            with self.subTest(winner=winner):
                claims = self.root / f"race-{winner.name}"
                winning_role = "sql-review" if allowed else "csharp-review"
                winning_claim = json.dumps({"run": str(winner), "role": winning_role})

                def racing_open(
                    *args: object,
                    destination: Path = claims / f"{AGENT}.json",
                    content: str = winning_claim,
                    **kwargs: object,
                ) -> int:
                    destination.write_text(content, encoding="utf-8")
                    raise FileExistsError

                with mock.patch.object(guard, "CLAIMS", claims), mock.patch.object(os, "open", racing_open):
                    reason = self.decide("Read", file_path=self.prompt())
                    if allowed:
                        self.assertIsNone(reason)
                    else:
                        self.assert_denied(reason)
                    self.assertEqual(winning_claim, (claims / f"{AGENT}.json").read_text(encoding="utf-8"))
                    self.assertIsNone(self.decide("Write", file_path=self.result(winning_role, winner)))
                    if allowed:
                        self.assert_denied(self.decide("Write", file_path=self.result()))

    def test_a_run_folder_inside_another_runs_tree_is_never_claimed(self) -> None:
        # A pull request's tree, copied into the snapshot, may hold a run folder whose run.json names a result file
        # of the author's choosing.
        chosen = self.root / "chosen" / "anywhere.txt"
        planted = [
            self.run_directory / "source" / "code-review-run-planted",
            self.run_directory / "source" / "app" / "code-review-run-planted",
            self.run_directory / "work" / "code-review-run-planted",
        ]
        for nested in planted:
            nested.mkdir(parents=True)
            role = {"id": "csharp-review", "prompt_file": str(nested / "p.md"), "result_file": str(chosen)}
            (nested / "run.json").write_text(json.dumps({"roles": [role]}), encoding="utf-8")
            with self.subTest(nested=nested):
                self.assert_denied(
                    self.decide("Read", file_path=str(nested / "p.md")),
                    f"{nested} sits inside the review run {self.run_directory}",
                )
                self.assertFalse(self.claims.exists(), "a denied first read claims nothing")
                self.assert_denied(self.decide("Write", file_path=str(chosen)), "read the prompt file")
        self.claim()
        self.assertEqual(
            {"run": str(self.run_directory), "role": "csharp-review"},
            json.loads((self.claims / f"{AGENT}.json").read_text(encoding="utf-8")),
        )
        self.assertIsNone(self.decide("Write", file_path=self.result()))
        for nested in planted:
            with self.subTest(nested=nested, claimed=True):
                self.assert_denied(self.decide("Write", file_path=str(chosen)), "write only your own result file")
                command = review_run.self_check_command(nested, "csharp-review")
                self.assert_denied(self.decide("Bash", command=command), "sits inside the review run")

    def test_a_run_is_found_only_under_the_temporary_directory(self) -> None:
        # The fixture's runs sit in a folder of the temporary directory; held to another one, they are no runs.
        elsewhere = guard._spellings(self.root / "elsewhere")
        with mock.patch.object(guard, "TEMPORARY_ROOTS", elsewhere):
            self.assert_denied(self.decide("Read", file_path=self.prompt()), "is not a reviewer prompt")
        self.assertFalse(self.claims.exists())
        self.claim()

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
                    ("Bash", {"command": review_run.self_check_command(self.run_directory, "csharp-review")}),
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

    def test_a_run_or_claim_the_parser_refuses_denies_as_unreadable_and_a_stale_one_is_left_in_place(self) -> None:
        run_file = self.run_directory / "run.json"
        run = run_file.read_bytes()
        stale = self.claims / f"{OTHER_AGENT}.json"
        for reason, content in UNPARSEABLE_JSON.items():
            with self.subTest(reason=reason):
                run_file.write_bytes(content)
                self.assertEqual(
                    f"Code-review reviewer boundary: the review run {self.run_directory} could not be read.",
                    self.decide("Read", file_path=self.prompt()),
                )
                run_file.write_bytes(run)
                self.claims.mkdir(exist_ok=True)
                (self.claims / f"{AGENT}.json").write_bytes(content)
                self.assertEqual(
                    "Code-review reviewer boundary: this reviewer's claim could not be read.",
                    self.decide("Write", file_path=self.result()),
                )
                (self.claims / f"{AGENT}.json").unlink()
                # A new claim prunes claims whose run is gone; one it cannot read is not known to be gone.
                stale.write_bytes(content)
                self.claim()
                self.assertEqual(content, stale.read_bytes())
                (self.claims / f"{AGENT}.json").unlink()

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


class FieldKindTests(GuardFixture):
    """A hook event, run, or claim field of the wrong kind denies the call with the guidance its own check gives."""

    def write_claim(self, claim: Any) -> None:
        self.claims.mkdir(exist_ok=True)
        (self.claims / f"{AGENT}.json").write_text(json.dumps(claim), encoding="utf-8")

    def test_hook_event_fields_of_the_wrong_kind_deny_with_their_guidance(self) -> None:
        tool_input = "Code-review reviewer boundary: the tool input could not be read."
        agent = (
            "Code-review reviewer boundary: the hook event carries no agent ID, so the guard cannot tell which "
            "reviewer is calling."
        )
        path = "Code-review reviewer boundary: give an absolute path inside the review run."
        base: dict[str, Any] = {"tool_name": "Read", "cwd": str(self.outside_cwd), "agent_id": AGENT}
        cases: list[tuple[dict[str, Any], str]] = [
            ({**base, "tool_input": "file_path"}, tool_input),
            ({**base, "tool_input": [self.prompt()]}, tool_input),
            ({**base, "tool_input": None}, tool_input),
            ({**base, "tool_input": {"file_path": self.prompt()}, "agent_id": 7}, agent),
            ({**base, "tool_input": {"file_path": self.prompt()}, "agent_id": [AGENT]}, agent),
            ({**base, "tool_input": {"file_path": self.prompt()}, "agent_id": {"id": AGENT}}, agent),
            ({**base, "tool_input": {"file_path": self.prompt()}, "agent_id": True}, agent),
            ({**base, "tool_input": {"file_path": 3}}, path),
            ({**base, "tool_input": {"file_path": None}}, path),
            ({**base, "tool_input": {"file_path": [self.prompt()]}}, path),
            ({**base, "tool_input": {"file_path": ""}}, path),
        ]
        for event, reason in cases:
            with self.subTest(event=event):
                self.assertEqual(reason, guard.decide(event))
        self.assertFalse(self.claims.exists(), "a denied call claims nothing")

    def test_tool_input_fields_of_the_wrong_kind_deny_a_claimed_reviewer(self) -> None:
        self.claim()
        glob = "Code-review reviewer boundary: Glob patterns must be relative to the run folder path, without '..'."
        command = (
            "Code-review reviewer boundary: run no command except the self-check and source commands the prompt "
            "gives, exactly as written."
        )
        path = "Code-review reviewer boundary: give an absolute path inside the review run."
        source = str(self.run_directory / "source")
        cases: list[tuple[str, dict[str, Any], str]] = [
            ("Glob", {"pattern": 5, "path": source}, glob),
            ("Glob", {"pattern": ["*.cs"], "path": source}, glob),
            ("Grep", {"pattern": "x", "path": 5}, path),
            ("Bash", {"command": 5}, command),
            ("Bash", {"command": None}, command),
            ("Bash", {"command": [review_run.self_check_command(self.run_directory, "csharp-review")]}, command),
            ("Write", {"file_path": {"path": self.result()}}, path),
        ]
        for tool, tool_input, reason in cases:
            with self.subTest(tool=tool, input=tool_input):
                self.assertEqual(reason, self.decide(tool, **tool_input))

    def test_run_fields_of_the_wrong_kind_deny_with_the_unreadable_run(self) -> None:
        reason = f"Code-review reviewer boundary: the review run {self.run_directory} could not be read."
        role = {
            "id": "csharp-review",
            "prompt_file": self.prompt(),
            "result_file": self.result(),
        }
        runs: list[Any] = [
            [],
            {},
            {"roles": {}},
            {"roles": "csharp-review"},
            {"roles": ["csharp-review"]},
            {"roles": [{**role, "id": 5}]},
            {"roles": [{**role, "prompt_file": None}]},
            {"roles": [{**role, "prompt_file": ""}]},
            {"roles": [{key: value for key, value in role.items() if key != "result_file"}]},
            {"roles": [{**role, "self_check": None}]},
            {"roles": [{**role, "self_check": ""}]},
        ]
        for run in runs:
            with self.subTest(run=run):
                (self.run_directory / "run.json").write_text(json.dumps(run), encoding="utf-8")
                self.assertEqual(reason, self.decide("Read", file_path=self.prompt()))
        self.assertFalse(self.claims.exists(), "a denied first read claims nothing")

    def test_claim_fields_of_the_wrong_kind_deny_with_the_unreadable_claim(self) -> None:
        reason = "Code-review reviewer boundary: this reviewer's claim could not be read."
        run = str(self.run_directory)
        claims: list[Any] = [
            [],
            "claim",
            {"run": 5, "role": "csharp-review"},
            {"run": [run], "role": "csharp-review"},
            {"run": run, "role": None},
            {"run": run},
        ]
        for claim in claims:
            with self.subTest(claim=claim):
                self.write_claim(claim)
                self.assertEqual(reason, self.decide("Write", file_path=self.result()))
                self.assertEqual(reason, self.decide("Read", file_path=self.prompt()))

    def test_a_tool_name_of_the_wrong_kind_denies_through_the_fail_closed_path(self) -> None:
        reason = "Code-review reviewer boundary: the guard could not evaluate this call (DeniedField)."
        for tool, found in ((7, "integer 7"), (["Read"], "an array"), (None, "null"), ({}, "an object")):
            event = {"tool_name": tool, "tool_input": {"file_path": self.prompt()}, "agent_id": AGENT}
            with self.subTest(tool=tool):
                with self.assertRaises(guard.Denied) as caught:
                    guard.decide(event)
                self.assertIs(guard.DeniedField, type(caught.exception))
                self.assertEqual(f"hook event.tool_name must be a string, found {found}", str(caught.exception))
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    self.assertEqual(0, guard.main(json.dumps(event)))
                decision = json.loads(stdout.getvalue())["hookSpecificOutput"]
                self.assertEqual(
                    ("deny", reason), (decision["permissionDecision"], decision["permissionDecisionReason"])
                )
        self.assertFalse(self.claims.exists())


class PluginScopeTests(GuardFixture):
    """As a plugin's hook, the guard sees every session's calls and holds only the plugin's reviewer."""

    def plugin_decide(self, tool: str, agent: str | None, agent_type: str | None, **tool_input: object) -> str | None:
        event: dict[str, Any] = {"tool_name": tool, "tool_input": tool_input, "cwd": str(self.outside_cwd)}
        if agent is not None:
            event["agent_id"] = agent
        if agent_type is not None:
            event["agent_type"] = agent_type
        return guard.decide(event, "a-suite")

    def test_the_checkout_runs_the_deployed_agents_hook_and_names_the_bare_agent(self) -> None:
        self.assertIsNone(guard.PLUGIN)
        self.assertEqual("code-review-reviewer", guard.reviewer_agent_type(None))
        self.assertEqual("a-suite:code-review-reviewer", guard.reviewer_agent_type("a-suite"))

    def test_the_main_session_and_other_agents_pass_wherever_they_read(self) -> None:
        outside = str(self.checkout / "app" / "x.cs")
        for agent, agent_type in ((None, None), ("", None), (AGENT, "Explore"), (AGENT, "code-review-reviewer")):
            with self.subTest(agent=agent, agent_type=agent_type):
                self.assertIsNone(self.plugin_decide("Read", agent, agent_type, file_path=outside))
                self.assertIsNone(self.plugin_decide("Bash", agent, agent_type, command="git status"))
        self.assertFalse(self.claims.exists())

    def test_a_subagent_call_without_its_type_is_denied(self) -> None:
        for agent_type in (None, "", 3):
            with self.subTest(agent_type=agent_type):
                event: dict[str, Any] = {"tool_name": "Read", "tool_input": {"file_path": self.prompt()}}
                event["agent_id"] = AGENT
                if agent_type is not None:
                    event["agent_type"] = agent_type
                self.assert_denied(guard.decide(event, "a-suite"), "names a subagent but not its type")

    def test_the_plugins_reviewer_is_held_as_the_deployed_one_is(self) -> None:
        reviewer = "a-suite:code-review-reviewer"
        outside = str(self.checkout / "app" / "x.cs")
        self.assert_denied(self.plugin_decide("Bash", AGENT, reviewer, command="git status"), "read the prompt file")
        self.assertIsNone(self.plugin_decide("Read", AGENT, reviewer, file_path=self.prompt()))
        self.assert_denied(self.plugin_decide("Read", AGENT, reviewer, file_path=outside), "may only look inside")
        self.assertIsNone(self.plugin_decide("Write", AGENT, reviewer, file_path=self.result()))


class HookTests(GuardFixture):
    def hook(self, stdin: str | bytes, **environment: str) -> subprocess.CompletedProcess[Any]:
        # The hook process finds its runs and claims under the system temporary directory, here this test's own.
        temporary = str(self.root)
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
        self.assertTrue((self.root / "code-review-reviewer-claims" / f"{AGENT}.json").is_file())
        outside = {**event, "tool_input": {"file_path": str(self.checkout / "app" / "x.cs")}}
        decision = json.loads(self.hook(json.dumps(outside)).stdout)["hookSpecificOutput"]
        self.assertEqual(("PreToolUse", "deny"), (decision["hookEventName"], decision["permissionDecision"]))
        self.assertIn("may only look inside your own review run", decision["permissionDecisionReason"])
        self.assertEqual("deny", json.loads(self.hook("not json").stdout)["hookSpecificOutput"]["permissionDecision"])

    def test_a_stream_the_console_cannot_decode_is_denied_not_left_to_the_runner(self) -> None:
        # The runner's FAILED line and exit 1 would read to Claude Code as a hook error, which lets the call through.
        # 0x81 is undefined in code page 1252.
        result = self.hook(b'{"tool_name": "Read", "x": "\x81"}', PYTHONIOENCODING="cp1252")
        self.assertEqual((0, b""), (result.returncode, result.stderr))
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(
            ("deny", "Code-review reviewer boundary: the guard could not evaluate this call (JSONDecodeError)."),
            (decision["permissionDecision"], decision["permissionDecisionReason"]),
        )

    def test_ctrl_c_while_the_guard_decides_denies_the_call(self) -> None:
        event = {"tool_name": "Read", "tool_input": {"file_path": self.prompt()}, "agent_id": AGENT}
        stdout = io.StringIO()
        with mock.patch.object(guard, "decide", side_effect=KeyboardInterrupt), contextlib.redirect_stdout(stdout):
            self.assertEqual(0, guard.main(json.dumps(event)))
        decision = json.loads(stdout.getvalue())["hookSpecificOutput"]
        self.assertEqual(
            ("deny", "Code-review reviewer boundary: the guard could not evaluate this call (KeyboardInterrupt)."),
            (decision["permissionDecision"], decision["permissionDecisionReason"]),
        )

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


class EvidenceAbsentTests(GuardFixture):
    """Each call whose evidence never existed, no hook event, no tool input, or no run.json beside the prompt, is
    denied, never allowed for want of a reason to deny it."""

    def test_an_empty_hook_event_is_denied(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(0, guard.main(""))
        self.assertEqual("deny", json.loads(stdout.getvalue())["hookSpecificOutput"]["permissionDecision"])

    def test_a_hook_event_that_repeats_a_key_is_denied(self) -> None:
        # Which tool_input the call carries is ambiguous, so neither is judged as the one it runs.
        self.claim()
        inside = json.dumps({"file_path": self.prompt()})
        stream = f'{{"tool_name": "Read", "tool_input": {inside}, "tool_input": {inside}, "agent_id": "{AGENT}"}}'
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(0, guard.main(stream))
        decision = json.loads(stdout.getvalue())["hookSpecificOutput"]
        self.assertEqual(
            ("deny", "Code-review reviewer boundary: the guard could not evaluate this call (JsonRefused)."),
            (decision["permissionDecision"], decision["permissionDecisionReason"]),
        )

    def test_a_hook_event_with_no_tool_input_is_denied(self) -> None:
        self.claim()
        for tool in ("Read", "Write", "Bash"):
            with self.subTest(tool=tool):
                self.assertEqual(
                    "Code-review reviewer boundary: the tool input could not be read.",
                    guard.decide({"tool_name": tool, "agent_id": AGENT, "cwd": str(self.outside_cwd)}),
                )

    def test_a_prompt_in_a_run_with_no_run_json_claims_nothing(self) -> None:
        (self.run_directory / "run.json").unlink()
        self.assert_denied(self.decide("Read", file_path=self.prompt()), "is not a reviewer prompt")
        self.assertFalse(self.claims.exists(), "a denied read claims nothing")
        self.assert_denied(self.decide("Write", file_path=self.result()), "read the prompt file your task names first")


def link_folder(target: Path, link: Path) -> None:
    """A real link to a folder: a junction on Windows, which needs no privilege, else a symbolic link."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_a_read_in_a_run_reached_through_a_link_is_logged_by_its_place_in_the_snapshot(tmp_path: Path) -> None:
    real = tmp_path / "real run"
    read = real / guard.SOURCE / "src" / "a.py"
    read.parent.mkdir(parents=True)
    read.write_text("x\n", encoding="utf-8")
    (real / "work").mkdir()
    link = tmp_path / "linked run"
    link_folder(real, link)
    guard._log_read("Read", read.resolve(), (link, {"id": "csharp-review"}))
    assert guard.read_log(link, "csharp-review").read_text(encoding="utf-8").splitlines() == ['"src/a.py"']


# Windows drops a name's trailing dots and spaces when it makes a path absolute; the guard and the read count judge a
# path by its exact spelling, so these hold the textual key where the platform alone can tell it from the lexical one.


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows drops a name's trailing dots")
def test_a_trusted_folder_keeps_its_spelling_where_windows_would_drop_a_trailing_dot(tmp_path: Path) -> None:
    given = guard._spellings(tmp_path / "trusted.")[0]
    assert str(given) == os.path.normcase(str(tmp_path / "trusted."))


def counted_reads(run: Path, lines: list[str]) -> dict[str, int]:
    """The read counts `check` gives a role whose log names `lines`, relative to the run's snapshot."""
    log = guard.read_log(run, "csharp-review")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    state: Any = {"reads": {}}
    review_dispatch.count_reads(run, state, ["csharp-review"])
    return dict(state["reads"]["csharp-review"])


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows drops a name's trailing dots")
def test_a_read_is_counted_by_the_spelling_the_log_gives_it(tmp_path: Path) -> None:
    read = tmp_path / "run" / "source" / "app" / "a.py"
    read.parent.mkdir(parents=True)
    read.write_text("x\n", encoding="utf-8")
    counts = counted_reads(tmp_path / "run", ["app/a.py", "app/a.py."])
    assert counts == {"files": 2, "bytes": 2 * read.stat().st_size, "searches": 0}


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows drops a name's trailing dots")
def test_the_manifest_is_left_out_of_a_run_whose_folder_ends_in_a_dot(tmp_path: Path) -> None:
    # Windows makes `run.` as `run`; the run is still named `run.`, and its manifest is judged by that spelling.
    run = tmp_path / "run."
    manifest = run / "source" / review_runtime.SOURCE_SNAPSHOT_MANIFEST
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}\n", encoding="utf-8")
    assert counted_reads(run, [review_runtime.SOURCE_SNAPSHOT_MANIFEST]) == {"files": 0, "bytes": 0, "searches": 0}
