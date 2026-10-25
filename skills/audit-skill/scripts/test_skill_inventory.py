from __future__ import annotations

import ast
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
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import check_provenance
import skill_inventory
from git_client import GitClient, GitError, GitResult

SCRIPT = Path(__file__).resolve().parent / "skill_inventory.py"


def write(path: Path, text: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    return path


def run(*arguments: str) -> tuple[int, list[str], str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = skill_inventory.main(list(arguments))
    return code, stdout.getvalue().splitlines(), stderr.getvalue()


def git(*arguments: str) -> None:
    subprocess.run(["git", *arguments], check=True, capture_output=True)


def execute(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-B", str(SCRIPT), *arguments], capture_output=True, text=True, check=False)


class TemporaryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()

    def tearDown(self) -> None:
        self._temporary.cleanup()


class LocateTests(TemporaryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.root / "home with space"
        self.repo = self.root / "repo"
        self.user_skills = self.home / ".claude" / "skills"

    def locate(self, name: str, *extra: str) -> tuple[int, list[str], str]:
        return run("locate", name, "--home", str(self.home), *extra)

    def test_single_user_skill_reports_file_directory_and_scope(self) -> None:
        skill = write(self.user_skills / "alpha" / "skill.md", "x")
        code, lines, _ = self.locate("alpha", "--repo", str(self.repo))
        self.assertEqual(0, code)
        self.assertEqual(
            [
                f"REPO {self.repo.as_posix()}",
                f"SKILL_FILE {skill.as_posix()}",
                f"SKILL_DIR {skill.parent.as_posix()}",
                "SCOPE user",
            ],
            lines,
        )

    def test_project_skill_found_through_the_current_git_root(self) -> None:
        git("init", "-q", str(self.repo))
        skill = write(self.repo / ".agents" / "skills" / "alpha" / "SKILL.md", "x")
        (self.repo / "nested").mkdir()
        completed = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "locate", "alpha", "--home", str(self.home)],
            cwd=self.repo / "nested",
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        lines = completed.stdout.splitlines()
        self.assertEqual(f"SKILL_FILE {skill.as_posix()}", lines[1])
        self.assertEqual("SCOPE project-agents", lines[3])

    def test_the_same_file_reached_through_two_roots_counts_once(self) -> None:
        skill = write(self.user_skills / "alpha" / "SKILL.md", "x")
        code, lines, _ = self.locate("alpha", "--repo", str(self.home))
        self.assertEqual(0, code)
        self.assertIn(f"SKILL_FILE {skill.as_posix()}", lines)

    def test_two_distinct_copies_are_ambiguous(self) -> None:
        user = write(self.user_skills / "alpha" / "SKILL.md", "x")
        project = write(self.repo / ".claude" / "skills" / "alpha" / "SKILL.md", "y")
        code, lines, _ = self.locate("alpha", "--repo", str(self.repo))
        self.assertEqual(1, code)
        self.assertEqual(
            [
                f"AMBIGUOUS user {user.as_posix()}",
                f"AMBIGUOUS project-claude {project.as_posix()}",
                f"STOP ambiguous skill name 'alpha' — found at: {user.as_posix()}, {project.as_posix()}."
                " Rename one or delete the duplicate.",
            ],
            lines[1:],
        )

    def test_a_shim_that_points_to_the_project_skill_is_that_skill(self) -> None:
        pointer = "Read and follow `../../../.claude/skills/alpha/SKILL.md` as the authoritative workflow.\n"
        write(self.repo / ".agents" / "skills" / "alpha" / "SKILL.md", pointer)
        project = write(self.repo / ".claude" / "skills" / "alpha" / "SKILL.md", "y")
        code, lines, _ = self.locate("alpha", "--repo", str(self.repo))
        self.assertEqual(0, code)
        self.assertEqual([f"SKILL_FILE {project.as_posix()}", "SCOPE project-claude"], [lines[1], lines[3]])

    def test_a_project_agents_copy_that_points_elsewhere_stays_ambiguous(self) -> None:
        pointer = "Read and follow `../../../.claude/skills/beta/SKILL.md` as the authoritative workflow.\n"
        agents = write(self.repo / ".agents" / "skills" / "alpha" / "SKILL.md", pointer)
        project = write(self.repo / ".claude" / "skills" / "alpha" / "SKILL.md", "y")
        code, lines, _ = self.locate("alpha", "--repo", str(self.repo))
        self.assertEqual(1, code)
        self.assertEqual(
            [f"AMBIGUOUS project-agents {agents.as_posix()}", f"AMBIGUOUS project-claude {project.as_posix()}"],
            lines[1:3],
        )
        self.assertTrue(lines[3].startswith("STOP ambiguous skill name 'alpha'"))

    def source_skill(self, name: str) -> Path:
        write(self.repo / "deploy-meta" / f"{name}.json", "{}")
        return write(self.repo / "skills" / name / "SKILL.md", "source")

    def test_source_skill_found_in_a_repository_with_its_metadata(self) -> None:
        skill = self.source_skill("alpha")
        code, lines, _ = self.locate("alpha", "--repo", str(self.repo))
        self.assertEqual(0, code)
        self.assertEqual(
            [
                f"REPO {self.repo.as_posix()}",
                f"SKILL_FILE {skill.as_posix()}",
                f"SKILL_DIR {skill.parent.as_posix()}",
                "SCOPE source",
            ],
            lines,
        )

    def test_source_skill_is_preferred_over_the_deployed_user_copy(self) -> None:
        write(self.user_skills / "alpha" / "SKILL.md", "deployed")
        skill = self.source_skill("alpha")
        code, lines, _ = self.locate("alpha", "--repo", str(self.repo))
        self.assertEqual(0, code)
        self.assertEqual([f"SKILL_FILE {skill.as_posix()}", "SCOPE source"], [lines[1], lines[3]])

    def test_skills_folder_without_metadata_is_not_a_source(self) -> None:
        user = write(self.user_skills / "alpha" / "SKILL.md", "deployed")
        write(self.repo / "skills" / "alpha" / "SKILL.md", "not a source skill")
        write(self.repo / "deploy-meta" / "beta.json", "{}")
        code, lines, _ = self.locate("alpha", "--repo", str(self.repo))
        self.assertEqual(0, code)
        self.assertEqual([f"SKILL_FILE {user.as_posix()}", "SCOPE user"], [lines[1], lines[3]])

    def test_source_skill_and_a_project_skill_stay_ambiguous(self) -> None:
        source = self.source_skill("alpha")
        write(self.user_skills / "alpha" / "SKILL.md", "deployed")
        project = write(self.repo / ".claude" / "skills" / "alpha" / "SKILL.md", "y")
        code, lines, _ = self.locate("alpha", "--repo", str(self.repo))
        self.assertEqual(1, code)
        self.assertEqual(
            [f"AMBIGUOUS project-claude {project.as_posix()}", f"AMBIGUOUS source {source.as_posix()}"], lines[1:3]
        )
        self.assertTrue(lines[3].startswith("STOP ambiguous skill name 'alpha'"))

    def test_missing_skill_lists_available_source_skills(self) -> None:
        self.source_skill("alpha")
        write(self.repo / "skills" / "shared-asset" / "SKILL.md", "x")
        code, lines, _ = self.locate("missing", "--repo", str(self.repo))
        self.assertEqual(1, code)
        self.assertEqual(
            [
                "NOT_FOUND missing",
                "AVAILABLE source alpha",
                "STOP skill 'missing' not found. Available skills: source: alpha",
            ],
            lines[1:],
        )

    def test_missing_skill_lists_available_skills_per_scope(self) -> None:
        write(self.user_skills / "Beta" / "SKILL.md", "x")
        write(self.user_skills / "alpha" / "skill.md", "x")
        (self.user_skills / "empty").mkdir()
        write(self.repo / ".agents" / "skills" / "gamma" / "SKILL.md", "x")
        code, lines, _ = self.locate("missing", "--repo", str(self.repo))
        self.assertEqual(1, code)
        self.assertEqual(
            [
                "NOT_FOUND missing",
                "AVAILABLE user alpha",
                "AVAILABLE user Beta",
                "AVAILABLE project-agents gamma",
                "STOP skill 'missing' not found. Available skills: user: alpha, Beta / project-agents: gamma",
            ],
            lines[1:],
        )

    def test_the_stop_line_names_every_scope_as_locate_prints_it(self) -> None:
        write(self.user_skills / "alpha" / "SKILL.md", "x")
        write(self.repo / ".agents" / "skills" / "beta" / "SKILL.md", "x")
        write(self.repo / ".claude" / "skills" / "gamma" / "SKILL.md", "x")
        write(self.repo / ".claude" / "skills" / "delta" / "SKILL.md", "x")
        self.source_skill("epsilon")
        code, lines, _ = self.locate("missing", "--repo", str(self.repo))
        self.assertEqual(1, code)
        self.assertEqual(
            "STOP skill 'missing' not found. Available skills: user: alpha / project-agents: beta"
            " / project-claude: delta, gamma / source: epsilon",
            lines[-1],
        )

    def test_outside_a_repository_only_the_user_scope_is_searched(self) -> None:
        write(self.root / ".claude" / "skills" / "alpha" / "SKILL.md", "x")
        lines = skill_inventory.locate("alpha", self.home, None)
        self.assertEqual(
            ["REPO none", "NOT_FOUND alpha", "STOP skill 'alpha' not found. Available skills: none"], lines
        )

    def test_an_empty_name_stops_with_the_usage_line(self) -> None:
        for arguments in (("",), ("  ",), ()):
            with self.subTest(arguments=arguments):
                code, lines, error = run("locate", *arguments, "--home", str(self.home), "--repo", str(self.repo))
                self.assertEqual((1, ""), (code, error))
                self.assertEqual(["STOP usage: audit-skill [<plugin>:]<SkillName> [<plugin folder>]"], lines)

    def test_invalid_names_are_rejected(self) -> None:
        for name in ("../alpha", "a/b", "a:b:c", "plug/in:alpha"):
            with self.subTest(name=name):
                code, lines, error = self.locate(name)
                self.assertEqual((1, ""), (code, error))
                self.assertEqual(1, len(lines))
                self.assertTrue(lines[0].startswith("FAILED invalid skill name"))

    def test_git_toplevel_finds_the_root_or_nothing(self) -> None:
        git("init", "-q", str(self.repo))
        (self.repo / "sub").mkdir()
        toplevel = skill_inventory.git_toplevel(self.repo / "sub")
        if toplevel is None:
            self.fail("a subdirectory of a repository has a top level")
        self.assertEqual(self.repo, toplevel.resolve())
        plain = self.root / "plain"
        plain.mkdir()
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.root)}):
            self.assertIsNone(skill_inventory.git_toplevel(plain))

    def test_git_that_cannot_run_or_finish_gives_no_top_level(self) -> None:
        commands: list[list[str]] = []

        def stalled(command: Sequence[str], timeout: float) -> GitResult:
            commands.append(list(command))
            raise GitError("git did not finish within 300 seconds", kind="timeout")

        self.assertIsNone(skill_inventory.git_toplevel(self.root, GitClient(stalled)))
        self.assertEqual(
            [["git", "-C", str(self.root), "rev-parse", "--path-format=absolute", "--show-toplevel"]], commands
        )

    def test_the_top_level_is_the_absolute_path_git_answers(self) -> None:
        answers = {
            "a path": (GitResult(0, "C:/work/repo with space\n", ""), Path("C:/work/repo with space")),
            "a failure": (GitResult(128, "", "fatal: not a git repository"), None),
            "nothing": (GitResult(0, "\n", ""), None),
        }
        for case, (answer, expected) in answers.items():
            commands: list[list[str]] = []

            def answering(
                command: Sequence[str],
                timeout: float,
                *,
                answer: GitResult = answer,
                commands: list[list[str]] = commands,
            ) -> GitResult:
                commands.append(list(command))
                return answer

            with self.subTest(case=case):
                self.assertEqual(expected, skill_inventory.git_toplevel(self.root, GitClient(answering)))
                self.assertEqual(
                    [["git", "-C", str(self.root), "rev-parse", "--path-format=absolute", "--show-toplevel"]],
                    commands,
                )


class InventoryTests(TemporaryTestCase):
    def inventory(self) -> list[str]:
        code, lines, error = run("inventory", str(self.root))
        self.assertEqual(0, code, error)
        return lines

    def test_files_are_classified_sized_and_estimated(self) -> None:
        # prose: "---\n" "name: x\n" "---\n" "abcd\n" = 21 chars; code: "```bash\n" "ls\n" "```\n" = 15 chars.
        write(self.root / "SKILL.md", "---\nname: x\n---\nabcd\n```bash\nls\n```\n")
        write(self.root / "scripts" / "run.py", "x = 1\n")
        write(self.root / "docs" / "guide.md", "12345678\n")
        write(self.root / "data.json", "{}\n")
        write(self.root / "logo.png", b"\x89PNG\0\x01")
        write(self.root / "scripts" / "__pycache__" / "run.pyc", b"\0")
        self.assertEqual(
            [
                "FILE data 3 1 data.json",
                "FILE doc 9 3 docs/guide.md",
                "FILE data 6 0 logo.png",
                "FILE helper 6 2 scripts/run.py",
                "FILE main 36 11 SKILL.md",
                "TOTAL main 1 36 11",
                "TOTAL helper 1 6 2",
                "TOTAL doc 1 9 3",
                "TOTAL data 2 9 1",
                "TOTAL all 5 60 17",
                "TOTAL outside 0 0 0",
            ],
            self.inventory(),
        )

    def structure_flags(self) -> list[str]:
        return [line for line in self.inventory() if line.startswith(("BODY_", "DOC_", "NESTED_"))]

    def test_byte_size_alone_is_never_flagged(self) -> None:
        write(self.root / "SKILL.md", "x" * 9000)
        write(self.root / "notes.md", "x" * 5000)
        self.assertEqual([], self.structure_flags())

    def test_a_body_over_500_lines_after_its_frontmatter_is_flagged(self) -> None:
        frontmatter_lines = "---\nname: demo\ndescription: d\n---\n"
        write(self.root / "SKILL.md", frontmatter_lines + "line\n" * 500)
        self.assertEqual([], self.structure_flags())
        write(self.root / "SKILL.md", frontmatter_lines + "line\n" * 501)
        self.assertEqual(["BODY_OVER_500_LINES 501"], self.structure_flags())

    def test_a_doc_over_100_lines_needs_a_table_of_contents_first(self) -> None:
        write(self.root / "SKILL.md", "x\n")
        write(self.root / "short.md", "# Short\n" + "line\n" * 99)
        write(self.root / "listed.md", "# Listed\n\n## Contents\n- One\n\n## One\n" + "line\n" * 100)
        write(self.root / "toc.md", "# Toc\n\n### Table of contents\n- One\n\n## One\n" + "line\n" * 100)
        write(self.root / "late.md", "# Late\n\n## One\n" + "line\n" * 100 + "## Contents\n")
        write(self.root / "fenced.md", "```markdown\n## Contents\n```\n" + "line\n" * 100)
        self.assertEqual(["DOC_NO_TOC 103 fenced.md", "DOC_NO_TOC 104 late.md"], self.structure_flags())

    def test_a_doc_reached_only_through_another_doc_is_a_nested_reference(self) -> None:
        write(self.root / "SKILL.md", "---\nname: demo\n---\nRead [a](references/a.md).\n")
        write(
            self.root / "references" / "a.md",
            "See `b.md`, `references/b.md`, and ${CLAUDE_SKILL_DIR}/references/b.md.\n",
        )
        write(self.root / "references" / "b.md", "Back to `a.md`; `missing.md`, `../outside.md`, `SKILL.md`.\n")
        self.assertEqual(
            [
                "NESTED_REFERENCE references/a.md:1 b.md",
                "NESTED_REFERENCE references/a.md:1 ${CLAUDE_SKILL_DIR}/references/b.md",
            ],
            self.structure_flags(),
        )
        write(
            self.root / "SKILL.md",
            "---\nname: demo\n---\nRead `references/a.md`, then ${CLAUDE_SKILL_DIR}/references/b.md.\n",
        )
        self.assertEqual([], self.structure_flags())

    def test_missing_main_file_is_flagged(self) -> None:
        write(self.root / "README.md", "x")
        self.assertIn("NO_MAIN", self.inventory())

    def test_duplicated_blocks_and_inlined_helpers_are_flagged(self) -> None:
        block = "```bash\nset -e\necho one\necho two\n```\n"
        write(self.root / "SKILL.md", f"Intro\n{block}\nAgain\n{block}")
        write(self.root / "scripts" / "run.sh", "#!/usr/bin/env bash\n  set -e\n\necho one\necho two\nexit 0\n")
        flags = [line for line in self.inventory() if line.startswith(("DUPLICATE_", "INLINED_"))]
        self.assertEqual(
            [
                "INLINED_HELPER SKILL.md:2 scripts/run.sh",
                "DUPLICATE_BLOCK SKILL.md:9 SKILL.md:2",
                "INLINED_HELPER SKILL.md:9 scripts/run.sh",
            ],
            flags,
        )

    def test_reads_outside_the_folder_and_declared_dependencies_are_reported(self) -> None:
        source = self.root / "source"
        skill = source / "skills" / "alpha"
        write(
            skill / "SKILL.md",
            "---\n"
            "description: see `../ignored.md`\n"  # 2: frontmatter is skipped
            "---\n"
            "Read `../shared.md`.\n"  # 4
            "Then read `${CLAUDE_SKILL_DIR}/../beta/SKILL.md` and `../shared.md` again.\n"  # 5: counted once
            "See `../missing.md`.\n"  # 6
            "Inside: `./notes.md` and `../alpha/notes.md`.\n",
        )  # 7: inside the skill folder
        # A plain relative path resolves against docs/, a ${CLAUDE_SKILL_DIR} one against the skill folder.
        write(skill / "docs" / "guide.md", "Also `../../shared.md` and `${CLAUDE_SKILL_DIR}/../shared.md`.\n")
        write(source / "skills" / "shared.md", "12345678\n")  # 9 bytes, ceil(9 / 4) = 3 tokens
        write(source / "skills" / "beta" / "SKILL.md", "abcd\n")  # 5 bytes, 2 tokens
        write(source / "deploy-meta" / "alpha.json", '{"shared_deps": ["shared.md"], "skill_deps": ["beta"]}')
        code, lines, error = run("inventory", str(skill))
        self.assertEqual(0, code, error)
        self.assertEqual(
            [
                "OUTSIDE_READ docs/guide.md:1 9 3 ../../shared.md",
                "OUTSIDE_READ docs/guide.md:1 9 3 ${CLAUDE_SKILL_DIR}/../shared.md",
                "OUTSIDE_READ SKILL.md:4 9 3 ../shared.md",
                "OUTSIDE_READ SKILL.md:5 5 2 ${CLAUDE_SKILL_DIR}/../beta/SKILL.md",
                "OUTSIDE_READ SKILL.md:5 9 3 ../shared.md",
                "OUTSIDE_MISSING SKILL.md:6 ../missing.md",
                "TOTAL outside 2 14 5",
                "DECLARED shared shared.md",
                "DECLARED skill beta",
            ],
            [line for line in lines if line.startswith(("OUTSIDE", "TOTAL outside", "DECLARED"))],
        )

    def test_this_skill_names_no_file_outside_its_folder(self) -> None:
        # Auditing the audit skill must not report its own prose example as an outside read.
        code, lines, error = run("inventory", str(SCRIPT.parent.parent))
        self.assertEqual(0, code, error)
        self.assertEqual(
            ["TOTAL outside 0 0 0"], [line for line in lines if line.startswith(("OUTSIDE", "TOTAL outside"))]
        )

    def test_markdown_the_skill_loads_that_is_not_utf8_is_unreadable_and_fails_the_inventory(self) -> None:
        # A runtime's file reader decodes UTF-16, so such a file still costs tokens; it is never reported as free.
        skill = self.root / "skills" / "u"
        long_doc = ("# Guide\n" + "line\n" * 200).encode("utf-16")
        write(skill / "SKILL.md", "---\nname: u\n---\nRead `references/guide.md`.\nAnd `../shared.md`.\n")
        write(skill / "references" / "guide.md", long_doc)
        write(skill / "references" / "legacy.md", "caf\xe9\n".encode("cp1252"))
        write(self.root / "skills" / "shared.md", long_doc)
        # A helper or data file is measured rather than loaded, so one that is not UTF-8 still counts 0.
        write(skill / "scripts" / "tool.ps1", "Write-Output 1\n".encode("utf-16"))
        write(skill / "logo.png", b"\x89PNG\0\x01")
        code, lines, error = run("inventory", str(skill))
        self.assertEqual((1, ""), (code, error))
        self.assertEqual(
            [
                "UNREADABLE references/guide.md not UTF-8 text",
                "UNREADABLE references/legacy.md not UTF-8 text",
                "TOTAL outside 0 0 0",
                "UNREADABLE ../shared.md not UTF-8 text",
            ],
            [line for line in lines if line.startswith(("UNREADABLE", "OUTSIDE", "TOTAL outside", "DOC_"))],
        )
        self.assertIn("FILE helper", next(line for line in lines if line.endswith("scripts/tool.ps1")))
        unreadable = write(self.root / "skills" / "v" / "SKILL.md", "x\n".encode("utf-16"))
        code, lines, _ = run("inventory", str(unreadable.parent))
        self.assertEqual((1, ["UNREADABLE SKILL.md not UTF-8 text"]), (code, [x for x in lines if "UNREADABLE" in x]))

    def test_missing_directory_fails(self) -> None:
        code, lines, error = run("inventory", str(self.root / "absent"))
        self.assertEqual((1, ""), (code, error))
        self.assertEqual(1, len(lines))
        self.assertTrue(lines[0].startswith("FAILED skill directory not found"))

    def test_an_unreadable_file_fails_with_its_path(self) -> None:
        write(self.root / "SKILL.md", "x")
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError(13, "Permission denied", "locked.md")):
            code, lines, error = run("inventory", str(self.root))
        self.assertEqual((1, ""), (code, error))
        self.assertEqual(["FAILED cannot read locked.md: Permission denied"], lines)


