from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import memory_audit
import skill_roots
from flat_text import flat_text
from git_test_base import GitTestCase


def temporary_store_folds_case() -> bool:
    """Whether the file system holding the temporary directory, where every fixture store lives, compares names
    without regard to case, as NTFS does and a Linux file system does not."""
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / "Probe.md").write_bytes(b"")
        return (Path(directory) / "probe.md").exists()


# The tests that need a store where MEMORY.md and memory.md are one file run where the store is one, and their
# counterparts where it is not.
FOLDS_CASE = temporary_store_folds_case()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


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


class CommandLineContractTests(GitTestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_a_missing_memory_directory_is_a_failed_line(self) -> None:
        absent = self.root / "absent dir"
        for arguments in (["audit"], ["reindex"], ["delete", "keep.md"]):
            command, *rest = arguments
            with self.subTest(command=command):
                self.assertEqual(
                    (1, f"FAILED memory directory not found: {absent}\n", ""),
                    run_main(command, "--memory-dir", str(absent), *rest),
                )

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


class MemoryAuditTests(GitTestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.memory_dir = self.root / "memory"
        self.repo = self.root / "repo"
        self.memory_dir.mkdir()
        self.repo.mkdir()

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def run_audit(self, **options) -> dict:
        return memory_audit.audit(self.memory_dir, self.repo, **options)

    def by_file(self, result: dict) -> dict[str, dict]:
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

    def test_frontmatter_the_shared_reader_would_refuse_is_read_leniently(self) -> None:
        # Pinned while parse_frontmatter is kept instead of skill-core's frontmatter.py; see the comment above it.
        for text, values, body in (
            ("---\nname: n\ndescription: `x` y\n---\nBody.", {"name": "n", "description": "`x` y"}, "Body."),
            ("---\nname: first\nname: second\n---\nBody.", {"name": "first"}, "Body."),
            ("---\nmetadata:\n  type: user\n  type: other\n---\nBody.", {"type": "user"}, "Body."),
            ("---\nname: open\nBody.", {}, "---\nname: open\nBody."),
        ):
            with self.subTest(text=text):
                self.assertEqual((values, body), memory_audit.parse_frontmatter(text))

    def test_a_block_scalar_description_is_read_whole(self) -> None:
        write(
            self.memory_dir / "folded.md",
            "---\nname: folded\ndescription: >-\n  First line of the hook\n  continues here.\n"
            "metadata:\n  type: project\n---\n\nBody.\n",
        )
        entry = self.by_file(self.run_audit())["folded.md"]
        self.assertEqual("First line of the hook continues here.", entry["description"])
        self.assertEqual("project", entry["type"])

    def test_block_scalars_fold_or_keep_lines_as_their_style_says(self) -> None:
        for text, values in (
            ("---\ndescription: >\n  one\n  two\n\n  three\n---\n", {"description": "one two\nthree"}),
            ("---\ndescription: |-\n  one\n    two\n---\n", {"description": "one\n  two"}),
            ("---\ndescription: |1- # note\n   one\n   two\n\n---\n", {"description": "one\n  two"}),
            (
                "---\ndescription: >-\n  type: not a key\nname: n\n---\n",
                {"description": "type: not a key", "name": "n"},
            ),
            ("---\nmetadata:\n  note: |\n    nested\n  type: user\n---\n", {"note": "nested", "type": "user"}),
            ("---\ndescription: >-\nname: n\n---\n", {"name": "n"}),
            ("---\ndescription: >-\n  never closed\n", {}),
        ):
            with self.subTest(text=text):
                self.assertEqual(values, memory_audit.parse_frontmatter(text)[0])

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
        write(self.memory_dir / "one.md", memory("one", "Body."))
        target = self.root / "no such folder" / "report.json"
        code, stdout, stderr = run_main("audit", "--memory-dir", str(self.memory_dir), "--output", str(target))
        self.assertEqual((1, ""), (code, stderr))
        self.assertRegex(stdout, rf"\AFAILED cannot write {re.escape(str(target))}: [^\n]+\n\Z")

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


class ReindexTests(GitTestCase):
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

    def test_hooks_use_shared_flattening_before_exact_character_and_word_cutoffs(self) -> None:
        for text, limit, expected in (
            (" \talpha\r\n beta \v café\u00a0設計\u2003", 150, "alpha beta café 設計"),
            (" \t\r\n\u00a0\u2003", 150, ""),
            ("x" * 149, 150, "x" * 149),
            ("x" * 150, 150, "x" * 150),
            ("x" * 151, 150, "x" * 147 + "..."),
            ("設" * 150, 150, "設" * 150),
            ("設" * 151, 150, "設" * 147 + "..."),
            ("alpha\tbeta\r\ngamma", 10, "alpha..."),
            ("alpha,;:.\tbeta gamma", 12, "alpha..."),
        ):
            with (
                self.subTest(text=text, limit=limit),
                mock.patch.object(memory_audit, "flat_text", wraps=flat_text) as flatten,
            ):
                self.assertEqual(expected, memory_audit.one_line(text, limit))
            flatten.assert_called_once_with(text)

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
        with mock.patch.object(memory_audit.os, "replace", side_effect=PermissionError(13, "Access is denied")):
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


class DeleteTests(GitTestCase):
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
        link = self.memory_dir / "linked.md"
        if sys.platform == "win32":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
        else:
            link.symlink_to(target, target_is_directory=True)
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


class MemoryStoreGuardTests(GitTestCase):
    """delete and reindex change only a directory that is a memory store, and never one inside a skills directory."""

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

    def run_both(self, directory: Path) -> list[tuple[int, str, str]]:
        with (
            mock.patch.object(Path, "home", return_value=self.home),
            mock.patch.object(skill_roots, "SKILLS_ROOT", self.skills_root),
        ):
            return [
                run_main("reindex", "--memory-dir", str(directory), "--write"),
                run_main("delete", "--memory-dir", str(directory), "SKILL.md", "notes.md"),
            ]

    def test_a_directory_without_an_index_is_refused_and_left_alone(self) -> None:
        directory = self.root / "plain folder"
        before = self.populate(directory, index=False)
        failure = f"FAILED {directory} is not a memory directory: it holds no MEMORY.md\n"
        self.assertEqual([(1, failure, "")] * 2, self.run_both(directory))
        self.assertEqual(before, self.snapshot())

    def test_a_directory_inside_a_skills_directory_is_refused_and_left_alone(self) -> None:
        for root in (self.home / ".claude" / "skills", self.home / ".agents" / "skills", self.skills_root):
            directory = root / "some-skill"
            with self.subTest(root=root):
                before = self.populate(directory)
                failure = (
                    f"FAILED {directory} is inside the skills directory {root}; "
                    "name the memory directory resolve printed\n"
                )
                self.assertEqual([(1, failure, "")] * 2, self.run_both(directory))
                self.assertEqual(before, self.snapshot())

    def test_a_memory_store_beside_a_skills_directory_is_changed(self) -> None:
        directory = self.root / "projects" / "memory"
        self.populate(directory)
        (directory / "SKILL.md").unlink()
        write(self.skills_root / "some-skill" / "SKILL.md", "---\nname: some-skill\n---\n")
        reindexed, deleted = self.run_both(directory)
        self.assertEqual((0, f"INDEX_LINE notes.md\nWROTE {(directory / 'MEMORY.md').as_posix()}\n", ""), reindexed)
        self.assertEqual((1, "FAILED SKILL.md: no such file\n", ""), deleted)
        self.assertEqual(
            "- [notes](notes.md) — notes description\n", (directory / "MEMORY.md").read_text(encoding="utf-8")
        )


class ConsoleTests(GitTestCase):
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


class ResolveTests(GitTestCase):
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

    def resolve(self, environment: dict[str, str] | None = None, repo: Path | None = None) -> dict:
        return memory_audit.resolve(repo or self.repo, self.home, environment or {}, self.managed)

    def derived(self) -> Path:
        return self.home / ".claude" / "projects" / memory_audit.encode_project(str(self.repo.resolve())) / "memory"

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
        """Run resolve from cwd with no Claude Code environment overrides and the test's managed settings."""
        with (
            mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.root)}),
            mock.patch.object(Path, "cwd", return_value=cwd or self.repo),
            mock.patch.object(memory_audit, "managed_settings_path", return_value=self.managed),
        ):
            for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_PROJECT_DIR_NAME"):
                os.environ.pop(name, None)
            result = run_main("resolve", "--home", str(self.home), *arguments)
        self.assertEqual((code, ""), (result[0], result[2]), result[1])
        return result[1].splitlines()

    def test_command_line_defaults_to_the_current_repository(self) -> None:
        self.derived().mkdir(parents=True)
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
        self.assertEqual([mock.call(note), mock.call(" \r\n\t ")], flatten.call_args_list)

    def test_error_reasons_use_shared_flattening_and_preserve_strerror_selection(self) -> None:
        message = " \tCannot\r\n read  café\u00a0設計\u2003memory.\v "
        for error, reason, description in (
            (OSError(13, message, "one.md"), message, "Cannot read café 設計 memory.: one.md"),
            (OSError(message), message, "Cannot read café 設計 memory."),
        ):
            with (
                self.subTest(error=error),
                mock.patch.object(memory_audit, "flat_text", wraps=flat_text) as flatten,
                mock.patch.object(memory_audit, "run", side_effect=error),
            ):
                self.assertEqual((1, f"FAILED {description}\n", ""), run_main("resolve", "--repo", str(self.repo)))
            flatten.assert_called_once_with(reason)

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

    def test_invalid_or_unreadable_settings_leave_the_directory_unresolved(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        write(settings, json.dumps({"autoMemoryDirectory": "relative/path"}))
        self.assertIsNone(self.resolve()["memory_dir"])
        write(settings, "{not json")
        self.assertIsNone(self.resolve()["memory_dir"])


if __name__ == "__main__":
    unittest.main()
