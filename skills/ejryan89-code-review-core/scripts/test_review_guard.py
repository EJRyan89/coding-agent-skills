"""Regression tests for the reviewer boundary hook."""

from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, override
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import atomic_file
import file_lock
import json_writer
import path_containment
import path_keys
import private_folder
import review_dispatch
import review_guard as guard
import review_pipeline as rp
import review_run
import review_runtime
from executable_locator import PYTHON
from review_source import filled, source_commands
from test_path_keys import case_sensitive_folder, hard_link_pair
from test_review_archive import unstatable
from test_review_io import UNPARSEABLE_JSON
from test_review_runtime_validation import directory_link

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
REAL_LINK = os.link
REAL_UNLINK = Path.unlink
REAL_LSTAT = Path.lstat
AGENT = "a71dab35ebc1b97eb"  # the form Claude Code gives an agent ID
OTHER_AGENT = "a73176509870d6114"
OTHER_USER = 4242  # another local account's user id
CLAIMS_NAME = "code-review-reviewer-claims"
UNREADABLE_CLAIM = (
    "Code-review reviewer boundary: this reviewer's claim could not be read; run the role again with a fresh "
    "reviewer, which claims it anew."
)
# The age, in seconds, past which a claim that does not parse is pruned: the guard's UNREADABLE_CLAIM_AGE.
UNREADABLE_CLAIM_AGE = 600
# The age, in seconds, past which a copy a killed create left beside the claims is pruned: LEFTOVER_COPY_AGE.
LEFTOVER_COPY_AGE = 600
THIRD_AGENT = "a000000000000000f"