class ToolsTests(TemporaryTestCase):
    def tools(self, text: str) -> list[str]:
        code, lines, error = run("tools", str(write(self.root / "SKILL.md", text)))
        self.assertEqual(0, code, error)
        return lines

    def test_allowed_tools_formats_and_model(self) -> None:
        cases = {
            'allowed-tools: ["Bash", "Read"]\nmodel: "haiku"': ["MODEL haiku", "ALLOWED Bash", "ALLOWED Read"],
            "allowed-tools: Bash(git status:*), Bash(git add:*) Read": ["MODEL none", "ALLOWED Bash", "ALLOWED Read"],
            "allowed-tools: Bash(p), PowerShell(p)": ["MODEL none", "ALLOWED Bash", "ALLOWED PowerShell"],
            "allowed-tools: Bash(echo $(date) now) Read": ["MODEL none", "ALLOWED Bash", "ALLOWED Read"],
            "allowed-tools:\n  - Grep\n  - mcp__srv__find\nmodel: sonnet": [
                "MODEL sonnet",
                "ALLOWED Grep",
                "ALLOWED mcp__srv__find",
            ],
            "name: x": ["MODEL none", "NO_ALLOWED_TOOLS"],
        }
        for frontmatter, expected in cases.items():
            with self.subTest(frontmatter=frontmatter):
                lines = self.tools(f"---\n{frontmatter}\n---\nBody.\n")
                self.assertEqual(expected, [line for line in lines if line.startswith(("MODEL", "ALLOWED", "NO_"))])

    def test_only_tool_use_contexts_count_as_references(self) -> None:
        lines = self.tools(
            "---\n"
            'allowed-tools: ["Bash", "Read", "Grep", "Write", "PowerShell", "Edit"]\n'
            "---\n"
            "Before executing, read and apply the contract.\n"  # 4: implied Read, not used
            "Read the file. Grep the output.\n"  # 5: sentence verbs only
            "Use `Grep -l` for the list.\n"  # 6
            'Run `Glob("*", path=x)` first.\n'  # 7
            "Then start the Agent tool and AskUserQuestion.\n"  # 8
            "Call mcp__srv__find with the name.\n"  # 9
            "```powershell\nGet-ChildItem\n```\n"  # 10-12
            "```bash\nls\n```\n"  # 13-15
            "```text\nEdit(x) in a text fence\n```\n"  # 16-18
        )
        self.assertEqual(
            [
                "USED Grep 6",
                "USED Glob 7",
                "USED Agent 8",
                "USED AskUserQuestion 8",
                "USED mcp__srv__find 9",
                "USED Bash 10",
                "USED PowerShell 10",
                "USED Edit 17",
                "IMPLIED Read 4",
                "UNUSED_ALLOWED Write",
                "MISSING_ALLOWED Glob 7",
                "MISSING_ALLOWED Agent 8",
                "MISSING_ALLOWED AskUserQuestion 8",
                "MISSING_ALLOWED mcp__srv__find 9",
                "UNSCOPED_ALLOWED Bash",
                "UNSCOPED_ALLOWED PowerShell",
            ],
            [line for line in lines if not line.startswith(("MODEL", "DESCRIPTION", "INVOCATION", "ALLOWED"))],
        )

    def test_powershell_fence_uses_bash_unless_powershell_is_allowed(self) -> None:
        lines = self.tools('---\nallowed-tools: ["Bash"]\n---\n```pwsh\nGet-Date\n```\n')
        self.assertEqual(["USED Bash 4"], [line for line in lines if line.startswith(("USED", "UNUSED", "MISSING"))])

    def test_a_shell_fence_uses_every_allowed_shell_since_the_model_may_pick_either(self) -> None:
        # On Windows Claude Code may run a bash fence through its PowerShell tool, so a PowerShell grant is used.
        for fence in ("bash", "pwsh", "console", "sh", "shell"):
            with self.subTest(fence=fence):
                lines = self.tools(
                    f'---\nallowed-tools: ["Bash(ls *)", "PowerShell(ls *)"]\n---\n```{fence}\nls x\ngit status\n```\n'
                )
                self.assertEqual(
                    [
                        "USED Bash 4",
                        "USED PowerShell 4",
                        "UNGRANTED Bash 6 git status",
                        "UNGRANTED PowerShell 6 git status",
                    ],
                    [line for line in lines if line.startswith(("USED", "UNUSED", "MISSING", "UNGRANTED", "IDLE"))],
                )

    def test_prose_that_implies_running_a_command_implies_either_shell(self) -> None:
        lines = self.tools('---\nallowed-tools: ["Bash(ls *)", "PowerShell(ls *)"]\n---\nRun the scripts.\n')
        self.assertEqual(
            ["IMPLIED Bash 4", "IMPLIED PowerShell 4"],
            [line for line in lines if line.startswith(("IMPLIED", "UNUSED"))],
        )

    def implied(self, body: str, allowed: str = '["Read", "Edit", "AskUserQuestion", "Grep"]') -> list[str]:
        lines = self.tools(f"---\nallowed-tools: {allowed}\n---\n{body}")
        return [line for line in lines if line.startswith(("USED", "IMPLIED", "UNUSED"))]

    def test_a_prohibited_action_does_not_imply_its_tool(self) -> None:
        cases = {
            "Run the commands; do not read the archive or the scripts yourself.\n": "Read",
            "Never write, modify, or delete files, and never edit a report by hand.\n": "Edit",
            "Report the result without asking the user.\n": "AskUserQuestion",
            "Do not import the modules, write glue code, or read the core scripts.\n": "Read",
            "This step does not read files.\n": "Read",
            "Never open `notes. old` or search it.\n": "Grep",  # a code span's dot ends no clause
            "Do not cross the line, but never read it.\n": "Read",
        }
        for body, tool in cases.items():
            with self.subTest(body=body):
                self.assertIn(f"UNUSED_ALLOWED {tool}", self.implied(body))

    def test_an_action_outside_the_negation_still_implies_its_tool(self) -> None:
        cases = {
            "If it is not a repository, ask the user which one to use.\n": "IMPLIED AskUserQuestion 4",
            "If there is no file, or the file does not exist, "
            "list them and ask the user.\n": "IMPLIED AskUserQuestion 4",
            "Do not count braces yourself. On failure, read the end of the log.\n": "IMPLIED Read 4",
            "Do not count braces: read the log.\n": "IMPLIED Read 4",
            "Do not edit the file you read.\n": "IMPLIED Read 4",
            "Do not stop, but read the log.\n": "IMPLIED Read 4",
            "Never guess. Search the archive.\n": "IMPLIED Grep 4",
            "Pass `--read-only`; never edit by hand.\n": "IMPLIED Read 4",
        }
        for body, expected in cases.items():
            with self.subTest(body=body):
                self.assertIn(expected, self.implied(body))

    def test_a_prompt_handed_to_a_subagent_neither_uses_nor_implies_its_tools(self) -> None:
        prompt = (
            "Start one subagent with the Agent tool, with exactly this prompt and nothing else: "
            "`Read <prompt file> and follow it exactly.`\n"
        )
        self.assertEqual(["USED Agent 4", "UNUSED_ALLOWED Read"], self.implied(prompt, '["Read", "Agent"]'))
        quoted = 'Give the subagent this prompt: "Read the plan and ask the user." Then report.\n'
        self.assertEqual(
            ["UNUSED_ALLOWED Read", "UNUSED_ALLOWED AskUserQuestion"],
            self.implied(quoted, '["Read", "AskUserQuestion"]'),
        )

    def test_a_quoted_span_that_is_not_a_subagent_prompt_still_counts(self) -> None:
        cases = {
            "Use `Read` on the plan file.\n": "USED Read 4",
            "Answer the prompt: `Read the plan`.\n": "USED Read 4",
            "Write the subagent's prompt file, then read it back.\n": "IMPLIED Read 4",
        }
        for body, expected in cases.items():
            with self.subTest(body=body):
                self.assertIn(expected, self.implied(body, '["Read"]'))

    def grants(self, allowed: str, body: str = "") -> list[str]:
        lines = self.tools(f"---\nallowed-tools: {allowed}\n---\n{body}")
        return [line for line in lines if line.startswith(("UNSCOPED", "UNPAIRED", "UNGRANTED"))]

    def test_unscoped_shell_grants_are_reported(self) -> None:
        cases = {
            '["Bash", "Read"]': ["UNSCOPED_ALLOWED Bash"],
            '["PowerShell(*)", "Bash(:*)"]': ["UNSCOPED_ALLOWED PowerShell(*)", "UNSCOPED_ALLOWED Bash(:*)"],
            "Bash Read": ["UNSCOPED_ALLOWED Bash"],
            '["Bash(git status)", "PowerShell(git status)", "Read"]': [],
        }
        for allowed, expected in cases.items():
            with self.subTest(allowed=allowed):
                self.assertEqual(expected, self.grants(allowed))

    def test_a_shell_pattern_without_its_twin_is_reported(self) -> None:
        self.assertEqual(
            ["UNPAIRED_ALLOWED Bash(git status)", "UNPAIRED_ALLOWED PowerShell(gh auth status)"],
            self.grants('["Bash(git status)", "Bash(ls *)", "PowerShell(ls *)", "PowerShell(gh auth status)"]'),
        )

    def test_a_sibling_grant_that_does_not_end_at_one_script_is_reported(self) -> None:
        sibling = 'python -B "${CLAUDE_SKILL_DIR}/../core/scripts/'
        cases = {
            f"{sibling}*": True,
            f'{sibling}a*.py" *': True,
            f"{sibling}": True,
            "python -B ${CLAUDE_SKILL_DIR}/../core/scripts/a.py *": True,
            f'{sibling}a.py" *': False,
            f'{sibling}a.py"': False,
            'python -B "${CLAUDE_SKILL_DIR}/scripts/*': False,
        }
        for pattern, reported in cases.items():
            with self.subTest(pattern=pattern):
                allowed = json.dumps([f"Bash({pattern})", f"PowerShell({pattern})"])
                lines = self.tools(f"---\nallowed-tools: {allowed}\n---\n")
                expected = [f"SIBLING_FOLDER_ALLOWED {tool}({pattern})" for tool in ("Bash", "PowerShell")]
                self.assertEqual(expected if reported else [], [x for x in lines if x.startswith("SIBLING_")])

    def test_a_pattern_whose_wildcard_continues_its_path_is_reported(self) -> None:
        own = 'python -B "${CLAUDE_SKILL_DIR}/scripts/'
        cases = {
            f"{own}*": True,
            f'{own}a*.py" *': True,
            f"{own}a.py *": True,
            "python tools/a.py*": True,
            "python tools/*": True,
            "python tools/a.py:*": False,
            "* --version": True,
            f'{own}a.py" *': False,
            f'{own}a.py"': False,
            "python tools/a.py *": False,
            "gh pr create *": False,
            "git rev-parse --show-toplevel": False,
            # A sibling's folder is reported once, as SIBLING_FOLDER_ALLOWED.
            'python -B "${CLAUDE_SKILL_DIR}/../core/scripts/*': False,
        }
        for pattern, reported in cases.items():
            with self.subTest(pattern=pattern):
                allowed = json.dumps([f"Bash({pattern})", f"PowerShell({pattern})"])
                lines = self.tools(f"---\nallowed-tools: {allowed}\n---\n")
                expected = [f"OPEN_PATH_ALLOWED {tool}({pattern})" for tool in ("Bash", "PowerShell")]
                self.assertEqual(expected if reported else [], [x for x in lines if x.startswith("OPEN_PATH")])

    def test_a_pattern_that_names_no_script_before_its_wildcard_is_reported(self) -> None:
        own = 'python -B "${CLAUDE_SKILL_DIR}/scripts/'
        cases = {
            "python *": True,
            "python -B *": True,
            "{{PYTHON}} -c *": True,
            "python -m pytest *": True,
            "python -X a.py *": True,
            "python -Bc *": True,
            "bash -c *": True,
            "pwsh -Command *": True,
            "pwsh -File a.ps1 *": True,
            "node -e *": True,
            "git *": True,
            "git -C repo *": True,
            "gh *": True,
            "ls *": True,
            f'{own}a.py" *': False,
            "python tools/a.py *": False,
            "python -B -I tools/a.py *": False,
            'bash "${CLAUDE_SKILL_DIR}/scripts/update.sh" *': False,
            '"${CLAUDE_SKILL_DIR}/scripts/run.sh" *': False,
            "pwsh a.ps1 *": False,
            "gh pr create *": False,
            "git status:*": False,
            # A pattern with no * matches only the command it names.
            "git rev-parse --show-toplevel": False,
            'python -c "print(1)"': False,
            # A pattern whose * continues a path is reported once, as OPEN_PATH_ALLOWED.
            f"{own}*": False,
        }
        for pattern, reported in cases.items():
            with self.subTest(pattern=pattern):
                allowed = json.dumps([f"Bash({pattern})", f"PowerShell({pattern})"])
                lines = self.tools(f"---\nallowed-tools: {allowed}\n---\n")
                expected = [f"NO_SCRIPT_ALLOWED {tool}({pattern})" for tool in ("Bash", "PowerShell")]
                self.assertEqual(expected if reported else [], [x for x in lines if x.startswith("NO_SCRIPT")])

    def test_commands_are_read_from_every_fence_form_and_from_code_spans_that_run_a_script(self) -> None:
        # The grant checks read a command wherever the body shows one: a fence Markdown allows anywhere, under any
        # command shell's label, and a code span outside a fence in which a program runs a script.
        write(self.root / "skill" / "scripts" / "start.py", 'RUNS_REPOSITORY_CODE = "starts configured servers"\n')
        command = 'python -B "${CLAUDE_SKILL_DIR}/scripts/start.py" go'
        shapes = {
            "a top-level fence": f"```bash\n{command}\n```\n",
            "a fence indented under a list item": f"1. Run:\n\n    ```bash\n    {command}\n    ```\n",
            "a fence on a list item's marker line": f"- ```sh\n  {command}\n  ```\n",
            "a fence in a blockquote": f"> ```bash\n> {command}\n> ```\n",
            "a tilde fence": f"~~~powershell\n{command}\n~~~\n",
            "a zsh fence": f"```zsh\n{command}\n```\n",
            "a code span": f"Run `{command}` from the root.\n",
        }
        wide = json.dumps(['Bash(python -B "${CLAUDE_SKILL_DIR}/scripts/start.py" *)'])
        for shape, body in shapes.items():
            with self.subTest(shape=shape):
                found = [line for line in self.repository_code(wide, body) if line.startswith("GRANTED")]
                self.assertEqual(1, len(found), found)
                self.assertTrue(found[0].endswith(command), found)
        not_commands = {
            "a span that only names the script": "See `${CLAUDE_SKILL_DIR}/scripts/start.py`.\n",
            "a span that starts with the script": "Its `start.py go` command.\n",
            "a text fence": f"```text\n{command}\n```\n",
            "a span inside a fence": f"```text\n`{command}`\n```\n",
        }
        for shape, body in not_commands.items():
            with self.subTest(shape=shape):
                self.assertEqual([], self.repository_code(wide, body))

    def test_a_code_span_that_runs_a_script_is_checked_as_a_fence_command_is(self) -> None:
        own = 'python -B "${CLAUDE_SKILL_DIR}/scripts/'
        allowed = json.dumps([f'Bash({own}a.py" *)', f'PowerShell({own}a.py" *)'])
        body = f'Run `{own}a.py" go`, then `{own}b.py" go`, and `{own}a.py" --cwd "$PWD"`.\n'
        lines = self.tools(f"---\nallowed-tools: {allowed}\n---\n{body}")
        self.assertEqual(
            [
                f'UNGRANTED Bash 4 {own}b.py" go',
                f'UNGRANTED PowerShell 4 {own}b.py" go',
                f'EXPANDS 4 {own}a.py" --cwd "$PWD"',
            ],
            [line for line in lines if line.startswith(("UNGRANTED", "EXPANDS"))],
        )

    def test_each_parenthesized_entry_of_a_plain_scalar_is_its_own_grant(self) -> None:
        cases = {
            "Bash(ls *), PowerShell(ls *)": [],
            "Bash(ls *) PowerShell(ls *), Read": [],
            "Bash(ls *), PowerShell(git status)": [
                "UNPAIRED_ALLOWED Bash(ls *)",
                "UNPAIRED_ALLOWED PowerShell(git status)",
                "UNGRANTED PowerShell 5 ls x",
            ],
            "Bash(echo $(date) *), PowerShell(echo $(date) *)": [
                "UNGRANTED Bash 5 ls x",
                "UNGRANTED PowerShell 5 ls x",
            ],
        }
        for allowed, expected in cases.items():
            with self.subTest(allowed=allowed):
                self.assertEqual(expected, self.grants(allowed, "```bash\nls x\n```\n"))

    def test_commands_no_pattern_grants_are_reported_per_shell(self) -> None:
        own = 'python -B "${CLAUDE_SKILL_DIR}/scripts/'
        allowed = json.dumps([f"Bash({own}*)", f"PowerShell({own}*)", "Bash(git rev-parse:*)"])
        body = (
            "```bash\n"
            f'{own}x.py" run --plan "<plan file>"\n'  # 5: granted to both
            "git rev-parse --show-toplevel\n"  # 6: granted to Bash only
            "\n"
            "# a comment\n"
            "python -B .github/scripts/ai_config.py --check\n"  # 9: granted to neither
            'python -B "${CLAUDE_SKILL_DIR}/../core/scripts/y.py" &&\n'  # 10: a sibling's script
            "```\n"
        )
        self.assertEqual(
            [
                "UNPAIRED_ALLOWED Bash(git rev-parse:*)",
                "UNGRANTED PowerShell 6 git rev-parse --show-toplevel",
                "UNGRANTED Bash 9 python -B .github/scripts/ai_config.py --check",
                "UNGRANTED PowerShell 9 python -B .github/scripts/ai_config.py --check",
                'UNGRANTED Bash 10 python -B "${CLAUDE_SKILL_DIR}/../core/scripts/y.py"',
                'UNGRANTED PowerShell 10 python -B "${CLAUDE_SKILL_DIR}/../core/scripts/y.py"',
            ],
            self.grants(allowed, body),
        )

    def test_a_script_that_runs_repository_code_is_reported_apart_from_ungranted_commands(self) -> None:
        # Such a script starts code the target repository configures, so its command must keep prompting: one a
        # grant covers is reported, and one no grant covers is the intended state rather than a missing grant.
        skill = self.root / "skill"
        write(skill / "scripts" / "start.py", 'RUNS_REPOSITORY_CODE = "starts configured servers"\n')
        write(skill / "scripts" / "empty.py", 'RUNS_REPOSITORY_CODE = ""\n')
        write(skill / "scripts" / "nested.py", 'def f() -> None:\n    RUNS_REPOSITORY_CODE = "x"\n')
        write(skill / "scripts" / "broken.py", "RUNS_REPOSITORY_CODE = (\n")
        write(self.root / "core" / "scripts" / "sibling.py", 'RUNS_REPOSITORY_CODE = "starts a server"\n')
        own = 'python -B "${CLAUDE_SKILL_DIR}/scripts/'
        body = (
            "```bash\n"
            f'{own}start.py" --root "<repository root>"\n'  # 5
            f'{own}empty.py"\n'  # 6
            f'{own}nested.py"\n'  # 7
            f'{own}broken.py"\n'  # 8
            f'{own}missing.py"\n'  # 9
            'python -B "${CLAUDE_SKILL_DIR}/../core/scripts/sibling.py"\n'  # 10
            "```\n"
        )
        narrow = json.dumps([f'Bash({own}empty.py")', f'PowerShell({own}empty.py")'])
        self.assertEqual(
            [
                f'REPOSITORY_CODE Bash 5 {own}start.py" --root "<repository root>"',
                f'REPOSITORY_CODE PowerShell 5 {own}start.py" --root "<repository root>"',
            ]
            + [
                f'UNGRANTED {tool} {number} {own}{name}.py"'
                for number, name in ((7, "nested"), (8, "broken"), (9, "missing"))
                for tool in ("Bash", "PowerShell")
            ]
            + [
                'REPOSITORY_CODE Bash 10 python -B "${CLAUDE_SKILL_DIR}/../core/scripts/sibling.py"',
                'REPOSITORY_CODE PowerShell 10 python -B "${CLAUDE_SKILL_DIR}/../core/scripts/sibling.py"',
            ],
            self.repository_code(narrow, body),
        )
        wide = json.dumps(["Bash(python -B *)", "PowerShell(python -B *)"])
        self.assertEqual(
            [
                f'GRANTED_REPOSITORY_CODE Bash 5 {own}start.py" --root "<repository root>"',
                f'GRANTED_REPOSITORY_CODE PowerShell 5 {own}start.py" --root "<repository root>"',
                'GRANTED_REPOSITORY_CODE Bash 10 python -B "${CLAUDE_SKILL_DIR}/../core/scripts/sibling.py"',
                'GRANTED_REPOSITORY_CODE PowerShell 10 python -B "${CLAUDE_SKILL_DIR}/../core/scripts/sibling.py"',
            ],
            self.repository_code(wide, body),
        )
        # Without a shell grant nothing is pre-approved, so the command prompts as it should.
        self.assertIn(
            f'REPOSITORY_CODE Bash 5 {own}start.py" --root "<repository root>"', self.repository_code('["Read"]', body)
        )

    def repository_code(self, allowed: str, body: str) -> list[str]:
        text = f"---\nallowed-tools: {allowed}\n---\n{body}"
        code, lines, error = run("tools", str(write(self.root / "skill" / "SKILL.md", text)))
        self.assertEqual(0, code, error)
        return [line for line in lines if line.startswith(("UNGRANTED", "REPOSITORY_CODE", "GRANTED_REPOSITORY"))]

    def test_commands_that_expand_shell_variables_are_reported(self) -> None:
        # Claude Code 2.1.288 asked for approval of a granted command whose arguments held "$PWD", in both shells.
        # It fills in ${CLAUDE_SKILL_DIR} and $ARGUMENTS itself before the command runs.
        allowed = json.dumps(["Bash(python -B *)", "PowerShell(python -B *)"])
        body = (
            "```bash\n"
            'python -B "${CLAUDE_SKILL_DIR}/scripts/a.py" "$ARGUMENTS" --price "\\$5"\n'  # 5
            'python -B "${CLAUDE_SKILL_DIR}/scripts/a.py" --cwd "$PWD"\n'  # 6
            'python -B x.py --home "${HOME}" --at "$(date)"\n'  # 7
            "```\n"
        )
        self.assertEqual(
            [
                'EXPANDS 6 python -B "${CLAUDE_SKILL_DIR}/scripts/a.py" --cwd "$PWD"',
                'EXPANDS 7 python -B x.py --home "${HOME}" --at "$(date)"',
            ],
            [line for line in self.tools(f"---\nallowed-tools: {allowed}\n---\n{body}") if line.startswith("EXPANDS")],
        )

    def test_a_skill_with_no_allowed_tools_still_reports_commands_that_expand(self) -> None:
        # No grant and no user rule pre-approves a command that expands a variable, so it prompts either way.
        body = '```bash\ngit -C "$REPO" log\n```\n'
        self.assertEqual(
            [
                "MODEL none",
                "DESCRIPTION 17 5",
                "INVOCATION user-only",
                "NO_ALLOWED_TOOLS",
                "USED Bash 5",
                'EXPANDS 6 git -C "$REPO" log',
            ],
            self.tools(f"---\ndescription: x. Use it when y.\ndisable-model-invocation: true\n---\n{body}"),
        )

    def test_each_command_a_line_joins_is_judged_on_its_own(self) -> None:
        # Claude Code checks each command of a line joined by &&, ||, ;, or | against the grants, so a pattern for
        # the first command approves none of the rest.
        allowed = json.dumps(["Bash(git status *)", "PowerShell(git status *)", "Bash(sort)", "PowerShell(sort)"])
        body = (
            "```bash\n"
            "git status --short && rm -rf build\n"  # 5
            "git status || git log; git status | sort\n"  # 6
            'git status --format "a && b; c | d"\n'  # 7: quoted operators join nothing
            "git status \\; sort\n"  # 8: an escaped operator joins nothing
            "```\n"
        )
        self.assertEqual(
            [
                "UNGRANTED Bash 5 rm -rf build",
                "UNGRANTED PowerShell 5 rm -rf build",
                "UNGRANTED Bash 6 git log",
                "UNGRANTED PowerShell 6 git log",
            ],
            self.grants(allowed, body),
        )

    def test_a_pattern_that_matches_any_command_of_a_joined_line_is_not_idle(self) -> None:
        allowed = json.dumps(["Bash(git status *)", "PowerShell(git status *)", "Bash(sort)", "PowerShell(sort)"])
        self.assertEqual([], self.idle(f"allowed-tools: {allowed}", "```bash\ngit log | sort && git status\n```\n"))

    def test_a_joined_line_runs_repository_code_only_in_the_command_that_runs_the_script(self) -> None:
        skill = self.root / "skill"
        write(skill / "scripts" / "start.py", 'RUNS_REPOSITORY_CODE = "starts configured servers"\n')
        own = 'python -B "${CLAUDE_SKILL_DIR}/scripts/'
        allowed = json.dumps(["Bash(git status)", "PowerShell(git status)"])
        self.assertEqual(
            [f'REPOSITORY_CODE Bash 5 {own}start.py"', f'REPOSITORY_CODE PowerShell 5 {own}start.py"'],
            self.repository_code(allowed, f'```bash\ngit status && {own}start.py"\n```\n'),
        )

    def test_without_shell_grants_no_command_is_reported_ungranted(self) -> None:
        self.assertEqual([], self.grants('["Read"]', "```bash\nls\n```\n"))
        self.assertEqual(["UNSCOPED_ALLOWED Bash"], self.grants('["Bash"]', "```bash\nls\n```\n"))

    def idle(self, front: str, body: str) -> list[str]:
        lines = self.tools(f"---\n{front}\n---\n{body}")
        return [line for line in lines if line.startswith(("IDLE_ALLOWED", "HIDDEN_ALLOWED"))]

    def test_a_scoped_shell_pattern_no_command_or_code_span_matches_is_reported(self) -> None:
        # A pattern pre-approves only what it matches, so one that matches nothing the skill shows approves a
        # command the skill never asks for, such as one a repository leaves to prompt.
        allowed = json.dumps(["Bash(git status)", "PowerShell(git status)", "Bash(gh pr view *)", "Bash", "Read"])
        self.assertEqual(
            ["IDLE_ALLOWED Bash(git status)", "IDLE_ALLOWED PowerShell(git status)", "IDLE_ALLOWED Bash(gh pr view *)"],
            self.idle(f"allowed-tools: {allowed}", "```bash\ngit log\n```\n"),
        )
        conforming = {
            "a fence command": "```powershell\ngit status\n```\nThen `gh pr view 7`.\n",
            "code spans": "Run `git status`, then ` gh pr view 7 `.\n",
        }
        for case, body in conforming.items():
            with self.subTest(case=case):
                self.assertEqual([], self.idle(f"allowed-tools: {allowed}", body))
        # A span inside a fence is fence text, not a command the prose shows.
        self.assertIn(
            "IDLE_ALLOWED Bash(git status)", self.idle(f"allowed-tools: {allowed}", "```text\n`git status`\n```\n")
        )

    def test_any_grant_in_a_hidden_skill_is_reported_once(self) -> None:
        # No turn starts a hidden skill, so its grants approve nothing, and each idle pattern would only repeat that.
        hidden = "disable-model-invocation: true\nuser-invocable: false"
        self.assertEqual(
            ["HIDDEN_ALLOWED"], self.idle(f'{hidden}\nallowed-tools: ["Read", "Bash(git status)"]', "Use `Read`.\n")
        )
        self.assertEqual([], self.idle(hidden, "Imported by other skills.\n"))
        self.assertEqual([], self.idle(f"{hidden}\nallowed-tools: []", "Imported by other skills.\n"))
        user_only = 'disable-model-invocation: true\nallowed-tools: ["Read"]'
        self.assertEqual([], self.idle(user_only, "Use `Read`.\n"))

    def test_without_allowed_tools_nothing_is_missing_or_unused(self) -> None:
        lines = self.tools("Use the `Read` tool.\n")
        self.assertEqual(
            ["MODEL none", "DESCRIPTION 0 0", "INVOCATION model", "NO_ALLOWED_TOOLS", "USED Read 1"], lines
        )

    def test_listing_reports_the_description_every_session_loads(self) -> None:
        # 53 characters, ceil(53 / 4) = 14 tokens
        description = 'description: "Internal support. Not intended for direct invocation."'
        cases = {
            "description: Run the report.": [
                "DESCRIPTION 15 4",
                "INVOCATION model",
                "DESCRIPTION_LEAD Run the report.",
            ],
            "description: >-\n  Run the\n  report.": [
                "DESCRIPTION 15 4",
                "INVOCATION model",
                "DESCRIPTION_LEAD Run the report.",
            ],
            "description: 'Run the report.'\ndisable-model-invocation: 'true'": [
                "DESCRIPTION 15 4",
                "INVOCATION user-only",
            ],
            description: [
                "DESCRIPTION 53 14",
                "INVOCATION model",
                "DESCRIPTION_LEAD Internal support.",
                "INTERNAL_LISTED",
            ],
            f"{description}\ndisable-model-invocation: true": ["DESCRIPTION 53 14", "INVOCATION user-only"],
            f"{description}\ndisable-model-invocation: true\nuser-invocable: false": [
                "DESCRIPTION 53 14",
                "INVOCATION hidden",
            ],
        }
        for frontmatter, expected in cases.items():
            with self.subTest(frontmatter=frontmatter):
                lines = self.tools(f"---\n{frontmatter}\n---\nBody.\n")
                self.assertEqual(
                    expected, [line for line in lines if line.startswith(("DESCRIPTION", "INVOCATION", "INTERNAL"))]
                )

    def test_the_description_lead_is_its_first_sentence_where_a_late_use_case_shows(self) -> None:
        # Claude Code and Codex both shorten a long listing, so a description whose use case comes last leads with
        # words that say nothing of when to start the skill; the lead shows it, and the audit judges it.
        cases = {
            "description: >-\n  Built over many releases by the platform team, e.g. for audits.\n"
            "  Use it to check a deployment's manifest.": "DESCRIPTION_LEAD Built over many releases by the platform"
            " team, e.g. for audits.",
            "description: Check a deployment's manifest. Use it when asked.": "DESCRIPTION_LEAD Check a deployment's"
            " manifest.",
            "description: check a manifest when asked": "DESCRIPTION_LEAD check a manifest when asked",
            # A semicolon joins two clauses of one sentence; it ends no sentence.
            "description: Audit a skill; Claude Code reads it. Use it when asked.": "DESCRIPTION_LEAD Audit a skill;"
            " Claude Code reads it.",
        }
        for frontmatter, expected in cases.items():
            with self.subTest(frontmatter=frontmatter):
                lines = self.tools(f"---\n{frontmatter}\n---\nBody.\n")
                self.assertEqual([expected], [line for line in lines if line.startswith("DESCRIPTION_LEAD")])

    def test_a_source_skill_the_model_may_start_names_its_trigger_scenario(self) -> None:
        write(self.root / "deploy-meta" / "alpha.json", "{}")
        skill = write(self.root / "skills" / "alpha" / "SKILL.md", "---\ndescription: Run.\n---\nBody.\n")
        scenarios = self.root / "tests" / "fixtures" / "skill-evals" / "alpha"
        write(scenarios / "review" / "scenario.json", '{"mode": "review", "requests": ["a"]}')
        code, lines, error = run("tools", str(skill))
        self.assertEqual(0, code, error)
        self.assertIn(f"NO_TRIGGER_SCENARIO {scenarios.as_posix()}", lines, "a review scenario judges no description")
        spec = write(
            scenarios / "description" / "scenario.json",
            '{"mode": "trigger", "requests": ["a", "b", "c"], "near_misses": ["d", "e"]}',
        )
        broken = write(scenarios / "broken" / "scenario.json", "{")
        _, lines, _ = run("tools", str(skill))
        found = [line for line in lines if line.startswith(("INVOCATION", "TRIGGER", "NO_"))]
        self.assertEqual(
            [
                "INVOCATION model",
                f"TRIGGER_SCENARIO_UNREADABLE {broken.as_posix()} not JSON:",
                f"TRIGGER_SCENARIO {spec.as_posix()} 3 2",
                "NO_ALLOWED_TOOLS",
            ],
            [found[0], found[1][: found[1].index(" not JSON:") + len(" not JSON:")], *found[2:]],
        )

    def test_a_user_only_repository_or_deployed_skill_names_no_trigger_scenario(self) -> None:
        write(self.root / "deploy-meta" / "alpha.json", "{}")
        cases = {
            "user-only source skill": (
                self.root / "skills" / "alpha" / "SKILL.md",
                "---\ndescription: Run.\ndisable-model-invocation: true\n---\nBody.\n",
            ),
            "repository skill": (self.root / ".claude" / "skills" / "alpha" / "SKILL.md", "Body.\n"),
            "deployed copy": (self.root / "home" / ".claude" / "skills" / "alpha" / "SKILL.md", "Body.\n"),
        }
        for case, (path, text) in cases.items():
            with self.subTest(case=case):
                code, lines, error = run("tools", str(write(path, text)))
                self.assertEqual(0, code, error)
                self.assertEqual([], [line for line in lines if "TRIGGER_SCENARIO" in line])

    def test_frontmatter_it_cannot_read_fails_with_the_reason(self) -> None:
        cases = {
            "---\ndescription:\n  nested: value\n---\nBody.\n": "description: a nested mapping is not supported",
            "---\nmodel: [a, b]\n---\nBody.\n": "model must be a single value",
            "---\nname: x\nBody.\n": "frontmatter is not closed",
        }
        for text, reason in cases.items():
            with self.subTest(reason=reason):
                code, lines, error = run("tools", str(write(self.root / "SKILL.md", text)))
                self.assertEqual((1, ""), (code, error))
                self.assertEqual(1, len(lines))
                self.assertTrue(lines[0].startswith("FAILED "))
                self.assertIn(reason, lines[0])

    def test_keys_it_does_not_read_may_be_structured(self) -> None:
        lines = self.tools("---\ndescription: Run.\nhooks:\n  PreToolUse:\n    - matcher: Bash\n---\nBody.\n")
        self.assertEqual(
            ["MODEL none", "DESCRIPTION 4 1", "INVOCATION model", "DESCRIPTION_LEAD Run.", "NO_ALLOWED_TOOLS"], lines
        )

    def test_missing_file_fails(self) -> None:
        code, lines, error = run("tools", str(self.root / "absent.md"))
        self.assertEqual((1, ""), (code, error))
        self.assertEqual(1, len(lines))
        self.assertTrue(lines[0].startswith("FAILED skill file not found"))

    def test_a_file_that_is_not_utf8_text_fails_instead_of_reading_as_empty(self) -> None:
        for content in ("---\nallowed-tools: Read\n---\ncaf\xe9\n".encode("cp1252"), b"---\n\0\n---\n"):
            with self.subTest(content=content):
                skill = write(self.root / "SKILL.md", content)
                code, lines, error = run("tools", str(skill))
                self.assertEqual((1, ""), (code, error))
                self.assertEqual([f"FAILED cannot read {skill.as_posix()}: not UTF-8 text"], lines)


