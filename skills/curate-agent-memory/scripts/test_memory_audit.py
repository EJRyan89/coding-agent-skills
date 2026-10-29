from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import memory_audit
import skill_roots
from file_presence import NotAFile
from flat_text import flat_text
from git_client import MISSING_GIT, GitClient, GitError, GitResult


def temporary_store_folds_case() -> bool:
    """Whether the file system holding the temporary directory, where every fixture store lives, compares names
    without regard to case, as NTFS does and a Linux file system does not."""
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / "Probe.md").write_bytes(b"")
        return (Path(directory) / "probe.md").exists()


# The tests that need a store where MEMORY.md and memory.md are one file run where the store is one, and their
# counterparts where it is not.
FOLDS_CASE = temporary_store_folds_case()

# Characters a reader splits a line at: the two a shell does, and the three str.splitlines also does.
LINE_BREAKS = ("\n", "\r", "\u2028", "\u2029", "\x85")


def file_name_breaks() -> tuple[str, ...]:
    """The line breaks the temporary directory's file system takes in a file name: Windows refuses CR and LF."""
    accepted = []
    with tempfile.TemporaryDirectory() as directory:
        for character in LINE_BREAKS:
            try:
                (Path(directory) / f"probe{character}.md").write_bytes(b"")
            except OSError:
                continue
            accepted.append(character)
    return tuple(accepted)


NAME_BREAKS = file_name_breaks()
RESOLVE_LABELS = (
    "REPO ",
    "MEMORY_DIR ",
    "SOURCE ",
    "EXISTS ",
    "REFUSED ",
    "NO_MEMORY_DIR",
    "CANDIDATE ",
    "NOTE ",
    "FAILED ",
)


def one_fact_per_line(test: unittest.TestCase, output: str, prefixes: tuple[str, ...]) -> list[str]:
    """The lines of `output`, after asserting that each starts with a fact's label and holds no other line break."""
    lines = output.split("\n")
    test.assertEqual("", lines.pop(), "output ends with one newline")
    for line in lines:
        test.assertEqual([line], line.splitlines(), "a line holds another line break")
        test.assertTrue(line.startswith(prefixes), line)
    return lines


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def directory_link(link: Path, target: Path) -> Path:
    """A link at `link` to the directory `target`: a junction on Windows, which needs no privilege, else a symbolic
    link. Removing `target` afterwards leaves it dangling."""
    if sys.platform == "win32":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)
    return link


def memory(name: str, body: str, kind: str = "feedback") -> str:
    return f"---\nname: {name}\ndescription: {name} description\nmetadata:\n  type: {kind}\n---\n\n{body}\n"


