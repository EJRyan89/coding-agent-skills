from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import memory_audit


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def memory(name: str, body: str, kind: str = "feedback") -> str:
    return f"---\nname: {name}\ndescription: {name} description\nmetadata:\n  type: {kind}\n---\n\n{body}\n"


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
        write(self.memory_dir / "sut.md", memory("sut", "Never name the object under test sut; derive the variable name from its class."))
        self.assertEqual([], self.by_file(self.run_audit())["sut.md"]["overlaps"])

    def test_extra_instruction_files_are_searched(self) -> None:
        instructions = write(
            self.root / "user" / "CLAUDE.md",
            "- Always prefix local branches with the personal namespace followed by a kebab-case description.\n",
        )
        write(self.memory_dir / "branch.md", memory("branch", "Prefix local branches with the personal namespace and a kebab-case description."))
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
        with redirect_stdout(captured), mock.patch.dict(os.environ, environment or {}), \
                mock.patch.object(tempfile, "tempdir", str(self.root / "Temporary Files")):
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
        self.assertEqual(2, memory_audit.main(["audit", "--memory-dir", str(self.root / "absent")]))

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
            with self.subTest(environment=environment, arguments=arguments), \
                    mock.patch.object(Path, "home", return_value=home):
                line = self.main_output(*arguments, environment=environment).strip()
                report = json.loads(Path(line.removeprefix("REPORT ")).read_text(encoding="utf-8"))
                self.assertEqual([str(expected)], [o["file"] for o in report["memories"][0]["overlaps"]])


class ReindexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.memory_dir = Path(self._temporary.name) / "memory"
        self.memory_dir.mkdir()
        self.index = self.memory_dir / "MEMORY.md"

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def reindex(self, *extra: str) -> list[str]:
        captured = io.StringIO()
        with redirect_stdout(captured):
            self.assertEqual(0, memory_audit.main(["reindex", "--memory-dir", str(self.memory_dir), *extra]))
        return captured.getvalue().splitlines()

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
        self.assertFalse(self.index.exists())
        self.reindex("--write")
        self.assertEqual("- [one](one.md) — one description\n", self.index.read_text(encoding="utf-8"))
        self.assertEqual(["MEMORY.md", "one.md"], sorted(path.name for path in self.memory_dir.iterdir()))

    def test_empty_directory_without_index_writes_nothing(self) -> None:
        self.assertEqual(["UNCHANGED"], self.reindex("--write"))
        self.assertFalse(self.index.exists())

    def test_long_descriptions_become_one_short_line(self) -> None:
        description = "word " * 60
        write(self.memory_dir / "long.md", f"---\nname: long\ndescription: {description}\n---\nBody.\n")
        self.reindex("--write")
        hook = self.index.read_text(encoding="utf-8").split(" — ", 1)[1].rstrip("\n")
        self.assertLessEqual(len(hook), 150)
        self.assertTrue(hook.endswith("word..."), hook)

    def test_index_size_against_the_load_limit_is_reported(self) -> None:
        for number in range(181):
            write(self.memory_dir / f"m{number:03}.md", memory(f"m{number}", "Body."))
        self.assertIn("NEAR_LIMIT lines=181/200 bytes=", "\n".join(self.reindex()))
        for number in range(181, 201):
            write(self.memory_dir / f"m{number:03}.md", memory(f"m{number}", "Body."))
        self.assertIn("OVER_LIMIT lines=201/200 bytes=", "\n".join(self.reindex()))

    def test_missing_directory_is_rejected(self) -> None:
        self.assertEqual(2, memory_audit.main(["reindex", "--memory-dir", str(self.memory_dir / "absent")]))


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
        git("-C", str(self.repo), "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "init")

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

    def test_invalid_or_unreadable_settings_leave_the_directory_unresolved(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        write(settings, json.dumps({"autoMemoryDirectory": "relative/path"}))
        self.assertIsNone(self.resolve()["memory_dir"])
        write(settings, "{not json")
        self.assertIsNone(self.resolve()["memory_dir"])


if __name__ == "__main__":
    unittest.main()