class ScanTests(TemporaryTestCase):
    def test_cues_are_reported_with_lines_and_context(self) -> None:
        skill = write(
            self.root / "SKILL.md",
            "---\n"
            "description: parse every file in a loop\n"  # 2: frontmatter is skipped
            "---\n"
            "Find the skill and run `git rev-parse --show-toplevel`.\n"  # 4: no cue
            "Use `find . -name x` or `cat`.\n"  # 5
            "For each file, parse the output and sort it.\n"  # 6
            "Read the file and then search it for X.\n"  # 7
            "Start one `Agent` now.\n"  # 8
            'Run `Glob("**/*")` without a cap.\n'  # 9
            '`Grep("x", glob="**/*", head_limit=20)` is bounded.\n'  # 10
            "Write a Python script to normalize it.\n"  # 11
            "```bash\n"  # 12
            "find . -type f | wc -l\n"  # 13
            "python -c 'print(1)'\n"  # 14
            "```\n"
            "```text\n"
            "parse and sort for each\n"  # 17: prose cues do not apply in code
            "```\n",
        )
        code, lines, error = run("scan", str(skill))
        self.assertEqual(0, code, error)
        self.assertEqual(
            [
                (5, "shell-file-command"),
                (6, "loop"),
                (6, "rule-based-work"),
                (7, "read-then-search"),
                (8, "agent"),
                (9, "wide-glob"),
                (11, "generated-code"),
                (11, "rule-based-work"),
                (13, "shell-file-command"),
                (14, "generated-code"),
                (8, "subagent-reply"),  # the Agent delegation never bounds its reply
            ],
            [(int(line.split()[2]), line.split()[3]) for line in lines],
        )
        self.assertEqual(f"CUE {skill.as_posix()} 5 shell-file-command Use `find . -name x` or `cat`.", lines[0])

    def test_inline_python_is_generated_code_in_every_interpreter_spelling(self) -> None:
        # The token is split so that a deployment, which renders this file, leaves it for the scan to read.
        token = "{{" + "PYTHON}}"
        for interpreter in (token, "python", "python3", "Python3"):
            for command in (f"{interpreter} -c 'print(1)'", f'{interpreter} -B -I -c "import sys"'):
                for place, content in (
                    ("fence", f"```bash\n{command}\n```\n"),
                    ("inline code", f"Run `{command}` to count them.\n"),
                ):
                    with self.subTest(command=command, place=place):
                        skill = write(self.root / "SKILL.md", content)
                        code, lines, error = run("scan", str(skill))
                        self.assertEqual(0, code, error)
                        self.assertEqual(["generated-code"], [line.split()[3] for line in lines])

    def test_code_given_to_an_interpreter_on_its_command_line_is_generated_code_in_every_form(self) -> None:
        token = "{{" + "PYTHON}}"
        for command in (
            f"{token} -Bc 'print(1)'",
            f"{token} -config",  # Python reads it as -c with the code onfig
            "python -X utf8 -c 'print(1)'",
            "python -Xutf8 -W ignore --check-hash-based-pycs never -c 'print(1)'",
            "python3.11 -c 'print(1)'",
            "python.exe -c 'x'",
            "/usr/bin/python3 -c 'x'",
            "py -c 'print(1)'",
            "py -3 -c 'print(1)'",
            "py -3.11-64 -I -c 'print(1)'",
            f'"{token}" -c "print(1)"',
            "echo 'print(1)' | python -",
            "echo 'print(1)' | python -B -",
            "git status && python3 -c 'x'",
            "node -e 'console.log(1)'",
            "node --no-warnings -pe '1'",
            "node --eval='1'",
            "pwsh -Command '1'",
            "pwsh -NoProfile -c '1'",
            "powershell.exe -ExecutionPolicy Bypass -EncodedCommand ZQBjAGgAbwA=",
        ):
            for place, content in (
                ("fence", f"```bash\n{command}\n```\n"),
                ("inline code", f"Run `{command}` to count them.\n"),
            ):
                with self.subTest(command=command, place=place):
                    skill = write(self.root / "SKILL.md", content)
                    code, lines, error = run("scan", str(skill))
                    self.assertEqual(0, code, error)
                    self.assertIn("generated-code", [line.split()[3] for line in lines])

    def test_a_plain_python_command_is_not_generated_code(self) -> None:
        token = "{{" + "PYTHON}}"
        for command in (
            token + ' -B "${CLAUDE_SKILL_DIR}/scripts/count.py" -c 3',
            f"{token} -m json.tool -c",
            "python3 scripts/count.py --config -c",
            "python -X dev scripts/count.py -c",
            "py -3 scripts/count.py -c",
            f"{token}-c",
            "cpython -c 'print(1)'",
            "node scripts/count.js -e",
            "node -r ./setup.js scripts/count.js -e",
            "pwsh -File scripts/count.ps1 -Command x",
            "pwsh -NoProfile -ExecutionPolicy Bypass -File x.ps1",
            "the .py file and py.typed -c",
        ):
            for place, content in (
                ("fence", f"```bash\n{command}\n```\n"),
                ("inline code", f"Run `{command}` to count them.\n"),
            ):
                with self.subTest(command=command, place=place):
                    skill = write(self.root / "SKILL.md", content)
                    code, lines, error = run("scan", str(skill))
                    self.assertEqual(0, code, error)
                    self.assertEqual([], [line for line in lines if line])

    def test_per_item_commands_relayed_output_replies_and_runtime_prompts(self) -> None:
        delegation = "Start one subagent per role with this prompt: `Read <prompt file> and follow it.`\n"
        skill = write(
            self.root / "SKILL.md",
            "For each pull request, run the prepare command.\n"  # 1
            "Show its stdout to the user as-is.\n"  # 2
            "```bash\n"
            'python -B "${CLAUDE_SKILL_DIR}/scripts/pipeline.py" prepare\n'  # 4: the command that writes the prompt
            "```\n" + delegation,  # 6
        )
        code, lines, error = run("scan", str(skill))
        self.assertEqual(0, code, error)
        self.assertEqual(
            [(1, "loop"), (1, "per-item-command"), (2, "relayed-output"), (6, "agent"), (6, "subagent-reply")],
            [(int(line.split()[2]), line.split()[3]) for line in lines if line.startswith("CUE ")],
        )
        self.assertEqual(
            [f"RUNTIME_PROMPT {skill.as_posix()} 6 " + "${CLAUDE_SKILL_DIR}/scripts/pipeline.py"],
            [line for line in lines if line.startswith("RUNTIME_PROMPT ")],
        )

        bounded = write(self.root / "bounded.md", delegation + "Each subagent must reply with exactly: DONE.\n")
        _, lines, _ = run("scan", str(bounded))
        self.assertEqual(
            [
                f"RUNTIME_PROMPT {bounded.as_posix()} 1 unknown",
                f"RUNTIME_PROMPT_UNSAMPLED {bounded.as_posix()} 1 not in a skill source tree",
            ],
            [line for line in lines if " agent " not in line],
            "a bounded reply is not flagged, and no earlier command names the writer",
        )

    def source_skill(self, name: str = "alpha") -> Path:
        """A skill in a source tree whose subagent reads a prompt its script writes at runtime."""
        write(self.root / "deploy-meta" / f"{name}.json", "{}")
        return write(
            self.root / "skills" / name / "SKILL.md",
            "```bash\n"
            'python -B "${CLAUDE_SKILL_DIR}/scripts/pipeline.py" prepare\n'
            "```\n"
            "Start one subagent with this prompt: `Read <prompt file> and follow it.`\n"  # 4
            "Each subagent must reply with exactly: DONE.\n",
        )

    def test_a_runtime_prompt_with_no_sample_says_why(self) -> None:
        skill = self.source_skill()
        _, lines, _ = run("scan", str(skill))
        samples = (self.root / "docs" / "runtime-prompts" / "alpha").as_posix()
        self.assertEqual(
            [
                f"RUNTIME_PROMPT {skill.as_posix()} 4 " + "${CLAUDE_SKILL_DIR}/scripts/pipeline.py",
                f"RUNTIME_PROMPT_UNSAMPLED {skill.as_posix()} 4 no sample under {samples}",
            ],
            [line for line in lines if line.startswith("RUNTIME_PROMPT")],
        )

        loose = write(self.root / "loose" / "SKILL.md", skill.read_text(encoding="utf-8"))
        _, lines, _ = run("scan", str(loose))
        self.assertEqual(
            [f"RUNTIME_PROMPT_UNSAMPLED {loose.as_posix()} 4 not in a skill source tree"],
            [line for line in lines if line.startswith("RUNTIME_PROMPT_")],
        )

    def test_a_sample_is_measured_with_the_files_it_names_and_whether_it_bounds_the_reply(self) -> None:
        skill = self.source_skill()
        write(self.root / "skills" / "core" / "references" / "reviewer.md", "x" * 40 + "\n")
        write(self.root / "skills" / "core" / "scripts" / "check.py", "print()\n")
        samples = self.root / "docs" / "runtime-prompts" / "alpha"
        bounded = write(
            samples / "reviewer.txt",
            "Follow <source>\\skills\\core\\references\\reviewer.md, for what to review.\n"
            'Run python -B "<source>\\skills\\core\\scripts\\check.py" --run "<temp>\\run".\n'
            "Then reply with exactly: WROTE <temp>\\run\\result.json\n",
        )
        unbounded = write(samples / "synthesis.txt", "Write the result to <temp>/result.json.\n")
        write(samples / "notes.md", "Not a sample.\n")
        _, lines, _ = run("scan", str(skill), str(write(self.root / "skills" / "alpha" / "more.md", "Text.\n")))
        self.assertEqual(
            [
                f"RUNTIME_PROMPT {skill.as_posix()} 4 " + "${CLAUDE_SKILL_DIR}/scripts/pipeline.py",
                f"RUNTIME_PROMPT_SAMPLE {bounded.as_posix()} "
                + str(skill_inventory.estimate_tokens(bounded.read_text(encoding="utf-8"), "doc")),
                f"RUNTIME_PROMPT_READ {bounded.as_posix()} skills/core/references/reviewer.md 11",
                f"REPLY_BOUNDED {bounded.as_posix()} yes",
                f"RUNTIME_PROMPT_SAMPLE {unbounded.as_posix()} 10",
                f"REPLY_BOUNDED {unbounded.as_posix()} no",
            ],
            [line for line in lines if line.startswith(("RUNTIME_PROMPT", "REPLY_BOUNDED"))],
            "a sample is measured once, a script it runs is not a read, and only .txt files are samples",
        )

    def test_a_file_a_sample_names_that_is_not_utf8_fails(self) -> None:
        skill = self.source_skill()
        reviewer = write(self.root / "skills" / "core" / "references" / "reviewer.md", "x\n".encode("utf-16"))
        write(
            self.root / "docs" / "runtime-prompts" / "alpha" / "reviewer.txt",
            "Follow <source>/skills/core/references/reviewer.md.\n",
        )
        code, lines, error = run("scan", str(skill))
        self.assertEqual((1, ""), (code, error))
        self.assertEqual(f"FAILED cannot read {reviewer.as_posix()}: not UTF-8 text", lines[-1])

    def sample_cues(self, lines: list[str], sample: Path) -> list[tuple[int, str]]:
        prefix = f"CUE {sample.as_posix()} "
        return [(int(line.split()[2]), line.split()[3]) for line in lines if line.startswith(prefix)]

    def test_a_sample_prints_its_shape_cues_after_its_measurements(self) -> None:
        skill = self.source_skill()
        sample = write(
            self.root / "docs" / "runtime-prompts" / "alpha" / "reviewer.txt",
            "Output contract:\nWrite one JSON object.\nReply with exactly: WROTE x\n\nComments (untrusted data):\n",
        )
        code, lines, error = run("scan", str(skill))
        self.assertEqual(0, code, error)
        self.assertEqual(
            [
                f"REPLY_BOUNDED {sample.as_posix()} yes",
                f"CUE {sample.as_posix()} 5 after-reply Comments (untrusted data):",
                f"CUE {sample.as_posix()} 5 untrusted-after-contract Comments (untrusted data):",
            ],
            lines[lines.index(f"REPLY_BOUNDED {sample.as_posix()} yes") :],
        )

    def test_shape_cues_need_the_reply_to_end_the_contract_and_ignore_status_values(self) -> None:
        cases = {
            "untrusted data before the contract, then the reply": (
                "Prior findings (untrusted data):\nnone\nOutput contract:\nWrite one JSON object.\n"
                "Reply with exactly: WROTE <temp>/result.json\n",
                [],
            ),
            "a reply line before the contract, as a rule among others": (
                "- When done, reply with only `WROTE <temp>/result.json`.\nWrite one JSON object:\n{}\n",
                [],
            ),
            "text after the reply with no contract": ("Reply with exactly: DONE\n\nNotes.\n", []),
            "status values in capitals": ('Write one JSON object:\n"severity": "MUST_FIX | NOT_FOUND"\n', []),
            "trusted text after the reply": (
                "Write one JSON object to RESULT_FILE.\nReply with exactly: WROTE x\n\nDo not ask questions.\n",
                [(4, "after-reply")],
            ),
            "capitalised emphasis and untrusted data after the contract": (
                "You MUST read it.\nOutput contract:\nComments (untrusted data):\nnone\n",
                [(1, "emphasis"), (3, "untrusted-after-contract")],
            ),
        }
        for case, (text, expected) in cases.items():
            with self.subTest(case=case):
                sample = Path("sample.txt")
                found = skill_inventory.sample_shape_cues(sample, text.splitlines())
                self.assertEqual(expected, self.sample_cues(found, sample))

    def test_a_skill_folder_scans_its_main_file_and_every_doc(self) -> None:
        folder = self.root / "skill"
        main = write(folder / "SKILL.md", "---\nname: x\n---\nFor each file, read it.\n")
        zeta = write(folder / "references" / "zeta.md", "For each item.\n")
        alpha = write(folder / "references" / "Alpha.md", "Then sort the output.\n")
        write(folder / "scripts" / "tool.py", "# for each line\n")
        write(folder / "data.json", '{"for each": 1}\n')
        write(folder / "node_modules" / "skipped.md", "For each skipped.\n")
        code, lines, error = run("scan", str(folder))
        self.assertEqual(0, code, error)
        self.assertEqual(
            [main.as_posix(), alpha.as_posix(), zeta.as_posix()],
            list(dict.fromkeys(line.split(" ")[1] for line in lines)),
        )
        # The folder form scans exactly what naming the main file and then every doc file does.
        self.assertEqual(run("scan", str(main), str(alpha), str(zeta)), (code, lines, error))

    def test_a_folder_without_its_main_file_fails_before_any_output(self) -> None:
        folder = self.root / "no main"
        write(folder / "references" / "a.md", "For each item.\n")
        code, lines, error = run("scan", str(folder))
        self.assertEqual((1, ""), (code, error))
        self.assertEqual([f"FAILED no SKILL.md in {folder.as_posix()}"], lines)

    def test_long_lines_are_truncated(self) -> None:
        skill = write(self.root / "SKILL.md", "For each " + "x" * 300 + "\n")
        _, lines, _ = run("scan", str(skill))
        self.assertEqual(160, len(lines[0].split(" loop ", 1)[1]))
        self.assertTrue(lines[0].endswith("..."))

    def test_missing_files_fail_before_any_output(self) -> None:
        present = write(self.root / "SKILL.md", "For each item.\n")
        code, lines, error = run("scan", str(present), str(self.root / "absent.md"))
        self.assertEqual((1, ""), (code, error))
        self.assertEqual(1, len(lines))
        self.assertTrue(lines[0].startswith("FAILED file not found"))
        self.assertIn("absent.md", lines[0])

    def test_a_file_that_is_not_utf8_text_fails_before_any_output(self) -> None:
        present = write(self.root / "SKILL.md", "For each item.\n")
        legacy = write(self.root / "legacy.md", "For each caf\xe9.\n".encode("cp1252"))
        code, lines, error = run("scan", str(present), str(legacy))
        self.assertEqual((1, ""), (code, error))
        self.assertEqual([f"FAILED cannot read {legacy.as_posix()}: not UTF-8 text"], lines)

    def test_a_prose_line_naming_a_subagent_is_an_agent_cue(self) -> None:
        skill = write(
            self.root / "SKILL.md",
            "Start one subagent per file, and reply with DONE.\n"  # 1
            "Read the subagents' notes.\n"  # 2
            "```text\nsubagent in a fence\n```\n",
        )
        _, lines, _ = run("scan", str(skill))
        self.assertEqual(
            [(1, "agent"), (2, "agent")],
            [(int(line.split()[2]), line.split()[3]) for line in lines if line.split()[3] == "agent"],
        )