def run_main(*arguments: str) -> tuple[int, str, str]:
    """Run the command line in process; a usage error's SystemExit becomes its code."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            code = memory_audit.main(list(arguments))
        except SystemExit as exit_:
            if not isinstance(exit_.code, int):
                raise AssertionError(f"exit code {exit_.code!r}") from exit_
            code = exit_.code
    return code, stdout.getvalue(), stderr.getvalue()


class CommandLineContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_usage_errors_exit_2(self) -> None:
        for arguments in (
            ["audit"],
            ["reindex", "--memory-dir"],
            ["delete", "--memory-dir", str(self.root)],
            ["resolve", "--unknown"],
            ["forget"],
        ):
            with self.subTest(arguments=arguments):
                code, stdout, stderr = run_main(*arguments)
                self.assertEqual((2, ""), (code, stdout))
                self.assertIn("usage:", stderr)


class MemoryAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.memory_dir = self.root / "memory"
        self.repo = self.root / "repo"
        self.memory_dir.mkdir()
        self.repo.mkdir()

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def run_audit(self, **options: Any) -> dict[str, Any]:
        return memory_audit.audit(self.memory_dir, self.repo, **options)

    def by_file(self, result: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {entry["file"]: entry for entry in result["memories"]}

    def test_index_consistency_is_reported(self) -> None:
        write(self.memory_dir / "kept.md", memory("kept", "Kept body."))
        write(self.memory_dir / "orphan.md", memory("orphan", "Orphan body."))
        write(
            self.memory_dir / "MEMORY.md",
            "# Memory Index\n\n- [Kept](kept.md) — hook\n- [Gone](gone.md) — hook\n- [Kept again](kept.md) — hook\n"
            f"- [Long line is fine](kept.md) — {'x' * 400}\n",
        )
        result = self.run_audit()
        self.assertEqual(["orphan.md"], result["unindexed_files"])
        self.assertEqual(["gone.md"], result["index"]["missing_files"])
        self.assertEqual(["kept.md"], result["index"]["duplicate_entries"])
        self.assertFalse(result["index"]["over_limit"])
        self.assertFalse(result["index"]["near_limit"])

    if FOLDS_CASE:

        def test_a_case_variant_index_is_not_a_memory(self) -> None:
            for name in ("memory.md", "Memory.md"):
                with self.subTest(name=name):
                    (self.memory_dir / memory_audit.INDEX_NAME).unlink(missing_ok=True)
                    index = write(self.memory_dir / name, "# Memory Index\n\nNotes stay here.\n")
                    required = self.memory_dir / memory_audit.INDEX_NAME
                    self.assertTrue(required.is_file(), "the fixture requires a case-insensitive store")
                    self.assertTrue(required.samefile(index))
                    result = self.run_audit()
                    self.assertTrue(result["index"]["exists"])
                    self.assertEqual(str(required), result["index"]["path"])
                    self.assertEqual(0, result["index"]["entries"])
                    self.assertEqual(3, result["index"]["lines"])
                    self.assertEqual(index.stat().st_size, result["index"]["bytes"])
                    self.assertEqual([], result["memories"])
                    self.assertEqual([], result["unindexed_files"])
                    write(self.memory_dir / "rule.md", memory("rule", "Rule body."))
                    result = self.run_audit()
                    self.assertEqual(["rule.md"], list(self.by_file(result)))
                    self.assertEqual(["rule.md"], result["unindexed_files"])
                    self.assertEqual("rule description", self.by_file(result)["rule.md"]["description"])
                    (self.memory_dir / "rule.md").unlink()
                    index.unlink()

    else:

        def test_a_case_variant_of_the_index_is_a_memory_of_its_own(self) -> None:
            write(self.memory_dir / memory_audit.INDEX_NAME, "# Memory Index\n")
            write(self.memory_dir / "memory.md", memory("variant", "Variant body."))
            result = self.run_audit()
            self.assertTrue(result["index"]["exists"])
            self.assertEqual(str(self.memory_dir / memory_audit.INDEX_NAME), result["index"]["path"])
            self.assertEqual(["memory.md"], list(self.by_file(result)))
            self.assertEqual(["memory.md"], result["unindexed_files"])

    def test_a_dot_prefixed_index_target_marks_the_memory_as_indexed(self) -> None:
        write(self.memory_dir / "rule.md", memory("rule", "Rule body."))
        write(self.memory_dir / "MEMORY.md", "- [Rule](./rule.md) — hook\n")
        result = self.run_audit()
        self.assertEqual([], result["unindexed_files"])
        self.assertTrue(self.by_file(result)["rule.md"]["indexed"])
        self.assertEqual([], result["index"]["missing_files"])
        self.assertEqual([], result["index"]["duplicate_entries"])

    def test_duplicate_index_targets_keep_their_original_spellings_in_the_report(self) -> None:
        write(self.memory_dir / "rule.md", memory("rule", "Rule body."))
        write(
            self.memory_dir / "MEMORY.md",
            "- [Rule](./rule.md) — hook\n- [Rule again](rule.md) — hook\n"
            "- [Gone](./gone.md) — hook\n- [Gone again](gone.md) — hook\n",
        )
        result = self.run_audit()
        self.assertEqual([], result["unindexed_files"])
        self.assertEqual(["./gone.md", "gone.md"], result["index"]["missing_files"])
        self.assertEqual(["./gone.md", "./rule.md", "gone.md", "rule.md"], result["index"]["duplicate_entries"])

    if FOLDS_CASE:

        def test_case_only_index_targets_are_indexed_and_duplicate_on_windows(self) -> None:
            write(self.memory_dir / "rule.md", memory("rule", "Rule body."))
            self.assertTrue((self.memory_dir / "RULE.md").is_file(), "the fixture requires a case-insensitive store")
            write(self.memory_dir / "MEMORY.md", "- [Rule](RULE.md) — hook\n")
            result = self.run_audit()
            self.assertEqual([], result["unindexed_files"])
            self.assertTrue(self.by_file(result)["rule.md"]["indexed"])
            write(self.memory_dir / "MEMORY.md", "- [Rule](./rule.md) — hook\n- [Rule again](RULE.md) — hook\n")
            result = self.run_audit()
            self.assertEqual([], result["unindexed_files"])
            self.assertEqual([], result["index"]["missing_files"])
            self.assertEqual(["./rule.md", "RULE.md"], result["index"]["duplicate_entries"])

    else:

        def test_a_case_only_index_target_names_another_file(self) -> None:
            write(self.memory_dir / "rule.md", memory("rule", "Rule body."))
            write(self.memory_dir / "MEMORY.md", "- [Rule](RULE.md) — hook\n")
            result = self.run_audit()
            self.assertEqual(["rule.md"], result["unindexed_files"])
            self.assertFalse(self.by_file(result)["rule.md"]["indexed"])
            self.assertEqual(["RULE.md"], result["index"]["missing_files"])

    def test_index_line_and_byte_limits_are_both_enforced(self) -> None:
        write(self.memory_dir / "MEMORY.md", "\n".join(f"line {n}" for n in range(250)))
        self.assertTrue(self.run_audit()["index"]["over_limit"])
        write(self.memory_dir / "MEMORY.md", "\n".join("x" * 200 for _ in range(130)))
        index = self.run_audit()["index"]
        self.assertLess(index["lines"], memory_audit.INDEX_LINE_LIMIT)
        self.assertGreater(index["bytes"], memory_audit.INDEX_BYTE_LIMIT)
        self.assertTrue(index["over_limit"])
        write(self.memory_dir / "MEMORY.md", "\n".join(f"line {n}" for n in range(185)))
        index = self.run_audit()["index"]
        self.assertFalse(index["over_limit"])
        self.assertTrue(index["near_limit"])

    def test_frontmatter_fields_including_nested_type_are_read(self) -> None:
        write(self.memory_dir / "one.md", memory("one-slug", "Body.", kind="project"))
        entry = self.by_file(self.run_audit())["one.md"]
        self.assertEqual("one-slug", entry["name"])
        self.assertEqual("one-slug description", entry["description"])
        self.assertEqual("project", entry["type"])

    def test_a_block_scalar_description_is_read_whole(self) -> None:
        write(
            self.memory_dir / "folded.md",
            "---\nname: folded\ndescription: >-\n  First line of the hook\n  continues here.\n"
            "metadata:\n  type: project\n---\n\nBody.\n",
        )
        entry = self.by_file(self.run_audit())["folded.md"]
        self.assertEqual("First line of the hook continues here.", entry["description"])
        self.assertEqual("project", entry["type"])

    def test_links_resolve_by_name_or_file_stem(self) -> None:
        write(self.memory_dir / "first.md", memory("first-slug", "See [[second]], [[first-slug]] and [[missing-one]]."))
        write(self.memory_dir / "second.md", memory("other-slug", "Second."))
        entry = self.by_file(self.run_audit())["first.md"]
        self.assertEqual(["first-slug", "missing-one", "second"], entry["links"])
        self.assertEqual(["missing-one"], entry["broken_links"])

    def test_cited_paths_are_checked_and_identifiers_ignored(self) -> None:
        write(self.repo / "docs" / "guide.md", "Guide.\n")
        write(
            self.memory_dir / "paths.md",
            memory(
                "paths",
                "Read `docs/guide.md:12` and `docs/removed.md`, not `owner/repo` or `https://x.test/a.md` "
                "or `skills/<name>/SKILL.md`.",
            ),
        )
        cited = {item["path"]: item["exists"] for item in self.by_file(self.run_audit())["paths.md"]["cited_paths"]}
        self.assertEqual({"docs/guide.md": True, "docs/removed.md": False}, cited)

    def test_overlaps_point_at_matching_destination_text(self) -> None:
        write(
            self.repo / "docs" / "guidelines.md",
            "# Tests\n\n- Name the object under test after its class, never the generic sut variable name.\n\n"
            "- Unrelated rule about database migration folders and release versions.\n",
        )
        write(
            self.memory_dir / "sut.md",
            memory("sut", "Never name the object under test sut; derive the variable name from its class."),
        )
        overlaps = self.by_file(self.run_audit())["sut.md"]["overlaps"]
        self.assertEqual(1, len(overlaps))
        self.assertTrue(overlaps[0]["file"].endswith("guidelines.md"))
        self.assertEqual(3, overlaps[0]["line"])

    def test_large_unrelated_blocks_do_not_register_as_overlaps(self) -> None:
        glossary = " ".join(f"term{n} means something specific here" for n in range(200))
        write(self.repo / "docs" / "glossary.md", f"{glossary} object test class variable name\n")
        write(
            self.memory_dir / "sut.md",
            memory("sut", "Never name the object under test sut; derive the variable name from its class."),
        )
        self.assertEqual([], self.by_file(self.run_audit())["sut.md"]["overlaps"])

    def test_extra_instruction_files_are_searched(self) -> None:
        instructions = write(
            self.root / "user" / "CLAUDE.md",
            "- Always prefix local branches with the personal namespace followed by a kebab-case description.\n",
        )
        write(
            self.memory_dir / "branch.md",
            memory("branch", "Prefix local branches with the personal namespace and a kebab-case description."),
        )
        overlaps = memory_audit.audit(self.memory_dir, None, [instructions])["memories"][0]["overlaps"]
        self.assertEqual(str(instructions), overlaps[0]["file"])

    def test_all_instruction_and_guidance_destinations_are_searched(self) -> None:
        rule = "Never name the object under test sut; derive the variable name from its class instead."
        destinations = [
            ".claude/rules/testing/naming.md",
            "CLAUDE.local.md",
            ".claude/CLAUDE.md",
            "README.md",
            "CONTRIBUTING.md",
            "src/AGENTS.md",
            ".github/copilot-instructions.md",
        ]
        write(self.memory_dir / "sut.md", memory("sut", rule))
        for relative in destinations:
            with self.subTest(destination=relative):
                write(self.repo / relative, f"- {rule}\n")
                overlaps = self.by_file(self.run_audit())["sut.md"]["overlaps"]
                self.assertTrue(any(Path(o["file"]).as_posix().endswith(relative) for o in overlaps), overlaps)
                (self.repo / relative).unlink()

    def test_user_level_rules_and_skipped_directories(self) -> None:
        rule = "Never name the object under test sut; derive the variable name from its class instead."
        user_dir = self.root / "user-config"
        write(user_dir / "rules" / "preferences.md", f"- {rule}\n")
        write(self.repo / "node_modules" / "pkg" / "README.md", f"- {rule}\n")
        write(self.memory_dir / "sut.md", memory("sut", rule))
        overlaps = memory_audit.audit(self.memory_dir, self.repo, user_dir=user_dir)["memories"][0]["overlaps"]
        self.assertEqual([str(user_dir / "rules" / "preferences.md")], [o["file"] for o in overlaps])

    def test_age_is_measured_from_modification_time(self) -> None:
        path = write(self.memory_dir / "old.md", memory("old", "Old."))
        os.utime(path, (1_000_000, 1_000_000))
        entry = memory_audit.audit(self.memory_dir, now=1_000_000 + 86400 * 3)["memories"][0]
        self.assertEqual(3.0, entry["age_days"])

    def main_output(self, *arguments: str, environment: dict[str, str] | None = None) -> str:
        captured = io.StringIO()
        with (
            redirect_stdout(captured),
            mock.patch.dict(os.environ, environment or {}),
            mock.patch.object(tempfile, "tempdir", str(self.root / "Temporary Files")),
        ):
            (self.root / "Temporary Files").mkdir(exist_ok=True)
            (self.memory_dir / "MEMORY.md").touch()
            self.assertEqual(0, memory_audit.main(["audit", "--memory-dir", str(self.memory_dir), *arguments]))
        return captured.getvalue()

    def test_command_line_writes_the_report_to_a_new_temporary_file(self) -> None:
        write(self.memory_dir / "one.md", memory("one", "Body."))
        first, second = (self.main_output().strip() for _ in range(2))
        for line in (first, second):
            self.assertRegex(line, r"^REPORT \S")
            path = Path(line.removeprefix("REPORT "))
            self.assertEqual(self.root / "Temporary Files", path.parent)
            report = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(["one.md"], [entry["file"] for entry in report["memories"]])
        self.assertNotEqual(first, second, "a rerun must not overwrite the report the agent is still reading")
        explicit = self.root / "chosen report.json"
        self.assertEqual(f"REPORT {explicit}", self.main_output("--output", str(explicit)).strip())
        self.assertTrue(explicit.is_file())

    def test_an_output_inside_a_skills_directory_is_refused_before_writing(self) -> None:
        write(self.memory_dir / "one.md", memory("one", "Body."))
        skills_root = Path(memory_audit.__file__).resolve().parents[2]
        home = self.root / "home"
        targets = [
            skills_root / "curate-agent-memory" / "report.json",  # beside SKILL.md
            skills_root / "review-prs" / "report.json",
            home / ".claude" / "skills" / "curate-agent-memory" / "report.json",
            home / ".agents" / "skills" / "curate-agent-memory" / "report.json",
        ]
        with mock.patch.object(Path, "home", return_value=home):
            for target in targets:
                self.addCleanup(target.unlink, missing_ok=True)  # if a regression wrote it
                target.parent.mkdir(parents=True, exist_ok=True)
                with self.subTest(target=target):
                    code, stdout, stderr = run_main(
                        "audit", "--memory-dir", str(self.memory_dir), "--output", str(target)
                    )
                    self.assertEqual((1, ""), (code, stderr))
                    self.assertTrue(stdout.startswith(f"FAILED {target} is inside the skills directory "), stdout)
                    self.assertEqual(1, len(stdout.splitlines()), stdout)
                    self.assertFalse(target.exists())

    def test_an_unwritable_output_is_a_failed_line(self) -> None:
        write(self.memory_dir / "MEMORY.md", "")
        write(self.memory_dir / "one.md", memory("one", "Body."))
        target = self.root / "no such folder" / "report.json"
        code, stdout, stderr = run_main("audit", "--memory-dir", str(self.memory_dir), "--output", str(target))
        self.assertEqual((1, ""), (code, stderr))
        self.assertRegex(stdout, rf"\AFAILED cannot write {re.escape(str(target))}: [^\n]+\n\Z")

    def test_the_report_is_two_space_indented_in_its_keys_order_escaped_past_ascii(self) -> None:
        # The agent and the person it reports to read the file as text, so its bytes are fixed.
        write(self.memory_dir / "MEMORY.md", "")
        write(self.memory_dir / "one.md", memory("one", "Body."))
        target = self.root / "report.json"
        instructions = self.root / "CLAUDE.md"
        write(instructions, "- A rule.\n")
        with mock.patch.object(memory_audit, "audit", return_value={"name": "café", "counts": [1]}) as audited:
            code, _, _ = run_main(
                "audit",
                "--memory-dir",
                str(self.memory_dir),
                "--repo",
                str(self.root),
                "--instructions",
                str(instructions),
                "--output",
                str(target),
            )
        self.assertEqual(0, code)
        # The report is the audit of the memory directory, the repository, and the instruction files it was given.
        self.assertEqual((self.memory_dir, self.root, [instructions]), audited.call_args.args)
        # Written in text mode, so each line ends as the platform ends one; read back the same way.
        self.assertEqual(
            '{\n  "name": "caf\\u00e9",\n  "counts": [\n    1\n  ]\n}\n', target.read_text(encoding="utf-8")
        )

    def test_a_report_json_cannot_hold_is_refused_by_name_before_the_output_exists(self) -> None:
        write(self.memory_dir / "MEMORY.md", "")
        write(self.memory_dir / "one.md", memory("one", "Body."))
        target = self.root / "report.json"
        # A memory's text or a path may hold a lone surrogate, which no UTF-8 file can hold.
        for value, refusal in (
            (float("nan"), "the value holds NaN or an infinity, which JSON does not define"),
            ("\udc80", "the value holds a lone surrogate U+DC80, which UTF-8 cannot encode"),
        ):
            with self.subTest(refusal=refusal), mock.patch.object(memory_audit, "audit", return_value={"x": value}):
                self.assertEqual(
                    (1, f"FAILED the audit report cannot be written: {refusal}\n", ""),
                    run_main("audit", "--memory-dir", str(self.memory_dir), "--output", str(target)),
                )
            self.assertFalse(target.exists())

    def test_an_existing_output_is_refused_and_left_alone(self) -> None:
        write(self.memory_dir / "MEMORY.md", "- [one](one.md) — one\n")
        write(self.memory_dir / "one.md", memory("one", "Body."))
        write(self.root / "CLAUDE.md", "- A rule.\n")
        for target in (self.memory_dir / "one.md", self.memory_dir / "MEMORY.md", self.root / "CLAUDE.md"):
            before = target.read_bytes()
            with self.subTest(target=target.name):
                self.assertEqual(
                    (
                        1,
                        f"FAILED {target} already exists, and audit never replaces a file; omit --output to write a "
                        "new temporary file\n",
                        "",
                    ),
                    run_main("audit", "--memory-dir", str(self.memory_dir), "--output", str(target)),
                )
                self.assertEqual(before, target.read_bytes())

    def test_an_output_inside_a_memory_store_is_refused_before_writing(self) -> None:
        write(self.memory_dir / "MEMORY.md", "")
        config = self.root / "config"
        projects = config / "projects"
        for target, inside in (
            (self.memory_dir / "report.json", f"the memory directory {self.memory_dir}"),
            (projects / "C--other" / "memory" / "report.json", f"Claude Code's projects folder {projects}"),
        ):
            target.parent.mkdir(parents=True, exist_ok=True)
            with self.subTest(target=target), mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
                self.assertEqual(
                    (1, f"FAILED {target} is inside {inside}; omit --output to write a new temporary file\n", ""),
                    run_main("audit", "--memory-dir", str(self.memory_dir), "--output", str(target)),
                )
                self.assertFalse(target.exists())

    def test_an_output_that_appears_after_the_check_is_never_replaced(self) -> None:
        write(self.memory_dir / "MEMORY.md", "")
        target = write(self.root / "late.json", "kept\n")
        with mock.patch.object(memory_audit, "output_refusal", return_value=None):
            self.assertEqual(
                (1, f"FAILED {target} already exists, and audit never replaces a file\n", ""),
                run_main("audit", "--memory-dir", str(self.memory_dir), "--output", str(target)),
            )
        self.assertEqual("kept\n", target.read_text(encoding="utf-8"))

    def test_entries_that_are_no_regular_file_are_unreadable_and_the_rest_are_audited(self) -> None:
        write(self.memory_dir / "MEMORY.md", "- [a](a.md) — a\n- [gone](dangling.md) — gone\n")
        write(self.memory_dir / "a.md", memory("a", "Body of a."))
        write(self.memory_dir / "locked.md", memory("locked", "Locked."))
        (self.memory_dir / "sub.md").mkdir()
        write(self.root / "target" / "inner.md", memory("inner", "Inner."))
        directory_link(self.memory_dir / "linked.md", self.root / "target")
        (self.root / "vanished").mkdir()
        directory_link(self.memory_dir / "dangling.md", self.root / "vanished")
        (self.root / "vanished").rmdir()
        code, stdout, stderr = run_main("audit", "--memory-dir", str(self.memory_dir), "--output", str(self.root / "r"))
        self.assertEqual((0, f"REPORT {self.root / 'r'}\n", ""), (code, stdout, stderr))
        report = json.loads((self.root / "r").read_text(encoding="utf-8"))
        self.assertEqual(["a.md", "locked.md"], [entry["file"] for entry in report["memories"]])
        not_regular = [
            {"file": name, "reason": "not a regular file"} for name in ("dangling.md", "linked.md", "sub.md")
        ]
        self.assertEqual(not_regular, report["unreadable"])
        self.assertEqual([], report["index"]["missing_files"], "a dangling link is there, just not a memory")
        original = Path.read_text

        def read_text(path: Path, *arguments: object, **options: object) -> str:
            if path.name == "locked.md":
                raise PermissionError(13, "Access is denied", str(path))
            return original(path, *arguments, **options)  # type: ignore[arg-type]  # forwards read_text's own options

        with mock.patch.object(Path, "read_text", autospec=True, side_effect=read_text):
            result = self.run_audit()
        self.assertEqual(["a.md"], [entry["file"] for entry in result["memories"]])
        self.assertEqual(
            sorted([*not_regular, {"file": "locked.md", "reason": "Access is denied"}], key=lambda e: e["file"]),
            result["unreadable"],
        )

    def test_command_line_searches_the_claude_config_directory_by_default(self) -> None:
        rule = "Never name the object under test sut; derive the variable name from its class instead."
        write(self.memory_dir / "sut.md", memory("sut", rule))
        config = self.root / "Config Dir"
        write(config / "CLAUDE.md", f"- {rule}\n")
        home = self.root / "home"
        write(home / ".claude" / "rules" / "style.md", f"- {rule}\n")
        explicit = self.root / "explicit"
        write(explicit / "CLAUDE.md", f"- {rule}\n")
        cases = (
            ({"CLAUDE_CONFIG_DIR": str(config)}, (), config / "CLAUDE.md"),
            ({"CLAUDE_CONFIG_DIR": ""}, (), home / ".claude" / "rules" / "style.md"),
            ({"CLAUDE_CONFIG_DIR": str(config)}, ("--user-dir", str(explicit)), explicit / "CLAUDE.md"),
        )
        for environment, arguments, expected in cases:
            with (
                self.subTest(environment=environment, arguments=arguments),
                mock.patch.object(Path, "home", return_value=home),
            ):
                line = self.main_output(*arguments, environment=environment).strip()
                report = json.loads(Path(line.removeprefix("REPORT ")).read_text(encoding="utf-8"))
                self.assertEqual([str(expected)], [o["file"] for o in report["memories"][0]["overlaps"]])


class ReindexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.memory_dir = Path(self._temporary.name) / "memory"
        self.memory_dir.mkdir()
        self.index = self.memory_dir / "MEMORY.md"
        self.index.write_bytes(b"")

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def reindex(self, *extra: str, code: int = 0) -> list[str]:
        result = run_main("reindex", "--memory-dir", str(self.memory_dir), *extra)
        self.assertEqual((code, ""), (result[0], result[2]), result[1])
        return result[1].splitlines()

    def test_the_index_reads_as_utf8_keeping_non_ascii_text_and_replacing_an_undecodable_byte(self) -> None:
        self.index.write_bytes("# Café notes 設計\n".encode() + b"Kept \xff line\n")
        self.assertEqual("# Café notes 設計\nKept � line\n", memory_audit.reindex(self.memory_dir)["content"])

    def test_no_index_reads_as_none_and_a_directory_in_its_place_raises(self) -> None:
        self.index.unlink()
        self.assertEqual("", memory_audit.reindex(self.memory_dir)["content"])
        self.index.mkdir()
        with self.assertRaises(NotAFile):
            memory_audit.reindex(self.memory_dir)

    def snapshot(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in self.memory_dir.iterdir() if path.name != "MEMORY.md"}

    def test_index_is_rebuilt_in_place_from_frontmatter(self) -> None:
        write(self.memory_dir / "kept.md", memory("kept", "Body."))
        write(self.memory_dir / "manual.md", "---\nname: manual\n---\n\nBody line.\n")
        write(self.memory_dir / "plain.md", "# Plain heading\n\nNo frontmatter here.\n")
        write(self.memory_dir / "bracket.md", "---\nname: Use [x] style\n---\nText.\n")
        write(self.memory_dir / "a-new.md", memory("new one", "New."))
        write(
            self.index,
            "# Memory Index\n\n## Work\n"
            "- [Hand title](kept.md) — stale hook\n"
            "- [Gone](gone.md) — missing file\n"
            "- [Manual](manual.md): hand-written hook\n"
            "- [Again](./kept.md) — duplicate\n\n",
        )
        before = self.snapshot()
        lines = self.reindex("--write")
        self.assertEqual(
            "# Memory Index\n\n## Work\n"
            "- [Hand title](kept.md) — kept description\n"
            "- [Manual](manual.md) — hand-written hook\n"
            "- [new one](a-new.md) — new one description\n"
            "- [Use (x) style](bracket.md) — Text.\n"
            "- [plain](plain.md) — Plain heading\n",
            self.index.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            [
                "INDEX_LINE kept.md",
                "INDEX_LINE manual.md",
                "INDEX_LINE a-new.md",
                "INDEX_LINE bracket.md",
                "INDEX_LINE plain.md",
                "ADDED a-new.md",
                "ADDED bracket.md",
                "ADDED plain.md",
                "DROPPED missing gone.md",
                "DROPPED duplicate ./kept.md",
                f"WROTE {self.index.as_posix()}",
            ],
            lines,
        )
        self.assertEqual(before, self.snapshot(), "memory files must never change")
        self.assertEqual(["UNCHANGED"], self.reindex("--write")[-1:])

    def test_without_write_nothing_changes(self) -> None:
        write(self.memory_dir / "one.md", memory("one", "Body."))
        self.assertEqual(["INDEX_LINE one.md", "ADDED one.md", f"WOULD_WRITE {self.index.as_posix()}"], self.reindex())
        self.assertEqual(b"", self.index.read_bytes())
        self.reindex("--write")
        self.assertEqual("- [one](one.md) — one description\n", self.index.read_text(encoding="utf-8"))
        self.assertEqual(["MEMORY.md", "one.md"], sorted(path.name for path in self.memory_dir.iterdir()))

    def test_an_entry_at_the_old_staging_name_and_the_file_it_links_to_are_left_untouched(self) -> None:
        write(self.memory_dir / "one.md", memory("one", "Body."))
        outside = write(Path(self._temporary.name) / "outside.txt", "outside\n")
        for staged in ("hard link", "file"):
            with self.subTest(staged=staged):
                self.index.write_bytes(b"")
                staging = self.memory_dir / ".MEMORY.md.tmp"
                staging.unlink(missing_ok=True)
                if staged == "hard link":
                    os.link(outside, staging)
                else:
                    write(staging, "kept by the user\n")
                before = staging.read_bytes()
                self.assertEqual(
                    (0, ["INDEX_LINE one.md", "ADDED one.md", f"WROTE {self.index.as_posix()}"]),
                    memory_audit.reindex_lines(self.memory_dir, True),
                )
                self.assertEqual(b"outside\n", outside.read_bytes())
                self.assertEqual(before, staging.read_bytes())
                self.assertFalse(self.index.samefile(outside))
                self.assertEqual("- [one](one.md) — one description\n", self.index.read_text(encoding="utf-8"))
                self.assertEqual(
                    [".MEMORY.md.tmp", "MEMORY.md", "one.md"], sorted(path.name for path in self.memory_dir.iterdir())
                )

    def test_a_write_that_fails_part_way_leaves_the_index_as_it_was(self) -> None:
        write(self.memory_dir / "one.md", memory("one", "Body."))
        self.index.write_bytes(b"# Kept index\n")
        write_part = os.write

        def part_way(descriptor: int, data: bytes | memoryview) -> int:
            write_part(descriptor, bytes(data[:3]))
            raise OSError(28, "No space left on device")

        with mock.patch.object(os, "write", side_effect=part_way):
            code, lines = memory_audit.reindex_lines(self.memory_dir, True)
        self.assertEqual(
            (1, f"FAILED cannot write {self.index.as_posix()}: No space left on device"), (code, lines[-1])
        )
        self.assertEqual(b"# Kept index\n", self.index.read_bytes())
        self.assertEqual(["MEMORY.md", "one.md"], sorted(path.name for path in self.memory_dir.iterdir()))

    def outside_memory(self) -> Path:
        """A memory file beside the store, which no index entry may bring into it."""
        return write(Path(self._temporary.name) / "outside" / "secret.md", memory("secret", "Secret body."))

    def assert_target_never_read(self, target: str, reason: str) -> None:
        """An entry linking `target` stays as written and is reported unreadable by reindex and audit alike."""
        write(self.memory_dir / "kept.md", memory("kept", "Body."))
        write(self.index, f"- [Secret]({target}) — hand hook\n- [Kept](kept.md) — stale hook\n")
        lines = self.reindex("--write")
        self.assertEqual(
            f"- [Secret]({target}) — hand hook\n- [Kept](kept.md) — kept description\n",
            self.index.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            [
                f"INDEX_LINE {target}",
                "INDEX_LINE kept.md",
                f"UNREADABLE {target}: {reason}",
                f"WROTE {self.index.as_posix()}",
            ],
            lines,
        )
        result = memory_audit.audit(self.memory_dir)
        self.assertEqual([{"file": target, "reason": reason}], result["unreadable"])
        self.assertEqual([], result["index"]["missing_files"])
        self.assertEqual(["kept.md"], [entry["file"] for entry in result["memories"]])

    def test_a_target_with_a_parent_segment_is_never_read(self) -> None:
        self.outside_memory()
        self.assert_target_never_read("../outside/secret.md", "not a file name directly inside the memory directory")

    def test_an_absolute_target_is_never_read(self) -> None:
        secret = self.outside_memory()
        self.assert_target_never_read(secret.as_posix(), "not a file name directly inside the memory directory")

    def test_a_target_through_a_link_or_junction_is_never_followed(self) -> None:
        directory_link(self.memory_dir / "linked", self.outside_memory().parent)
        self.assert_target_never_read("linked/secret.md", "not a file name directly inside the memory directory")

    def test_a_target_that_is_no_markdown_file_is_never_read(self) -> None:
        write(self.memory_dir / "notes.txt", memory("notes", "Body."))
        self.assert_target_never_read("notes.txt", "not a .md file")

    def test_a_target_directly_in_the_store_is_still_read(self) -> None:
        write(self.memory_dir / "kept.md", memory("kept", "Body."))
        write(self.memory_dir / "other.md", memory("other", "Body."))
        write(self.index, "- [Kept](./kept.md) — stale hook\n- [Other](other.md) — stale hook\n")
        self.reindex("--write")
        self.assertEqual(
            "- [Kept](./kept.md) — kept description\n- [Other](other.md) — other description\n",
            self.index.read_text(encoding="utf-8"),
        )

    def test_audit_and_reindex_agree_on_target_identity_and_preserve_the_surviving_link(self) -> None:
        write(self.memory_dir / "rule.md", memory("rule", "Rule body."))
        before = self.snapshot()
        # Case variants of one target are one file only where the store folds case.
        variants = (("./rule.md", "RULE.md"), ("RULE.md", "rule.md")) if FOLDS_CASE else ()
        for first, second in (("rule.md", "./rule.md"), *variants):
            with self.subTest(first=first, second=second):
                original = f"# Index\n\n- [Rule]({first}) — hook\n- [Rule again]({second}) — hook\n"
                write(self.index, original)
                report = memory_audit.audit(self.memory_dir)
                self.assertEqual([], report["unindexed_files"])
                self.assertEqual([], report["index"]["missing_files"])
                self.assertEqual(sorted([first, second]), report["index"]["duplicate_entries"])
                self.assertTrue(report["memories"][0]["indexed"])
                expected = [f"INDEX_LINE {first}", f"DROPPED duplicate {second}"]
                self.assertEqual([*expected, f"WOULD_WRITE {self.index.as_posix()}"], self.reindex())
                self.assertEqual(original, self.index.read_text(encoding="utf-8"))
                self.assertEqual([*expected, f"WROTE {self.index.as_posix()}"], self.reindex("--write"))
                self.assertEqual(
                    f"# Index\n\n- [Rule]({first}) — rule description\n", self.index.read_text(encoding="utf-8")
                )
                report = memory_audit.audit(self.memory_dir)
                self.assertEqual([], report["unindexed_files"])
                self.assertEqual([], report["index"]["missing_files"])
                self.assertEqual([], report["index"]["duplicate_entries"])
                self.assertTrue(report["memories"][0]["indexed"])
                self.assertEqual([f"INDEX_LINE {first}", "UNCHANGED"], self.reindex("--write"))
                self.assertEqual(before, self.snapshot(), "memory files must never change")

    def test_parenthesized_targets_preserve_existing_titles_hooks_and_memories(self) -> None:
        cases = (
            ("decision(cache).md", "decision(cache).md"),
            ("decision(cache).md", r"decision\(cache\).md"),
            ("decision(cache(inner)).md", "./decision(cache(inner)).md"),
            ("decision(cache(inner)).md", r"decision\(cache(inner)\).md"),
            ("decision)cache.md", r"decision\)cache.md"),
            ("decision(cache.md", r"decision\(cache.md"),
        )
        for file, target in cases:
            with self.subTest(target=target):
                path = write(self.memory_dir / file, "---\nname: derived title\n---\n\nDerived body hook.\n")
                original = f"# Index\n\n- [Chosen title]({target}) — Keep this hook (and this detail)\n"
                write(self.index, original)
                before = self.snapshot()
                report = memory_audit.audit(self.memory_dir)
                self.assertEqual([], report["unindexed_files"])
                self.assertEqual([], report["index"]["missing_files"])
                self.assertEqual([], report["index"]["duplicate_entries"])
                self.assertEqual(1, report["index"]["entries"])
                self.assertTrue(report["memories"][0]["indexed"])
                self.assertEqual(original, memory_audit.reindex(self.memory_dir)["content"])
                for extra in ((), ("--write",), ("--write",)):
                    self.assertEqual([f"INDEX_LINE {target}", "UNCHANGED"], self.reindex(*extra))
                    self.assertEqual(original.encode("utf-8"), self.index.read_bytes())
                    self.assertEqual(before, self.snapshot(), "memory files must never change")
                path.unlink()

    def test_escaped_and_balanced_targets_share_identity_but_keep_report_spellings(self) -> None:
        file = "decision(cache).md"
        write(self.memory_dir / file, memory("decision-cache", "Body."))
        # The second spelling also differs in case only where the store folds case.
        second = "DECISION(cache).md" if FOLDS_CASE else "decision(cache).md"
        first, gone = r"./decision\(cache\).md", r"gone\(cache\).md"
        original = f"- [Chosen title]({first}) — stale hook\n- [Duplicate]({second}) — hook\n- [Gone]({gone})\n"
        write(self.index, original)
        before = self.snapshot()
        report = memory_audit.audit(self.memory_dir)
        self.assertEqual([], report["unindexed_files"])
        self.assertEqual([gone], report["index"]["missing_files"])
        self.assertEqual(sorted([first, second]), report["index"]["duplicate_entries"])
        expected = [f"INDEX_LINE {first}", f"DROPPED duplicate {second}", f"DROPPED missing {gone}"]
        self.assertEqual([*expected, f"WOULD_WRITE {self.index.as_posix()}"], self.reindex())
        self.assertEqual(original.encode("utf-8"), self.index.read_bytes())
        self.assertEqual([*expected, f"WROTE {self.index.as_posix()}"], self.reindex("--write"))
        self.assertEqual(
            f"- [Chosen title]({first}) — decision-cache description\n", self.index.read_text(encoding="utf-8")
        )
        self.assertEqual([f"INDEX_LINE {first}", "UNCHANGED"], self.reindex("--write"))
        self.assertEqual([], memory_audit.audit(self.memory_dir)["index"]["missing_files"])
        self.assertEqual(before, self.snapshot(), "memory files must never change")

    def test_generated_parenthesized_targets_round_trip_through_audit_and_reindex(self) -> None:
        for file, target in (
            ("decision(cache).md", r"decision\(cache\).md"),
            ("decision(cache(inner)).md", r"decision\(cache\(inner\)\).md"),
            ("decision(cache.md", r"decision\(cache.md"),
            ("decision)cache.md", r"decision\)cache.md"),
            ("decision (cache).md", r"decision \(cache\).md"),
        ):
            with self.subTest(file=file):
                path = write(self.memory_dir / file, "---\nname: Chosen title\n---\n\nBody hook.\n")
                write(self.index, "")
                before = self.snapshot()
                expected = ["INDEX_LINE " + file, "ADDED " + file]
                self.assertEqual([*expected, f"WOULD_WRITE {self.index.as_posix()}"], self.reindex())
                self.assertEqual(b"", self.index.read_bytes())
                self.assertEqual([*expected, f"WROTE {self.index.as_posix()}"], self.reindex("--write"))
                self.assertEqual(f"- [Chosen title]({target}) — Body hook.\n", self.index.read_text(encoding="utf-8"))
                report = memory_audit.audit(self.memory_dir)
                self.assertEqual([], report["unindexed_files"])
                self.assertEqual([], report["index"]["missing_files"])
                self.assertTrue(report["memories"][0]["indexed"])
                self.assertEqual([f"INDEX_LINE {target}", "UNCHANGED"], self.reindex("--write"))
                self.assertEqual(before, self.snapshot(), "memory files must never change")
                path.unlink()

    def test_target_escape_parity_and_incomplete_links_are_not_misparsed(self) -> None:
        for target, decoded in (
            (r"folder\decision\(cache\).md", r"folder\decision(cache).md"),
            (r"folder\\decision(cache).md", r"folder\decision(cache).md"),
            (r"decision\_cache.md", "decision_cache.md"),
            (r"folder\\\(cache\).md", r"folder\(cache).md"),
        ):
            with self.subTest(target=target):
                write(self.index, f"- [Title]({target}) — hook (detail)\n")
                entries, _ = memory_audit.parse_index(self.index)
                self.assertEqual(1, len(entries))
                self.assertEqual(target, entries[0]["file"])
                self.assertEqual(decoded, entries[0]["target"])
                self.assertEqual("hook (detail)", entries[0]["hook"])
                write(self.index, memory_audit.index_line("Title", decoded, "hook"))
                generated, _ = memory_audit.parse_index(self.index)
                self.assertEqual(decoded, generated[0]["target"])
        for line in (
            "- [Unclosed](decision(cache.md) — hook",
            r"- [Escaped closing delimiter](decision.md\)",
            "- [Empty]() — hook",
            "- [Not a link] decision(cache).md",
        ):
            with self.subTest(line=line):
                write(self.index, line + "\n")
                self.assertEqual([], memory_audit.parse_index(self.index)[0])
                self.assertEqual(line + "\n", memory_audit.reindex(self.memory_dir)["content"])
                self.assertEqual(["UNCHANGED"], self.reindex("--write"))

    def test_an_empty_index_without_memories_is_unchanged(self) -> None:
        self.assertEqual(["UNCHANGED"], self.reindex("--write"))
        self.assertEqual(["MEMORY.md"], [path.name for path in self.memory_dir.iterdir()])
        self.assertEqual(b"", self.index.read_bytes())

    if FOLDS_CASE:

        def test_a_case_variant_index_without_memories_never_gets_a_self_entry(self) -> None:
            self.index.unlink()
            for name in ("memory.md", "Memory.md"):
                with self.subTest(name=name):
                    self.index.unlink(missing_ok=True)
                    index = write(self.memory_dir / name, "")
                    self.assertTrue(self.index.is_file(), "the fixture requires a case-insensitive store")
                    self.assertTrue(self.index.samefile(index))
                    for extra in ((), ("--write",), ("--write",)):
                        self.assertEqual(["UNCHANGED"], self.reindex(*extra))
                        self.assertEqual([], memory_audit.audit(self.memory_dir)["memories"])
                        self.assertEqual(b"", index.read_bytes())
                        self.assertEqual([name], [path.name for path in self.memory_dir.iterdir()])
                    index.unlink()

    if FOLDS_CASE:

        def test_a_case_variant_index_rebuild_preserves_memories_and_never_adds_itself(self) -> None:
            self.index.unlink()
            rule = write(self.memory_dir / "rule.md", memory("rule", "Rule body."))
            before = rule.read_bytes()
            for name in ("memory.md", "Memory.md"):
                with self.subTest(name=name):
                    self.index.unlink(missing_ok=True)
                    index = write(self.memory_dir / name, "# Memory Index\n\nNotes stay here.\n")
                    original = index.read_bytes()
                    self.assertTrue(self.index.is_file(), "the fixture requires a case-insensitive store")
                    self.assertTrue(self.index.samefile(index))
                    expected = ["INDEX_LINE rule.md", "ADDED rule.md"]
                    self.assertEqual([*expected, f"WOULD_WRITE {self.index.as_posix()}"], self.reindex())
                    self.assertEqual(original, index.read_bytes(), "preview must leave the index unchanged")
                    report = memory_audit.audit(self.memory_dir)
                    self.assertEqual(["rule.md"], [entry["file"] for entry in report["memories"]])
                    self.assertEqual(["rule.md"], report["unindexed_files"])
                    self.assertEqual([*expected, f"WROTE {self.index.as_posix()}"], self.reindex("--write"))
                    self.assertEqual(
                        "# Memory Index\n\nNotes stay here.\n- [rule](rule.md) — rule description\n",
                        self.index.read_text(encoding="utf-8"),
                    )
                    report = memory_audit.audit(self.memory_dir)
                    self.assertEqual(["rule.md"], [entry["file"] for entry in report["memories"]])
                    self.assertEqual([], report["unindexed_files"])
                    self.assertTrue(report["memories"][0]["indexed"])
                    self.assertEqual(1, report["index"]["entries"])
                    self.assertEqual([], report["index"]["missing_files"])
                    self.assertEqual([], report["index"]["duplicate_entries"])
                    self.assertEqual(["INDEX_LINE rule.md", "UNCHANGED"], self.reindex("--write"))
                    self.assertEqual(before, rule.read_bytes(), "memory files must never change")
                    self.assertEqual(["rule.md"], memory_audit.reindex(self.memory_dir)["entries"])
                    self.assertEqual(2, len(list(self.memory_dir.iterdir())), "no temporary file may remain")
                    self.index.unlink()

    def test_a_block_scalar_description_becomes_the_whole_hook(self) -> None:
        write(
            self.memory_dir / "folded.md",
            "---\nname: folded\ndescription: >-\n  First line of the hook\n  continues here.\n---\n\nBody.\n",
        )
        self.reindex("--write")
        self.assertEqual(
            "- [folded](folded.md) — First line of the hook continues here.\n", self.index.read_text(encoding="utf-8")
        )

    def test_derived_titles_escape_brackets_before_shared_flattening_without_truncation(self) -> None:
        title = " \tUse [café]\u00a0[設計]\u2003" + "x" * 151
        write(self.memory_dir / "bracket.md", f"---\nname: {title}\n---\nBody.\n")
        write(self.memory_dir / "stem [title].md", "Body.\n")
        before = self.snapshot()
        with mock.patch.object(memory_audit, "flat_text", wraps=flat_text) as flatten:
            result = memory_audit.reindex(self.memory_dir)
        self.assertEqual(
            f"- [Use (café) (設計) {'x' * 151}](bracket.md) — Body.\n- [stem (title)](stem [title].md) — Body.\n",
            result["content"],
        )
        flatten.assert_any_call(title.strip().replace("[", "(").replace("]", ")"))
        flatten.assert_any_call("stem (title)")
        self.assertEqual(before, self.snapshot())
        self.assertEqual(b"", self.index.read_bytes())

    def test_long_descriptions_become_one_short_line(self) -> None:
        description = "word " * 60
        write(self.memory_dir / "long.md", f"---\nname: long\ndescription: {description}\n---\nBody.\n")
        self.reindex("--write")
        hook = self.index.read_text(encoding="utf-8").split(" — ", 1)[1].rstrip("\n")
        self.assertLessEqual(len(hook), 150)
        self.assertTrue(hook.endswith("word..."), hook)

    def test_a_long_block_scalar_description_is_cut_like_any_other(self) -> None:
        lines = "".join(f"  line {number} of a long literal hook\n" for number in range(12))
        write(self.memory_dir / "literal.md", f"---\nname: literal\ndescription: |\n{lines}---\nBody.\n")
        self.reindex("--write")
        hook = self.index.read_text(encoding="utf-8").split(" — ", 1)[1].rstrip("\n")
        self.assertTrue(hook.startswith("line 0 of a long literal hook line 1 of"), hook)
        self.assertLessEqual(len(hook), 150)
        self.assertTrue(hook.endswith("..."), hook)
        self.assertNotIn("line 11", hook)

    def test_index_size_against_the_load_limit_is_reported(self) -> None:
        for number in range(181):
            write(self.memory_dir / f"m{number:03}.md", memory(f"m{number}", "Body."))
        self.assertIn("NEAR_LIMIT lines=181/200 bytes=", "\n".join(self.reindex()))
        for number in range(181, 201):
            write(self.memory_dir / f"m{number:03}.md", memory(f"m{number}", "Body."))
        lines = self.reindex("--write", code=1)
        self.assertIn("OVER_LIMIT lines=201/200 bytes=", "\n".join(lines))
        self.assertEqual(f"WROTE {self.index.as_posix()}", lines[-1], "an index over the limit is still written")

    def test_a_failed_write_is_the_last_line_and_leaves_no_temporary_file(self) -> None:
        write(self.memory_dir / "one.md", memory("one", "Body."))
        with mock.patch.object(os, "replace", side_effect=PermissionError(13, "Access is denied")):
            lines = self.reindex("--write", code=1)
        self.assertEqual(
            ["INDEX_LINE one.md", "ADDED one.md", f"FAILED cannot write {self.index.as_posix()}: Access is denied"],
            lines,
        )
        self.assertEqual(["MEMORY.md", "one.md"], sorted(path.name for path in self.memory_dir.iterdir()))
        self.assertEqual(b"", self.index.read_bytes())

    def test_an_unreadable_memory_is_a_failed_line(self) -> None:
        write(self.memory_dir / "one.md", memory("one", "Body."))
        with mock.patch.object(Path, "read_text", side_effect=PermissionError(13, "Access is denied", "one.md")):
            self.assertEqual(["FAILED Access is denied: one.md"], self.reindex(code=1))

    def test_entries_that_are_no_regular_file_stay_as_written_and_are_never_added(self) -> None:
        root = self.memory_dir.parent
        self.index.write_text("- [Kept](dangling.md) — kept as written\n", encoding="utf-8")
        write(self.memory_dir / "a.md", memory("a", "Body."))
        (self.memory_dir / "sub.md").mkdir()
        write(root / "target" / "inner.md", memory("inner", "Inner."))
        directory_link(self.memory_dir / "linked.md", root / "target")
        (root / "vanished").mkdir()
        directory_link(self.memory_dir / "dangling.md", root / "vanished")
        (root / "vanished").rmdir()
        self.assertEqual(
            [
                "INDEX_LINE dangling.md",
                "INDEX_LINE a.md",
                "ADDED a.md",
                "UNREADABLE dangling.md: not a regular file",
                "UNREADABLE linked.md: not a regular file",
                "UNREADABLE sub.md: not a regular file",
                f"WROTE {self.index.as_posix()}",
            ],
            self.reindex("--write"),
        )
        self.assertEqual(
            "- [Kept](dangling.md) — kept as written\n- [a](a.md) — a description\n",
            self.index.read_text(encoding="utf-8"),
        )

    def test_a_memory_named_with_a_line_break_is_refused_and_nothing_is_written(self) -> None:
        write(self.memory_dir / "one.md", memory("one", "Body."))
        for character in NAME_BREAKS:
            name = f"bad{character}ADDED forged.md"
            with self.subTest(character=repr(character)):
                write(self.memory_dir / name, memory("bad", "Body."))
                code, output, error = run_main("reindex", "--memory-dir", str(self.memory_dir), "--write")
                self.assertEqual((1, ""), (code, error))
                self.assertEqual(
                    [f"FAILED a memory file name holds a line break, so no entry can link it: {name!r}"],
                    one_fact_per_line(self, output, ("FAILED ",)),
                )
                self.assertEqual(b"", self.index.read_bytes())
                (self.memory_dir / name).unlink()

    def test_printed_file_names_are_flattened_and_entries_keep_the_exact_target(self) -> None:
        write(self.memory_dir / "two  spaces.md", memory("two  spaces", "Body."))
        write(self.index, "- [Gone](gone  twice.md) — missing\n")
        code, output, error = run_main("reindex", "--memory-dir", str(self.memory_dir), "--write")
        self.assertEqual((0, ""), (code, error))
        self.assertEqual(
            [
                "INDEX_LINE two spaces.md",
                "ADDED two spaces.md",
                "DROPPED missing gone twice.md",
                f"WROTE {self.index.as_posix()}",
            ],
            one_fact_per_line(self, output, ("INDEX_LINE ", "ADDED ", "DROPPED ", "WROTE ")),
        )
        self.assertEqual(
            "- [two spaces](two  spaces.md) — two spaces description\n", self.index.read_text(encoding="utf-8")
        )


class DeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.memory_dir = self.root / "memory"
        write(self.memory_dir / "keep.md", memory("keep", "Keep."))
        write(self.memory_dir / "gone.md", memory("gone", "Gone."))
        write(self.memory_dir / "other.md", memory("other", "Other."))
        write(self.memory_dir / "MEMORY.md", "- [keep](keep.md) — keep description\n")
        write(self.memory_dir / "sub" / "inner.md", memory("inner", "Inner."))
        write(self.memory_dir / "notes.txt", "Notes.\n")
        write(self.root / "outside.md", memory("outside", "Outside."))

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def delete(self, *names: str) -> tuple[int, list[str]]:
        captured = io.StringIO()
        with redirect_stdout(captured):
            code = memory_audit.main(["delete", "--memory-dir", str(self.memory_dir), *names])
        return code, captured.getvalue().splitlines()

    def snapshot(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }

    def test_named_memories_are_deleted(self) -> None:
        index = (self.memory_dir / "MEMORY.md").read_bytes()
        self.assertEqual((0, ["DELETED gone.md", "DELETED other.md"]), self.delete("gone.md", "other.md"))
        self.assertEqual(
            ["memory/MEMORY.md", "memory/keep.md", "memory/notes.txt", "memory/sub/inner.md", "outside.md"],
            sorted(self.snapshot()),
        )
        self.assertEqual(index, (self.memory_dir / "MEMORY.md").read_bytes())

    def test_any_refused_name_deletes_nothing(self) -> None:
        refusals = {
            "../outside.md": "FAILED ../outside.md: not a file name directly inside the memory directory",
            "..\\outside.md": "FAILED ..\\outside.md: not a file name directly inside the memory directory",
            "..": "FAILED ..: not a file name directly inside the memory directory",
            str(
                self.root / "outside.md"
            ): f"FAILED {self.root / 'outside.md'}: not a file name directly inside the memory directory",
            "sub/inner.md": "FAILED sub/inner.md: not a file name directly inside the memory directory",
            "sub": "FAILED sub: not a .md file",
            "notes.txt": "FAILED notes.txt: not a .md file",
            "MEMORY.md": "FAILED MEMORY.md: the index is rebuilt with reindex, never deleted",
            "memory.md": "FAILED memory.md: the index is rebuilt with reindex, never deleted",
            "absent.md": "FAILED absent.md: no such file",
        }
        before = self.snapshot()
        for name, failure in refusals.items():
            with self.subTest(name=name):
                self.assertEqual((1, [failure]), self.delete(name))
                self.assertEqual((1, [failure]), self.delete("keep.md", name, "gone.md"))
                self.assertEqual(before, self.snapshot())

    def test_every_refusal_is_reported(self) -> None:
        before = self.snapshot()
        self.assertEqual(
            (
                1,
                [
                    "FAILED MEMORY.md: the index is rebuilt with reindex, never deleted",
                    "FAILED absent.md: no such file",
                ],
            ),
            self.delete("gone.md", "MEMORY.md", "absent.md"),
        )
        self.assertEqual(before, self.snapshot())

    def test_a_name_given_twice_is_refused(self) -> None:
        before = self.snapshot()
        self.assertEqual((1, ["FAILED KEEP.md: named more than once"]), self.delete("keep.md", "KEEP.md"))
        self.assertEqual(before, self.snapshot())

    def test_a_subdirectory_named_like_a_memory_is_refused(self) -> None:
        (self.memory_dir / "folder.md").mkdir()
        before = self.snapshot()
        self.assertEqual((1, ["FAILED folder.md: not a regular file"]), self.delete("folder.md", "gone.md"))
        self.assertEqual(before, self.snapshot())
        self.assertTrue((self.memory_dir / "folder.md").is_dir())

    def test_a_link_is_never_followed(self) -> None:
        target = self.root / "target"
        write(target / "precious.md", memory("precious", "Precious."))
        link = directory_link(self.memory_dir / "linked.md", target)
        self.assertEqual((1, ["FAILED linked.md: not a regular file"]), self.delete("linked.md", "gone.md"))
        self.assertTrue((target / "precious.md").is_file())
        self.assertTrue((self.memory_dir / "gone.md").is_file())
        self.assertTrue(link.exists())

    def test_a_failure_mid_way_reports_what_was_already_deleted(self) -> None:
        unlink = Path.unlink

        def failing(path: Path, missing_ok: bool = False) -> None:
            if path.name == "other.md":
                raise PermissionError(13, "Access is denied")
            unlink(path, missing_ok)

        with mock.patch.object(Path, "unlink", autospec=True, side_effect=failing):
            self.assertEqual(
                (1, ["DELETED gone.md", "FAILED other.md: Access is denied"]),
                self.delete("gone.md", "other.md", "keep.md"),
            )
        self.assertEqual(
            [
                "memory/MEMORY.md",
                "memory/keep.md",
                "memory/notes.txt",
                "memory/other.md",
                "memory/sub/inner.md",
                "outside.md",
            ],
            sorted(self.snapshot()),
        )

    def test_a_name_holding_a_line_break_is_refused_on_one_line(self) -> None:
        for character in LINE_BREAKS:
            name = f"gone{character}DELETED keep.md"
            with self.subTest(character=repr(character)):
                if character in NAME_BREAKS:
                    write(self.memory_dir / name, memory("bad", "Bad."))
                before = self.snapshot()
                code, output, error = run_main("delete", "--memory-dir", str(self.memory_dir), name, "gone.md")
                self.assertEqual((1, ""), (code, error))
                self.assertEqual(
                    [
                        "FAILED gone DELETED keep.md: the name holds a line break, so no line can name it; "
                        "rename the file first"
                    ],
                    one_fact_per_line(self, output, ("FAILED ",)),
                )
                self.assertEqual(before, self.snapshot())

    def test_deleted_and_failed_names_are_flattened(self) -> None:
        write(self.memory_dir / "two  spaces.md", memory("two", "Two."))
        code, output, error = run_main("delete", "--memory-dir", str(self.memory_dir), "two  spaces.md")
        self.assertEqual(
            (0, ["DELETED two spaces.md"], ""), (code, one_fact_per_line(self, output, ("DELETED ",)), error)
        )
        self.assertFalse((self.memory_dir / "two  spaces.md").exists())
        with mock.patch.object(Path, "unlink", side_effect=PermissionError(13, " Access\r\n is\u2028denied ")):
            code, output, error = run_main("delete", "--memory-dir", str(self.memory_dir), "gone.md")
        self.assertEqual(
            (1, ["FAILED gone.md: Access is denied"], ""), (code, one_fact_per_line(self, output, ("FAILED ",)), error)
        )


class MemoryStoreGuardTests(unittest.TestCase):
    """audit, reindex, and delete take only a directory that is a memory store, never one inside a skills directory."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.home = self.root / "home"
        self.skills_root = self.root / "source skills"

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def populate(self, directory: Path, index: bool = True) -> dict[str, bytes]:
        write(directory / "SKILL.md", "---\nname: some-skill\ndescription: d\n---\nBody.\n")
        write(directory / "notes.md", memory("notes", "Notes."))
        if index:
            write(directory / "MEMORY.md", "- [notes](notes.md) — stale hook\n")
        return self.snapshot()

    def snapshot(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }

    def run_all(self, directory: Path) -> list[tuple[int, str, str]]:
        (self.root / "reports").mkdir(exist_ok=True)
        with (
            mock.patch.object(Path, "home", return_value=self.home),
            mock.patch.object(skill_roots, "SKILLS_ROOT", self.skills_root),
            mock.patch.object(tempfile, "tempdir", str(self.root / "reports")),
        ):
            return [
                run_main("audit", "--memory-dir", str(directory), "--user-dir", str(self.home)),
                run_main("reindex", "--memory-dir", str(directory), "--write"),
                run_main("delete", "--memory-dir", str(directory), "SKILL.md", "notes.md"),
            ]

    def test_a_directory_without_an_index_is_refused_and_left_alone(self) -> None:
        directory = self.root / "plain folder"
        before = self.populate(directory, index=False)
        failure = f"FAILED {directory} is not a memory directory: it holds no MEMORY.md\n"
        self.assertEqual([(1, failure, "")] * 3, self.run_all(directory))
        self.assertEqual(before, self.snapshot())

    def test_a_directory_in_the_index_place_is_refused_as_an_unreadable_index_and_left_alone(self) -> None:
        directory = self.root / "store"
        before = self.populate(directory, index=False)
        (directory / "MEMORY.md").mkdir()
        failure = f"FAILED {directory} is not a memory directory: its MEMORY.md cannot be read: not a file\n"
        self.assertEqual([(1, failure, "")] * 3, self.run_all(directory))
        self.assertEqual(before, self.snapshot())
        self.assertTrue((directory / "MEMORY.md").is_dir())

    def test_parse_index_raises_for_a_directory_in_the_index_place_and_reads_no_index_as_empty(self) -> None:
        index = self.root / "MEMORY.md"
        self.assertEqual(([], []), memory_audit.parse_index(index))
        index.mkdir()
        with self.assertRaises(NotAFile):
            memory_audit.parse_index(index)

    def test_a_directory_inside_a_skills_directory_is_refused_and_left_alone(self) -> None:
        for root in (self.home / ".claude" / "skills", self.home / ".agents" / "skills", self.skills_root):
            directory = root / "some-skill"
            with self.subTest(root=root):
                before = self.populate(directory)
                failure = (
                    f"FAILED {directory} is inside the skills directory {root}; "
                    "name the memory directory resolve printed\n"
                )
                self.assertEqual([(1, failure, "")] * 3, self.run_all(directory))
                self.assertEqual(before, self.snapshot())

    def test_a_memory_store_beside_a_skills_directory_is_changed(self) -> None:
        directory = self.root / "projects" / "memory"
        self.populate(directory)
        (directory / "SKILL.md").unlink()
        write(self.skills_root / "some-skill" / "SKILL.md", "---\nname: some-skill\n---\n")
        audited, reindexed, deleted = self.run_all(directory)
        self.assertEqual((0, ""), (audited[0], audited[2]))
        self.assertTrue(audited[1].startswith(f"REPORT {self.root / 'reports'}"), audited[1])
        self.assertEqual((0, f"INDEX_LINE notes.md\nWROTE {(directory / 'MEMORY.md').as_posix()}\n", ""), reindexed)
        self.assertEqual((1, "FAILED SKILL.md: no such file\n", ""), deleted)
        self.assertEqual(
            "- [notes](notes.md) — notes description\n", (directory / "MEMORY.md").read_text(encoding="utf-8")
        )


