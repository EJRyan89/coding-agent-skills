from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import skill_inventory

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
            [f"AMBIGUOUS user {user.as_posix()}", f"AMBIGUOUS project-claude {project.as_posix()}"], lines[1:]
        )

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
            [f"AMBIGUOUS project-claude {project.as_posix()}", f"AMBIGUOUS source {source.as_posix()}"], lines[1:]
        )

    def test_missing_skill_lists_available_source_skills(self) -> None:
        self.source_skill("alpha")
        write(self.repo / "skills" / "shared-asset" / "SKILL.md", "x")
        code, lines, _ = self.locate("missing", "--repo", str(self.repo))
        self.assertEqual(1, code)
        self.assertEqual(["NOT_FOUND missing", "AVAILABLE source alpha"], lines[1:])

    def test_missing_skill_lists_available_skills_per_scope(self) -> None:
        write(self.user_skills / "Beta" / "SKILL.md", "x")
        write(self.user_skills / "alpha" / "skill.md", "x")
        (self.user_skills / "empty").mkdir()
        write(self.repo / ".agents" / "skills" / "gamma" / "SKILL.md", "x")
        code, lines, _ = self.locate("missing", "--repo", str(self.repo))
        self.assertEqual(1, code)
        self.assertEqual(
            ["NOT_FOUND missing", "AVAILABLE user alpha", "AVAILABLE user Beta", "AVAILABLE project-agents gamma"],
            lines[1:],
        )

    def test_outside_a_repository_only_the_user_scope_is_searched(self) -> None:
        write(self.root / ".claude" / "skills" / "alpha" / "SKILL.md", "x")
        lines = skill_inventory.locate("alpha", self.home, None)
        self.assertEqual(["REPO none", "NOT_FOUND alpha"], lines)

    def test_invalid_names_are_rejected(self) -> None:
        for name in ("../alpha", "a/b", "", "a b"):
            with self.subTest(name=name):
                code, lines, error = self.locate(name)
                self.assertEqual((1, ""), (code, error))
                self.assertEqual(1, len(lines))
                self.assertTrue(lines[0].startswith("FAILED invalid skill name"))

    def test_git_toplevel_finds_the_root_or_nothing(self) -> None:
        git("init", "-q", str(self.repo))
        (self.repo / "sub").mkdir()
        toplevel = skill_inventory.git_toplevel(self.repo / "sub")
        assert toplevel is not None, "a subdirectory of a repository has a top level"
        self.assertEqual(self.repo, toplevel.resolve())
        plain = self.root / "plain"
        plain.mkdir()
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.root)}):
            self.assertIsNone(skill_inventory.git_toplevel(plain))


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
        for fence in ("bash", "pwsh"):
            with self.subTest(fence=fence):
                lines = self.tools(
                    f'---\nallowed-tools: ["Bash(ls *)", "PowerShell(ls *)"]\n---\n```{fence}\nls x\n```\n'
                )
                self.assertEqual(
                    ["USED Bash 4", "USED PowerShell 4"],
                    [line for line in lines if line.startswith(("USED", "UNUSED", "MISSING"))],
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

    def test_grant_patterns_match_as_claude_code_does(self) -> None:
        # Claude Code compares the command's text, quotes included: `*` matches any text, a pattern without one
        # matches exactly, and the legacy `prefix:*` matches the prefix followed by arguments. Claude Code 2.1.288
        # on Windows denied the unquoted form below for the quoted command and ran the quoted form, in both shells.
        cases = [
            ('python -B "${CLAUDE_SKILL_DIR}/scripts/*', 'python -B "${CLAUDE_SKILL_DIR}/scripts/a.py" --help', True),
            ("python -B ${CLAUDE_SKILL_DIR}/scripts/* *", 'python -B "${CLAUDE_SKILL_DIR}/scripts/a.py" --help', False),
            ("git rev-parse --show-toplevel", "git rev-parse --show-toplevel", True),
            ("git rev-parse --show-toplevel", "git rev-parse --show-toplevel x", False),
            ("git rev-parse:*", "git rev-parse --git-dir", True),
            ("git rev-parse:*", "git rev-parsed", False),
            ("ls *", "ls -la", True),
            ("ls *", "lsof", False),
        ]
        for pattern, command, expected in cases:
            with self.subTest(pattern=pattern, command=command):
                self.assertEqual(expected, skill_inventory.grants(pattern, command))

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

    def test_without_shell_grants_no_command_is_reported_ungranted(self) -> None:
        self.assertEqual([], self.grants('["Read"]', "```bash\nls\n```\n"))
        self.assertEqual(["UNSCOPED_ALLOWED Bash"], self.grants('["Bash"]', "```bash\nls\n```\n"))

    def test_without_allowed_tools_nothing_is_missing_or_unused(self) -> None:
        lines = self.tools("Use the `Read` tool.\n")
        self.assertEqual(
            ["MODEL none", "DESCRIPTION 0 0", "INVOCATION model", "NO_ALLOWED_TOOLS", "USED Read 1"], lines
        )

    def test_listing_reports_the_description_every_session_loads(self) -> None:
        # 53 characters, ceil(53 / 4) = 14 tokens
        description = 'description: "Internal support. Not intended for direct invocation."'
        cases = {
            "description: Run the report.": ["DESCRIPTION 15 4", "INVOCATION model"],
            "description: >-\n  Run the\n  report.": ["DESCRIPTION 15 4", "INVOCATION model"],
            "description: 'Run the report.'\ndisable-model-invocation: 'true'": [
                "DESCRIPTION 15 4",
                "INVOCATION user-only",
            ],
            description: ["DESCRIPTION 53 14", "INVOCATION model", "INTERNAL_LISTED"],
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
        self.assertEqual(["MODEL none", "DESCRIPTION 4 1", "INVOCATION model", "NO_ALLOWED_TOOLS"], lines)

    def test_missing_file_fails(self) -> None:
        code, lines, error = run("tools", str(self.root / "absent.md"))
        self.assertEqual((1, ""), (code, error))
        self.assertEqual(1, len(lines))
        self.assertTrue(lines[0].startswith("FAILED skill file not found"))


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
            [(1, "loop"), (1, "per-item-command"), (2, "relayed-output"), (6, "subagent-reply")],
            [(int(line.split()[2]), line.split()[3]) for line in lines if line.startswith("CUE ")],
        )
        self.assertEqual(
            [f"RUNTIME_PROMPT {skill.as_posix()} 6 " + "${CLAUDE_SKILL_DIR}/scripts/pipeline.py"],
            [line for line in lines if line.startswith("RUNTIME_PROMPT ")],
        )

        bounded = write(self.root / "bounded.md", delegation + "Each subagent must reply with exactly: DONE.\n")
        _, lines, _ = run("scan", str(bounded))
        self.assertEqual(
            [f"RUNTIME_PROMPT {bounded.as_posix()} 1 unknown"],
            lines,
            "a bounded reply is not flagged, and no earlier command names the writer",
        )

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


class ExitContractTests(TemporaryTestCase):
    """The process-level contract: 1 with a last stdout line FAILED for a failure, 2 for usage alone."""

    def execute(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *arguments], capture_output=True, text=True, check=False
        )

    def test_a_missing_input_exits_1_with_failed_on_stdout(self) -> None:
        completed = self.execute("inventory", str(self.root / "absent"))
        self.assertEqual(1, completed.returncode)
        self.assertEqual("", completed.stderr)
        self.assertTrue(completed.stdout.splitlines()[-1].startswith("FAILED skill directory not found"))

    def test_a_usage_error_exits_2(self) -> None:
        for arguments in ((), ("unknown",), ("inventory",)):
            with self.subTest(arguments=arguments):
                completed = self.execute(*arguments)
                self.assertEqual(2, completed.returncode)
                self.assertEqual("", completed.stdout)
                self.assertIn("usage:", completed.stderr)


if __name__ == "__main__":
    unittest.main()