PROSE_CUE_NAMES = {name for name, _ in skill_inventory.PROSE_CUES}


@pytest.mark.parametrize(
    ("line", "cue"),
    [
        # A flag chosen per printed line or value, after it or before it, or each printed path passed on.
        (
            "Invoke it with `--pull <pr>` for each `missing` and `--re-review <pr>` for each `stale`.",
            "assembled-arguments",
        ),
        ("Run it with one `--run` per `RUN` directory.", "assembled-arguments"),
        ("Pass `--repository-set <name>` when `set=` names one.", "assembled-arguments"),
        ('For a `SYNTHESIZED` line, the subject is `--synthesized "<title>"`.', "assembled-arguments"),
        ("Pass the main file followed by every `doc` path:", "assembled-arguments"),
        # Output split, grouped, keyed on a message's text, or renamed to a status.
        ("If it is too large, split its file list between two readers by directory.", "parsed-output"),
        ("Prepare the pull requests in groups of up to four.", "parsed-output"),
        ("- **`the run recorded no review; ...`**: the session ended early.", "parsed-output"),
        ("When it refuses, report it as `PINNED` below.", "parsed-output"),
        ("Use the label, such as `v2 F002`: review version `2` and finding `F002`.", "parsed-output"),
        # A last-seen value, a count, or a list of printed lines carried to a later step.
        ("Show the text of the last summary printed for each repository.", "carried-state"),
        ("Finish with how many were skipped.", "carried-state"),
        ("Run it with one argument per directory the first step printed.", "carried-state"),
        ("Keep a running total of the failures.", "carried-state"),
        # The follow-ups a change owes, decided by listed rules.
        ("After any frontmatter change to a skill, regenerate the reference.", "owed-by-rules"),
        ("When the change touches a skill's prompt, also run the evaluation.", "owed-by-rules"),
        ("Name the skills the change touched: a changed script or metadata.", "owed-by-rules"),
        ("Drift and polish go into the next milestone.", "owed-by-rules"),
        # A file or message drafted by hand.
        ("1. Write the title and the body from the repository's template, each to a file.", "hand-drafted"),
        ("Copy the `RUNTIME`, `MATRIX`, and `SKIPPED` lines into the pull request.", "hand-drafted"),
        ("If it is empty, print `usage: tool <name>` and stop.", "hand-drafted"),
        ("Finish with `Processed <N> repositories.` from the count.", "hand-drafted"),
    ],
)
def test_a_deterministic_prose_shape_is_its_cue(line: str, cue: str, tmp_path: Path) -> None:
    skill = write(tmp_path / "SKILL.md", line + "\n")
    _, lines, _ = run("scan", str(skill))
    assert cue in [found.split()[3] for found in lines if found]