class ConsoleTests(unittest.TestCase):
    def test_output_a_cp1252_console_cannot_encode_is_written_as_utf_8(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            memory_dir = Path(temporary) / "memory dir"
            write(memory_dir / "MEMORY.md", "")
            write(memory_dir / "arrow → ✓.md", memory("arrow", "Body."))
            result = subprocess.run(
                [sys.executable, "-B", memory_audit.__file__, "reindex", "--memory-dir", str(memory_dir)],
                capture_output=True,
                env={**os.environ, "PYTHONIOENCODING": "cp1252"},
                check=False,
            )
        self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(
            f"INDEX_LINE arrow → ✓.md\nADDED arrow → ✓.md\nWOULD_WRITE {(memory_dir / 'MEMORY.md').as_posix()}\n",
            result.stdout.decode("utf-8").replace("\r\n", "\n"),
        )


def git(*arguments: str) -> None:
    subprocess.run(["git", *arguments], check=True, capture_output=True)


class ResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.managed = self.root / "managed" / "managed-settings.json"
        self.repo.mkdir()
        git("-C", str(self.repo), "init", "-q", "-b", "main")
        git(
            "-C",
            str(self.repo),
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def resolve(self, environment: dict[str, str] | None = None, repo: Path | None = None) -> dict[str, Any]:
        return memory_audit.resolve(repo or self.repo, self.home, environment or {}, self.managed)

    def derived(self, root: Path | None = None) -> Path:
        folder = memory_audit.encode_project(str((root or self.repo).resolve()))
        return self.home / ".claude" / "projects" / folder / "memory"

    def store(self, directory: Path) -> Path:
        """Make directory a memory store, one holding MEMORY.md, as every later command requires."""
        write(directory / "MEMORY.md", "")
        return directory

    def test_default_is_derived_from_the_main_worktree_and_shared_by_worktrees(self) -> None:
        self.derived().mkdir(parents=True)
        worktree = self.root / "wt"
        git("-C", str(self.repo), "worktree", "add", "-q", "-b", "feature", str(worktree))
        for repo in (self.repo, worktree):
            with self.subTest(repo=repo.name):
                result = self.resolve(repo=repo)
                self.assertEqual(str(self.derived()), result["memory_dir"])
                self.assertTrue(result["exists"])
                self.assertIn("main worktree", result["source"])

    def test_missing_derived_directory_lists_candidates(self) -> None:
        other = self.home / ".claude" / "projects" / "C--elsewhere" / "memory"
        other.mkdir(parents=True)
        result = self.resolve()
        self.assertFalse(result["exists"])
        self.assertEqual([str(other)], result["candidates"])

    def test_auto_memory_directory_follows_settings_precedence(self) -> None:
        scopes = {
            "user": self.home / ".claude" / "settings.json",
            "project": self.repo / ".claude" / "settings.json",
            "local": self.repo / ".claude" / "settings.local.json",
            "managed": self.managed,
        }
        for scope, path in scopes.items():
            write(path, json.dumps({"autoMemoryDirectory": f"~/memory-{scope}"}))
            result = self.resolve()
            with self.subTest(scope=scope):
                self.assertEqual(str(self.home / f"memory-{scope}"), result["memory_dir"])
                self.assertIn(f"{scope} settings", result["source"])

    def test_a_missing_settings_named_directory_lists_candidates(self) -> None:
        other = self.home / ".claude" / "projects" / "C--elsewhere" / "memory"
        other.mkdir(parents=True)
        write(self.home / ".claude" / "settings.json", json.dumps({"autoMemoryDirectory": "~/memory-user"}))
        result = self.resolve()
        self.assertEqual(str(self.home / "memory-user"), result["memory_dir"])
        self.assertFalse(result["exists"])
        self.assertEqual([str(other)], result["candidates"])

    def test_an_existing_directory_lists_no_candidates(self) -> None:
        (self.home / ".claude" / "projects" / "C--elsewhere" / "memory").mkdir(parents=True)
        self.store(self.home / "memory-user")
        self.store(self.derived())
        with self.subTest(source="derived"):
            self.assertNotIn("candidates", self.resolve())
        write(self.home / ".claude" / "settings.json", json.dumps({"autoMemoryDirectory": "~/memory-user"}))
        with self.subTest(source="settings"):
            result = self.resolve()
            self.assertTrue(result["exists"])
            self.assertNotIn("candidates", result)

    def test_an_unusable_settings_value_lists_candidates(self) -> None:
        other = self.home / ".claude" / "projects" / "C--elsewhere" / "memory"
        other.mkdir(parents=True)
        write(self.home / ".claude" / "settings.json", json.dumps({"autoMemoryDirectory": "relative/path"}))
        result = self.resolve()
        self.assertIsNone(result["memory_dir"])
        self.assertEqual([str(other)], result["candidates"])

    def test_without_a_managed_settings_file_no_managed_scope_is_read_and_a_note_says_so(self) -> None:
        write(self.managed, json.dumps({"autoMemoryDirectory": "~/memory-managed"}))
        write(self.home / ".claude" / "settings.json", json.dumps({"autoMemoryDirectory": "~/memory-user"}))
        result = memory_audit.resolve(self.repo, self.home, {}, None)
        self.assertEqual(str(self.home / "memory-user"), result["memory_dir"])
        note = (
            "The Program Files folder is not named by an absolute path, so managed settings were not read; one there "
            "can set autoMemoryDirectory."
        )
        self.assertIn(note, result["notes"])
        self.assertNotIn(note, self.resolve()["notes"])

    def test_project_directory_name_and_config_dir_are_honored(self) -> None:
        config = self.root / "config"
        result = self.resolve({"CLAUDE_CONFIG_DIR": str(config), "CLAUDE_CODE_PROJECT_DIR_NAME": "shared"})
        self.assertEqual(str(config / "projects" / "shared" / "memory"), result["memory_dir"])
        self.assertEqual("CLAUDE_CODE_PROJECT_DIR_NAME", result["source"])

    SETTINGS_NOTE = (
        "NOTE A --settings file passed when Claude Code starts can also set autoMemoryDirectory; "
        "this audit cannot see it."
    )
    DERIVED_NOTE = "NOTE The directory name is derived by convention; confirm it before changing anything."

    def resolve_lines(self, *arguments: str, cwd: Path | None = None, code: int = 0) -> list[str]:
        """Run resolve from cwd with no Claude Code environment overrides and the test's managed settings, holding
        its output to one fact per line."""
        with (
            mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.root)}),
            mock.patch.object(Path, "cwd", return_value=cwd or self.repo),
            mock.patch.object(memory_audit, "managed_settings_path", return_value=self.managed),
        ):
            for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_PROJECT_DIR_NAME"):
                os.environ.pop(name, None)
            result = run_main("resolve", "--home", str(self.home), *arguments)
        self.assertEqual((code, ""), (result[0], result[2]), result[1])
        return one_fact_per_line(self, result[1], RESOLVE_LABELS)

    def test_a_settings_value_holding_a_line_break_is_refused_on_one_line(self) -> None:
        user = self.home / ".claude" / "settings.json"
        # The project's settings are named under the repository root git reports, which is the long form of a
        # temporary directory Windows may give by its short name.
        for scope, settings, shown in (
            ("user", user, user),
            ("project", self.repo / ".claude" / "settings.json", self.repo.resolve() / ".claude" / "settings.json"),
        ):
            for character in LINE_BREAKS:
                value = f"~/forged{character}SOURCE autoMemoryDirectory in user settings ({user}){character}EXISTS yes"
                with self.subTest(scope=scope, character=repr(character)):
                    write(settings, json.dumps({"autoMemoryDirectory": value}))
                    self.assertEqual(
                        [
                            f"REPO {self.repo.resolve()}",
                            "NO_MEMORY_DIR",
                            self.SETTINGS_NOTE,
                            f"NOTE autoMemoryDirectory in {shown} holds a line break: {flat_text(repr(value))}",
                        ],
                        self.resolve_lines(),
                    )
            settings.unlink()

    def test_a_directory_holding_a_line_break_resolves_to_none(self) -> None:
        for character in LINE_BREAKS:
            name = f"shared{character}EXISTS yes"
            with self.subTest(character=repr(character)):
                result = self.resolve({"CLAUDE_CODE_PROJECT_DIR_NAME": name})
                lines = one_fact_per_line(
                    self, "\n".join(memory_audit.resolve_lines(self.repo, result)) + "\n", RESOLVE_LABELS
                )
                directory = self.home / ".claude" / "projects" / name / "memory"
                self.assertEqual(
                    [
                        f"REPO {self.repo}",
                        "NO_MEMORY_DIR",
                        self.SETTINGS_NOTE,
                        "NOTE The memory directory from CLAUDE_CODE_PROJECT_DIR_NAME holds a line break: "
                        f"{flat_text(repr(str(directory)))}",
                    ],
                    lines,
                )

    def test_a_candidate_holding_a_line_break_is_left_out_with_a_note(self) -> None:
        projects = self.home / ".claude" / "projects"
        (projects / "C--elsewhere" / "memory").mkdir(parents=True)
        for character in NAME_BREAKS:
            (projects / f"C--x{character}CANDIDATE forged" / "memory").mkdir(parents=True)
        lines = self.resolve_lines()
        self.assertEqual(
            [f"CANDIDATE {projects / 'C--elsewhere' / 'memory'}"],
            [line for line in lines if line.startswith("CANDIDATE ")],
        )
        left_out = sorted(str(projects / f"C--x{character}CANDIDATE forged" / "memory") for character in NAME_BREAKS)
        self.assertEqual(
            [
                f"NOTE Left out a memory directory whose path holds a line break: {flat_text(repr(path))}"
                for path in left_out
            ],
            [line for line in lines if line.startswith("NOTE Left out")],
        )

    def test_a_repository_path_holding_a_line_break_is_a_failed_line(self) -> None:
        for character in LINE_BREAKS:
            repo = self.root / f"re{character}REPO forged"
            with self.subTest(character=repr(character)):
                self.assertEqual(
                    [f"FAILED the repository path holds a line break: {flat_text(repr(str(repo)))}"],
                    self.resolve_lines("--repo", str(repo), code=1),
                )

    def test_the_source_is_flattened(self) -> None:
        for character in LINE_BREAKS:
            result = {
                "memory_dir": "memory dir",
                "source": f"settings{character}EXISTS yes",
                "exists": True,
                "notes": [],
            }
            with self.subTest(character=repr(character)):
                self.assertEqual(
                    [f"REPO {self.repo}", "MEMORY_DIR memory dir", "SOURCE settings EXISTS yes", "EXISTS yes"],
                    one_fact_per_line(
                        self, "\n".join(memory_audit.resolve_lines(self.repo, result)) + "\n", RESOLVE_LABELS
                    ),
                )

    def block_reads(self, path: Path, value: object = True, **settings: object) -> None:
        write(path, json.dumps({**settings, "permissions": {"blockReadsOutsideWorkingDirectories": value}}))

    def blocked_note(self, by: str, scope: str, path: Path) -> str:
        return (
            f"permissions.blockReadsOutsideWorkingDirectories is on in {by} settings, so Claude Code loads no auto "
            f"memory from {str(self.home / 'chosen')!r}, which autoMemoryDirectory in {scope} settings ({path}) "
            "chooses, and saves none to it."
        )

    def test_a_repository_choice_is_not_used_while_reads_outside_are_blocked(self) -> None:
        (self.home / "chosen").mkdir(parents=True)
        project = self.repo / ".claude" / "settings.json"
        other = self.home / ".claude" / "projects" / "C--elsewhere" / "memory"
        other.mkdir(parents=True)
        for by, path in (
            ("managed", self.managed),
            ("local", self.repo / ".claude" / "settings.local.json"),
            ("project", project),
            ("user", self.home / ".claude" / "settings.json"),
        ):
            with self.subTest(by=by):
                write(project, json.dumps({"autoMemoryDirectory": "~/chosen"}))
                self.block_reads(path, **({"autoMemoryDirectory": "~/chosen"} if path == project else {}))
                result = self.resolve()
                self.assertIsNone(result["memory_dir"])
                self.assertEqual([str(other)], result["candidates"])
                self.assertEqual(self.blocked_note(by, "project", project), result["notes"][-1])
                path.unlink()

    def test_only_a_true_block_from_any_scope_counts(self) -> None:
        (self.home / "chosen").mkdir(parents=True)
        project = self.repo / ".claude" / "settings.json"
        write(project, json.dumps({"autoMemoryDirectory": "~/chosen"}))
        self.block_reads(self.managed, value=False)
        self.block_reads(self.home / ".claude" / "settings.json", value="true")
        self.assertEqual(str(self.home / "chosen"), self.resolve()["memory_dir"])
        self.block_reads(self.home / ".claude" / "settings.json")
        self.assertEqual(self.blocked_note("user", "project", project), self.resolve()["notes"][-1])

    def test_permissions_of_another_kind_leave_the_directory_unresolved_by_name(self) -> None:
        (self.home / "chosen").mkdir(parents=True)
        project = self.repo / ".claude" / "settings.json"
        user = self.home / ".claude" / "settings.json"
        for permissions in ("text", ["blockReadsOutsideWorkingDirectories"], None):
            with self.subTest(permissions=permissions):
                write(project, json.dumps({"autoMemoryDirectory": "~/chosen"}))
                write(user, json.dumps({"permissions": permissions}))
                result = self.resolve()
                self.assertIsNone(result["memory_dir"])
                self.assertEqual(f"Cannot read {user}: its permissions is not an object", result["notes"][-1])

    def test_an_untracked_local_choice_is_the_user_own_and_used(self) -> None:
        (self.home / "chosen").mkdir(parents=True)
        local = self.repo / ".claude" / "settings.local.json"
        write(local, json.dumps({"autoMemoryDirectory": "~/chosen"}))
        self.block_reads(self.home / ".claude" / "settings.json")
        result = self.resolve()
        self.assertEqual(str(self.home / "chosen"), result["memory_dir"])
        self.assertEqual(
            [
                self.SETTINGS_NOTE.removeprefix("NOTE "),
                "Claude Code honors a repository-level autoMemoryDirectory only after the folder is trusted.",
            ],
            result["notes"],
        )
        for supplied, patch in (
            ("tracked", None),
            ("a link", mock.patch.object(memory_audit, "is_link_or_junction", return_value=True)),
            ("unknown to git", mock.patch.object(GitClient, "run", side_effect=GitError("no git"))),
            # A folder whose own metadata cannot be read cannot tell either.
            (
                "unreadable",
                mock.patch.object(memory_audit, "is_link_or_junction", side_effect=PermissionError(13, "no")),
            ),
        ):
            with self.subTest(supplied=supplied):
                if patch is None:
                    git("-C", str(self.repo), "add", "--force", ".claude/settings.local.json")
                    result = self.resolve()
                    git("-C", str(self.repo), "rm", "--cached", "-q", ".claude/settings.local.json")
                else:
                    with patch:
                        result = self.resolve()
                self.assertIsNone(result["memory_dir"])
                self.assertEqual(self.blocked_note("user", "local", local), result["notes"][-1])

    def test_local_settings_through_a_linked_claude_folder_are_the_repository_own(self) -> None:
        # Claude Code counts the file the repository's when .claude is a link, and Node reads a junction as one.
        self.assertFalse(memory_audit.repository_supplied(self.repo, "local"))
        outside = self.home / "outside"
        outside.mkdir(parents=True)
        claude = self.repo / ".claude"
        if claude.exists():
            claude.rename(self.home / "moved-claude")
        directory_link(claude, outside)
        self.assertTrue(memory_audit.repository_supplied(self.repo, "local"))

    def test_local_settings_git_cannot_tell_about_are_the_repository_own(self) -> None:
        local = self.repo / ".claude" / "settings.local.json"
        write(local, "{}")
        self.assertFalse(memory_audit.repository_supplied(self.repo, "local"), "untracked")
        git("-C", str(self.repo), "add", "--force", ".claude/settings.local.json")
        self.assertTrue(memory_audit.repository_supplied(self.repo, "local"), "tracked")
        git("-C", str(self.repo), "rm", "--cached", "-q", ".claude/settings.local.json")
        (self.repo / ".git" / "config").write_text("[core\nbogus\n", encoding="utf-8")
        self.assertTrue(memory_audit.repository_supplied(self.repo, "local"), "a configuration git cannot parse")

    def test_local_settings_in_no_repository_are_the_users(self) -> None:
        plain = self.root / "plain folder"
        write(plain / ".claude" / "settings.local.json", "{}")
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.root)}):
            self.assertFalse(memory_audit.repository_supplied(plain, "local"))

    def test_the_main_worktree_core_worktree_names_is_read_whole(self) -> None:
        common = self.root / "shared git"
        paths = [self.root / "linked", self.root / "shared git" / "worktrees" / "linked", common]

        def main_worktree_given(configured: str | None) -> Path | None:
            with (
                mock.patch.object(GitClient, "rev_parse_paths", return_value=paths),
                mock.patch.object(GitClient, "quiet_output", return_value=configured),
            ):
                return memory_audit.main_worktree(self.root / "linked")

        # Only the line end git prints is cut: a name may end in another space or in any letter.
        self.assertEqual((common / "../work\tX").resolve(), main_worktree_given("../work\tX\r\n"))
        self.assertEqual((common / "../work\t").resolve(), main_worktree_given("../work\t\n"))
        with self.assertRaises(GitError) as context:
            main_worktree_given("\r\n")
        self.assertEqual(f"git config gave core.worktree in {common / 'config'} an empty value", str(context.exception))
        with self.assertRaises(memory_audit.UnknownMainWorktree):
            main_worktree_given(None)

    def test_a_used_repository_choice_states_the_block_rule(self) -> None:
        self.store(self.home / "chosen")
        project = self.repo / ".claude" / "settings.json"
        write(project, json.dumps({"autoMemoryDirectory": "~/chosen"}))
        self.assertEqual(
            [
                f"REPO {self.repo.resolve()}",
                f"MEMORY_DIR {self.home / 'chosen'}",
                f"SOURCE autoMemoryDirectory in project settings ({project.resolve()})",
                "EXISTS yes",
                self.SETTINGS_NOTE,
                "NOTE Claude Code honors a repository-level autoMemoryDirectory only after the folder is trusted.",
                "NOTE Claude Code ignores it while permissions.blockReadsOutsideWorkingDirectories is on, which a "
                "--settings file can set.",
            ],
            self.resolve_lines(),
        )
        write(self.home / ".claude" / "settings.json", json.dumps({"autoMemoryDirectory": "~/chosen"}))
        project.unlink()
        self.block_reads(self.managed)
        self.assertEqual(str(self.home / "chosen"), self.resolve()["memory_dir"], "a user's choice is not blocked")

    def test_command_line_defaults_to_the_current_repository(self) -> None:
        self.store(self.derived())
        (self.repo / "src" / "deep").mkdir(parents=True)
        root = self.repo.resolve()
        expected = [
            f"REPO {root}",
            f"MEMORY_DIR {self.derived()}",
            f"SOURCE derived from the repository's main worktree ({root})",
            "EXISTS yes",
            self.SETTINGS_NOTE,
            self.DERIVED_NOTE,
        ]
        self.assertEqual(expected, self.resolve_lines(cwd=self.repo / "src" / "deep"))
        self.assertEqual(expected, self.resolve_lines("--repo", str(root), cwd=self.root))

    def test_command_line_lists_candidates_when_the_directory_is_missing(self) -> None:
        other = self.home / ".claude" / "projects" / "C--elsewhere" / "memory"
        other.mkdir(parents=True)
        lines = self.resolve_lines()
        self.assertEqual(
            ["EXISTS no", f"CANDIDATE {other}"], [line for line in lines if line.startswith(("EXISTS", "CANDIDATE"))]
        )

    def test_command_line_lists_candidates_when_the_settings_named_directory_is_missing(self) -> None:
        other = self.home / ".claude" / "projects" / "C--elsewhere" / "memory"
        other.mkdir(parents=True)
        settings = self.home / ".claude" / "settings.json"
        write(settings, json.dumps({"autoMemoryDirectory": "~/memory-user"}))
        self.assertEqual(
            [
                f"REPO {self.repo.resolve()}",
                f"MEMORY_DIR {self.home / 'memory-user'}",
                f"SOURCE autoMemoryDirectory in user settings ({settings})",
                "EXISTS no",
                f"CANDIDATE {other}",
                self.SETTINGS_NOTE,
            ],
            self.resolve_lines(),
        )

    def test_notes_use_shared_flattening_without_truncating_or_reordering_facts(self) -> None:
        note = " \tRead\r\n café\u00a0設計\u2003notes.\v " + "x" * 151
        result = {
            "memory_dir": "memory dir",
            "source": "settings",
            "exists": False,
            "candidates": ["first", "second"],
            "notes": [note, " \r\n\t "],
        }
        with mock.patch.object(memory_audit, "flat_text", wraps=flat_text) as flatten:
            lines = memory_audit.resolve_lines(self.repo, result)
        self.assertEqual(
            [
                f"REPO {self.repo}",
                "MEMORY_DIR memory dir",
                "SOURCE settings",
                "EXISTS no",
                "CANDIDATE first",
                "CANDIDATE second",
                f"NOTE Read café 設計 notes. {'x' * 151}",
                "NOTE ",
            ],
            lines,
        )
        self.assertEqual([mock.call("settings"), mock.call(note), mock.call(" \r\n\t ")], flatten.call_args_list)

    def test_error_reasons_use_shared_flattening_and_preserve_strerror_selection(self) -> None:
        message = " \tCannot\r\n read  café\u00a0設計\u2003memory.\v "
        for error, flattened, description in (
            (OSError(13, message, "one.md"), [message, "one.md"], "Cannot read café 設計 memory.: one.md"),
            (OSError(message), [message], "Cannot read café 設計 memory."),
        ):
            with (
                self.subTest(error=error),
                mock.patch.object(memory_audit, "flat_text", wraps=flat_text) as flatten,
                mock.patch.object(memory_audit, "run", side_effect=error),
            ):
                self.assertEqual((1, f"FAILED {description}\n", ""), run_main("resolve", "--repo", str(self.repo)))
            self.assertEqual([mock.call(text) for text in flattened], flatten.call_args_list)

    def test_command_line_reports_no_directory_as_a_result(self) -> None:
        write(self.home / ".claude" / "settings.json", json.dumps({"autoMemoryDirectory": "relative/path"}))
        self.assertEqual(
            [
                f"REPO {self.repo.resolve()}",
                "NO_MEMORY_DIR",
                self.SETTINGS_NOTE,
                f"NOTE autoMemoryDirectory in {self.home / '.claude' / 'settings.json'} is not an absolute or ~/ path: "
                "'relative/path'",
            ],
            self.resolve_lines(),
        )

    def test_command_line_outside_a_repository_fails(self) -> None:
        outside = self.root / "plain folder"
        outside.mkdir()
        self.assertEqual(["FAILED not inside a Git repository; pass --repo"], self.resolve_lines(cwd=outside, code=1))

    def new_repository(self, folder: Path, *options: str) -> Path:
        """A repository at folder with one commit, made with git init's options."""
        git("-C", str(folder.parent), "init", "-q", "-b", "main", *options, folder.name)
        git("-C", str(folder), "commit", "-q", "--allow-empty", "-m", "init")
        return folder

    def test_a_submodule_and_a_separate_git_directory_derive_from_their_own_working_tree(self) -> None:
        source = self.new_repository(self.root / "submodule source")
        git("-C", str(self.repo), "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), "sub")
        submodule = self.repo / "sub"
        linked = self.root / "submodule worktree"
        git("-C", str(submodule), "worktree", "add", "-q", "-b", "feature", str(linked))
        separate = self.new_repository(self.root / "separate", f"--separate-git-dir={self.root / 'separate git'}")
        for folder, main in ((submodule, submodule), (linked, submodule), (separate, separate)):
            with self.subTest(folder=folder.name):
                result = self.resolve(repo=folder)
                self.assertEqual(str(self.derived(main)), result["memory_dir"])
                self.assertEqual(f"derived from the repository's main worktree ({main.resolve()})", result["source"])

    def test_a_linked_worktree_of_a_separate_git_directory_resolves_to_no_directory(self) -> None:
        separate = self.new_repository(self.root / "separate", f"--separate-git-dir={self.root / 'separate git'}")
        linked = self.root / "separate worktree"
        git("-C", str(separate), "worktree", "add", "-q", "-b", "feature", str(linked))
        other = self.store(self.home / ".claude" / "projects" / "C--elsewhere" / "memory")
        result = self.resolve(repo=linked)
        self.assertIsNone(result["memory_dir"])
        self.assertEqual([str(other)], result["candidates"])
        shared = (self.root / "separate git").resolve()
        self.assertEqual(
            f"{linked.resolve()} is a linked worktree whose shared Git directory {shared} "
            "names no main worktree; run resolve with --repo naming the main worktree, or pass --memory-dir.",
            result["notes"][-1],
        )

    def test_an_empty_core_worktree_is_reported_not_followed(self) -> None:
        separate = self.new_repository(self.root / "separate", f"--separate-git-dir={self.root / 'separate git'}")
        linked = self.root / "separate worktree"
        git("-C", str(separate), "worktree", "add", "-q", "-b", "feature", str(linked))
        shared = (self.root / "separate git").resolve()
        with (shared / "config").open("a", encoding="utf-8") as config:
            config.write("[core]\n\tworktree =\n")
        lines = self.resolve_lines("--repo", str(linked), cwd=self.root, code=1)
        self.assertEqual(
            [
                "FAILED git could not read the repository: "
                f"git config gave core.worktree in {shared / 'config'} an empty value"
            ],
            lines,
        )

    def test_a_core_worktree_git_cannot_read_is_reported_not_taken_for_none(self) -> None:
        separate = self.new_repository(self.root / "separate", f"--separate-git-dir={self.root / 'separate git'}")
        linked = self.root / "separate worktree"
        git("-C", str(separate), "worktree", "add", "-q", "-b", "feature", str(linked))
        run = GitClient.run
        refused = "warning: unable to access 'config': Permission denied\n"

        def answering(client: GitClient, arguments: Sequence[str], **options: Any) -> GitResult:
            if "core.worktree" in arguments:
                return GitResult(1, "", refused)
            return run(client, arguments, **options)

        with mock.patch.object(GitClient, "run", autospec=True, side_effect=answering):
            lines = self.resolve_lines("--repo", str(linked), cwd=self.root, code=1)
        self.assertEqual([f"FAILED git could not read the repository: {flat_text(refused)}"], lines)

    def test_a_git_failure_is_reported_by_its_own_reason(self) -> None:
        worktree = self.root / "wt"
        git("-C", str(self.repo), "worktree", "add", "-q", "-b", "feature", str(worktree))
        self.store(self.derived(worktree))  # where a silent fallback to the worktree's own folder would land
        refused = (
            f"fatal: detected dubious ownership in repository at '{worktree}'\nTo add an exception, run git config\n"
        )
        for failure, shown in (
            (GitError("git rev-parse did not finish within 300 seconds", kind="timeout"), None),
            (GitError(MISSING_GIT, kind="prerequisite"), None),
            (GitResult(128, "", refused), flat_text(refused)),
        ):
            if isinstance(failure, GitError):
                patch = mock.patch.object(GitClient, "run", side_effect=failure)
                shown = str(failure)
            else:
                patch = mock.patch.object(GitClient, "run", return_value=failure)
            for arguments, cwd in (((), worktree), (("--repo", str(worktree)), self.root)):
                with self.subTest(failure=shown, arguments=arguments), patch:
                    self.assertEqual(
                        [f"FAILED git could not read the repository: {shown}"],
                        self.resolve_lines(*arguments, cwd=cwd, code=1),
                    )

    def test_a_folder_in_no_repository_is_its_own_project(self) -> None:
        folder = self.root / "plain folder"
        folder.mkdir()
        derived = self.store(self.derived(folder))
        self.assertEqual(
            [
                f"REPO {folder.resolve()}",
                f"MEMORY_DIR {derived}",
                f"SOURCE derived from the folder ({folder.resolve()}), which is in no Git repository",
                "EXISTS yes",
                self.SETTINGS_NOTE,
                self.DERIVED_NOTE,
            ],
            self.resolve_lines("--repo", str(folder), cwd=self.root),
        )

    def test_a_subdirectory_as_repo_resolves_like_the_working_directory(self) -> None:
        chosen = self.store(self.home / "chosen")
        write(self.repo / ".claude" / "settings.json", json.dumps({"autoMemoryDirectory": "~/chosen"}))
        (self.repo / "src").mkdir()
        settings = (self.repo / ".claude" / "settings.json").resolve()
        from_inside = self.resolve_lines(cwd=self.repo / "src")
        self.assertEqual(from_inside, self.resolve_lines("--repo", str(self.repo / "src"), cwd=self.root))
        self.assertEqual(
            [
                f"REPO {self.repo.resolve()}",
                f"MEMORY_DIR {chosen}",
                f"SOURCE autoMemoryDirectory in project settings ({settings})",
            ],
            from_inside[:3],
        )

    def test_a_directory_the_later_commands_refuse_is_reported_with_the_candidates(self) -> None:
        other = self.store(self.home / ".claude" / "projects" / "C--elsewhere" / "memory")
        self.derived().mkdir(parents=True)
        facts = ("EXISTS", "REFUSED", "CANDIDATE")
        self.assertEqual(
            [
                "EXISTS yes",
                f"REFUSED {self.derived()} is not a memory directory: it holds no MEMORY.md",
                f"CANDIDATE {other}",
            ],
            [line for line in self.resolve_lines() if line.startswith(facts)],
        )
        self.derived().rmdir()
        skill = self.store(self.home / ".claude" / "skills" / "some-skill")
        write(
            self.home / ".claude" / "settings.json", json.dumps({"autoMemoryDirectory": "~/.claude/skills/some-skill"})
        )
        with mock.patch.object(Path, "home", return_value=self.home):
            lines = self.resolve_lines()
        self.assertEqual(
            [
                "EXISTS yes",
                f"REFUSED {skill} is inside the skills directory {self.home / '.claude' / 'skills'}; name the memory "
                "directory resolve printed",
                f"CANDIDATE {other}",
            ],
            [line for line in lines if line.startswith(facts)],
        )

    def test_invalid_or_unreadable_settings_leave_the_directory_unresolved(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        write(settings, json.dumps({"autoMemoryDirectory": "relative/path"}))
        self.assertIsNone(self.resolve()["memory_dir"])
        write(settings, "{not json")
        self.assertIsNone(self.resolve()["memory_dir"])


class SkillSourceTests(unittest.TestCase):
    """skill-source names a skill's editable source, never a file inside a skills directory."""

    LABELS = ("SKILL_FILE ", "NOTE ", "FAILED ")

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.checkout = self.root / "source checkout"
        self.skills_root = self.root / "running skills"
        self.source_file = write(self.checkout / "skills" / "demo" / "SKILL.md", "---\nname: demo\n---\n")

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def manifest(self, **sources: str) -> Path:
        """A deployer manifest recording each source ID's checkout as deploying the skill demo."""
        entries = {
            source: {"source_dir": folder, "skills": {"demo": {"hash": "0" * 64}}} for source, folder in sources.items()
        }
        return write(self.home / ".claude" / "skills" / ".deploy-manifest.json", json.dumps({"sources": entries}))

    def source(self, *arguments: str, code: int = 0) -> list[str]:
        with (
            mock.patch.object(Path, "home", return_value=self.home),
            mock.patch.object(skill_roots, "SKILLS_ROOT", self.skills_root),
        ):
            result = run_main("skill-source", *arguments, "--repo", str(self.repo), "--home", str(self.home))
        self.assertEqual((code, ""), (result[0], result[2]), result[1])
        return one_fact_per_line(self, result[1], self.LABELS)

    def test_the_manifest_source_and_a_project_skill_are_named(self) -> None:
        self.manifest(main=str(self.checkout))
        project = write(self.repo / ".claude" / "skills" / "demo" / "SKILL.md", "---\nname: demo\n---\n")
        write(self.home / ".claude" / "skills" / "demo" / "SKILL.md", "---\nname: demo\n---\n")
        self.assertEqual([f"SKILL_FILE {self.source_file}", f"SKILL_FILE {project}"], self.source("demo"))

    def test_a_skill_with_no_source_is_a_failed_line(self) -> None:
        missing = (
            "FAILED no editable source of skill {}: the deployer's manifest records none, and the repository has no "
            "project skill of that name; name its source file with --file"
        )
        write(self.home / ".claude" / "skills" / "demo" / "SKILL.md", "---\nname: demo\n---\n")
        self.assertEqual([missing.format("demo")], self.source("demo", code=1))
        self.manifest(main=str(self.checkout))
        self.assertEqual([missing.format("other")], self.source("other", code=1))
        # A manifest that records no sources at all names none, unlike one whose sources are of another kind.
        write(self.home / ".claude" / "skills" / ".deploy-manifest.json", json.dumps({"manifest_version": 8}))
        self.assertEqual([missing.format("demo")], self.source("demo", code=1))

    def test_a_source_inside_a_skills_directory_or_holding_a_line_break_is_passed_over(self) -> None:
        deployed = self.home / ".claude" / "skills" / "demo-home"
        write(deployed / "skills" / "demo" / "SKILL.md", "---\nname: demo\n---\n")
        self.manifest(deployed=str(deployed), gone=str(self.root / "gone"))
        self.assertEqual(
            [
                f"NOTE {deployed / 'skills' / 'demo' / 'SKILL.md'} is inside the skills directory "
                f"{self.home / '.claude' / 'skills'}; the deployer stops updating a skill changed there",
                f"NOTE {self.root / 'gone' / 'skills' / 'demo' / 'SKILL.md'} is not a file",
                "FAILED no editable source of skill demo: the deployer's manifest records none, and the repository "
                "has no project skill of that name; name its source file with --file",
            ],
            self.source("demo", code=1),
        )
        for character in LINE_BREAKS:
            folder = f"{self.checkout}{character}SKILL_FILE forged"
            with self.subTest(character=repr(character)):
                self.manifest(forged=folder, main=str(self.checkout))
                self.assertEqual(
                    [
                        f"NOTE {flat_text(repr(Path(folder) / 'skills' / 'demo' / 'SKILL.md'))} holds a line break",
                        f"SKILL_FILE {self.source_file}",
                    ],
                    self.source("demo"),
                )

    def test_a_named_file_is_checked_against_every_skills_directory(self) -> None:
        self.assertEqual([f"SKILL_FILE {self.source_file}"], self.source("--file", str(self.source_file)))
        for root in (self.home / ".claude" / "skills", self.home / ".agents" / "skills", self.skills_root):
            target = write(root / "demo" / "SKILL.md", "---\nname: demo\n---\n")
            with self.subTest(root=root):
                self.assertEqual(
                    [
                        f"NOTE {target} is inside the skills directory {root}; the deployer stops updating a skill "
                        "changed there",
                        f"FAILED {target} cannot be edited as a skill's source",
                    ],
                    self.source("--file", str(target), code=1),
                )

    def test_an_unreadable_manifest_or_a_bad_name_is_a_failed_line(self) -> None:
        write(self.home / ".claude" / "skills" / ".deploy-manifest.json", "{not json")
        lines = self.source("demo", code=1)
        self.assertEqual(1, len(lines))
        self.assertTrue(lines[0].startswith("FAILED cannot read the deployer manifest: "), lines)
        manifest = self.home / ".claude" / "skills" / ".deploy-manifest.json"
        manifest.unlink()
        manifest.mkdir()
        lines = self.source("demo", code=1)
        self.assertEqual(1, len(lines))
        self.assertTrue(lines[0].startswith("FAILED cannot read the deployer manifest: "), lines)
        for name in ("../demo", "demo\nSKILL_FILE forged", "demo\u2028x"):
            with self.subTest(name=repr(name)):
                self.assertEqual([f"FAILED {flat_text(repr(name))} is not a skill name"], self.source(name, code=1))

    def test_a_manifest_of_another_shape_is_a_failed_line_rather_than_no_source(self) -> None:
        manifest = self.home / ".claude" / "skills" / ".deploy-manifest.json"
        cases = {
            "it is not a JSON object": [],
            "its sources is not an object": {"sources": ["main"]},
            "its source 'main' does not name its source_dir and skills": {"sources": {"main": "checkout"}},
            "its source 'other' does not name its source_dir and skills": {
                "sources": {"other": {"source_dir": 7, "skills": {"demo": {}}}}
            },
            "its source 'third' does not name its source_dir and skills": {
                "sources": {"third": {"source_dir": str(self.checkout), "skills": ["demo"]}}
            },
        }
        for reason, document in cases.items():
            with self.subTest(reason=reason):
                write(manifest, json.dumps(document))
                self.assertEqual([f"FAILED cannot read the deployer manifest: {reason}"], self.source("demo", code=1))

    def test_a_git_failure_is_a_note_and_the_manifest_source_is_still_named(self) -> None:
        self.manifest(main=str(self.checkout))
        with (
            mock.patch.object(Path, "home", return_value=self.home),
            mock.patch.object(skill_roots, "SKILLS_ROOT", self.skills_root),
            mock.patch.object(Path, "cwd", return_value=self.repo),
            mock.patch.object(GitClient, "run", side_effect=GitError(MISSING_GIT, kind="prerequisite")),
        ):
            result = run_main("skill-source", "demo", "--home", str(self.home))
        self.assertEqual(
            (
                0,
                f"NOTE no project skill was looked for: git could not read the repository: {MISSING_GIT}\n"
                f"SKILL_FILE {self.source_file}\n",
                "",
            ),
            result,
        )

    def test_a_name_and_a_file_together_are_a_usage_error(self) -> None:
        code, output, error = run_main("skill-source", "demo", "--file", str(self.source_file))
        self.assertEqual((2, ""), (code, output))
        self.assertIn("not allowed with argument", error)


@pytest.mark.parametrize(
    "arguments", [["audit"], ["reindex"], ["delete", "keep.md"]], ids=["audit", "reindex", "delete"]
)
def test_a_missing_memory_directory_is_a_failed_line(tmp_path: Path, arguments: list[str]) -> None:
    absent = tmp_path / "absent dir"
    command, *rest = arguments
    assert (1, f"FAILED memory directory not found: {absent}\n", "") == run_main(
        command, "--memory-dir", str(absent), *rest
    )


# Pinned while parse_frontmatter is kept instead of ejryan89-skill-core's frontmatter.py; see the comment above it.
@pytest.mark.parametrize(
    ("text", "values", "body"),
    [
        pytest.param(
            "---\nname: n\ndescription: `x` y\n---\nBody.",
            {"name": "n", "description": "`x` y"},
            "Body.",
            id="a-backtick-value",
        ),
        pytest.param("---\nname: first\nname: second\n---\nBody.", {"name": "first"}, "Body.", id="a-repeated-key"),
        pytest.param(
            "---\nmetadata:\n  type: user\n  type: other\n---\nBody.",
            {"type": "user"},
            "Body.",
            id="a-repeated-nested-key",
        ),
        pytest.param("---\nname: open\nBody.", {}, "---\nname: open\nBody.", id="never-closed"),
    ],
)
def test_frontmatter_the_shared_reader_would_refuse_is_read_leniently(
    text: str, values: dict[str, str], body: str
) -> None:
    assert (values, body) == memory_audit.parse_frontmatter(text)


@pytest.mark.parametrize(
    ("text", "values"),
    [
        pytest.param(
            "---\ndescription: >\n  one\n  two\n\n  three\n---\n", {"description": "one two\nthree"}, id="folded"
        ),
        pytest.param("---\ndescription: |-\n  one\n    two\n---\n", {"description": "one\n  two"}, id="literal-strip"),
        pytest.param(
            "---\ndescription: |1- # note\n   one\n   two\n\n---\n",
            {"description": "one\n  two"},
            id="literal-with-indentation-and-comment",
        ),
        pytest.param(
            "---\ndescription: >-\n  type: not a key\nname: n\n---\n",
            {"description": "type: not a key", "name": "n"},
            id="a-key-inside-the-block",
        ),
        pytest.param(
            "---\nmetadata:\n  note: |\n    nested\n  type: user\n---\n",
            {"note": "nested", "type": "user"},
            id="nested-under-metadata",
        ),
        pytest.param("---\ndescription: >-\nname: n\n---\n", {"name": "n"}, id="an-empty-block"),
        pytest.param("---\ndescription: >-\n  never closed\n", {}, id="never-closed"),
    ],
)
def test_block_scalars_fold_or_keep_lines_as_their_style_says(text: str, values: dict[str, str]) -> None:
    assert values == memory_audit.parse_frontmatter(text)[0]


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        pytest.param(" \talpha\r\n beta \v café\u00a0設計\u2003", 150, "alpha beta café 設計", id="mixed-whitespace"),
        pytest.param(" \t\r\n\u00a0\u2003", 150, "", id="whitespace-only"),
        pytest.param("x" * 149, 150, "x" * 149, id="under-the-limit"),
        pytest.param("x" * 150, 150, "x" * 150, id="at-the-limit"),
        pytest.param("x" * 151, 150, "x" * 147 + "...", id="over-the-limit"),
        pytest.param("設" * 150, 150, "設" * 150, id="wide-at-the-limit"),
        pytest.param("設" * 151, 150, "設" * 147 + "...", id="wide-over-the-limit"),
        pytest.param("alpha\tbeta\r\ngamma", 10, "alpha...", id="cut-at-a-word"),
        pytest.param("alpha,;:.\tbeta gamma", 12, "alpha...", id="cut-before-punctuation"),
    ],
)
def test_hooks_use_shared_flattening_before_exact_character_and_word_cutoffs(
    text: str, limit: int, expected: str
) -> None:
    with mock.patch.object(memory_audit, "flat_text", wraps=flat_text) as flatten:
        assert expected == memory_audit.one_line(text, limit)
    flatten.assert_called_once_with(text)