def age_by(path: Path, seconds: float) -> None:
    """Move `path`'s modification time `seconds` further into the past."""
    status = path.stat()
    os.utime(path, (status.st_atime - seconds, status.st_mtime - seconds))


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
    @override
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

    def assert_no_claim(self, message: str = "a denied call claims nothing") -> None:
        """No claim file lies in the claims folder, which a call the guard got as far as judging made, empty and owner
        only, and one it denied before that left unmade."""
        self.assertEqual([], sorted(self.claims.glob("*")) if self.claims.is_dir() else [], message)

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
        self.assert_no_claim("a denied read claims nothing")
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
            json.loads(line)
            for line in guard.read_log(self.run_directory, "csharp-review").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [
                *[f"{nested.name}/{name}" for name in ("run.json", "Service.cs", "Service.cs", "Service.cs")],
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


class WithheldInputTests(GuardFixture):
    """A role the run withholds a pull-request text from reads only the paths its `reads` names, so the description,
    the comments, and the request and other roles' prompts that carry them stay out of its reach, however spelled."""

    @override
    def setUp(self) -> None:
        super().setUp()
        run, work = self.run_directory, self.run_directory / "work"
        self.reads = [
            str(work / "sql-review.prompt.md"),
            str(work / "sql-review.diff"),
            str(work / "analyzers.json"),
            str(run / "source"),
            str(run / "reviewer"),
            str(run / "closing-issues.json"),
        ]
        state = json.loads((run / "run.json").read_text(encoding="utf-8"))
        state["roles"][1]["reads"] = self.reads
        # Only the withheld role is given the closing issues.
        state["restricted"] = {str(run / "closing-issues.json"): ["sql-review"]}
        (run / "run.json").write_text(json.dumps(state), encoding="utf-8")
        for name in ("pull-request-body.md", "request.json", "closing-issues.json"):
            (run / name).write_text("{}", encoding="utf-8")
        for name in ("github-comments.json", "csharp-review.prompt.md", "sql-review.diff", "analyzers.json"):
            (work / name).write_text("{}", encoding="utf-8")

    def test_a_withheld_role_reads_only_the_paths_its_run_names(self) -> None:
        run, work = self.run_directory, self.run_directory / "work"
        self.claim("sql-review")
        for tool, tool_input in (
            ("Read", {"file_path": str(work / "sql-review.diff")}),
            ("Read", {"file_path": str(work / "analyzers.json")}),
            ("Read", {"file_path": str(run / "closing-issues.json")}),
            ("Read", {"file_path": str(run / "source" / "app" / "Service.cs")}),
            ("Read", {"file_path": str(run / "reviewer" / ".claude" / "agents" / "sql-review.md")}),
            ("Read", {"file_path": str(guard.REFERENCES / "generic-reviewer.md")}),
            ("Grep", {"pattern": "class", "path": str(run / "source")}),
            ("Glob", {"pattern": "**/*.cs", "path": str(run / "source")}),
        ):
            with self.subTest(tool=tool, input=tool_input):
                self.assertIsNone(self.decide(tool, **tool_input))
        for tool, tool_input in (
            ("Read", {"file_path": str(run / "pull-request-body.md")}),
            ("Read", {"file_path": str(run / "request.json")}),
            ("Read", {"file_path": str(run / "run.json")}),
            ("Read", {"file_path": str(work / "github-comments.json")}),
            ("Read", {"file_path": str(work / "csharp-review.prompt.md")}),
            ("Read", {"file_path": str(run / "source" / ".." / "pull-request-body.md")}),
            ("Read", {"file_path": str(work / "sql-review.diff" / ".." / ".." / "request.json")}),
            ("Read", {"file_path": str(run / "PULL-REQUEST-BODY.MD")}),
            ("Grep", {"pattern": "Closes", "path": str(run)}),
            ("Grep", {"pattern": "Closes", "path": str(work)}),
            ("Glob", {"pattern": "*.md", "path": str(run)}),
        ):
            with self.subTest(tool=tool, input=tool_input):
                self.assert_denied(self.decide(tool, **tool_input), "the inputs your prompt names")
        # A pattern that climbs out of an allowed folder is refused before its folder is judged, as for any role.
        self.assert_denied(self.decide("Glob", pattern="../*.md", path=str(run / "source")), "without '..'")

    def test_a_role_with_no_reads_keeps_the_whole_run(self) -> None:
        # Only a withheld role is held to its reads; every other role still reads anywhere in its own run.
        self.claim("csharp-review")
        for path in (self.run_directory / "pull-request-body.md", self.run_directory / "work" / "github-comments.json"):
            with self.subTest(path=path):
                self.assertIsNone(self.decide("Read", file_path=str(path)))

    def test_a_named_path_outside_the_run_is_still_outside_it(self) -> None:
        # A path in `reads` widens nothing past the run: the run is the outer limit whatever the record names.
        state = json.loads((self.run_directory / "run.json").read_text(encoding="utf-8"))
        state["roles"][1]["reads"] = [*self.reads, str(self.checkout)]
        (self.run_directory / "run.json").write_text(json.dumps(state), encoding="utf-8")
        self.claim("sql-review")
        self.assert_denied(self.decide("Read", file_path=str(self.checkout / "app" / "Collections.cs")))

    def test_a_read_that_loses_the_claim_race_is_held_to_the_claimed_role_s_reads(self) -> None:
        # Two first reads race: the other one wrote the claim between this one's look and its own write, so this read,
        # of another role's prompt and its review comments, is judged against the claimed role, its reads included.
        self.claims.mkdir(exist_ok=True)
        claim = {"run": str(self.run_directory), "role": "sql-review"}
        (self.claims / f"{AGENT}.json").write_text(json.dumps(claim), encoding="utf-8")
        with self.assertRaises(guard.Denied) as caught:
            guard._claim(AGENT, Path(self.prompt("csharp-review")))
        self.assertIn("the inputs your prompt names", str(caught.exception))
        guard._claim(AGENT, Path(self.prompt("sql-review")))  # its own prompt, which its reads name

    def test_the_closing_issues_reach_only_the_roles_the_run_gives_them(self) -> None:
        # A role with no reads keeps its whole run but the closing issues, which only the roles given them read, and a
        # search of a folder holding the file is refused as a read of the file is.
        run = self.run_directory
        self.claim("csharp-review")
        for tool, tool_input in (
            ("Read", {"file_path": str(run / "closing-issues.json")}),
            ("Read", {"file_path": str(run / "source" / ".." / "closing-issues.json")}),
            ("Grep", {"pattern": "Totals", "path": str(run)}),
            ("Glob", {"pattern": "*.json", "path": str(run)}),
        ):
            with self.subTest(tool=tool, input=tool_input):
                self.assert_denied(self.decide(tool, **tool_input), "holds one it does not")
        self.assertIsNone(self.decide("Read", file_path=str(run / "pull-request-body.md")))
        self.assertIsNone(self.decide("Grep", pattern="class", path=str(run / "source")))

    def test_a_run_the_parser_refuses_denies_its_restricted_paths_as_unreadable(self) -> None:
        # The claim's own read of run.json refuses such a run first, so the restriction's reader is held here alone:
        # json_reader refuses nesting past the recursion limit as a ValueError, which it turns into its denial.
        for reason, content in UNPARSEABLE_JSON.items():
            with self.subTest(reason=reason):
                (self.run_directory / "run.json").write_bytes(content)
                with self.assertRaises(guard.Denied) as caught:
                    guard._restricted(self.run_directory)
                # The denial names what the JSON reader refused.
                self.assertRegex(
                    str(caught.exception),
                    rf"^the review run {re.escape(str(self.run_directory))} could not be read: \S",
                )

    def test_a_run_json_whose_name_is_absent_for_a_moment_is_read_after_short_waits(self) -> None:
        # Holds on every platform, by injected refusals: `check` replaces run.json while the guard reads it, and on
        # Windows the replace may hide the name for a moment, in bursts under load, so each of the guard's reads of
        # run.json tries an absent name again after atomic_file.while_refused's waits rather than denying the call.
        for name, read in (("roles", guard._roles), ("restricted", guard._restricted)):
            with self.subTest(read=name):
                value, waits, absent = self.read_with_run_json_absent(read, 2)
                self.assertEqual(([0.01, 0.02], 2), (waits, absent))
                self.assertEqual(read(self.run_directory), value)

    def read_with_run_json_absent(self, read: Any, times: int) -> tuple[Any, list[float], int]:
        """What `read` gives for the run while run.json's read raises FileNotFoundError `times` times, the waits, and
        the absent reads."""
        real_read_bytes = Path.read_bytes
        run_file = self.run_directory / "run.json"
        absent: list[Path] = []
        waits: list[float] = []

        def read_bytes(path: Path) -> bytes:
            if path == run_file and len(absent) < times:
                absent.append(path)
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
            return real_read_bytes(path)

        with mock.patch.object(Path, "read_bytes", read_bytes), mock.patch.object(time, "sleep", waits.append):
            value = read(self.run_directory)
        return value, waits, len(absent)

    def test_a_malformed_restriction_denies_every_read(self) -> None:
        self.claim("csharp-review")
        malformed: list[Any] = [[], {"x": "csharp-review"}, {"x": [5]}]
        for restricted in malformed:
            state = json.loads((self.run_directory / "run.json").read_text(encoding="utf-8"))
            state["restricted"] = restricted
            (self.run_directory / "run.json").write_text(json.dumps(state), encoding="utf-8")
            with self.subTest(restricted=restricted):
                self.assert_denied(self.decide("Read", file_path=str(self.run_directory / "pull-request-body.md")))


class LocalPathTests(GuardFixture):
    """A path is held to the temporary directory and the skill's folder by its text, before anything resolves it."""

    @contextlib.contextmanager
    def untouched(self, marker: str) -> Any:
        """Fail on any resolve or file test of a path holding `marker`: resolving a path to a host that does not
        answer blocks past the hook's timeout, and Claude Code lets a call through when its hook times out."""
        touched: list[str] = []
        resolve, is_file = path_containment.resolved_path, Path.is_file

        def spy(real: Any) -> Any:
            def call(path: Path, *args: Any, **kwargs: Any) -> Any:
                if marker in os.path.normcase(str(path)):
                    touched.append(str(path))
                return real(path, *args, **kwargs)

            return call

        with mock.patch("review_guard.resolved_path", spy(resolve)), mock.patch.object(Path, "is_file", spy(is_file)):
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
            self.assert_no_claim("a denied first read claims nothing")
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

    @override
    def setUp(self) -> None:
        super().setUp()
        self.source = self.run_directory / "source"
        (self.source / "app" / "Service.cs").write_text("class Service {}\n", encoding="utf-8")
        (self.source / "app" / "Role.cs").write_text("class Role {}\n", encoding="utf-8")

    def logged(self, role: str = "csharp-review") -> list[str]:
        return [json.loads(line) for line in guard.read_log(self.run_directory, role).read_text("utf-8").splitlines()]

    @staticmethod
    def entry(relative: str) -> str:
        """A path as the log names it: relative to the snapshot, as the reviewer spelled it; review_dispatch keys it
        as the snapshot's file system compares names."""
        return Path(relative).as_posix()

    def test_check_skips_a_log_line_the_parser_refuses_and_counts_the_others(self) -> None:
        log = guard.read_log(self.run_directory, "csharp-review")
        lines = [json.dumps("app/Service.cs").encode(), *UNPARSEABLE_JSON.values(), json.dumps("app/Role.cs").encode()]
        log.write_bytes(b"\n".join(lines) + b"\n")
        state: Any = {"reads": {}}
        self.assertTrue(review_dispatch.count_reads(self.run_directory, state, ["csharp-review"]))
        size = sum((self.source / "app" / name).stat().st_size for name in ("Service.cs", "Role.cs"))
        self.assertEqual({"csharp-review": {"files": 2, "bytes": size, "searches": 0}}, state["reads"])
        self.assertFalse(log.exists(), "a counted log is removed")

    def test_check_refuses_a_log_line_that_is_json_of_another_shape_by_its_line_and_counts_no_log(self) -> None:
        # A well-formed line the guard never writes is not a write it was stopped in: counting past it would judge the
        # log whole when something else wrote to it.
        counted = guard.read_log(self.run_directory, "csharp-review")
        counted.write_text(json.dumps("app/Role.cs") + "\n", encoding="utf-8")
        log = guard.read_log(self.run_directory, "sql-review")
        # Not a search either: an object with another key, or another value, or more keys.
        shapes = ("[]", "1", "null", "true", '{"searched": "Grep"}', '{"search": 1}', '{"search": "Grep", "path": "a"}')
        for shape in shapes:
            with self.subTest(shape):
                log.write_text(f'"app/Service.cs"\n{{"search": "Grep"}}\n{shape}\n"app/Role.cs"\n', encoding="utf-8")
                state: Any = {"reads": {}}
                with self.assertRaises(review_run.PipelineError) as raised:
                    review_dispatch.count_reads(self.run_directory, state, ["csharp-review", "sql-review"])
                self.assertEqual(
                    f"{log} line 3 is JSON but neither a snapshot path nor a search, the only entries the reviewer "
                    "guard writes, so its reads cannot be counted",
                    str(raised.exception),
                )
                self.assertEqual({}, state["reads"], "no log is counted")
                self.assertTrue(log.exists() and counted.exists(), "no log is removed")

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
        for counted in ({"files": 1, "bytes": 5, "searches": 2}, {"files": 1, "bytes": 5}):
            with self.subTest(counted=counted):
                log.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
                state: Any = {"reads": {"csharp-review": counted}}
                self.assertTrue(review_dispatch.count_reads(self.run_directory, state, ["csharp-review"]))
                size = (self.source / "app" / "Service.cs").stat().st_size
                expected = {"files": 2, "bytes": 5 + size, **({"searches": 4} if "searches" in counted else {})}
                self.assertEqual({"csharp-review": expected}, state["reads"])

    def test_a_read_file_it_cannot_stat_fails_the_count_and_keeps_every_log_for_the_rerun(self) -> None:
        logs = [guard.read_log(self.run_directory, role) for role in ("csharp-review", "sql-review")]
        logs[0].write_text(json.dumps("app/Role.cs") + "\n", encoding="utf-8")
        logs[1].write_text("".join(json.dumps(entry) + "\n" for entry in ("app/Service.cs", "app")), encoding="utf-8")
        state: Any = {"reads": {}}
        with unstatable(self.source / "app" / "Service.cs"), self.assertRaises(PermissionError) as raised:
            review_dispatch.count_reads(self.run_directory, state, ["csharp-review", "sql-review"])
        self.assertIn("Service.cs", str(raised.exception))
        self.assertEqual({"reads": {}}, state, "no role is counted short of a file its reviewer read")
        self.assertTrue(all(log.exists() for log in logs), "every log stays for the rerun")
        self.assertTrue(review_dispatch.count_reads(self.run_directory, state, ["csharp-review", "sql-review"]))
        sizes = {name: (self.source / "app" / name).stat().st_size for name in ("Role.cs", "Service.cs")}
        self.assertEqual(
            {
                "csharp-review": {"files": 1, "bytes": sizes["Role.cs"], "searches": 0},
                "sql-review": {"files": 1, "bytes": sizes["Service.cs"], "searches": 0},
            },
            state["reads"],
            "the folder a log names is no file read",
        )

    def test_a_logged_path_holding_a_nul_counts_for_nothing_and_the_others_are_counted(self) -> None:
        log = guard.read_log(self.run_directory, "csharp-review")
        log.write_text(
            "".join(json.dumps(entry) + "\n" for entry in ("app/Role.cs\0.txt", "app/Role.cs")), encoding="utf-8"
        )
        state: Any = {"reads": {}}
        self.assertTrue(review_dispatch.count_reads(self.run_directory, state, ["csharp-review"]))
        size = (self.source / "app" / "Role.cs").stat().st_size
        self.assertEqual({"csharp-review": {"files": 1, "bytes": size, "searches": 0}}, state["reads"])
        self.assertFalse(log.exists(), "a counted log is removed, so no rerun meets it again")

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

    def test_a_folder_where_the_read_log_is_shows_no_claim(self) -> None:
        # Only the log the claim creates proves the guard held the role. A folder in its place, which the claim cannot
        # replace, proves nothing, so the role reads as unclaimed, never as one the guard held.
        guard.read_log(self.run_directory, "csharp-review").mkdir(parents=True)
        self.claim()
        self.claim("sql-review", agent=OTHER_AGENT)
        self.assertEqual(
            (False, True),
            (guard.claimed(self.run_directory, "csharp-review"), guard.claimed(self.run_directory, "sql-review")),
        )

    def test_a_folder_where_the_read_log_is_counts_nothing_and_its_role_is_refused_as_unguarded(self) -> None:
        # check and finalize read the same evidence one way: a folder is no log, so check counts nothing for its role,
        # never failing on it, and finalize refuses the role as one no guard held.
        log = guard.read_log(self.run_directory, "csharp-review")
        log.mkdir(parents=True)
        state: Any = {"reads": {}, "runtime": "claude-code", "dispatch": "subagents"}
        self.assertFalse(review_dispatch.count_reads(self.run_directory, state, ["csharp-review"]))
        self.assertEqual({"reads": {}, "runtime": "claude-code", "dispatch": "subagents"}, state)
        self.assertTrue(log.is_dir(), "a folder that is no log is left as it is")
        self.assertEqual(
            {"csharp-review": guard.UNGUARDED.format(remedy=review_dispatch.UNGUARDED_REMEDY)},
            review_dispatch.unguarded_roles(self.run_directory, state, ["csharp-review"]),
        )

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

    def test_a_result_spelled_in_another_case_is_the_result_only_where_the_run_folds_case(self) -> None:
        # The run's file system decides, never the platform's name: on a case-sensitive one the other spelling is
        # another file, and on one that folds, as macOS's default APFS does, it is the result.
        self.claim()
        work = self.run_directory / "work"
        other = str(work / "CSHARP-REVIEW.result.json")
        for folds in (True, False):
            with self.subTest(folds=folds), mock.patch.dict(path_keys._FOLDS_CASE, {str(work): folds}):
                reason = self.decide("Write", file_path=other)
                if folds:
                    self.assertIsNone(reason)
                else:
                    self.assert_denied(reason, "write only your own result file")

    @unittest.skipUnless(sys.platform in ("win32", "linux"), "only a separate volume keeps case on macOS")
    def test_a_result_spelled_in_another_case_is_refused_where_two_hard_links_share_a_case_sensitive_folder(
        self,
    ) -> None:
        # Two hard links spelled in each other's case find one file, as one name would in a folder that folds case;
        # the folder still keeps case, so RESULT.JSON is another file and never the role's result.json.
        run = make_run(self.root / "code-review-run-ghi", ["csharp-review"])
        out = case_sensitive_folder(run / "out")
        hard_link_pair(out, "0A.txt", "0a.TXT")
        manifest = json.loads((run / "run.json").read_text(encoding="utf-8"))
        manifest["roles"][0]["result_file"] = str(out / "result.json")
        (run / "run.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.enterContext(mock.patch.dict(path_keys._FOLDS_CASE, clear=True))
        self.claim(run=run)
        self.assert_denied(self.decide("Write", file_path=str(out / "RESULT.JSON")), "write only your own result file")
        self.assertIsNone(self.decide("Write", file_path=str(out / "result.json")))

    def test_a_result_below_a_folder_spelled_with_the_kelvin_sign_is_refused_where_the_run_keeps_it_apart(
        self,
    ) -> None:
        # str.lower takes the Kelvin sign to k, but NTFS and Linux's file systems keep it apart from K, so a folder
        # spelled with it is a sibling the reviewer would create, never the folder holding its result, even where the
        # run folds the case of ASCII letters.
        run = make_run(self.root / "code-review-run-kel", ["csharp-review"])
        out = run / "Kx"
        out.mkdir()
        manifest = json.loads((run / "run.json").read_text(encoding="utf-8"))
        manifest["roles"][0]["result_file"] = str(out / "result.json")
        (run / "run.json").write_text(json.dumps(manifest), encoding="utf-8")
        sibling = run / "\u212ax"
        try:
            one = os.path.samestat(os.lstat(sibling), os.lstat(out))
        except FileNotFoundError:
            one = False
        self.enterContext(mock.patch.dict(path_keys._FOLDS_CASE, {str(run): True}))
        self.claim(run=run)
        reason = self.decide("Write", file_path=str(sibling / "result.json"))
        if one:
            self.assertIsNone(reason)
        else:
            self.assert_denied(reason, "write only your own result file")
        self.assertIsNone(self.decide("Write", file_path=str(run / "kX" / "result.json")))
        self.assertIsNone(self.decide("Write", file_path=str(out / "result.json")))

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
                ):
                    if bad != command:
                        with self.subTest(quote=quote, command=bad):
                            self.assert_denied(self.decide("Bash", command=bad))

    def test_a_smart_single_quote_is_denied_in_a_path_or_pattern_and_a_smart_double_quote_is_kept(self) -> None:
        # PowerShell ends a single-quoted string at each smart single quote, which Bash reads as text; inside single
        # quotes it reads a smart double quote as text, as Bash does.
        self.claim()
        for command in source_commands(self.run_directory, "csharp-review"):
            for quote in ("\u2018", "\u2019", "\u201a", "\u201b"):
                with self.subTest(command=command, quote=quote):
                    self.assert_denied(
                        self.decide("Bash", command=filled(command, f"Service{quote}; Write-Output marker; #")),
                        "writing each single quote in it as",
                    )
            for quote in ("\u201c", "\u201d", "\u201e"):
                with self.subTest(command=command, quote=quote):
                    self.assertIsNone(self.decide("Bash", command=filled(command, f"Service{quote}; x; #")))

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

    def test_bash_runs_both_source_commands_on_a_lazy_snapshot_and_on_one_written_whole(self) -> None:
        # The guard reads no snapshot kind: the pipeline gives every role `source-search`, and `review_source.py`
        # itself refuses `source-file` on a snapshot that holds every file already.
        fetch, search = source_commands(self.run_directory, "csharp-review")
        manifest = self.run_directory / "source" / review_runtime.SOURCE_SNAPSHOT_MANIFEST
        self.claim()
        kinds: tuple[tuple[str, dict[str, Any]], ...] = (
            ("written whole", {}),
            ("lazy", {review_runtime.SNAPSHOT_FETCHABLE: {}}),
        )
        for kind, entries in kinds:
            manifest.write_text(json.dumps({"files": {}, **entries}), encoding="utf-8")
            for command in (search.replace("<pattern>", "Service"), fetch.replace("<path>", "app/Service.cs")):
                with self.subTest(snapshot=kind, command=command):
                    self.assertIsNone(self.decide("Bash", command=command))

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
            # A value on more than one line, or none.
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

    # What a reviewer may search for or fetch: each character either shell reads inside double quotes, and both quotes.
    LITERALS = (
        'a.cs" && whoami && echo "',
        "$(whoami)",
        "`whoami`",
        "a\\",
        "a\\.b",
        "print[ ]*[$]",
        '${x} `id` "q" \\ it\'s [$]',
        "'",
        "''",
        "it's",
    )

    def test_a_path_or_pattern_holding_any_shell_character_is_allowed_as_the_literal_it_spells(self) -> None:
        self.claim()
        for command, option in zip(
            source_commands(self.run_directory, "csharp-review"), ("path", "pattern"), strict=True
        ):
            for value in self.LITERALS:
                written = filled(command, value)
                with self.subTest(command=written):
                    self.assertIsNone(self.decide("Bash", command=written))
                    self.assertEqual(value, guard.source_value(written, option))

    def test_a_value_spelled_any_other_way_is_denied(self) -> None:
        self.claim()
        for command in source_commands(self.run_directory, "csharp-review"):
            placeholder = "'<path>'" if "<path>" in command else "'<pattern>'"
            for spelled in (
                # Another command after the value, or around it.
                "'a'; rm -rf x",
                "'a' && x",
                "'a' | sh",
                "'a' > x",
                "'a' &",
                "'a'$(x)",
                "'a'`x`",
                "'a'\"$x\"",
                "'a'$x",
                # A literal to Bash that PowerShell reads otherwise, or that is not the one spelling.
                "$'a'",
                "'a'\\''b'",
                "'a''b'",
                "'a'\"b\"",
                "'a'b",
                "'a' 'b'",
                # A quote never closed, or none.
                "'a",
                "'a'\"'",
                "'a'\"'\"'",
                "a",
                # A NUL, a line break, or nothing.
                "'a\0b'",
                "'a\nb'",
                "'a\rb'",
                "''",
            ):
                bad = command.replace(placeholder, spelled)
                with self.subTest(command=bad):
                    self.assert_denied(self.decide("Bash", command=bad))

    def test_a_word_after_the_value_is_denied_so_argparse_never_reads_another_run_or_role(self) -> None:
        # argparse takes the last --run, --role, and option it reads, and an abbreviation of each, so any word after
        # the value could name another run or role than the one the guard checked.
        self.claim()
        other = self.other_run
        commands = source_commands(self.run_directory, "csharp-review")
        for command, option in zip(commands, ("path", "pattern"), strict=True):
            value = filled(command, "a")
            for bad in (
                f"{value} --run='{other}' --role='sql-review' --{option}='b'",
                f"{value} --{option}='b'",
                f"{value} --ru='{other}' --{option}='b'",
                f"{value} --ro='sql-review'",
                f"{value} * --{option}='b'",
                f"{value} {{x,y}} --{option}='b'",
                f"{value} #'",
                f"{value} --% --{option}='%HOME%'",
                f"{value} @args --{option}='b'",
            ):
                with self.subTest(command=bad):
                    self.assert_denied(self.decide("Bash", command=bad), "as the command's last word")

    def test_a_dollar_sign_before_a_letter_is_denied_in_a_pattern_and_allowed_in_a_path(self) -> None:
        # Claude Code asks before a command with a $ before a letter, even in single quotes; a pattern writes it as
        # [$], and a path, which has no other spelling, is left to that prompt.
        self.claim()
        fetch, search = source_commands(self.run_directory, "csharp-review")
        for pattern in ("$HOME", "a$b", "print[ ]*$x", "${HOME}$Z"):
            with self.subTest(pattern=pattern):
                self.assert_denied(self.decide("Bash", command=filled(search, pattern)), "as [$]")
        for pattern in ("print[ ]*[$]", "a$", "$(", "${x}", "$1", "[$]HOME"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(self.decide("Bash", command=filled(search, pattern)))
        self.assertIsNone(self.decide("Bash", command=filled(fetch, "app/routes/posts.$slug.tsx")))

    # Characters Python's str.strip drops that Bash keeps, as part of the last word or as a command of its own after a
    # line break: a form feed, a vertical tab, U+001C to U+001F, U+0085, Unicode spaces, and a carriage return.
    KEPT_BY_BASH = (
        "\x0c",
        "\x0b",
        "\x1c",
        "\x1d",
        "\x1e",
        "\x1f",
        "\x85",
        "\u00a0",
        "\u2003",
        "\u202f",
        "\u3000",
        "\r",
        "\r\n",
        "\n\x1c",
        "\n\u2003",
    )

    def test_a_character_bash_keeps_at_either_end_of_a_command_is_denied_and_ascii_padding_is_not(self) -> None:
        self.claim()
        check = review_run.self_check_command(self.run_directory, "csharp-review")
        commands = [check, *(filled(command, "a") for command in source_commands(self.run_directory, "csharp-review"))]
        for command in commands:
            for kept in self.KEPT_BY_BASH:
                for bad in (f"{command}{kept}", f"{kept}{command}"):
                    with self.subTest(command=bad):
                        self.assert_denied(self.decide("Bash", command=bad))
            for padded in (f"  {command}  ", f"\t{command}\n", f"\n{command} \n\n"):
                with self.subTest(command=padded):
                    self.assertIsNone(self.decide("Bash", command=padded))

    def test_a_declared_self_check_with_a_character_bash_keeps_at_its_end_is_denied(self) -> None:
        run = self.root / "code-review-run-synthesis"
        run.mkdir()
        check = f'{PYTHON} -B "{self.root / "review_insights.py"}" synthesize --report "{self.root / "i.json"}" --check'
        role = {
            "id": "synthesis",
            "prompt_file": str(run / "synthesis-prompt.md"),
            "result_file": str(run / "synthesis-result.json"),
            "self_check": check,
        }
        (run / "run.json").write_text(json.dumps({"roles": [role]}), encoding="utf-8")
        self.assertIsNone(self.decide("Read", agent=OTHER_AGENT, file_path=role["prompt_file"]))
        self.assertIsNone(self.decide("Bash", agent=OTHER_AGENT, command=f"\t{check}\n"))
        for kept in self.KEPT_BY_BASH:
            with self.subTest(kept=kept):
                self.assert_denied(self.decide("Bash", agent=OTHER_AGENT, command=f"{check}{kept}"))


class ClaimTests(GuardFixture):
    def test_a_racing_first_read_keeps_the_first_written_run_and_role(self) -> None:
        for winner, allowed in ((self.run_directory, True), (self.other_run, False)):
            with self.subTest(winner=winner):
                claims = self.root / f"race-{winner.name}"
                winning_role = "sql-review" if allowed else "csharp-review"
                winning_claim = json.dumps({"run": str(winner), "role": winning_role})

                def racing_link(
                    copy: Path,
                    target: Path,
                    *,
                    destination: Path = claims / f"{AGENT}.json",
                    content: str = winning_claim,
                ) -> None:
                    destination.write_text(content, encoding="utf-8")
                    REAL_LINK(copy, target)

                with mock.patch.object(guard, "CLAIMS", claims), mock.patch.object(os, "link", racing_link):
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
                self.assert_no_claim("a denied first read claims nothing")
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
        self.assert_no_claim()
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
        self.assert_no_claim()

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

    def test_a_run_or_claim_the_parser_refuses_denies_as_unreadable_and_is_pruned_only_once_it_is_old(self) -> None:
        run_file = self.run_directory / "run.json"
        run = run_file.read_bytes()
        stale = self.claims / f"{OTHER_AGENT}.json"
        for reason, content in UNPARSEABLE_JSON.items():
            with self.subTest(reason=reason):
                run_file.write_bytes(content)
                self.assertRegex(
                    self.decide("Read", file_path=self.prompt()) or "",
                    rf"^Code-review reviewer boundary: the review run {re.escape(str(self.run_directory))} could not "
                    r"be read: \S.*\.$",
                )
                run_file.write_bytes(run)
                self.claims.mkdir(exist_ok=True)
                (self.claims / f"{AGENT}.json").write_bytes(content)
                self.assertEqual(UNREADABLE_CLAIM, self.decide("Write", file_path=self.result()))
                (self.claims / f"{AGENT}.json").unlink()
                # A new claim prunes claims whose run is gone; one it cannot read is kept while a guard may still be
                # writing it, and pruned once it is old.
                stale.write_bytes(content)
                self.claim()
                self.assertEqual(content, stale.read_bytes())
                (self.claims / f"{AGENT}.json").unlink()
                age_by(stale, UNREADABLE_CLAIM_AGE)
                self.claim()
                self.assertFalse(stale.exists())
                (self.claims / f"{AGENT}.json").unlink()

    def test_a_claim_whose_write_fails_after_the_create_or_part_way_leaves_none_and_a_retry_claims(self) -> None:
        # The disk fills once the claim's file is created, or part way through its write: whichever file the guard
        # writes the claim through, the failed call leaves no claim, so the retry is a first read that claims anew.
        claim = {"run": str(self.run_directory), "role": "csharp-review"}
        for fault, patches in (
            ("after the create", self.full_disk_after_the_create()),
            ("part way through the write", self.full_disk_part_way()),
        ):
            with self.subTest(fault=fault):
                shutil.rmtree(self.claims, ignore_errors=True)
                with contextlib.ExitStack() as stack:
                    for patch in patches:
                        stack.enter_context(patch)
                    reason = self.decide("Read", file_path=self.prompt())
                self.assertEqual(
                    "Code-review reviewer boundary: this reviewer's claim could not be made: "
                    f"[Errno {errno.ENOSPC}] {os.strerror(errno.ENOSPC)}.",
                    reason,
                )
                self.assertFalse((self.claims / f"{AGENT}.json").exists(), "a failed write leaves no claim")
                self.assertFalse(guard.claimed(self.run_directory, "csharp-review"))
                self.claim()
                self.assertEqual(claim, json.loads((self.claims / f"{AGENT}.json").read_text(encoding="utf-8")))
                self.assertTrue(guard.claimed(self.run_directory, "csharp-review"))
                guard.read_log(self.run_directory, "csharp-review").unlink()

    def full_disk_after_the_create(self) -> list[Any]:
        """os.open creating a file in the claims folder other than the prune's lock, then failing as a full disk does,
        the file left created."""
        real_open = os.open

        def full_disk(file: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            descriptor = real_open(file, flags, *args, **kwargs)
            if flags & os.O_CREAT and Path(file).parent == self.claims and Path(file).name != guard.PRUNE_LOCK:
                os.close(descriptor)
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
            return descriptor

        return [mock.patch.object(os, "open", full_disk)]

    def full_disk_part_way(self) -> list[Any]:
        """Every write, through a descriptor or a stream os.fdopen made, writing the first half of what it is given
        and then failing as a full disk does."""
        real_write, real_fdopen = os.write, os.fdopen

        def full_disk(descriptor: int, data: Any) -> int:
            real_write(descriptor, bytes(data[: len(data) // 2]))
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

        class HalfStream:
            def __init__(self, stream: Any) -> None:
                self.stream = stream

            def __enter__(self) -> HalfStream:
                return self

            def __exit__(self, *exc: object) -> None:
                self.stream.close()

            def write(self, text: str) -> int:
                self.stream.write(text[: len(text) // 2])
                self.stream.flush()
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

        def half_stream(*args: Any, **kwargs: Any) -> HalfStream:
            return HalfStream(real_fdopen(*args, **kwargs))

        return [mock.patch.object(os, "write", full_disk), mock.patch.object(os, "fdopen", half_stream)]

    def test_a_parallel_first_call_at_any_step_of_a_claim_reads_the_whole_claim_or_none(self) -> None:
        # The agent's second first read runs in the middle of the first one's claim, at each step the claim's write
        # takes: it finds no claim and claims itself, or finds the whole claim, never part of one, and both reads hold.
        claim = {"run": str(self.run_directory), "role": "csharp-review"}
        ran: list[str] = []
        for step in ("open", "fdopen", "write", "fsync", "link"):
            with self.subTest(step=step):
                shutil.rmtree(self.claims, ignore_errors=True)
                parallel: list[str | None] = []
                with mock.patch.object(os, step, self.parallel_first_read(step, getattr(os, step), parallel, ran)):
                    self.assertIsNone(self.decide("Read", file_path=self.prompt()))
                self.assertEqual([None] if parallel else [], parallel, "the parallel first read holds")
                self.assertEqual(claim, json.loads((self.claims / f"{AGENT}.json").read_text(encoding="utf-8")))
        self.assertIn("link", ran, "the claim is linked into place")

    def parallel_first_read(self, step: str, real: Any, parallel: list[str | None], ran: list[str]) -> Any:
        """`real`, the os function named `step`, which first makes the agent's parallel first read, once, and keeps
        its denial reason, or None, in `parallel`, and `step` in `ran`."""

        def at_step(*args: Any, **kwargs: Any) -> Any:
            if not parallel:
                parallel.append("running")
                parallel[0] = self.decide("Read", file_path=self.prompt())
                ran.append(step)
            return real(*args, **kwargs)

        return at_step

    def test_a_claim_made_whose_copy_is_left_holds_and_is_allowed_and_one_not_made_leaves_none(self) -> None:
        # Holds on every platform. A claim made whose copy's name no try removed, as on Windows while another reader
        # holds the claim open, is allowed; one whose removal an interrupt ended still denies, and holds.
        real_create = atomic_file.create_bytes

        def unfinished(made: bool, failure: BaseException | None = None) -> Any:
            def create(path: Path, data: bytes, **kwargs: Any) -> None:
                copy = path.with_name(f".{path.name}.left.tmp")
                if made:
                    real_create(path, data, **kwargs)
                raise atomic_file.UnfinishedCopyError(
                    target=path,
                    copy=copy,
                    failure=failure or OSError(errno.EIO, "I/O error"),
                    removal=PermissionError(errno.EACCES, "Permission denied"),
                    made=made,
                )

            return create

        target = self.claims / f"{AGENT}.json"
        with mock.patch.object(atomic_file, "create_bytes", unfinished(made=False)):
            self.assertEqual(
                "Code-review reviewer boundary: this reviewer's claim could not be made: Cannot remove "
                f"{target.with_name(f'.{target.name}.left.tmp')}, the unfinished copy of {target}: [Errno "
                f"{errno.EACCES}] Permission denied.",
                self.decide("Read", file_path=self.prompt()),
            )
        self.assert_no_claim("a claim not made is no claim")
        self.assertFalse(guard.claimed(self.run_directory, "csharp-review"))
        with mock.patch.object(atomic_file, "create_bytes", unfinished(made=True, failure=KeyboardInterrupt())):
            self.assertEqual(
                "Code-review reviewer boundary: this reviewer's claim is made, but the copy it was written through is "
                "left; read the prompt file your task names first.",
                self.decide("Read", file_path=self.prompt()),
            )
        self.assertTrue(guard.claimed(self.run_directory, "csharp-review"), "the claim holds, so its log is started")
        self.assertIsNone(self.decide("Read", file_path=self.prompt()))
        self.assertIsNone(self.decide("Write", file_path=self.result()))
        self.assert_denied(self.decide("Write", file_path=self.result("sql-review")), "write only your own result file")
        with mock.patch.object(atomic_file, "create_bytes", unfinished(made=True)):
            self.assertIsNone(self.decide("Read", agent=OTHER_AGENT, file_path=self.prompt("sql-review")))
        self.assertTrue(guard.claimed(self.run_directory, "sql-review"), "the claim holds, so its log is started")
        self.assertIsNone(self.decide("Write", agent=OTHER_AGENT, file_path=self.result("sql-review")))
        self.assert_denied(self.decide("Write", agent=OTHER_AGENT, file_path=self.result()), "write only your own")

    def test_a_claim_whose_copy_a_reader_holds_past_every_try_is_allowed_and_the_copy_pruned_once_old(self) -> None:
        # Holds on every platform, by injected refusals of every try to remove the copy's name, as a file system may
        # refuse it while another guard's prune or this agent's parallel first read holds the claim open throughout.
        refused: list[Path] = []

        def unlink(path: Path, missing_ok: bool = False) -> None:
            if path.name.startswith(f".{AGENT}.json."):
                refused.append(path)
                raise PermissionError(errno.EACCES, "the file is being used by another process", str(path))
            REAL_UNLINK(path, missing_ok=missing_ok)

        with mock.patch.object(Path, "unlink", unlink), mock.patch.object(time, "sleep"):
            self.claim()
        self.assertGreater(len(refused), 2, "the copy's removal was tried after waits, and refused each time")
        self.assert_made_claim_with_its_copy_left(refused[:1])

    def test_a_reader_holding_the_claim_open_while_its_create_removes_the_copy_keeps_no_read_from_its_claim(
        self,
    ) -> None:
        # The race of a claim made while another reader, another guard's prune or this agent's parallel first read,
        # holds it open by its own name, as both do, with a real handle from the link until the call is decided. Holds
        # on every platform: where the file system removes the copy's name meanwhile, as Linux, macOS, and the NTFS of
        # CI's Windows runner all do, no copy is left; where one refuses it, the copy is left and pruned once old.
        reader = contextlib.ExitStack()
        held: list[Path] = []

        def link_then_hold(source: Any, target: Any, *args: Any, **kwargs: Any) -> None:
            REAL_LINK(source, target, *args, **kwargs)
            if Path(target).name == f"{AGENT}.json":
                reader.callback(os.close, os.open(target, os.O_RDONLY))
                held.append(Path(target))

        with reader, mock.patch.object(os, "link", link_then_hold), mock.patch.object(time, "sleep"):
            self.claim()
        self.assertEqual(1, len(held), "the reader held the claim from its link until the call was decided")
        left = sorted(self.claims.glob(f".{AGENT}.json.*.tmp"))
        self.assert_made_claim_with_its_copy_left(left)

    def assert_made_claim_with_its_copy_left(self, copies: list[Path]) -> None:
        """The agent's claim of the csharp-review role holds, its copies, if any, are left beside it, and a later
        claim prunes them once they are LEFTOVER_COPY_AGE old, keeping the claim they are names of."""
        claim = {"run": str(self.run_directory), "role": "csharp-review"}
        self.assertEqual(claim, json.loads((self.claims / f"{AGENT}.json").read_text(encoding="utf-8")))
        for copy in copies:
            self.assertTrue(copy.is_file(), "the copy is left for the prune of leftover copies")
        self.assertTrue(guard.claimed(self.run_directory, "csharp-review"), "the claim's read log is started")
        self.assertIsNone(self.decide("Read", file_path=self.prompt()))
        self.assertIsNone(self.decide("Write", file_path=self.result()))
        self.assert_denied(self.decide("Write", file_path=self.result("sql-review")), "write only your own result file")
        for copy in copies:
            age_by(copy, LEFTOVER_COPY_AGE)
        self.claim("sql-review", agent=OTHER_AGENT)
        self.assertEqual([], sorted(self.claims.glob(f".{AGENT}.json.*.tmp")), "a later claim prunes the old copy")
        self.assertEqual(claim, json.loads((self.claims / f"{AGENT}.json").read_text(encoding="utf-8")))

    def test_an_older_guard_s_empty_or_partial_claim_denies_naming_the_remedy_and_is_pruned_once_old(self) -> None:
        # An older guard created a claim and then wrote it, so a write that failed or was killed left it empty or
        # partial. Its own agent is denied with the remedy; a new claim prunes it once no guard can still be writing it.
        whole = json.dumps({"run": str(self.run_directory), "role": "csharp-review"}).encode()
        for content in (b"", whole[: len(whole) // 2]):
            with self.subTest(content=content):
                shutil.rmtree(self.claims, ignore_errors=True)
                self.claims.mkdir()
                broken = self.claims / f"{OTHER_AGENT}.json"
                broken.write_bytes(content)
                for tool, tool_input in (
                    ("Read", {"file_path": self.prompt()}),
                    ("Write", {"file_path": self.result()}),
                ):
                    self.assertEqual(UNREADABLE_CLAIM, self.decide(tool, agent=OTHER_AGENT, **tool_input))
                age_by(broken, UNREADABLE_CLAIM_AGE - 60)
                self.claim()
                self.assertEqual(content, broken.read_bytes(), "a young claim is kept")
                age_by(broken, 120)
                self.claim(agent="a000000000000000f")
                self.assertFalse(broken.exists(), "an old claim is pruned")
                # Every call of its agent was denied, so its next read is a first read, of any role's prompt, as a
                # fresh agent's is, and then holds it to that role alone.
                self.claim("sql-review", agent=OTHER_AGENT)
                self.assert_denied(self.decide("Write", agent=OTHER_AGENT, file_path=self.result()), "write only")
                self.assertIsNone(self.decide("Write", agent=OTHER_AGENT, file_path=self.result("sql-review")))

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

    def test_a_new_claim_keeps_a_claim_whose_run_file_it_cannot_stat(self) -> None:
        # Its run is not known to be gone, so the claim another reviewer holds stays.
        self.claims.mkdir()
        live = self.claims / "a000000000000000f.json"
        live.write_text(json.dumps({"run": str(self.other_run), "role": "csharp-review"}), encoding="utf-8")
        with unstatable(self.other_run / "run.json"):
            self.claim()
        self.assertEqual(sorted([f"{AGENT}.json", live.name]), sorted(path.name for path in self.claims.iterdir()))


STALE_CLAIMS = ("unparseable and old", "of a run that is gone")


class PruneTests(GuardFixture):
    """A first read prunes under a lock and removes only the file it judged, so no claim made after the judgment is
    removed; it prunes a copy a killed create left; and a claim the file system cannot link into place denies naming
    the cause and the remedy."""

    def stale_claim(self, kind: str) -> tuple[Path, str]:
        """Agent OTHER_AGENT's claim, of `kind`, which a prune removes, in an otherwise empty claims folder, with the
        reason its agent's own read is denied while the claim is there."""
        shutil.rmtree(self.claims, ignore_errors=True)
        self.claims.mkdir()
        stale = self.claims / f"{OTHER_AGENT}.json"
        if kind == "unparseable and old":
            stale.write_bytes(b'{"run": ')
            age_by(stale, UNREADABLE_CLAIM_AGE)
            return stale, UNREADABLE_CLAIM
        gone = self.root / "code-review-run-gone"
        stale.write_text(json.dumps({"run": str(gone), "role": "csharp-review"}), encoding="utf-8")
        return stale, (
            f"Code-review reviewer boundary: the review run {gone} could not be read: {os.strerror(errno.ENOENT)}."
        )

    def test_no_guard_prunes_while_another_does_so_a_claim_made_meanwhile_is_never_removed(self) -> None:
        # Guard A judges agent X's claim one to prune. Just before A removes it, guard B prunes it and X's next read
        # claims anew at the same path. A holds the prune's lock, so B leaves the prune to A and claims, and
        # X's read is judged against the claim still there; without the lock, A would remove the claim X's read was
        # just let through on, and X's next read would be a first read again.
        for kind in STALE_CLAIMS:
            with self.subTest(kind=kind):
                stale, denied = self.stale_claim(kind)
                reads: dict[str, str | None] = {}

                def unlink_after_the_race(
                    path: Path, missing_ok: bool = False, *, stale: Path = stale, reads: dict[str, str | None] = reads
                ) -> None:
                    if path == stale and not reads:
                        # Marked first, so a prune B or X makes, had it no lock to wait on, removes for real.
                        reads["started"] = None
                        reads["B"] = self.decide("Read", agent=THIRD_AGENT, file_path=self.prompt("sql-review"))
                        reads["X"] = self.decide("Read", agent=OTHER_AGENT, file_path=self.prompt("sql-review"))
                    REAL_UNLINK(path, missing_ok=missing_ok)

                with mock.patch.object(Path, "unlink", unlink_after_the_race):
                    self.claim()
                self.assertEqual(
                    {"started": None, "B": None, "X": denied}, reads, "X's read is judged against the claim A holds"
                )
                self.assertFalse(stale.exists(), "A removes the claim it judged")
                self.claim("sql-review", agent=OTHER_AGENT)
                self.assertEqual(
                    {"run": str(self.run_directory), "role": "sql-review"},
                    json.loads(stale.read_text(encoding="utf-8")),
                )

    @unittest.skipIf(
        sys.platform == "win32",
        "Windows refuses to remove a file a handle without delete sharing holds open, as the prune holds the claim it "
        "judges until it compares it, so no claim can be removed and remade there meanwhile",
    )
    def test_a_prune_removes_only_the_file_it_judged_so_a_claim_remade_after_the_judgment_is_kept(self) -> None:
        # Holds on Linux and macOS. Where no lock holds, as beside an older guard, which prunes unlocked: between A's
        # judgment of X's claim and its removal, the older guard removes the claim and X's next read claims anew at
        # the same path. A compares the file it judged with the one at the path, and keeps the newer claim.
        for kind in STALE_CLAIMS:
            with self.subTest(kind=kind):
                stale, _denied = self.stale_claim(kind)
                raced: list[str | None] = []

                def lstat_after_the_race(
                    path: Path, *, stale: Path = stale, raced: list[str | None] = raced
                ) -> os.stat_result:
                    if path == stale and not raced:
                        raced.append("racing")
                        stale.unlink()
                        raced[0] = self.decide("Read", agent=OTHER_AGENT, file_path=self.prompt("sql-review"))
                    return REAL_LSTAT(path)

                with mock.patch.object(Path, "lstat", lstat_after_the_race):
                    self.claim()
                self.assertEqual([None], raced, "X's read claims anew")
                self.assertEqual(
                    {"run": str(self.run_directory), "role": "sql-review"},
                    json.loads(stale.read_text(encoding="utf-8")),
                    "the claim X's read was let through on holds",
                )
                self.assertIsNone(self.decide("Write", agent=OTHER_AGENT, file_path=self.result("sql-review")))

    def test_a_prune_another_guard_holds_the_lock_of_is_left_to_it_and_the_claim_is_made(self) -> None:
        stale, _denied = self.stale_claim("of a run that is gone")
        lock = self.claims / guard.PRUNE_LOCK
        descriptor = file_lock.take(lock)
        self.assertIsNotNone(descriptor)
        try:
            self.claim()
            self.assertTrue(stale.exists(), "the lock's holder prunes, not this guard")
        finally:
            file_lock.release(descriptor or 0, lock)
        self.claim(agent=THIRD_AGENT)
        self.assertFalse(stale.exists())
        self.assertFalse(lock.exists(), "a prune leaves no lock behind")

    def test_a_lock_that_cannot_be_opened_or_is_refused_for_a_while_leaves_the_prune_to_a_later_claim(self) -> None:
        for name, failure in (
            ("cannot be opened", PermissionError(errno.EACCES, "Permission denied")),
            ("refused for a while", file_lock.FileLockError(errno.ENOLCK, "No locks available")),
        ):
            with self.subTest(name=name):
                stale, _denied = self.stale_claim("of a run that is gone")
                with mock.patch.object(file_lock, "take", side_effect=failure):
                    self.claim()
                self.assertTrue(stale.exists(), "the prune is left, and the claim is made")
                self.claim(agent=THIRD_AGENT)
                self.assertFalse(stale.exists())

    def test_a_file_system_with_no_lock_at_all_prunes_unlocked_and_keeps_no_lock_file(self) -> None:
        stale, _denied = self.stale_claim("of a run that is gone")
        unsupported = file_lock.FileLockError(errno.ENOTSUP, "Operation not supported")
        with mock.patch.object(file_lock, "try_lock", side_effect=unsupported):
            self.claim()
        self.assertFalse(stale.exists())
        self.assertEqual([f"{AGENT}.json"], [path.name for path in self.claims.iterdir()])

    def test_a_copy_a_killed_create_left_is_pruned_once_old_and_a_younger_one_and_its_claim_are_kept(self) -> None:
        # Holds on every platform CI runs, each of which keeps its temporary directory on a file system with hard
        # links. A create killed before it removed its copy's own name leaves `.<agent>.json.<random>.tmp` beside the
        # claims: once linked into place, the copy and the claim are one file, and before that, the copy alone.
        self.claims.mkdir()
        claim = self.claims / f"{OTHER_AGENT}.json"
        claim.write_text(json.dumps({"run": str(self.other_run), "role": "csharp-review"}), encoding="utf-8")
        linked = self.claims / f".{OTHER_AGENT}.json.k1l7ed.tmp"
        os.link(claim, linked)
        unlinked = self.claims / f".{THIRD_AGENT}.json.p4r7ly.tmp"
        young = self.claims / f".{THIRD_AGENT}.json.y0un9x.tmp"
        for copy in (unlinked, young):
            copy.write_bytes(b'{"run": ')
        for copy in (linked, unlinked):
            age_by(copy, LEFTOVER_COPY_AGE)
        age_by(young, LEFTOVER_COPY_AGE - 60)
        opened: list[Path] = []
        real_open = Path.open

        def recording_open(path: Path, *args: Any, **kwargs: Any) -> Any:
            opened.append(path)
            return real_open(path, *args, **kwargs)

        # Windows refuses to remove a name of a file a handle without delete sharing holds open, so a prune that
        # opened a copy a create is still using would keep that create from removing its copy's name.
        with mock.patch.object(Path, "open", recording_open):
            self.claim()
        self.assertNotIn(young, opened, "a copy younger than LEFTOVER_COPY_AGE is never opened")
        self.assertIn(unlinked, opened, "an old copy is judged open")
        self.assertEqual(
            sorted([f"{AGENT}.json", claim.name, young.name]), sorted(path.name for path in self.claims.iterdir())
        )
        self.assertEqual(
            {"run": str(self.other_run), "role": "csharp-review"}, json.loads(claim.read_text(encoding="utf-8"))
        )

    def test_a_copy_gone_before_its_age_is_read_is_kept_and_the_claim_is_made(self) -> None:
        # Holds on every platform: a create removes its copy's name right after linking it into place, so a copy the
        # prune listed is often gone by the time its age is read. It is kept unjudged, never opened, and the read
        # claims as if it were not there.
        self.claims.mkdir()
        vanished = self.claims / f".{OTHER_AGENT}.json.g0n3xx.tmp"
        vanished.write_bytes(b'{"run": ')
        age_by(vanished, LEFTOVER_COPY_AGE)

        def gone(path: Path) -> os.stat_result:
            if path == vanished:
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
            return REAL_LSTAT(path)

        with mock.patch.object(Path, "lstat", gone):
            self.claim()
        self.assertTrue(vanished.exists(), "a copy whose age cannot be read is kept")
        self.assertEqual(
            {"run": str(self.run_directory), "role": "csharp-review"},
            json.loads((self.claims / f"{AGENT}.json").read_text(encoding="utf-8")),
        )

    def test_a_claims_folder_whose_file_system_refuses_hard_links_denies_naming_the_cause_and_the_remedy(self) -> None:
        # Holds on every platform: os.link refuses as a file system without hard links does on each of them.
        target = self.claims / f"{AGENT}.json"

        def refusing(code: int) -> Any:
            def link(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
                raise OSError(code, os.strerror(code), str(source), None, str(destination))

            return link

        for code in sorted(atomic_file.NO_HARD_LINK_ERRNOS):
            with self.subTest(errno=errno.errorcode[code]):
                with mock.patch.object(os, "link", refusing(code)):
                    reason = self.decide("Read", file_path=self.prompt())
                self.assertEqual(
                    "Code-review reviewer boundary: this reviewer's claim could not be made: "
                    f"[Errno {code}] the file system refused to link the finished copy into place: "
                    f"{os.strerror(code)}: {str(target)!r}; the reviewer guard makes each claim whole through a hard "
                    f"link, which the file system holding {self.root} refuses; give the guard a runtime or temporary "
                    "directory on a file system with hard links, such as a local disk's.",
                    reason,
                )
                self.assert_no_claim("no claim and no copy is left")
                self.assertFalse(guard.claimed(self.run_directory, "csharp-review"))

        # The same errno raised naming another path than the claim, such as the claims folder, is no refusal of the
        # claim's link, so it names its error alone.
        def elsewhere(*args: Any, **kwargs: Any) -> None:
            raise OSError(errno.EXDEV, os.strerror(errno.EXDEV), str(self.claims))

        with mock.patch.object(atomic_file, "create_bytes", elsewhere):
            self.assertEqual(
                "Code-review reviewer boundary: this reviewer's claim could not be made: "
                f"[Errno {errno.EXDEV}] {os.strerror(errno.EXDEV)}: {str(self.claims)!r}.",
                self.decide("Read", file_path=self.prompt()),
            )
        self.assert_no_claim()
        # Any other refusal of the link names its error alone.
        with mock.patch.object(os, "link", refusing(errno.EACCES)):
            self.assertEqual(
                "Code-review reviewer boundary: this reviewer's claim could not be made: "
                f"[Errno {errno.EACCES}] the file system refused to link the finished copy into place: "
                f"{os.strerror(errno.EACCES)}: {str(target)!r}.",
                self.decide("Read", file_path=self.prompt()),
            )
        self.assert_no_claim()
        self.claim()


class ClaimsFolderTests(GuardFixture):
    """The claims folder is the user's alone before every claim is read or written: one another local account made
    first, as it can at the predictable name in Linux's shared /tmp, denies every call rather than holding a claim
    that account could remove or plant."""

    def assert_folder_denied(self, why: str, tool: str = "Read", agent: str = AGENT, **tool_input: str) -> None:
        reason = self.decide(tool, agent, **(tool_input or {"file_path": self.prompt()}))
        self.assertIsNotNone(reason)
        self.assertTrue(
            (reason or "").startswith(
                f"Code-review reviewer boundary: the reviewer claims folder is not yours alone: {why}"
            ),
            reason,
        )

    @unittest.skipIf(sys.platform == "win32", "Windows has no user ids; private_folder's WindowsTests cover it")
    def test_a_claims_folder_another_account_owns_denies_the_claim_and_every_later_call(self) -> None:
        self.claim()
        # The parent is this user's, which the parent check would judge another's under the id the test pretends.
        with (
            mock.patch.object(private_folder, "user_id", return_value=OTHER_USER),
            mock.patch.object(private_folder, "parent_refusal", return_value=None),
        ):
            self.assert_folder_denied(f"{self.claims} is owned by user id ")
            self.assert_folder_denied(f"{self.claims} is owned by user id ", "Write", file_path=self.result())
            self.assert_folder_denied(
                f"{self.claims} is owned by user id ", "Read", agent=OTHER_AGENT, file_path=self.prompt("sql-review")
            )
        self.assertEqual([f"{AGENT}.json"], [path.name for path in self.claims.iterdir()], "no claim was written")

    @unittest.skipIf(sys.platform == "win32", "Windows mode bits say nothing about who may write a folder")
    def test_a_claims_folder_its_group_or_anyone_can_write_denies_the_claim_and_every_later_call(self) -> None:
        for mode in (0o770, 0o707, 0o777):
            with self.subTest(mode=oct(mode)):
                self.claims.mkdir(mode=0o700)
                self.claims.chmod(mode)
                self.assert_folder_denied(f"{self.claims} can be written by users other than its owner (mode {mode:o})")
                self.assertEqual([], list(self.claims.iterdir()), "no claim was written")
                self.claims.chmod(0o700)
                self.claim()
                self.claims.chmod(mode)
                source = str(self.run_directory / "source" / "app" / "Service.cs")
                self.assert_folder_denied(f"{self.claims} can be written by users other than", file_path=source)
                self.assert_folder_denied(
                    f"{self.claims} can be written by users other than", "Write", file_path=self.result()
                )
                self.assertEqual(mode, stat.S_IMODE(self.claims.stat().st_mode), "the folder was left as it is")
                self.claims.chmod(0o700)
                shutil.rmtree(self.claims)

    def test_a_claims_folder_that_is_a_link_denies_the_claim_and_nothing_is_written_through_it(self) -> None:
        target = self.root / "elsewhere"
        target.mkdir(mode=0o700)
        directory_link(self.claims, target)
        self.assert_folder_denied(f"{self.claims} is a link or junction, not a folder of this user's own")
        self.assertEqual([], list(target.iterdir()), "no claim was written through the link")
        # A claim already behind the link, as another account would plant one, binds nobody.
        (target / f"{AGENT}.json").write_text(
            json.dumps({"run": str(self.run_directory), "role": "csharp-review"}), encoding="utf-8"
        )
        self.assert_folder_denied(f"{self.claims} is a link or junction", "Write", file_path=self.result())

    def test_a_claims_folder_that_cannot_be_made_denies_naming_why(self) -> None:
        claims = self.root / "missing" / "claims"
        with mock.patch.object(guard, "CLAIMS", claims):
            reason = self.decide("Read", file_path=self.prompt())
        self.assertTrue(
            (reason or "").startswith(
                f"Code-review reviewer boundary: the reviewer claims folder {claims} cannot be used: "
            ),
            reason,
        )
        self.assertFalse(claims.parent.exists(), "nothing was made above the claims folder")

    def test_a_missing_claims_folder_is_made_owner_only(self) -> None:
        self.claim()
        self.assertTrue((self.claims / f"{AGENT}.json").is_file())
        if sys.platform != "win32":
            self.assertEqual(0o700, stat.S_IMODE(self.claims.stat().st_mode))


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
        self.assert_no_claim("a denied call claims nothing")

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
        # The denial names the field the run holds of the wrong kind.
        reason = f"Code-review reviewer boundary: the review run {self.run_directory} could not be read: run"
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
            # The paths a role withheld a pull-request text may read: a list that holds none, or that is no list of
            # paths, would otherwise read as no limit at all.
            {"roles": [{**role, "reads": None}]},
            {"roles": [{**role, "reads": []}]},
            {"roles": [{**role, "reads": str(self.run_directory / "source")}]},
            {"roles": [{**role, "reads": [5]}]},
            {"roles": [{**role, "reads": [""]}]},
        ]
        for run in runs:
            with self.subTest(run=run):
                (self.run_directory / "run.json").write_text(json.dumps(run), encoding="utf-8")
                denial = self.decide("Read", file_path=self.prompt()) or ""
                self.assertTrue(denial.startswith(reason), denial)
        self.assert_no_claim("a denied first read claims nothing")

    def test_claim_fields_of_the_wrong_kind_deny_with_the_unreadable_claim(self) -> None:
        reason = UNREADABLE_CLAIM
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
        self.assert_no_claim()


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
        self.assert_no_claim()

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
        # The hook process finds its runs under the system temporary directory, and its claims in the runtime
        # directory, here both this test's own unless the test names another.
        temporary = str(self.root)
        variables = dict.fromkeys(("TMPDIR", "TEMP", "TMP", "XDG_RUNTIME_DIR"), temporary)
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT_DIRECTORY / "review_guard.py")],
            input=stdin.encode("utf-8") if isinstance(stdin, str) else stdin,
            capture_output=True,
            env={**os.environ, **variables, **environment},
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
        self.assertTrue((private_folder.location(CLAIMS_NAME, self.root) / f"{AGENT}.json").is_file())
        outside = {**event, "tool_input": {"file_path": str(self.checkout / "app" / "x.cs")}}
        decision = json.loads(self.hook(json.dumps(outside)).stdout)["hookSpecificOutput"]
        self.assertEqual(("PreToolUse", "deny"), (decision["hookEventName"], decision["permissionDecision"]))
        self.assertIn("may only look inside your own review run", decision["permissionDecisionReason"])
        self.assertEqual("deny", json.loads(self.hook("not json").stdout)["hookSpecificOutput"]["permissionDecision"])

    @unittest.skipIf(sys.platform == "win32", "Windows has no user ids, so its runtime directory is never read")
    def test_the_hook_keeps_claims_in_the_runtime_directory_where_one_is_set_and_else_the_temporary_one(self) -> None:
        event = json.dumps(
            {
                "tool_name": "Read",
                "tool_input": {"file_path": self.prompt()},
                "cwd": str(self.outside_cwd),
                "agent_id": AGENT,
            }
        )
        runtime = self.root / "runtime"
        runtime.mkdir(mode=0o700)
        self.assertEqual(b"", self.hook(event, XDG_RUNTIME_DIR=str(runtime)).stdout)
        self.assertTrue((private_folder.location(CLAIMS_NAME, runtime) / f"{AGENT}.json").is_file())
        self.assertFalse(private_folder.location(CLAIMS_NAME, self.root).exists())
        # A relative runtime directory is never resolved against the working directory: the temporary one serves.
        other = json.dumps({**json.loads(event), "agent_id": OTHER_AGENT})
        self.assertEqual(b"", self.hook(other, XDG_RUNTIME_DIR="runtime").stdout)
        self.assertTrue((private_folder.location(CLAIMS_NAME, self.root) / f"{OTHER_AGENT}.json").is_file())

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

    def test_an_event_holding_a_lone_surrogate_is_denied_wherever_its_path_leads(self) -> None:
        # A `\udc80` escape is a lone surrogate, which Claude Code's runtime opens as U+FFFD, so the path the guard
        # would judge is not the one opened: the event is refused, and the call denied even inside the snapshot.
        self.claim()
        for path in (self.run_directory / "source" / "app" / "x\udc80.cs", self.checkout / "app" / "x\udc80.cs"):
            with self.subTest(path=path):
                event = {"tool_name": "Read", "tool_input": {"file_path": str(path)}, "agent_id": AGENT}
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    self.assertEqual(0, guard.main(json.dumps(event)))
                decision = json.loads(stdout.getvalue())["hookSpecificOutput"]
                self.assertEqual(
                    ("deny", "Code-review reviewer boundary: the guard could not evaluate this call (JsonRefused)."),
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

    def test_a_deny_reason_json_cannot_carry_still_denies_the_call_with_the_fixed_reason(self) -> None:
        # A path resolved through a link can name a file whose bytes are not UTF-8, which Python holds as a lone
        # surrogate; json_writer refuses one, and the guard must still print a denial rather than end in FAILED.
        event = {"tool_name": "Read", "tool_input": {"file_path": self.prompt()}, "agent_id": AGENT}
        stdout = io.StringIO()
        reason = "Code-review reviewer boundary: /outside/x\udcff is outside it."
        with mock.patch.object(guard, "decide", return_value=reason), contextlib.redirect_stdout(stdout):
            self.assertEqual(0, guard.main(json.dumps(event)))
        self.assertEqual(
            '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", '
            '"permissionDecisionReason": "Code-review reviewer boundary: the guard denied this call for a reason that '
            'holds what JSON cannot carry, such as a path whose name is not valid Unicode."}}\n',
            stdout.getvalue(),
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
        self.assert_no_claim("a denied read claims nothing")
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