@pytest.mark.parametrize(
    "line",
    [
        # Flags the user chose, a flag with no printed value after its quantifier, and a pasted block.
        "Pass after the clone path each of `--cross-major` and `--unpin` that the user started it with.",
        'Add `--remove "<pr>"` for each removal and `--candidates` unless `--no-review` was given.',
        "Name each confirmed pull request with its own `--confirm`.",
        "Then add `--force` when given. Ask once for the run, never per pull request.",
        # A value shown once, a recommendation to split a file, and lines that come in groups.
        "Associate it using the finding's label, such as `v2 F002`, as the report shows it.",
        "Recommend splitting the body into reference files.",
        "`CONFLICT` lines come in groups of two or more versions.",
        "- **`judgment`**: the evaluation's result for that model.",
        # A line the last command printed, with no per-item record, and a rule that sets no follow-up.
        "For each `ROLE` line the last `prepare` printed, start one subagent.",
        "Read the files the change touches and their tests.",
        "When the change sets or clears a flag, update the metadata.",
        "Decide whether the change touches that skill's prompt.",
        "The script names the shipped skills a change reaches.",
        # A script that drafts, a block pasted as printed, and a line the script prints.
        "It drafts the body from the repository's pull request template.",
        "Paste the lines between `CITATION BEGIN` and `CITATION END` into the pull request as they are.",
        "It prints `UPDATED <dashboard> rows=<count>` on success.",
        "File each finding as one issue written from the template its field names.",
    ],
)
def test_prose_a_script_already_does_or_the_user_decides_is_no_deterministic_prose_cue(
    line: str, tmp_path: Path
) -> None:
    skill = write(tmp_path / "SKILL.md", line + "\n")
    _, lines, _ = run("scan", str(skill))
    assert [found for found in lines if found and found.split()[3] in PROSE_CUE_NAMES] == []


def test_deterministic_prose_cues_skip_fences_and_frontmatter(tmp_path: Path) -> None:
    skill = write(
        tmp_path / "SKILL.md",
        "---\ndescription: Finish with how many were skipped.\n---\n```text\nFinish with how many were skipped.\n```\n",
    )
    code, lines, error = run("scan", str(skill))
    assert (code, [line for line in lines if line], error) == (0, [], "")


class StatusLineTests(TemporaryTestCase):
    """The status lines and exit codes a skill's Markdown names, compared with what its scripts print and exit with."""

    def skill(self, body: str, script: str = "", name: str = "tool.py") -> Path:
        folder = self.root / "repo" / "skills" / "alpha"
        write(self.root / "repo" / ".git" / "HEAD", "ref: refs/heads/main\n")
        write(folder / "SKILL.md", "---\nname: alpha\n---\n" + body)
        if script:
            write(folder / "scripts" / name, script)
        return folder

    def status(self, folder: Path) -> list[str]:
        code, lines, error = run("scan", str(folder))
        self.assertEqual((0, ""), (code, error))
        return [line for line in lines if line.startswith(("UNPRINTED ", "UNEXPLAINED ", "EXIT_MISMATCH "))]

    def test_a_document_promising_a_line_no_script_prints_is_unprinted(self) -> None:
        folder = self.skill(
            "It prints `READY <run>` and then `DONE`, or `GONE <path>` when it removed one.\n",
            'import sys\n\ndef main() -> int:\n    print(f"READY {sys.argv}")\n    print("DONE")\n    return 0\n',
        )
        self.assertEqual([f"UNPRINTED {(folder / 'SKILL.md').as_posix()} 4 GONE"], self.status(folder))

    def test_a_script_whose_output_cannot_be_measured_fails_the_scan_rather_than_printing_nothing(self) -> None:
        for name, content, reason in (
            ("tool.py", b"print('READY')\n\xff\n", "not UTF-8 text"),
            ("tool.sh", b"echo READY\n\xff\n", "not UTF-8 text"),
            ("binary.py", b"\xff\xfe\0", "not UTF-8 text"),
            ("broken.py", b"def (:\n", "not valid Python"),
        ):
            with self.subTest(name=name):
                folder = self.skill("It prints `READY` and then `DONE`.\n")
                script = folder / "scripts" / name
                script.parent.mkdir(parents=True, exist_ok=True)
                script.write_bytes(content)
                code, lines, _ = run("scan", str(folder))
                self.assertEqual((1, f"FAILED cannot read {script.resolve().as_posix()}: {reason}"), (code, lines[-1]))
                script.unlink()

    def test_a_command_naming_a_script_that_does_not_exist_is_named_on_its_own_line(self) -> None:
        folder = self.skill(
            "Run it:\n\n```bash\n"
            'python -B "${CLAUDE_SKILL_DIR}/scripts/missing_tool.py" run --report "<insights.json>"'
            ' --data "${CLAUDE_SKILL_DIR}/references/data.json" --then "${CLAUDE_SKILL_DIR}/scripts/later.py"\n'  # 7
            "```\n\nThen `python -B tools/gone.py check` from the repository, and `python $HOME/x.py`.\n"  # 10
            "A manifest names `generatedBy: gone.py`, a value rather than a tool's path.\n"
        )
        code, lines, error = run("scan", str(folder))
        self.assertEqual((0, ""), (code, error))
        skill_md = (folder / "SKILL.md").as_posix()
        prefix = skill_inventory.SKILL_DIR_PREFIX
        self.assertEqual(
            [
                f"SCRIPT_MISSING {skill_md} 7 {prefix}scripts/missing_tool.py",
                f"SCRIPT_MISSING {skill_md} 7 {prefix}scripts/later.py",
                f"SCRIPT_MISSING {skill_md} 10 tools/gone.py",
            ],
            [line for line in lines if line.startswith("SCRIPT_MISSING ")],
        )
        write(folder / "scripts" / "missing_tool.py", 'print("RUN")\n')
        write(folder / "scripts" / "later.py", 'print("RUN")\n')
        write(self.root / "repo" / "tools" / "gone.py", 'print("RUN")\n')
        code, lines, error = run("scan", str(folder))
        self.assertEqual([], [line for line in lines if line.startswith("SCRIPT_MISSING ")], error)

    def test_frontmatter_names_no_command_whose_script_could_be_missing(self) -> None:
        folder = self.skill("Body.\n")
        write(
            folder / "SKILL.md",
            "---\nname: alpha\ndescription: Runs `python -B tools/absent.py` for you.\n---\nBody.\n",
        )
        code, lines, error = run("scan", str(folder))
        self.assertEqual((0, ""), (code, error))
        self.assertEqual([], [line for line in lines if line.startswith("SCRIPT_MISSING ")])

    def test_a_document_and_its_script_that_agree_print_nothing(self) -> None:
        folder = self.skill(
            "Run it:\n\n```bash\n"
            'python -B "${CLAUDE_SKILL_DIR}/scripts/tool.py" run\n'
            "```\n\nIt prints `READY <run>`, or a last line `FAILED <reason>` and exits 1.\n",
            "from console import run_main\n\n"
            'def main() -> int:\n    print(f"READY {1}")\n    return 0\n\n'
            'if __name__ == "__main__":\n    run_main(main)\n',
        )
        self.assertEqual([], self.status(folder))

    def test_a_line_the_script_prints_that_the_document_never_names_is_unexplained(self) -> None:
        folder = self.skill(
            '```bash\npython -B "${CLAUDE_SKILL_DIR}/scripts/tool.py" run\n```\n\nIt prints `READY <run>`.\n',
            "def main(lines: list[str]) -> int:\n"
            '    print(f"READY {1}")\n'  # 2
            '    lines.append("NOTHING_TO_DO")\n'  # 3
            '    return ["STALE x"] and 0\n',  # 4: a listed string
        )
        script = (folder / "scripts" / "tool.py").as_posix()
        self.assertEqual(
            [f"UNEXPLAINED {script} 3 NOTHING_TO_DO", f"UNEXPLAINED {script} 4 STALE"], self.status(folder)
        )

    def test_values_a_script_compares_looks_up_or_passes_are_no_lines(self) -> None:
        folder = self.skill(
            '```bash\npython -B "${CLAUDE_SKILL_DIR}/scripts/tool.py"\n```\n',
            '"""DOCUMENTED <line> a docstring names it."""\n'
            "import os\n\n"
            "def main(state: str) -> int:\n"
            '    """INNER also a docstring."""\n'
            '    if state == "MERGED":\n'
            "        return 0\n"
            '    run(["git", "rev-parse", "HEAD"], key={"OPEN": 1}, name="OWNER")\n'
            '    return int(os.environ["GITHUB_STEP_SUMMARY"] or 0)\n',
        )
        self.assertEqual([], self.status(folder))

    def test_a_word_the_script_holds_anywhere_or_completes_from_a_prefix_is_printed(self) -> None:
        folder = self.skill(
            "It prints `RUNTIME <runtime> DISAGREES <level>`, `CONFIG_ERROR <file>`, and `OVERDUE <role>` lines.\n",
            "def main(severity: str, level: str) -> None:\n"
            '    print(f"RUNTIME x DISAGREES {level}")\n'
            '    print(f"CONFIG_{severity} file")\n'
            '    state = "overdue"\n'
            "    print(state.upper())\n",
        )
        self.assertEqual([], self.status(folder))

    def test_only_a_prose_line_about_output_names_lines_and_an_example_names_none(self) -> None:
        folder = self.skill(
            "No `SHOULD FIX` tier.\n"
            "Recommend a one-line reply such as `WROTE <path>`, which the subagent prints.\n"
            "```text\nIt prints `ABSENT` here.\n```\n",
            'print("PRESENT")\n',
        )
        self.assertEqual([], self.status(folder))

    def test_an_exit_code_no_script_uses_is_a_mismatch(self) -> None:
        folder = self.skill(
            "It exits 3 when the plan is stale, exit code 2 for a usage error, and exits 1 on a `FAILED` line.\n",
            "import argparse\nimport sys\n\nfrom console import run_main\n\n"
            "STALE = 4\n\n"
            "def main() -> int:\n    argparse.ArgumentParser().parse_args()\n    return STALE if sys.argv else 0\n",
        )
        self.assertEqual([f"EXIT_MISMATCH {(folder / 'SKILL.md').as_posix()} 4 3"], self.status(folder))

    def test_an_exit_code_past_the_digit_limit_is_a_mismatch_as_stated(self) -> None:
        code = "1" + "0" * 5000
        folder = self.skill(f"It exits with code {code} when the plan is stale.\n", "import sys\n\nsys.exit(1)\n")
        self.assertEqual([f"EXIT_MISMATCH {(folder / 'SKILL.md').as_posix()} 4 {code}"], self.status(folder))

    def test_exit_codes_come_from_exit_calls_and_named_constants(self) -> None:
        folder = self.skill(
            "It exits 3 or exits with status 5, never 2.\n",
            "import sys\n\nREFUSED = 5\n\n"
            "def stop() -> None:\n    sys.exit(3)\n\n"
            "def refuse() -> None:\n    raise SystemExit(REFUSED)\n",
        )
        self.assertEqual([], self.status(folder))
        write(folder / "SKILL.md", "---\nname: alpha\n---\nIt exits 2 on a usage error.\n")
        self.assertEqual(
            [f"EXIT_MISMATCH {(folder / 'SKILL.md').as_posix()} 4 2"], self.status(folder), "no argparse, no 2"
        )

    def test_a_shell_script_prints_what_it_echoes_and_exits_with_what_it_exits(self) -> None:
        folder = self.skill(
            '```bash\nbash "${CLAUDE_SKILL_DIR}/scripts/update.sh"\n```\n\n'
            "It prints `UP_TO_DATE <sha>` and exits 0, or exits 4.\n",
            '#!/usr/bin/env bash\n# PINNED in a comment prints nothing\necho "UP_TO_DATE $sha"\n'
            'echo "PINNED $ref"\nexit 3\n',
            name="update.sh",
        )
        script = (folder / "scripts" / "update.sh").as_posix()
        self.assertEqual(
            [f"EXIT_MISMATCH {(folder / 'SKILL.md').as_posix()} 8 4", f"UNEXPLAINED {script} 4 PINNED"],
            self.status(folder),
        )

    def test_a_repository_tool_is_explained_by_its_skill_and_a_sibling_skills_script_is_not(self) -> None:
        folder = self.skill(
            "```bash\npython -B tools/canary.py\n"
            'python -B "${CLAUDE_SKILL_DIR}/../core/scripts/pipeline.py" run\n```\n\n'
            "The pipeline prints `ROLE <id>` lines.\n"
        )
        tool = write(self.root / "repo" / "tools" / "canary.py", 'print("DEPLOYED x")\n')
        write(folder.parent / "core" / "scripts" / "pipeline.py", 'print("ROLE x")\nprint("NOTE x")\n')
        write(folder.parent / "core" / "scripts" / "records.py", 'print("RECORDED x")\n')
        write(folder.parent / "core" / "scripts" / "test_pipeline.py", 'print("SUITE_ONLY x")\n')
        self.assertEqual([f"UNEXPLAINED {tool.resolve().as_posix()} 1 DEPLOYED"], self.status(folder))
        write(
            folder / "SKILL.md",
            "---\nname: alpha\n---\n```bash\npython -B tools/canary.py\n"
            'python -B "${CLAUDE_SKILL_DIR}/../core/scripts/pipeline.py" run\n```\n\n'
            "It prints `DEPLOYED <id>`, then `RECORDED <x>`, never `SUITE_ONLY <x>`.\n",
        )
        self.assertEqual(
            [f"UNPRINTED {(folder / 'SKILL.md').as_posix()} 9 SUITE_ONLY"],
            self.status(folder),
            "the scripts beside a script the skill runs count, its suites never",
        )

    def test_skill_cores_failed_line_counts_and_its_own_lines_do_too(self) -> None:
        folder = self.skill(
            "It prints `UNSAFE <path>` or `FAILED <reason>` lines.\n", 'from console import run_main\n\nprint("OK x")\n'
        )
        self.assertEqual([f"UNPRINTED {(folder / 'SKILL.md').as_posix()} 4 UNSAFE"], self.status(folder))
        write(folder.parent / "ejryan89-skill-core" / "scripts" / "guard.py", 'print(f"UNSAFE {1}")\n')
        self.assertEqual([], self.status(folder))

    def test_a_skill_that_holds_and_runs_no_script_compares_nothing(self) -> None:
        folder = self.skill("It prints `NOWHERE <x>` and exits 9.\n")
        self.assertEqual([], self.status(folder))

    def test_a_script_named_outside_the_skill_compares_nothing_more(self) -> None:
        folder = self.skill(
            '```bash\npython -B "${CLAUDE_SKILL_DIR}/scripts/tool.py"\npython -B "$HOME/x.py"\n```\n\n'
            "It prints `BROKEN <x>`.\n",
            "x = 1\n",
        )
        self.assertEqual([f"UNPRINTED {(folder / 'SKILL.md').as_posix()} 9 BROKEN"], self.status(folder))

    def test_scanning_files_compares_no_status_lines(self) -> None:
        folder = self.skill("It prints `GONE <x>`.\n", 'print("HERE")\n')
        _, lines, _ = run("scan", str(folder / "SKILL.md"))
        self.assertEqual([], [line for line in lines if line])


class InstructionsTests(unittest.TestCase):
    def test_every_line_the_script_prints_has_a_stated_handling(self) -> None:
        # The fact names the script prints: a string, or an f-string's leading text, that starts with an upper-case
        # name followed by a space or ending there. Tool names are data, not facts.
        printed: set[str] = set()
        for node in ast.walk(ast.parse(SCRIPT.read_text(encoding="utf-8"))):
            if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
                node = node.values[0]
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                match = re.match(r"([A-Z][A-Z0-9]+(?:_[A-Z0-9]+)*)(?: |$)", node.value)
                if match and match.group(1) not in skill_inventory.KNOWN_TOOLS:
                    printed.add(match.group(1))
        self.assertLess({"FAILED", "NO_MAIN", "DECLARED_UNREADABLE", "NO_ALLOWED_TOOLS", "RUNTIME_PROMPT"}, printed)
        skill = SCRIPT.parents[1]
        instructions = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (skill / "SKILL.md", skill / "references" / "delegation-and-model.md")
        )
        self.assertEqual(
            [], sorted(name for name in printed if not re.search(rf"`{name}(?:[ `])", instructions)), "unhandled"
        )