class JsonFileDecodingTests(unittest.TestCase):
    """Each JSON file reads as UTF-8 on every platform, whatever the locale's codec."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_settings_after_a_byte_order_mark_read_their_non_ascii_text(self) -> None:
        path = self.root / "settings.json"
        text = json.dumps({"autoMemoryDirectory": "café"}, ensure_ascii=False)
        path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
        self.assertEqual({"autoMemoryDirectory": "café"}, memory_audit.read_settings(path))

    def test_a_manifest_source_dir_reads_its_non_ascii_text(self) -> None:
        manifest = self.root / ".claude" / "skills" / memory_audit.MANIFEST_NAME
        manifest.parent.mkdir(parents=True)
        source = {"source_dir": "/café", "skills": {"example": {}}}
        manifest.write_bytes(json.dumps({"sources": {"one": source}}, ensure_ascii=False).encode("utf-8"))
        self.assertEqual(
            [Path("/café") / "skills" / "example" / "SKILL.md"],
            memory_audit.manifest_skill_files(self.root, "example"),
        )


class ManagedSettingsPathTests(unittest.TestCase):
    """Windows' managed settings file, run on every platform: it is under the Program Files folder only when that
    folder is named by an absolute path, so no settings are read relative to the working directory."""

    def managed(self, program_files: str | None) -> Path | None:
        environment = {key: value for key, value in os.environ.items() if key.upper() != "PROGRAMFILES"}
        if program_files is not None:
            environment["PROGRAMFILES"] = program_files
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(os.environ, environment, clear=True):
            return memory_audit.managed_settings_path()

    def test_an_absolute_program_files_folder_holds_the_managed_settings(self) -> None:
        folder = Path(tempfile.gettempdir()).resolve()
        self.assertEqual(folder / "ClaudeCode" / "managed-settings.json", self.managed(str(folder)))

    def test_an_unset_empty_or_relative_program_files_folder_gives_no_managed_settings(self) -> None:
        for value in (None, "", ".", "Program Files"):
            with self.subTest(value=value):
                self.assertIsNone(self.managed(value))


def link_folder(target: Path, link: Path) -> None:
    """A real link to a folder: a junction on Windows, which needs no privilege, else a symbolic link."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_a_target_reached_through_a_linked_folder_is_the_file_it_names(tmp_path: Path) -> None:
    memory, real = tmp_path / "memory", tmp_path / "real"
    memory.mkdir()
    real.mkdir()
    link_folder(real, memory / "shared")
    named = memory_audit.target_identity(memory, "shared/note.md")
    assert named == memory_audit.target_identity(real, "note.md")
    assert named == memory_audit.target_identity(memory, "./shared/note.md")
    assert named != memory_audit.target_identity(memory, "note.md")


def test_a_destination_named_twice_is_read_once_and_case_counts_where_the_platform_keeps_it(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link_folder(real, tmp_path / "linked")
    notes = real / "CLAUDE.md"
    notes.write_text("rules\n", encoding="utf-8")
    named = [notes, tmp_path / "linked" / "CLAUDE.md", real / "." / "CLAUDE.md"]
    assert memory_audit.destination_files(None, named) == [notes]
    lower = real / "claude.md"
    lower.write_text("rules\n", encoding="utf-8")
    # Windows folds the two names into one file; Linux keeps two files, and both are read.
    expected = [notes] if sys.platform == "win32" else [notes, lower]
    assert memory_audit.destination_files(None, [notes, lower]) == expected