class TemplateValueTests(unittest.TestCase):
    def test_every_template_value_the_skill_requires_is_one_a_command_or_grant_uses(self) -> None:
        # A value only a sentence names promises what no command reads, as a home folder the script never searches
        # would, and the deployment would require it for that sentence alone.
        skill = SCRIPT.parents[1]
        lines = (skill / "SKILL.md").read_text(encoding="utf-8").splitlines()
        languages, _ = skill_inventory.parse_fences(lines)
        start = skill_inventory.body_start(lines)
        commands = [line for index, line in enumerate(lines) if index < start or languages[index] is not None]
        used = set(re.findall(r"\{\{([A-Z_]+)\}\}", "\n".join(commands)))
        metadata = json.loads((skill.parents[1] / "deploy-meta" / f"{skill.name}.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(used), sorted(metadata["required_vars"]))


class ExitContractTests(TemporaryTestCase):
    """The process-level contract: 1 with a last stdout line FAILED for a failure, 2 for usage alone."""

    def test_a_missing_input_exits_1_with_failed_on_stdout(self) -> None:
        completed = execute("inventory", str(self.root / "absent"))
        self.assertEqual(1, completed.returncode)
        self.assertEqual("", completed.stderr)
        self.assertTrue(completed.stdout.splitlines()[-1].startswith("FAILED skill directory not found"))

    def test_output_survives_a_console_that_cannot_encode_it(self) -> None:
        # A Windows pipe defaults to a legacy code page, and every fact names its file as it is named.
        skill = write(self.root / "skill → ✓" / "SKILL.md", "For each item.\n")
        completed = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "scan", str(skill)],
            capture_output=True,
            env={**os.environ, "PYTHONIOENCODING": "cp1252"},
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr.decode("utf-8", "replace"))
        self.assertEqual(
            [f"CUE {skill.as_posix()} 1 loop For each item."], completed.stdout.decode("utf-8").splitlines()
        )


# Claude Code compares the command's text, quotes included: `*` matches any text, a pattern without one
# matches exactly, and the legacy `prefix:*` matches the prefix followed by arguments. Claude Code 2.1.288
# on Windows denied the unquoted form below for the quoted command and ran the quoted form, in both shells.
@pytest.mark.parametrize(
    ("pattern", "command", "expected"),
    [
        ('python -B "${CLAUDE_SKILL_DIR}/scripts/*', 'python -B "${CLAUDE_SKILL_DIR}/scripts/a.py" --help', True),
        ("python -B ${CLAUDE_SKILL_DIR}/scripts/* *", 'python -B "${CLAUDE_SKILL_DIR}/scripts/a.py" --help', False),
        ("git rev-parse --show-toplevel", "git rev-parse --show-toplevel", True),
        ("git rev-parse --show-toplevel", "git rev-parse --show-toplevel x", False),
        ("git rev-parse:*", "git rev-parse --git-dir", True),
        ("git rev-parse:*", "git rev-parsed", False),
        ("ls *", "ls -la", True),
        ("ls *", "lsof", False),
        # A `*` at the end after a space, as the only wildcard, also matches the bare command; without the space
        # it continues the word.
        ("ls *", "ls", True),
        ("ls:*", "ls", True),
        ("ls*", "lsof", True),
        ("* --help *", "npm --help", False),
        ('python -B "${CLAUDE_SKILL_DIR}/scripts/a.py" *', 'python -B "${CLAUDE_SKILL_DIR}/scripts/a.py"', True),
        ("python tools/a.py *", "python tools/a.py --write", True),
        ("python tools/a.py *", "python tools/a.py/../../b.py", False),
        ("python tools/a.py*", "python tools/a.py/../../b.py", True),
        # No path is normalized, so a pattern ending in a folder also matches a `..` path out of it, and one
        # ending at a script's closing quote matches only that script.
        (
            'python -B "${CLAUDE_SKILL_DIR}/scripts/*',
            'python -B "${CLAUDE_SKILL_DIR}/scripts/../../core/scripts/config.py" write x',
            True,
        ),
        (
            'python -B "${CLAUDE_SKILL_DIR}/../core/scripts/a.py" *',
            'python -B "${CLAUDE_SKILL_DIR}/../core/scripts/a.py" run',
            True,
        ),
        (
            'python -B "${CLAUDE_SKILL_DIR}/../core/scripts/a.py" *',
            'python -B "${CLAUDE_SKILL_DIR}/../core/scripts/a.py/../config.py" write x',
            False,
        ),
        (
            'python -B "${CLAUDE_SKILL_DIR}/scripts/a.py" *',
            'python -B "${CLAUDE_SKILL_DIR}/scripts/a.py/../../../core/scripts/config.py" write x',
            False,
        ),
    ],
)
def test_grant_patterns_match_as_claude_code_does(pattern: str, command: str, expected: bool) -> None:
    assert expected == skill_inventory.grants(pattern, command)


@pytest.mark.parametrize(
    ("pattern", "command", "expected"),
    [
        ("git status *", "git status --short && rm -rf build", False),
        ("git status *", "git status ; rm -rf build", False),
        ("git status *", "git status || rm -rf build", False),
        ("git status *", "git status | rm -rf build", False),
        ("git status *", "git status && git status --short", True),
        ("git *", 'git commit -m "a && b; c | d"', True),
        ("git *", "git commit -m 'a && b'", True),
        ("git *", "git log 2>&1", True),
    ],
)
def test_a_joined_command_is_granted_only_when_the_pattern_matches_each_command(
    pattern: str, command: str, expected: bool
) -> None:
    assert expected == skill_inventory.grants(pattern, command)


def test_a_command_is_split_at_each_operator_outside_quotes_and_escapes() -> None:
    assert skill_inventory.command_parts("a && b || c; d | e 'f; g' \"h | i\" | j \\&& k") == [
        "a",
        "b",
        "c",
        "d",
        "e 'f; g' \"h | i\"",
        "j \\&& k",
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param((), id="no-command"),
        pytest.param(("unknown",), id="unknown-command"),
        pytest.param(("inventory",), id="inventory-without-a-directory"),
    ],
)
def test_a_usage_error_exits_2(arguments: tuple[str, ...]) -> None:
    completed = execute(*arguments)
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "usage:" in completed.stderr


# Every line skill_inventory.py prints that is a measurement or a location rather than a check, and so rests on no
# guidance; a new line belongs here or among the checks.
MEASUREMENT_LINES = frozenset(
    {
        "AGENT",
        "AGENT_CLAUDE_MD",
        "AGENT_MODEL",
        "AGENT_TOOLS",
        "AGENT_UNREADABLE",
        "ALLOWED",
        "AMBIGUOUS",
        "AVAILABLE",
        "CARRIED_BY",
        "CUE",
        "DECLARED",
        "DECLARED_UNREADABLE",
        "DESCRIPTION_LEAD",
        "FAILED",
        "FILE",
        "IMPLIED",
        "LEFT_OUT",
        "NOT_A_CHECK",
        "NOT_FOUND",
        "NO_ALLOWED_TOOLS",
        "NO_MAIN",
        "PLUGIN",
        "PLUGINS_UNREADABLE",
        "PLUGIN_CARRIES",
        "REPO",
        "REPOSITORY_CODE",
        "RUNTIME",
        "RUNTIME_NEEDS",
        "RUNTIME_PROMPT_READ",
        "RUNTIME_PROMPT_SAMPLE",
        "RUNTIME_UNDECLARED",
        "RUNTIME_UNREADABLE",
        "SCOPE",
        "SKILL_DIR",
        "SKILL_FILE",
        "STOP",
        "SUBAGENT_EFFORT",
        "SUBAGENT_MODEL",
        "TOTAL",
        "TRIGGER_SCENARIO",
        "TRIGGER_SCENARIO_UNREADABLE",
        "UNREADABLE",
        "USED",
    }
)
PROVENANCE = SCRIPT.parents[1] / "references" / "provenance.json"


def test_every_line_the_script_prints_is_a_check_or_a_measurement() -> None:
    printed = set(skill_inventory.python_facts(SCRIPT.read_text(encoding="utf-8")).lines)
    assert set() == skill_inventory.CHECK_LINES & MEASUREMENT_LINES
    assert sorted(printed) == sorted(skill_inventory.CHECK_LINES | MEASUREMENT_LINES)


def test_the_cues_are_the_ones_scan_names() -> None:
    assert {
        "after-reply",
        "agent",
        "assembled-arguments",
        "carried-state",
        "emphasis",
        "generated-code",
        "hand-drafted",
        "loop",
        "owed-by-rules",
        "parsed-output",
        "per-item-command",
        "read-then-search",
        "relayed-output",
        "rule-based-work",
        "shell-file-command",
        "subagent-reply",
        "untrusted-after-contract",
        "wide-glob",
    } == skill_inventory.CUE_NAMES


def test_every_check_has_a_provenance_entry_and_every_entry_names_a_check() -> None:
    entries = json.loads(PROVENANCE.read_text(encoding="utf-8"))["checks"]
    assert sorted(skill_inventory.CHECKS - set(entries)) == [], "checks without an entry"
    assert sorted(set(entries) - skill_inventory.CHECKS) == [], "entries without a check"
    assert sorted(skill_inventory.CHECKS) == sorted(check_provenance.load(PROVENANCE, skill_inventory.CHECKS))


def test_provenance_names_each_source_of_a_check_and_refuses_a_measurement() -> None:
    code, lines, _ = run("provenance", "DOC_NO_TOC", "FILE", "UNSCOPED_ALLOWED", "DOC_NO_TOC")
    assert code == 1
    assert lines == [
        "PAGE DOC_NO_TOC https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices For "
        "reference files longer than 100 lines, include a table of contents at the top, so Claude sees the full "
        "scope even when it previews the file with a partial read.",
        "NOT_A_CHECK FILE",
        "PAGE UNSCOPED_ALLOWED https://code.claude.com/docs/en/permissions A rule that names a tool with no "
        "specifier, such as Bash, matches every use of that tool.",
    ]


def test_provenance_of_checks_alone_exits_0_and_a_decision_is_one_line() -> None:
    code, lines, _ = run("provenance", "UNPAIRED_ALLOWED")
    assert code == 0
    assert len(lines) == 1
    assert lines[0].startswith("DECISION UNPAIRED_ALLOWED On Windows the model may run")


def test_provenance_fails_on_a_file_that_leaves_a_check_out() -> None:
    with tempfile.TemporaryDirectory() as folder:
        path = write(Path(folder) / "provenance.json", json.dumps({"checks": {"wide-glob": [{"decision": "A."}]}}))
        with pytest.raises(skill_inventory.InputError) as caught:
            skill_inventory.provenance(["wide-glob"], path)
    assert "provenance.json: no entry for AGENT_HOOK, AGENT_UNDECLARED, " in str(caught.value)


def test_require_text_refuses_a_file_it_cannot_read_with_the_reason() -> None:
    with tempfile.TemporaryDirectory() as folder:
        path = write(Path(folder) / "With Space" / "SKILL.md", "text")
        with (
            mock.patch.object(Path, "read_bytes", side_effect=PermissionError(13, "Permission denied")),
            pytest.raises(skill_inventory.InputError) as caught,
        ):
            skill_inventory.require_text(path)
    assert str(caught.value) == f"cannot read {path.as_posix()}: Permission denied"


REPOSITORY_ROOT = SCRIPT.parents[3]
GUARDED_AGENT = (
    "---\n"
    "name: guarded\n"
    "description: Runs one prepared role.\n"
    "tools: Read, Grep\n"
    "model: inherit\n"
    "omitClaudeMd: true\n"
    "hooks:\n"
    "  PreToolUse:\n"
    '    - matcher: "Read|Grep"\n'
    "      hooks:\n"
    "        - type: command\n"
    "          command: >-\n"
    '            python -I -B -c "import guard;\n'
    '            guard.main()"\n'
    "          timeout: 30\n"
    "          onFailure: block\n"
    "  Stop:\n"
    "    - hooks:\n"
    "        - type: command\n"
    "          command: 'node \"$HOME/stop.js\"'\n"
    "---\n"
    "Follow the prompt file.\n"
)
# Every candidate line the subagent, model, and runtime checks print, which no skill this source ships yields.
SUBAGENT_CANDIDATES = (
    "AGENT_UNDEFINED",
    "AGENT_UNDECLARED",
    "AGENT_UNREADABLE",
    "UNGUARDED_SUBAGENT",
    "PINNED_SUBAGENT",
    "RUNTIME_OVERCLAIMED",
    "RUNTIME_UNDECLARED",
    "RUNTIME_UNREADABLE",
)


# JSON past the parser's nesting limit, and JSON holding more digits than Python converts to an integer from text.
UNPARSEABLE_JSON = {"too_deep": "[" * 100_000 + "]" * 100_000, "over_long_integer": '{"value": ' + "1" * 5_000 + "}"}
# Python source past the parser's limits: too deep for its stack, and too many nested parentheses.
UNPARSEABLE_PYTHON = {"too_deep": "-" * 100_000 + "1\n", "too_many_parentheses": "x = " + "(" * 300 + ")" * 300 + "\n"}
# Each character a line reader may end a line on, and a control character a terminal reads as the start of an escape.
LINE_UNSAFE_CHARACTERS = ("\n", "\r", "\x0b", "\x0c", "\x1c", "\x85", "\u2028", "\u2029", "\x1b", "\x00", "\x7f")
LINE_UNSAFE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")


class SourceTreeTestCase(TemporaryTestCase):
    """A skill in a throwaway source tree, with its metadata, its siblings, and the agents beside them."""

    def skill(self, body: str, metadata: object = None, name: str = "alpha", frontmatter: str = "") -> Path:
        write(self.root / "deploy-meta" / f"{name}.json", json.dumps({} if metadata is None else metadata))
        return write(self.root / "skills" / name / "SKILL.md", f"---\ndescription: Run.\n{frontmatter}---\n{body}")

    def agent(self, name: str, text: str = GUARDED_AGENT) -> Path:
        return write(self.root / "agents" / f"{name}.md", text)

    def lines(self, command: str, target: Path, prefixes: tuple[str, ...]) -> list[str]:
        code, lines, error = run(command, str(target))
        self.assertEqual(0, code, error)
        return [line for line in lines if line.startswith(prefixes)]


class AgentTests(SourceTreeTestCase):
    def test_inventory_declares_agents_with_the_skill_that_declares_them_and_tools(self) -> None:
        skill = self.skill(
            "Body.\n",
            {"skill_deps": ["core"], "agent_deps": ["own"], "tools": ["gh"], "optional_tools": ["copilot"]},
        )
        write(self.root / "deploy-meta" / "core.json", '{"skill_deps": ["base"], "agent_deps": ["guarded", "own"]}')
        write(self.root / "deploy-meta" / "base.json", '{"agent_deps": ["deep"]}')
        self.assertEqual(
            [
                "DECLARED skill core",
                "DECLARED agent own",
                "DECLARED agent guarded via core",
                "DECLARED agent deep via base",
                "DECLARED tool gh",
                "DECLARED optional-tool copilot",
            ],
            self.lines("inventory", skill.parent, ("DECLARED",)),
        )

    def test_a_definition_s_values_print_as_one_field_of_one_line_whatever_they_hold(self) -> None:
        skill = self.skill("Start one fresh subagent of type `guarded` for each role.\n", {"agent_deps": ["guarded"]})
        forged = GUARDED_AGENT.replace("model: inherit", "model: in\x1bherit\x7f").replace(
            "timeout: 30", "timeout: 3\x1b0"
        )
        definition = self.agent("guarded", forged.replace("tools: Read, Grep", "tools: Read\x1b[31m, Grep"))
        code, lines, error = run("scan", str(skill.parent))
        self.assertEqual(0, code, error)
        self.assertEqual(
            [
                f"AGENT guarded {definition.as_posix()}",
                "AGENT_TOOLS guarded Read[31m,Grep",
                "AGENT_MODEL guarded inherit",
                "AGENT_CLAUDE_MD guarded omitted",
                "AGENT_HOOK guarded PreToolUse Read|Grep python timeout=30 on_failure=block",
                "AGENT_HOOK guarded Stop * node timeout=default on_failure=default",
            ],
            [line for line in lines if line.startswith("AGENT")],
        )
        self.assertEqual([], [line for line in lines if LINE_UNSAFE.search(line)])

    def test_a_value_a_metadata_file_gives_stays_on_its_line(self) -> None:
        skill = self.skill("Body.\n", {"tools": ["gh\nAGENT forged", "git\u2028AGENT forged\x1b[0m"]})
        code, lines, error = run("inventory", str(skill.parent))
        self.assertEqual(0, code, error)
        self.assertEqual(
            ["DECLARED tool gh AGENT forged", "DECLARED tool git AGENT forged [0m"],
            [line for line in lines if "forged" in line],
        )

    def test_unparseable_metadata_is_unreadable_and_the_inventory_carries_on(self) -> None:
        for shape, text in UNPARSEABLE_JSON.items():
            with self.subTest(shape=shape):
                skill = self.skill("Body.\n")
                metadata = write(self.root / "deploy-meta" / "alpha.json", text)
                code, lines, error = run("inventory", str(skill.parent))
                self.assertEqual(0, code, error)
                self.assertIn(f"DECLARED_UNREADABLE {metadata.as_posix()}", lines)

    def test_unparseable_data_files_and_scenarios_are_passed_over_and_an_unparseable_script_fails_the_scan(
        self,
    ) -> None:
        for shape in sorted({*UNPARSEABLE_JSON, *UNPARSEABLE_PYTHON}):
            with self.subTest(shape=shape):
                skill = self.skill('```bash\npython "${CLAUDE_SKILL_DIR}/scripts/broken.py"\n```\n')
                script = write(skill.parent / "scripts" / "broken.py", UNPARSEABLE_PYTHON.get(shape, "x = 1\n"))
                write(skill.parent / "references" / "settings.json", UNPARSEABLE_JSON.get(shape, "{}"))
                scenario = self.root / "tests" / "fixtures" / "skill-evals" / "alpha" / "broken" / "scenario.json"
                write(scenario, UNPARSEABLE_JSON.get(shape, "{}"))
                for command, target in (("inventory", skill.parent), ("scan", skill.parent), ("tools", skill)):
                    code, lines, error = run(command, str(target))
                    if command == "scan" and shape in UNPARSEABLE_PYTHON:
                        # What the script prints cannot be measured, so the comparison never reads it as nothing.
                        self.assertEqual(
                            (1, f"FAILED cannot read {script.resolve().as_posix()}: not valid Python"),
                            (code, lines[-1]),
                            error,
                        )
                        continue
                    self.assertEqual(0, code, (command, lines, error))
                    self.assertEqual([], [line for line in lines if line.startswith("FAILED")], command)

    def test_scan_reads_the_definition_of_each_type_the_skill_names(self) -> None:
        skill = self.skill("Start one fresh subagent of type `guarded` for each role.\n", {"agent_deps": ["guarded"]})
        definition = self.agent("guarded")
        self.assertEqual(
            [
                f"AGENT guarded {definition.as_posix()}",
                "AGENT_TOOLS guarded Read,Grep",
                "AGENT_MODEL guarded inherit",
                "AGENT_CLAUDE_MD guarded omitted",
                "AGENT_HOOK guarded PreToolUse Read|Grep python timeout=30 on_failure=block",
                "AGENT_HOOK guarded Stop * node timeout=default on_failure=default",
            ],
            self.lines("scan", skill.parent, ("AGENT",)),
        )

    def test_a_definition_with_no_tools_model_or_hooks_grants_everything_and_follows_the_session(self) -> None:
        skill = self.skill("Start a `plain` subagent.\n", {"agent_deps": ["plain"]})
        definition = self.agent("plain", "---\nname: plain\ndescription: Anything.\n---\nBody.\n")
        self.assertEqual(
            [
                f"AGENT plain {definition.as_posix()}",
                "AGENT_TOOLS plain all",
                "AGENT_MODEL plain none",
                "AGENT_CLAUDE_MD plain loaded",
            ],
            self.lines("scan", skill.parent, ("AGENT",)),
        )

    def test_a_type_with_no_definition_or_no_declaration_is_a_candidate(self) -> None:
        skill = self.skill(
            "Start one subagent of type `missing`.\n"  # 4
            "If this session has no `missing` type, use a general-purpose subagent instead.\n"
            "Then start an `orphan` subagent, and an `Explore` subagent to search.\n",  # 6
        )
        self.agent("orphan")
        lines = self.lines("scan", skill.parent, ("AGENT_UNDEFINED", "AGENT_UNDECLARED", "UNGUARDED"))
        self.assertEqual(
            [f"AGENT_UNDEFINED {skill.as_posix()} 4 missing", f"AGENT_UNDECLARED {skill.as_posix()} 6 orphan"], lines
        )

    def test_outside_a_source_tree_a_named_type_needs_only_its_definition(self) -> None:
        home = self.root / "home"
        skill = write(home / ".claude" / "skills" / "alpha" / "SKILL.md", "Start a subagent of type `guarded`.\n")
        write(home / ".claude" / "agents" / "guarded.md", GUARDED_AGENT)
        lines = self.lines("scan", skill.parent, ("AGENT",))
        self.assertIn("AGENT_TOOLS guarded Read,Grep", lines)
        self.assertEqual([], [line for line in lines if line.startswith(("AGENT_UNDEFINED", "AGENT_UNDECLARED"))])

    def test_an_unreadable_definition_is_reported_in_place_of_its_grants(self) -> None:
        skill = self.skill("Start a subagent of type `broken`.\n", {"agent_deps": ["broken"]})
        definition = self.agent("broken", "---\nname: a\nmodel: [a\n---\nBody.\n")
        lines = self.lines("scan", skill.parent, ("AGENT",))
        self.assertEqual(1, len(lines), lines)
        self.assertTrue(lines[0].startswith(f"AGENT_UNREADABLE {definition.as_posix()} "), lines)

    def test_a_general_purpose_subagent_is_unguarded_only_where_a_guarded_type_is_at_hand(self) -> None:
        body = (
            "Start one fresh general-purpose subagent with this prompt.\n"  # 4
            "If this session has no `guarded` type, use a general-purpose subagent instead.\n"  # 5
        )
        skill = self.skill(body, {"skill_deps": ["core"]})
        self.assertEqual([], self.lines("scan", skill.parent, ("UNGUARDED",)), "no guarded type is declared")
        write(self.root / "deploy-meta" / "core.json", '{"agent_deps": ["guarded"]}')
        self.agent("guarded")
        self.assertEqual(
            [f"UNGUARDED_SUBAGENT {skill.as_posix()} 4 guarded"],
            self.lines("scan", skill.parent, ("UNGUARDED",)),
            "the fallback line names the guarded type, so it is not unguarded",
        )
        self.agent("guarded", "---\nname: guarded\ndescription: No hooks.\n---\nBody.\n")
        self.assertEqual([], self.lines("scan", skill.parent, ("UNGUARDED",)), "a type with no hook guards nothing")

    def test_review_prs_names_the_reviewer_its_tools_model_and_guard(self) -> None:
        skill = REPOSITORY_ROOT / "skills" / "review-prs"
        definition = REPOSITORY_ROOT / "agents" / "code-review-reviewer.md"
        self.assertEqual(
            [
                f"AGENT code-review-reviewer {definition.as_posix()}",
                "AGENT_TOOLS code-review-reviewer Read,Grep,Glob,Write,Edit,Bash",
                "AGENT_MODEL code-review-reviewer inherit",
                "AGENT_CLAUDE_MD code-review-reviewer omitted",
                "AGENT_HOOK code-review-reviewer PreToolUse Read|Grep|Glob|Write|Edit|Bash {{PYTHON}} timeout=30 "
                "on_failure=block",
            ],
            self.lines("scan", skill, ("AGENT",)),
        )
        self.assertIn(
            "DECLARED agent code-review-reviewer via ejryan89-code-review-core",
            self.lines("inventory", skill, ("DECLARED",)),
        )


class SubagentSettingTests(SourceTreeTestCase):
    def test_a_relayed_line_is_where_a_role_takes_its_model_and_effort(self) -> None:
        skill = self.skill(
            "When a `MODEL` line names that role, start its subagent on that model.\n"  # 4
            "When an `EFFORT` line names that role, start its subagent at that reasoning effort.\n"  # 5
            "Each `MODEL` line names a role; no sentence here delegates.\n",
        )
        self.assertEqual(
            [
                f"SUBAGENT_MODEL {skill.as_posix()} 4 printed MODEL",
                f"SUBAGENT_EFFORT {skill.as_posix()} 5 printed EFFORT",
            ],
            self.lines("tools", skill, ("SUBAGENT", "PINNED")),
        )

    def test_a_body_that_starts_a_subagent_on_a_literal_model_or_effort_pins_it(self) -> None:
        skill = self.skill(
            "Start one subagent on `haiku` at low effort for each file.\n"  # 4
            "Run the review subagent on opus.\n"  # 5
            "A subagent on haiku is cheaper, the docs say.\n",  # 6: starts nothing
        )
        self.assertEqual(
            [
                f"SUBAGENT_MODEL {skill.as_posix()} 4 haiku",
                f"SUBAGENT_EFFORT {skill.as_posix()} 4 low",
                f"SUBAGENT_MODEL {skill.as_posix()} 5 opus",
                f"PINNED_SUBAGENT {skill.as_posix()} 4 model haiku",
                f"PINNED_SUBAGENT {skill.as_posix()} 4 effort low",
                f"PINNED_SUBAGENT {skill.as_posix()} 5 model opus",
            ],
            self.lines("tools", skill, ("SUBAGENT", "PINNED")),
        )

    def test_a_profile_a_configuration_a_script_or_a_definition_pins_a_role(self) -> None:
        skill = self.skill(
            "Start a subagent of type `cheap` for each role.\n"
            'Run `python -B "${CLAUDE_SKILL_DIR}/../reach/scripts/run.py"`.\n',
            {"skill_deps": ["core"], "agent_deps": ["cheap"]},
        )
        self.agent("cheap", "---\nname: cheap\ndescription: Cheap.\nmodel: haiku\n---\nBody.\n")
        core = self.root / "skills" / "core"
        write(self.root / "deploy-meta" / "core.json", "{}")
        profile = write(core / "references" / "profile.md", "---\nmodel: haiku\neffort: inherit\n---\n# Profile\n")
        write(core / "references" / "other.md", "---\nmodel: inherit\n---\n# Inherits\n")
        config = write(core / "references" / "config.json", '{\n  "roles": [\n    {"reviewer_effort": "low"}\n  ]\n}\n')
        script = write(
            core / "scripts" / "dispatch.py", 'ROLE = {"model": "sonnet", "effort": None}\nrun(effort="low")\n'
        )
        write(core / "scripts" / "test_dispatch.py", 'CASE = {"model": "haiku"}\n')
        reached = write(self.root / "skills" / "reach" / "scripts" / "run.py", 'start(model="claude-opus-5-5")\n')
        self.assertEqual(
            [
                f"PINNED_SUBAGENT {(self.root / 'agents' / 'cheap.md').as_posix()} 4 model haiku",
                f"PINNED_SUBAGENT {config.as_posix()} 3 effort low",
                f"PINNED_SUBAGENT {profile.as_posix()} 2 model haiku",
                f"PINNED_SUBAGENT {script.as_posix()} 1 model sonnet",
                f"PINNED_SUBAGENT {script.as_posix()} 2 effort low",
                f"PINNED_SUBAGENT {reached.as_posix()} 1 model claude-opus-5-5",
            ],
            self.lines("tools", skill, ("PINNED",)),
        )

    def test_review_prs_and_review_document_take_each_role_from_printed_lines_and_pin_nothing(self) -> None:
        for name in ("review-prs", "review-document"):
            with self.subTest(skill=name):
                lines = self.lines("tools", REPOSITORY_ROOT / "skills" / name / "SKILL.md", ("SUBAGENT", "PINNED"))
                self.assertEqual(
                    {"SUBAGENT_MODEL printed MODEL", "SUBAGENT_EFFORT printed EFFORT"},
                    {" ".join([line.split()[0], *line.split()[3:]]) for line in lines},
                )


class RuntimeSupportTests(SourceTreeTestCase):
    GRANTS = 'allowed-tools: ["Agent", "Read"]\n'

    def runtime(self, skill: Path) -> list[str]:
        return self.lines("tools", skill, ("RUNTIME",))

    def full(self, **extra: object) -> dict[str, object]:
        return {"runtime_support": {"claude-code": "full", "codex": "full", "copilot-cli": "full"}, **extra}

    def test_review_document_states_each_runtimes_level_needs_and_reason(self) -> None:
        skill = REPOSITORY_ROOT / "skills" / "review-document"
        metadata = json.loads((REPOSITORY_ROOT / "deploy-meta" / "review-document.json").read_text(encoding="utf-8"))
        codex = metadata["runtime_support"]["codex"]
        lines = self.runtime(skill / "SKILL.md")
        self.assertIn("RUNTIME claude-code full needs=none", lines)
        self.assertIn(f"RUNTIME codex partial needs={','.join(codex['needs'])} {codex['reason']}", lines)
        self.assertIn("RUNTIME_NEEDS fail-closed-agent-hooks agent code-review-reviewer", lines)
        self.assertIn("RUNTIME_NEEDS sandbox-network tool gh", lines)
        self.assertEqual([], [line for line in lines if line.startswith("RUNTIME_OVERCLAIMED")])
        declared = self.lines("inventory", skill, ("DECLARED optional-tool",))
        self.assertEqual([f"DECLARED optional-tool {tool}" for tool in metadata["optional_tools"]], declared)

    def test_full_on_a_runtime_that_lacks_the_guard_an_agent_needs_is_overclaimed(self) -> None:
        skill = self.skill(
            "Start a subagent of type `guarded`.\n", self.full(agent_deps=["guarded"]), frontmatter=self.GRANTS
        )
        self.agent("guarded")
        self.assertEqual(
            [
                "RUNTIME claude-code full needs=none",
                "RUNTIME codex full needs=none",
                "RUNTIME copilot-cli full needs=none",
                "RUNTIME_NEEDS agent-delegation grant Agent",
                "RUNTIME_NEEDS fail-closed-agent-hooks agent guarded",
                "RUNTIME_OVERCLAIMED codex fail-closed-agent-hooks",
                "RUNTIME_OVERCLAIMED copilot-cli fail-closed-agent-hooks",
            ],
            self.runtime(skill),
        )

    def test_full_on_codex_for_a_network_tool_or_a_git_fetch_is_overclaimed(self) -> None:
        skill = self.skill("Body.\n", self.full(optional_tools=["gh"]))
        self.assertIn("RUNTIME_OVERCLAIMED codex sandbox-network", self.runtime(skill))
        skill = self.skill("Body.\n", self.full(), name="beta")
        write(skill.parent / "scripts" / "update.sh", '# git fetch here is a comment\ngit -C "$CLONE" status\n')
        write(skill.parent / "scripts" / "test_update.sh", "git fetch origin\n")
        self.assertEqual([], [line for line in self.runtime(skill) if line.startswith("RUNTIME_OVERCLAIMED")])
        write(skill.parent / "scripts" / "update.sh", 'if ! OUT=$(git -C "$CLONE" fetch --tags origin 2>&1); then\n')
        self.assertEqual(
            ["RUNTIME_NEEDS sandbox-network git fetch scripts/update.sh", "RUNTIME_OVERCLAIMED codex sandbox-network"],
            [line for line in self.runtime(skill) if line.startswith(("RUNTIME_NEEDS", "RUNTIME_OVERCLAIMED"))],
        )

    def test_full_on_codex_for_a_git_fetch_in_a_bash_suffixed_script_is_overclaimed(self) -> None:
        skill = self.skill("Body.\n", self.full())
        write(skill.parent / "scripts" / "update.BASH", 'git -C "$CLONE" fetch origin\n')
        self.assertEqual(
            [
                "RUNTIME_NEEDS sandbox-network git fetch scripts/update.BASH",
                "RUNTIME_OVERCLAIMED codex sandbox-network",
            ],
            [line for line in self.runtime(skill) if line.startswith(("RUNTIME_NEEDS", "RUNTIME_OVERCLAIMED"))],
        )

    def test_full_on_codex_for_a_python_git_fetch_is_overclaimed(self) -> None:
        skill = self.skill("Body.\n", self.full())
        write(skill.parent / "scripts" / "test_update.py", 'git(root, "fetch", "origin")\n')
        write(skill.parent / "scripts" / "update.py", 'git(root, "status")\n')
        self.assertEqual([], [line for line in self.runtime(skill) if line.startswith("RUNTIME_OVERCLAIMED")])
        write(skill.parent / "scripts" / "update.py", 'services.git.run(["fetch", "origin"])\n')
        self.assertEqual(
            ["RUNTIME_NEEDS sandbox-network git fetch scripts/update.py", "RUNTIME_OVERCLAIMED codex sandbox-network"],
            [line for line in self.runtime(skill) if line.startswith(("RUNTIME_NEEDS", "RUNTIME_OVERCLAIMED"))],
        )

    def test_a_python_script_that_does_not_parse_fails_the_runtime_derivation(self) -> None:
        skill = self.skill("Body.\n", self.full())
        write(skill.parent / "scripts" / "update.py", "git(\n")
        with self.assertRaisesRegex(skill_inventory.InputError, "update.py: not valid Python"):
            skill_inventory.own_needs(skill.parent)

    def test_a_partial_declaration_naming_the_lack_is_not_overclaimed(self) -> None:
        partial = {"level": "partial", "needs": ["sandbox-network"], "reason": "Its gh calls need the network."}
        support = {"claude-code": "full", "codex": partial, "copilot-cli": "full"}
        skill = self.skill("Body.\n", {"tools": ["gh"], "runtime_support": support})
        self.assertEqual(
            [
                "RUNTIME claude-code full needs=none",
                "RUNTIME codex partial needs=sandbox-network Its gh calls need the network.",
                "RUNTIME copilot-cli full needs=none",
                "RUNTIME_NEEDS sandbox-network tool gh",
            ],
            self.runtime(skill),
        )

    def test_a_skill_needs_what_each_skill_it_invokes_needs_but_not_its_user_only_start(self) -> None:
        skill = self.skill("Then invoke `beta --quick` for each one.\n", self.full())
        self.skill(
            "Run `gamma` once, then invoke `gamma`.\n",
            {"tools": ["gh"]},
            name="beta",
            frontmatter='allowed-tools: ["Agent"]\ndisable-model-invocation: true\n',
        )
        self.skill("Then invoke `alpha` again.\n", name="gamma", frontmatter='allowed-tools: ["Workflow"]\n')
        self.assertEqual(
            [
                "RUNTIME_NEEDS agent-delegation skill beta",
                "RUNTIME_NEEDS workflow skill beta",
                "RUNTIME_NEEDS sandbox-network skill beta",
                "RUNTIME_OVERCLAIMED codex workflow,sandbox-network",
                "RUNTIME_OVERCLAIMED copilot-cli workflow",
            ],
            [line for line in self.runtime(skill) if line.startswith(("RUNTIME_NEEDS", "RUNTIME_OVERCLAIMED"))],
        )

    def test_an_invoked_skill_s_required_and_optional_tools_each_carry_its_network_need(self) -> None:
        for key in ("tools", "optional_tools"):
            with self.subTest(key=key):
                skill = self.skill("Then invoke `beta`.\n", self.full())
                self.skill("Body.\n", {key: ["gh"]}, name="beta")
                self.assertEqual(
                    ["RUNTIME_NEEDS sandbox-network skill beta"],
                    [line for line in self.runtime(skill) if line.startswith("RUNTIME_NEEDS")],
                )

    def test_a_lowercase_skill_md_audited_on_its_own_invokes_nothing_and_reads_no_other_file(self) -> None:
        self.skill("Body.\n", self.full())
        skill = self.root / "skills" / "alpha" / "SKILL.md"
        lowered = skill.with_name("skill.md")
        skill.rename(lowered)
        self.assertEqual(
            [
                "RUNTIME claude-code full needs=none",
                "RUNTIME codex full needs=none",
                "RUNTIME copilot-cli full needs=none",
            ],
            self.runtime(lowered),
        )

    def test_a_missing_or_malformed_declaration_and_a_skill_outside_a_source_tree(self) -> None:
        self.assertEqual(["RUNTIME_UNDECLARED"], self.runtime(self.skill("Body.\n")))
        broken = self.runtime(self.skill("Body.\n", {"runtime_support": {"codex": "full"}}, name="beta"))
        self.assertEqual(["RUNTIME_UNREADABLE runtime_support does not declare claude-code"], broken)
        deployed = write(self.root / "home" / ".claude" / "skills" / "alpha" / "SKILL.md", "Body.\n")
        self.assertEqual([], self.runtime(deployed))


def test_no_skill_this_source_ships_or_keeps_yields_a_subagent_model_or_runtime_candidate() -> None:
    folders = sorted(
        path.parent
        for root in (REPOSITORY_ROOT / "skills", REPOSITORY_ROOT / ".claude" / "skills")
        for path in root.glob("*/SKILL.md")
    )
    found = []
    for folder in folders:
        for command, target in (("scan", folder), ("tools", folder / "SKILL.md")):
            code, lines, _ = run(command, str(target))
            assert code == 0, (folder, lines[-1:])
            found += [line for line in lines if line.startswith(SUBAGENT_CANDIDATES)]
    assert found == []


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('git -C "$CLONE" fetch --tags origin', {"fetch"}),
        ("OUT=$(git ls-remote --tags origin) && git push", {"ls-remote", "push"}),
        ("if git rev-parse --git-dir; then", {"rev-parse"}),
        ('echo "run git fetch first"', set()),
        ("  # git pull later", set()),
        ("legit fetch", set()),
    ],
)
def test_a_bash_script_runs_the_git_subcommand_after_gits_own_options(line: str, expected: set[str]) -> None:
    assert skill_inventory.bash_git_subcommands(line) == expected


# A plugin agent as tools/plugin_build.py writes it: the definition without its hooks, which move into the plugin's
# hooks file with each template value answered.
PLUGIN_AGENT = "---\nname: guarded\ndescription: Runs one prepared role.\ntools: Read, Grep\nmodel: inherit\n---\nGo.\n"
PLUGIN_HOOKS = {
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Read|Grep",
                "hooks": [{"type": "command", "command": 'python -I -B -c "import guard"', "timeout": 30}],
            }
        ],
        "Stop": [{"hooks": [{"type": "command", "command": "node stop.js", "onFailure": "block"}, {"type": "x"}]}],
    }
}


class PluginTests(TemporaryTestCase):
    """A skill's plugin form, in a plugin tree laid out as tools/plugin_build.py writes one and Claude Code installs
    one: the manifest, the skills with their template values answered, the agents namespaced under the plugin."""

    def setUp(self) -> None:
        super().setUp()
        self.home = self.root / "home with space"
        self.built = self.root / "built plugins"

    def plugin(self, name: str = "fixture-skills", folder: Path | None = None, hooks: object = None) -> Path:
        root = (self.built if folder is None else folder) / name
        write(root / ".claude-plugin" / "plugin.json", json.dumps({"name": name, "version": "0.0.0-dev"}))
        if hooks is not None:
            write(root / "hooks" / "hooks.json", json.dumps(hooks))
        return root

    def skill(self, root: Path, name: str = "alpha", text: str = "---\ndescription: Run.\n---\nBody.\n") -> Path:
        return write(root / "skills" / name / "SKILL.md", text)

    def locate(self, argument: str) -> list[str]:
        code, lines, error = run("locate", argument, "--home", str(self.home), "--repo", str(self.root / "repo"))
        self.assertEqual(("", 0 if lines and lines[-1].startswith("PLUGIN ") else 1), (error, code))
        return lines[1:]

    def test_a_directory_in_the_hooks_file_place_is_refused_by_name_never_read_as_no_hooks(self) -> None:
        root = self.plugin()
        skill = self.skill(root)
        hooks = root / "hooks" / "hooks.json"
        hooks.mkdir(parents=True)
        with self.assertRaises(skill_inventory.InputError) as caught:
            skill_inventory.plugin_hooks(skill.parent)
        self.assertEqual(f"cannot read {hooks.as_posix()}: not a file", str(caught.exception))

    def test_locate_finds_a_skill_in_a_built_plugin_folder_with_its_plugin(self) -> None:
        root = self.plugin()
        skill = self.skill(root)
        write(self.home / ".claude" / "skills" / "alpha" / "SKILL.md", "deployed")
        found = [
            f"SKILL_FILE {skill.as_posix()}",
            f"SKILL_DIR {skill.parent.as_posix()}",
            "SCOPE plugin",
            f"PLUGIN fixture-skills {root.as_posix()}",
        ]
        self.assertEqual(found, self.locate(f"alpha {self.built}"))
        self.assertEqual(found, self.locate(f"fixture-skills:alpha {root}"))

    def test_a_skill_two_plugins_carry_is_ambiguous_until_its_plugin_is_named(self) -> None:
        default, dotnet = self.plugin(), self.plugin("fixture-skills-dotnet")
        self.skill(default, "core")
        core = self.skill(dotnet, "core")
        lines = self.locate(f"core {self.built}")
        self.assertEqual(["AMBIGUOUS plugin", "AMBIGUOUS plugin"], [" ".join(line.split()[:2]) for line in lines[:2]])
        self.assertTrue(lines[2].endswith("Name the plugin, as <plugin>:<skill>, or its folder."), lines)
        self.assertEqual(f"SKILL_FILE {core.as_posix()}", self.locate(f"fixture-skills-dotnet:core {self.built}")[0])

    def test_an_installed_plugin_is_audited_when_it_is_the_only_copy_or_is_named(self) -> None:
        cache = self.home / ".claude" / "plugins" / "cache" / "fixture-market" / "fixture-skills"
        root = self.plugin(folder=cache, name="1.0.0")
        write(root / ".claude-plugin" / "plugin.json", json.dumps({"name": "fixture-skills"}))
        self.skill(root)
        self.assertEqual("SCOPE plugin", self.locate("alpha")[2])
        deployed = write(self.home / ".claude" / "skills" / "alpha" / "SKILL.md", "deployed")
        code, lines, _ = run("locate", "alpha", "--home", str(self.home))
        self.assertEqual((0, f"SKILL_FILE {deployed.as_posix()}", "SCOPE user"), (code, lines[1], lines[-1]))
        self.assertEqual(f"PLUGIN fixture-skills {root.as_posix()}", self.locate("fixture-skills:alpha")[-1])

    def test_a_skill_no_plugin_carries_lists_each_plugins_skills_by_their_plugin_names(self) -> None:
        self.skill(self.plugin(), "alpha")
        self.skill(self.plugin("other"), "beta")
        code, lines, _ = run("locate", f"gamma {self.built}", "--home", str(self.home))
        self.assertEqual(1, code)
        self.assertEqual(
            [
                "NOT_FOUND gamma",
                "AVAILABLE plugin fixture-skills:alpha",
                "AVAILABLE plugin other:beta",
                "STOP skill 'gamma' not found. Available skills: plugin: fixture-skills:alpha, other:beta",
            ],
            lines[1:],
        )

    def test_a_plugin_folder_that_does_not_exist_fails(self) -> None:
        code, lines, _ = run("locate", f"alpha {self.root / 'absent'}", "--home", str(self.home))
        self.assertEqual((1, [f"FAILED plugin folder not found: {(self.root / 'absent').as_posix()}"]), (code, lines))

    def test_inventory_names_the_plugin_and_what_it_carries_beside_the_skill(self) -> None:
        root = self.plugin()
        skill = self.skill(root)
        self.skill(root, "ejryan89-skill-core")
        write(root / "agents" / "guarded.md", PLUGIN_AGENT)
        code, lines, _ = run("inventory", str(skill.parent))
        self.assertEqual(0, code)
        self.assertEqual(
            [
                "CARRIED_BY fixture-skills",
                "PLUGIN_CARRIES skill ejryan89-skill-core",
                "PLUGIN_CARRIES agent fixture-skills:guarded",
            ],
            [line for line in lines if line.startswith(("CARRIED_BY", "PLUGIN", "LEFT_OUT", "DECLARED"))],
        )

    def test_scan_reads_namespaced_agents_and_the_hooks_the_plugin_runs_in_every_session(self) -> None:
        root = self.plugin(hooks=PLUGIN_HOOKS)
        skill = self.skill(
            root,
            text="---\ndescription: Run.\n---\n"  # 1-3
            "Start one fresh subagent of type `fixture-skills:guarded` for each role.\n"  # 4
            "Without it, start a general-purpose subagent.\n",  # 5
        )
        definition = write(root / "agents" / "guarded.md", PLUGIN_AGENT)
        code, lines, _ = run("scan", str(skill.parent))
        self.assertEqual(0, code)
        self.assertEqual(
            [
                f"AGENT fixture-skills:guarded {definition.as_posix()}",
                "AGENT_TOOLS fixture-skills:guarded Read,Grep",
                "AGENT_MODEL fixture-skills:guarded inherit",
                "AGENT_CLAUDE_MD fixture-skills:guarded loaded",
                "PLUGIN_HOOK PreToolUse Read|Grep python timeout=30 on_failure=default",
                "PLUGIN_HOOK Stop * node timeout=default on_failure=block",
                f"UNGUARDED_SUBAGENT {skill.as_posix()} 5 fixture-skills:guarded",
            ],
            [line for line in lines if line.startswith(("AGENT", "PLUGIN", "UNGUARDED"))],
        )

    def test_a_skill_in_a_plugin_without_hooks_prints_none(self) -> None:
        skill = self.skill(self.plugin(), text="---\ndescription: Run.\n---\nStart a general-purpose subagent.\n")
        code, lines, _ = run("scan", str(skill.parent))
        self.assertEqual((0, []), (code, [line for line in lines if line.startswith(("PLUGIN", "UNGUARDED"))]))

    def test_a_hooks_file_that_is_not_json_fails_the_scan(self) -> None:
        root = self.plugin()
        write(root / "hooks" / "hooks.json", "{not json")
        code, lines, _ = run("scan", str(self.skill(root).parent))
        self.assertEqual(1, code)
        self.assertTrue(lines[-1].startswith(f"FAILED {(root / 'hooks' / 'hooks.json').as_posix()} is not JSON"))

    def test_a_hooks_file_that_is_not_a_json_object_fails_the_scan(self) -> None:
        for shape, text in (("list", "[]"), ("string", '"hooks"'), ("null", "null")):
            with self.subTest(shape=shape):
                root = self.plugin(folder=self.root / shape)
                hooks = write(root / "hooks" / "hooks.json", text)
                code, lines, _ = run("scan", str(self.skill(root).parent))
                self.assertEqual((1, f"FAILED {hooks.as_posix()} is not a JSON object"), (code, lines[-1]))

    def test_an_unparseable_hooks_file_fails_the_scan(self) -> None:
        for shape, text in UNPARSEABLE_JSON.items():
            with self.subTest(shape=shape):
                root = self.plugin(folder=self.root / shape)
                hooks = write(root / "hooks" / "hooks.json", text)
                code, lines, _ = run("scan", str(self.skill(root).parent))
                self.assertEqual(1, code)
                self.assertTrue(lines[-1].startswith(f"FAILED {hooks.as_posix()} is not JSON: "), lines[-1])

    def test_an_unparseable_manifest_names_no_plugin_and_the_scan_carries_on(self) -> None:
        for shape, text in UNPARSEABLE_JSON.items():
            with self.subTest(shape=shape):
                root = self.plugin(folder=self.root / shape, hooks=PLUGIN_HOOKS)
                write(root / ".claude-plugin" / "plugin.json", text)
                code, lines, error = run("scan", str(self.skill(root).parent))
                self.assertEqual(0, code, error)
                self.assertEqual([], [line for line in lines if line.startswith(("PLUGIN", "FAILED"))])

    def test_every_hooks_field_prints_as_one_field_of_one_line_whatever_it_holds(self) -> None:
        for index, character in enumerate(LINE_UNSAFE_CHARACTERS):
            with self.subTest(character=repr(character)):
                forged = f"x{character}PLUGIN_HOOK forged"
                handler = {"type": "command", "command": f"py{character}thon -c 1", "timeout": forged}
                group = {"matcher": f"Read{forged}", "hooks": [{**handler, "onFailure": forged}]}
                root = self.plugin(folder=self.root / f"case {index}", hooks={"hooks": {f"Pre{forged}": [group]}})
                code, lines, error = run("scan", str(self.skill(root).parent))
                self.assertEqual(0, code, error)
                field = "xPLUGIN_HOOKforged"
                program = "py" if character.isspace() else "python"
                self.assertEqual(
                    [f"PLUGIN_HOOK Pre{field} Read{field} {program} timeout={field} on_failure={field}"],
                    [line for line in lines if "PLUGIN_HOOK" in line],
                )
                self.assertEqual([], [line for line in lines if LINE_UNSAFE.search(line)])

    def test_tools_judges_each_grant_against_the_plugins_answered_commands(self) -> None:
        # tools/plugin_build.py answers {{PYTHON}} with python in the fence and in the grant that names it, so the
        # answered grant matches its fence; a grant written by hand for another interpreter matches no command.
        own = '"${CLAUDE_SKILL_DIR}/scripts/tool.py"'
        quoted = own.replace('"', '\\"')
        skill = self.skill(
            self.plugin(),
            text="---\ndescription: Run.\n"
            f'allowed-tools: ["Bash(python -B {quoted} list *)", "Bash(python3 -B {quoted} report *)"]\n'
            "---\n```bash\n"
            f"python -B {own} list\n"
            "```\n```bash\n"
            f"python -B {own} report\n"  # 9
            "```\n",
        )
        code, lines, _ = run("tools", str(skill))
        self.assertEqual(0, code)
        self.assertEqual(
            [f"UNGRANTED Bash 9 python -B {own} report", f"IDLE_ALLOWED Bash(python3 -B {own} report *)"],
            [line for line in lines if line.startswith(("UNGRANTED Bash", "IDLE_ALLOWED"))],
        )


class SourcePluginTests(SourceTreeTestCase):
    """In its source tree, the plugins the source makes that carry the skill, or why none does."""

    def source(self, document: object = None) -> None:
        write(self.root / "source.json", json.dumps({"id": "owner/fixture-skills"} if document is None else document))
        write(self.root / ".claude-plugin" / "marketplace.json", '{"plugins": []}')

    def plugins(self, skill: Path) -> list[str]:
        return self.lines("inventory", skill.parent, ("CARRIED_BY", "LEFT_OUT", "PLUGINS_UNREADABLE"))

    def test_the_plugins_that_carry_the_skill_are_named(self) -> None:
        self.source({"id": "owner/fixture-skills", "bundles": {"dotnet": {"members": ["dotnet-one"], "opt_in": True}}})
        core = self.skill("Core.\n", {"selectable": False}, name="core")
        self.skill("Alpha.\n", {"skill_deps": ["core"]})
        self.skill("One.\n", {"skill_deps": ["core"]}, name="dotnet-one")
        self.assertEqual(["CARRIED_BY fixture-skills", "CARRIED_BY fixture-skills-dotnet"], self.plugins(core))

    def test_a_skill_no_plugin_carries_names_each_item_that_needs_it_and_why(self) -> None:
        self.source()
        rooted = self.skill("Rooted.\n", {"selectable": False, "required_vars": ["SOURCE_ROOT"]}, name="rooted")
        cleanup = self.skill("Cleanup.\n", {"skill_deps": ["rooted"]}, name="cleanup")
        orphan = self.skill("Orphan.\n", {"selectable": False}, name="orphan")
        # Built so that no template token appears in a skill's files, where the deployer would render one.
        reason = "LEFT_OUT cleanup rooted takes {" + "{SOURCE_ROOT}" + "}, which a plugin has no value for"
        self.assertEqual([reason], self.plugins(cleanup))
        self.assertEqual([reason], self.plugins(rooted))
        self.assertEqual(["LEFT_OUT orphan no menu item needs it"], self.plugins(orphan))

    def test_a_source_without_a_marketplace_makes_no_plugin_and_one_it_cannot_read_says_so(self) -> None:
        skill = self.skill("Alpha.\n")
        self.assertEqual([], self.plugins(skill))
        self.source({"id": "no-slash"})
        lines = self.plugins(skill)
        self.assertEqual(1, len(lines), lines)
        self.assertTrue(lines[0].startswith("PLUGINS_UNREADABLE ") and "names no source ID" in lines[0], lines)


class JsonFileDecodingTests(TemporaryTestCase):
    """Each JSON file the inventory reads decodes as UTF-8 on every platform, whatever the locale's codec."""

    def write_json(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def test_metadata_reads_its_non_ascii_text(self) -> None:
        self.write_json(self.root / "deploy-meta" / "example.json", {"about": "café"})
        self.assertEqual({"about": "café"}, skill_inventory.read_metadata(self.root, "example"))

    def test_a_plugin_manifest_beside_non_ascii_text_names_its_plugin(self) -> None:
        self.write_json(self.root / ".claude-plugin" / "plugin.json", {"description": "café", "name": "example"})
        self.assertEqual("example", skill_inventory.manifest_name(self.root))

    def test_a_trigger_scenario_with_non_ascii_requests_is_counted(self) -> None:
        self.write_json(self.root / "deploy-meta" / "example.json", {})
        scenario = self.root / skill_inventory.TRIGGER_SCENARIOS / "example" / "description" / "scenario.json"
        self.write_json(scenario, {"mode": "trigger", "requests": ["café"], "near_misses": []})
        self.assertEqual(
            [f"TRIGGER_SCENARIO {scenario.as_posix()} 1 0"],
            skill_inventory.trigger_scenario_lines(self.root / "skills" / "example", "model"),
        )

    def test_a_scenario_file_that_cannot_be_read_is_named_and_never_reads_as_missing(self) -> None:
        self.write_json(self.root / "deploy-meta" / "example.json", {})
        scenarios = self.root / skill_inventory.TRIGGER_SCENARIOS / "example"
        write(scenarios / "broken" / "scenario.json", "{")
        self.write_json(scenarios / "listed" / "scenario.json", ["trigger"])
        (scenarios / "folder" / "scenario.json").mkdir(parents=True)
        lines = skill_inventory.trigger_scenario_lines(self.root / "skills" / "example", "model")
        self.assertEqual(
            [
                ("TRIGGER_SCENARIO_UNREADABLE", (scenarios / name / "scenario.json").as_posix())
                for name in ("broken", "folder", "listed")
            ],
            [tuple(line.split()[:2]) for line in lines],
        )
        self.assertIn("not JSON", lines[0])
        self.assertTrue(lines[2].endswith("not a JSON object"), lines[2])
        # The reason is the operating system's own words, as the platform gives them for a folder read as a file.
        with self.assertRaises(OSError) as raised:
            (scenarios / "folder" / "scenario.json").read_bytes()
        folder = (scenarios / "folder" / "scenario.json").as_posix()
        self.assertEqual(f"TRIGGER_SCENARIO_UNREADABLE {folder} {raised.exception.strerror}", lines[1])


def test_a_line_splits_into_sentences_and_clauses_before_a_capital_span_emphasis_or_parenthesis() -> None:
    line = "Read it. Then stop; `NEXT` runs! (Once) more? *Done* here, e.g. lower stays."
    assert skill_inventory.sentences(line) == [
        "Read it.",
        "Then stop;",
        "`NEXT` runs!",
        "(Once) more?",
        "*Done* here, e.g. lower stays.",
    ]
