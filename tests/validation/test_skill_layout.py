"""Fixture tests for tests/validation/skill_layout.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_layout import (
    MAXIMUM_INLINE_EXECUTABLE_LINES,
    REPOSITORY_SKILL_SHIMS,
    WORKING_FILES_DOC,
    ExecutableFence,
    deploy_variable_problems,
    embedded_program_problems,
    find_executable_fences,
    fixture_source_problems,
    metadata_format_problems,
    output_placeholder_problems,
    repository_skill_name_problems,
    runtime_support_problems,
    script_dependency_problems,
    script_language_problems,
    script_layout_problems,
    shared_guidance_problems,
    sibling_path_problems,
    skill_path_problems,
    skill_tree_problems,
    trigger_scenario_problems,
    unsupported_script_problems,
)
from validation_support import REPOSITORY_SKILLS, SKILL_GUIDE, write_fixture_tree

from deployer import platform_support


class SkillTreeFixtures(unittest.TestCase):
    def test_an_executable_outside_a_skills_scripts_fails_in_every_kind_of_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "skills/alpha/SKILL.md": "",
                    "skills/alpha/scripts/run.py": "",
                    "skills/alpha/scripts/nested/run.sh": "",
                    "skills/alpha/scripts/nested/data.json": "",
                    # Python's compiled modules, which no commit holds.
                    "skills/alpha/scripts/__pycache__/run.cpython-311.pyc": "",
                    "skills/alpha/tool.py": "",
                    "skills/alpha/references/notes.md": "",
                    "skills/beta/SKILL.md": "",
                    "skills/beta/helper.ps1": "",
                    ".claude/skills/local/SKILL.md": "",
                    ".claude/skills/local/run.sh": "",
                    ".claude/skills/local/scripts/run.sh": "",
                },
            )
            below = "is in a folder below its skill's scripts/, which the script policies do not read; move it to"
            self.assertEqual(
                [
                    f"skills/alpha/scripts/nested/data.json {below} skills/alpha/scripts/",
                    f"skills/alpha/scripts/nested/run.sh {below} skills/alpha/scripts/",
                    "skills/alpha/tool.py is an executable file outside its skill's scripts/; move it to "
                    "skills/alpha/scripts/",
                    "skills/beta/helper.ps1 is an executable file outside its skill's scripts/; move it to "
                    "skills/beta/scripts/",
                    ".claude/skills/local/run.sh is an executable file outside its skill's scripts/; move it to "
                    ".claude/skills/local/scripts/",
                ],
                script_layout_problems(root),
            )

    def test_every_directory_under_skills_is_a_skill_or_a_shared_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "source.json": json.dumps({"shared_assets": {"shared.md": "owner", "templates": "owner"}}),
                    "skills/alpha/SKILL.md": "",
                    "skills/alpha/scripts/run.py": "",
                    "skills/shared.md": "",
                    # A shared asset source.json declares may be a directory.
                    "skills/templates/one/page.md": "",
                    # A category of skills is no longer a layout the deployer reads.
                    "skills/group/inner/SKILL.md": "",
                    "skills/nested/SKILL.md": "",
                    "skills/nested/child/SKILL.md": "",
                    "skills/empty/notes.md": "",
                },
            )
            self.assertEqual(
                [
                    "skills/empty holds no SKILL.md and source.json declares no such shared asset",
                    "skills/group holds no SKILL.md and source.json declares no such shared asset",
                    "skills/group/inner/SKILL.md is deeper than skills/<name>",
                    "skills/nested/child/SKILL.md is deeper than skills/<name>",
                ],
                skill_tree_problems(root),
            )

    def test_a_flat_skills_directory_has_no_tree_problems(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root, {"skills/alpha/SKILL.md": "", "skills/beta/scripts/run.py": "", "skills/beta/SKILL.md": ""}
            )
            self.assertEqual([], skill_tree_problems(root))

    def test_a_repository_skill_or_shim_must_be_named_as_a_skill_and_as_its_folder(self) -> None:
        def skill(name: str) -> str:
            return f"---\nname: {name}\ndescription: Does a thing. Use it when asked.\n---\n"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    f"{REPOSITORY_SKILLS}/good-skill/SKILL.md": skill("good-skill"),
                    f"{REPOSITORY_SKILL_SHIMS}/good-skill/SKILL.md": skill("good-skill"),
                    f"{REPOSITORY_SKILLS}/Claude_Tool/SKILL.md": skill("some-other-name"),
                    f"{REPOSITORY_SKILL_SHIMS}/Claude_Tool/SKILL.md": skill("some-other-name"),
                    f"{REPOSITORY_SKILLS}/claude-helper/SKILL.md": skill("claude-helper"),
                    f"{REPOSITORY_SKILLS}/-dash/SKILL.md": skill("-dash"),
                    f"{REPOSITORY_SKILLS}/renamed/SKILL.md": skill("old-name"),
                    f"{REPOSITORY_SKILL_SHIMS}/nameless/SKILL.md": "---\ndescription: Does a thing.\n---\n",
                    f"{REPOSITORY_SKILLS}/unreadable/SKILL.md": "---\nname: [unreadable, list]\n---\n",
                },
            )
            problems = repository_skill_name_problems(root)
            invalid = "is not a skill name: at most 64 lowercase letters, digits, and hyphens"
            expected = [
                f"{REPOSITORY_SKILLS}/-dash/SKILL.md: '-dash' {invalid}",
                (f"{REPOSITORY_SKILLS}/Claude_Tool/SKILL.md: 'Claude_Tool' {invalid}"),
                (f"{REPOSITORY_SKILLS}/Claude_Tool/SKILL.md declares name 'some-other-name', not its folder's name"),
                f"{REPOSITORY_SKILLS}/claude-helper/SKILL.md: 'claude-helper' contains the reserved word 'claude'",
                f"{REPOSITORY_SKILLS}/renamed/SKILL.md declares name 'old-name', not its folder's name 'renamed'",
                f"{REPOSITORY_SKILLS}/unreadable/SKILL.md: its frontmatter cannot be read: ",
                f"{REPOSITORY_SKILL_SHIMS}/Claude_Tool/SKILL.md: 'Claude_Tool' {invalid}",
                f"{REPOSITORY_SKILL_SHIMS}/Claude_Tool/SKILL.md declares name 'some-other-name', not its folder's name",
                f"{REPOSITORY_SKILL_SHIMS}/nameless/SKILL.md declares no name, not its folder's name 'nameless'",
            ]
            self.assertEqual(len(expected), len(problems), problems)
            for want, found in zip(expected, problems, strict=True):
                self.assertTrue(found.startswith(want), (want, found))
            self.assertFalse([problem for problem in problems if "good-skill" in problem], problems)


class SkillLayoutFixtures(unittest.TestCase):
    def test_shared_guidance_policy_finds_a_skill_naming_a_shared_markdown_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = {"shared_assets": {"runtime-compatibility.md": "owner", "helper.json": "dependency"}}
            files = {
                "source.json": json.dumps(source),
                "skills/alpha/SKILL.md": "Read runtime-compatibility.md before you start.\n",
                "skills/beta/SKILL.md": "First read ../runtime-compatibility.md.\n",
                # Only Markdown guidance is the adapter's to point to.
                "skills/gamma/SKILL.md": "Load helper.json for the table.\n",
                "skills/delta/SKILL.md": "Nothing shared here.\n",
            }
            write_fixture_tree(root, files)
            self.assertEqual(
                [
                    "skills/alpha/SKILL.md points to runtime-compatibility.md; the runtime adapter does that",
                    "skills/beta/SKILL.md points to runtime-compatibility.md; the runtime adapter does that",
                ],
                shared_guidance_problems(root),
            )

    def test_embedded_script_policy_recognizes_long_executable_fences(self) -> None:
        short_example = ["```bash", "echo one", "echo two", "```"]
        embedded_program = ["```python", *[f"line_{n}()" for n in range(6)], "```"]
        self.assertEqual(2, find_executable_fences(short_example)[0].body_line_count)
        self.assertGreater(
            find_executable_fences(embedded_program)[0].body_line_count,
            MAXIMUM_INLINE_EXECUTABLE_LINES,
        )
        self.assertEqual([], find_executable_fences(["```text", *["x"] * 9, "```"]))
        # A fence left open runs to the end of the file, as the renderer reads it; unclosed_fence_problems reports it.
        self.assertEqual([ExecutableFence(1, "sh", 2)], find_executable_fences(["```sh", "echo open", "echo more"]))
        # The detector the renderer uses: a tilde fence counts, and a longer opener holds a three-backtick line.
        tilde_program = ["~~~bash", *[f"echo {n}" for n in range(6)], "~~~"]
        self.assertEqual([ExecutableFence(1, "bash", 6)], find_executable_fences(tilde_program))
        nested = ["````markdown", "```bash", *[f"echo {n}" for n in range(6)], "```", "````"]
        self.assertEqual([], find_executable_fences(nested), "a Markdown example is not an executable fence")

    def test_fence_length_policy_holds_repository_skills(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            program = "```bash\n" + "".join(f"echo {n}\n" for n in range(6)) + "```\n"
            example = "```powershell\n" + "".join(f"echo {n}\n" for n in range(5)) + "```\n"
            write_fixture_tree(
                root,
                {".claude/skills/alpha/SKILL.md": example + program, "skills/beta/SKILL.md": program},
            )
            self.assertEqual(
                [
                    ".claude/skills/alpha/SKILL.md:8 contains a bash fence with 6 lines. Markdown may contain only "
                    "short command examples of at most 5 lines; move executable logic to the skill's scripts/ "
                    "directory."
                ],
                embedded_program_problems(root, REPOSITORY_SKILLS),
            )

    def test_every_label_a_program_or_command_fence_may_carry_is_held_to_the_rules(self) -> None:
        def fence(label: str, line: str, count: int = 12) -> str:
            return f"```{label}\n" + "".join(f"{line} {n}\n" for n in range(count)) + "```\n\n"

        labels = ("py", "python3", "zsh", "ksh", "cmd", "bat", "node", "mjs", "console", "python")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            text = "".join(fence(label, "run") for label in labels) + fence("text", "plain")
            write_fixture_tree(root, {"skills/demo/SKILL.md": text})
            reported = [
                problem.split(" contains a ", 1)[1].split(" fence", 1)[0] for problem in embedded_program_problems(root)
            ]
            self.assertEqual(list(labels), reported)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, {"skills/demo/SKILL.md": "```py\nos.system('tool --output \"<file>\"')\n```\n"})
            write_fixture_tree(root, {"skills/demo/notes.md": '```text\ntool --output "<file>"\n```\n'})
            self.assertEqual(
                [
                    "skills/demo/SKILL.md:2 leaves --output to the agent; let the script choose and print the path; "
                    f"see {WORKING_FILES_DOC}"
                ],
                output_placeholder_problems(root),
            )

    def test_output_placeholder_policy_holds_repository_skills(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fence = '```bash\npython -B tools/a.py "<skill>"\npython -B tools/a.py --output "<file>"\n```\n'
            write_fixture_tree(root, {".claude/skills/alpha/SKILL.md": fence})
            self.assertEqual(
                [
                    ".claude/skills/alpha/SKILL.md:3 leaves --output to the agent; let the script choose and print "
                    f"the path; see {WORKING_FILES_DOC}"
                ],
                output_placeholder_problems(root, REPOSITORY_SKILLS),
            )

    def test_fixture_source_policy_detects_each_problem(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def write(relative_path: str, text: str) -> None:
                (root / relative_path).parent.mkdir(parents=True, exist_ok=True)
                (root / relative_path).write_text(text, encoding="utf-8")

            write("source.json", json.dumps({"id": "owner/shipped", "bundles": {"suite": {"members": ["alpha"]}}}))
            write("deploy-meta/alpha.json", "{}")
            write("skills/alpha/SKILL.md", "# alpha\n")
            write("skills/alpha/notes.md", "See tests/fixtures/probe for a sample.\n")
            write("tests/fixtures/same/source.json", json.dumps({"id": "owner/shipped"}))
            write("tests/fixtures/clash/source.json", json.dumps({"id": "test/clash"}))
            write("tests/fixtures/clash/deploy-meta/alpha.json", "{}")
            write("tests/fixtures/clash/deploy-meta/suite.json", "{}")
            write("tests/fixtures/clash/skills/alpha/SKILL.md", "# alpha\n")
            write("tests/fixtures/clash/skills/suite/SKILL.md", "```bash\npython -B scripts/run.py\n```\n")
            self.assertEqual(
                [
                    "tests/fixtures/clash skill alpha shares its name with a shipped skill or bundle",
                    "tests/fixtures/clash skill suite shares its name with a shipped skill or bundle",
                    "tests/fixtures/clash: skills/suite/SKILL.md:2 runs a script by a bare relative path; see "
                    '"Paths to a skill\'s own files" in docs/adding-a-skill.md',
                    "tests/fixtures/same/source.json reuses the shipped source ID",
                    "skills/alpha/notes.md names tests/fixtures, which never ships",
                ],
                fixture_source_problems(root),
            )

    def test_unsupported_script_policy_rejects_javascript_and_typescript_anywhere_in_a_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {
                **dict.fromkeys(
                    ("scripts/run.js", "scripts/tool.MJS", "scripts/test_tool.cjs", "types.ts", "scripts/ok.py"), "\n"
                ),
                "SKILL.md": "# demo\n",
                "scripts/helper.psm1": "Write-Host hi\n",
                "references/run.cmd": "@echo off\ndel /q *\n",
                "references/run.bat": "@echo off\n",
                "references/run": '#!/usr/bin/env bash\nrm -rf "$1"\n',
                # A script named by its extension may start with #!, and a file that does not is no program.
                "scripts/tool.sh": "#!/usr/bin/env bash\necho hi\n",
                "references/notes.txt": "a note\n",
            }
            write_fixture_tree(root, {f"skills/demo/{name}": text for name, text in files.items()})
            write_fixture_tree(root, {".claude/skills/local/SKILL.md": "# local\n", ".claude/skills/local/x.cmd": ""})
            reason = "a skill's executable files are Bash, Python, and PowerShell"
            self.assertEqual(
                [
                    f".claude/skills/local/x.cmd: {reason}",
                    f"skills/demo/references/run starts with #!, so it runs as a program; {reason}, named by their "
                    "extension",
                    f"skills/demo/references/run.bat: {reason}",
                    f"skills/demo/references/run.cmd: {reason}",
                    f"skills/demo/scripts/helper.psm1: {reason}",
                    f"skills/demo/scripts/run.js: {reason}",
                    f"skills/demo/scripts/test_tool.cjs: {reason}",
                    f"skills/demo/scripts/tool.MJS: {reason}",
                    f"skills/demo/types.ts: {reason}",
                ],
                unsupported_script_problems(root),
            )

    def test_script_language_policy_detects_a_missing_interpreter_and_a_stale_guide(self) -> None:
        # The command that runs .py scripts here: python on Windows and python3 elsewhere.
        python = "python" if platform_support.WINDOWS else "python3"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "docs").mkdir()
            guide = root / "docs" / "adding-a-skill.md"
            guide.write_text(
                "# Adding a skill\n\nA skill's executable files are Bash, Python, and PowerShell: `.bash`, `.sh`, "
                "`.py`, `.ps1`, and `.js` files. JavaScript and TypeScript (`.mjs`, `.bat`, `.cmd`, `.psm1`) are "
                "rejected.\n",
                encoding="utf-8",
            )
            self.assertEqual(
                [
                    "deployer/tools.py does not list `pwsh`, which runs `.ps1` scripts",
                    f"{SKILL_GUIDE} names `.js` as an executable extension",
                    f"{SKILL_GUIDE} does not name `.cjs` as unsupported",
                    f"{SKILL_GUIDE} does not name `.js` as unsupported",
                    f"{SKILL_GUIDE} does not name `.ts` as unsupported",
                ],
                script_language_problems(root, {"bash", python}),
            )
            guide.write_text("# Adding a skill\n", encoding="utf-8")
            self.assertEqual(
                [f"{SKILL_GUIDE} does not state which languages a skill's executable files may use"],
                script_language_problems(root, {"bash", python, "pwsh"}),
            )

    def test_skill_path_policy_detects_install_paths_bare_scripts_and_undeclared_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "deploy-meta").mkdir()
            (root / "agents").mkdir()
            skills = {"alpha": {"skill_deps": ["core"]}, "beta": {}, "core": {}, "audit-skill": {}}
            for skill, metadata in skills.items():
                (root / "deploy-meta" / f"{skill}.json").write_text(json.dumps(metadata), encoding="utf-8")
                (root / "skills" / skill / "scripts").mkdir(parents=True)
                (root / "skills" / skill / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
            (root / "skills" / "alpha" / "scripts" / "run.py").write_text("", encoding="utf-8")
            (root / "skills" / "core" / "scripts" / "lib.py").write_text("", encoding="utf-8")
            (root / "skills" / "alpha" / "references").mkdir()
            (root / "skills" / "alpha" / "references" / "checks.md").write_text(
                "See ${CLAUDE_SKILL_DIR}/references/checks.md and ${CLAUDE_SKILL_DIR}/references/<target>.md.\n"
                "A reference file may describe `scripts/run.py`, which Claude Code does not expand variables in.\n",
                encoding="utf-8",
            )
            (root / "skills" / "alpha" / "SKILL.md").write_text(
                "Searches `{{HOME}}/.claude/skills/<SKILL_NAME>/`.\n"
                "```bash\n"
                'python -B "${CLAUDE_SKILL_DIR}/scripts/run.py" --in "data/scripts/x"\n'
                'python -B "${CLAUDE_SKILL_DIR}/../core/scripts/lib.py"\n'
                "```\n"
                'Prose names `scripts/run.py`, grants `Bash(python -B "${CLAUDE_SKILL_DIR}/scripts/*)`, and points at '
                "${CLAUDE_SKILL_DIR}/references/checks.md.\n"
                "Give the user ${CLAUDE_SKILL_DIR}/references/gone.md "
                "and `${CLAUDE_SKILL_DIR}/../core/scripts/old.py`.\n"
                "Read `references/checks.md`, `./scripts/run.py`, `../core/scripts/lib.py`, and `references/`.\n"
                "Read `${CLAUDE_SKILL_DIR}/references/checks.md`; the target's `.github/scripts/x.py` and "
                "`data/references/y` are not ours.\n"
                "```text\n"
                "`scripts/run.py` in an example fence\n"
                "```\n",
                encoding="utf-8",
            )
            # audit-skill describes the scripts/ convention of the skills it audits, so that span is its own.
            (root / "skills" / "audit-skill" / "SKILL.md").write_text(
                "Recommend a tested command under `scripts/`, never `scripts/x.py`.\n", encoding="utf-8"
            )
            (root / "skills" / "beta" / "SKILL.md").write_text(
                "Read `{{HOME}}/.claude/skills/core/notes.md`.\n"
                "```bash\n"
                "python -B scripts/run.py\n"
                'TOOL="../core/scripts/lib.py"\n'
                'python -B "${CLAUDE_SKILL_DIR}/../core/scripts/lib.py"\n'
                "```\n",
                encoding="utf-8",
            )
            (root / "agents" / "helper.md").write_text(
                "Run `{{HOME}}/.claude/skills/beta/x.py`.\n"
                "          command: 'python -B \"$HOME/x.py\"'\n"
                "          command: 'python -B \"${HOME}/x.py\"'\n"
                "          command: 'python -B \"$env:HOME/x.py\"'\n"
                "          command: python -I -B -c \"import os; os.path.expanduser('~/x.py')\"\n"
                "Mentions $HOMEPAGE and $HOME_DIR.\n",
                encoding="utf-8",
            )
            doc = '"Paths to a skill\'s own files" in docs/adding-a-skill.md'
            agents_doc = '"Subagent definitions" in docs/adding-a-skill.md'
            self.assertEqual(
                [
                    f"agents/helper.md:1 names skill beta by its install path; see {doc}",
                    f"agents/helper.md:2 finds a file through $HOME; see {agents_doc}",
                    f"agents/helper.md:3 finds a file through $HOME; see {agents_doc}",
                    f"agents/helper.md:4 finds a file through $HOME; see {agents_doc}",
                    f"skills/alpha/SKILL.md:6 names `scripts/run.py` by a bare relative path; see {doc}",
                    "skills/alpha/SKILL.md:7 names ${CLAUDE_SKILL_DIR}/references/gone.md, which does not exist",
                    "skills/alpha/SKILL.md:7 names ${CLAUDE_SKILL_DIR}/../core/scripts/old.py, which does not exist",
                    f"skills/alpha/SKILL.md:8 names `references/checks.md` by a bare relative path; see {doc}",
                    f"skills/alpha/SKILL.md:8 names `./scripts/run.py` by a bare relative path; see {doc}",
                    f"skills/alpha/SKILL.md:8 names `../core/scripts/lib.py` by a bare relative path; see {doc}",
                    f"skills/alpha/SKILL.md:8 names `references/` by a bare relative path; see {doc}",
                    f"skills/audit-skill/SKILL.md:1 names `scripts/x.py` by a bare relative path; see {doc}",
                    f"skills/beta/SKILL.md:1 names skill core by its install path; see {doc}",
                    f"skills/beta/SKILL.md:3 runs a script by a bare relative path; see {doc}",
                    f"skills/beta/SKILL.md:4 runs a script by a bare relative path; see {doc}",
                    "skills/beta/SKILL.md:5 reaches ../core without declaring it in skill_deps",
                ],
                skill_path_problems(root),
            )

    def test_skill_path_policy_reads_each_document_once_and_each_check_only_where_it_applies(self) -> None:
        """Which documents skill_path_problems reads (every agent, grouped skills, nested files, never a directory
        without SKILL.md), and where each check is off: fence openers, non-shell fences, prose, agents, skills."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def write(relative_path: str, text: str) -> None:
                (root / relative_path).parent.mkdir(parents=True, exist_ok=True)
                (root / relative_path).write_text(text, encoding="utf-8")

            for skill, metadata in {
                "alpha": {"skill_deps": ["core"]},
                "audit-skill": {},
                "core": {},
                "hollow": {},
                "inner": {"skill_deps": ["core"]},
            }.items():
                write(f"deploy-meta/{skill}.json", json.dumps(metadata))
            write(
                "skills/alpha/SKILL.md",
                "Names `scripts/`, which only audit-skill may.\n"  # 1
                "Then run scripts/run.py yourself.\n"  # 2
                "Read {{HOME}}/.claude/skills/unknown/x and {{HOME}}/.claude/skills/inner/y.\n"  # 3
                'Use "$HOME/x" freely.\n'  # 4
                "See ${CLAUDE_SKILL_DIR}/references/ and ${CLAUDE_SKILL_DIR}/references/deep/notes.md.\n"  # 5
                "```bash ${CLAUDE_SKILL_DIR}/../undeclared/x\n"  # 6
                "sh scripts/x.sh\n"  # 7
                "```\n"  # 8
                "```python\n"  # 9
                "subprocess.run(['python', 'scripts/x.py'])\n"  # 10
                "```\n"  # 11
                "```Shell\n"  # 12
                "bash ./scripts/y.sh\n"  # 13
                "```\n",  # 14
            )
            write(
                "skills/alpha/references/deep/notes.md",
                "Reach ${CLAUDE_SKILL_DIR}/../stranger/x.md, as `scripts/z.py` says.\n",
            )
            write("skills/audit-skill/SKILL.md", "Recommend `scripts/`.\n")
            write("skills/core/SKILL.md", "# core\n")
            write("skills/hollow/notes.md", "Read {{HOME}}/.claude/skills/core/x.\n")
            write("skills/inner/SKILL.md", "Read `scripts/run.py`.\n")
            write("agents/a.md", "Run ${CLAUDE_SKILL_DIR}/../core/x and ${CLAUDE_SKILL_DIR}/gone.md.\n")
            write("agents/b.md", 'command: "$HOME/x"\n')
            doc = '"Paths to a skill\'s own files" in docs/adding-a-skill.md'
            agents_doc = '"Subagent definitions" in docs/adding-a-skill.md'
            self.assertEqual(
                [
                    f"agents/b.md:1 finds a file through $HOME; see {agents_doc}",
                    "skills/alpha/references/deep/notes.md:1 reaches ../stranger without declaring it in skill_deps",
                    "skills/alpha/references/deep/notes.md:1 names ${CLAUDE_SKILL_DIR}/../stranger/x.md, "
                    "which does not exist",
                    f"skills/alpha/SKILL.md:1 names `scripts/` by a bare relative path; see {doc}",
                    f"skills/alpha/SKILL.md:3 names skill inner by its install path; see {doc}",
                    f"skills/alpha/SKILL.md:7 runs a script by a bare relative path; see {doc}",
                    f"skills/alpha/SKILL.md:13 runs a script by a bare relative path; see {doc}",
                    f"skills/inner/SKILL.md:1 names `scripts/run.py` by a bare relative path; see {doc}",
                ],
                skill_path_problems(root),
            )

    def test_output_placeholder_policy_flags_output_options_only_in_command_fences(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "skills" / "alpha" / "references").mkdir(parents=True)
            (root / "skills" / "alpha" / "SKILL.md").write_text(
                'Prose may say --output "<file>".\n'
                "```bash\n"
                'python -B "${CLAUDE_SKILL_DIR}/scripts/a.py" enumerate --output "<batch file>"\n'
                'python -B "${CLAUDE_SKILL_DIR}/scripts/a.py" sweep --plans <plan directory> --output=<x>\n'
                'python -B "${CLAUDE_SKILL_DIR}/scripts/a.py" check --run "<run directory>" --input "<input file>"\n'
                'python -B "${CLAUDE_SKILL_DIR}/scripts/a.py" install --spec "<spec file>" --plan "<plan file>"\n'
                'python -B "${CLAUDE_SKILL_DIR}/scripts/a.py" collect --output "$TMP/x.json"\n'
                "```\n"
                '```text\n--output "<file>"\n```\n',
                encoding="utf-8",
            )
            (root / "skills" / "alpha" / "references" / "notes.md").write_text(
                "```powershell\npython -B x.py --output-dir '<dir>' --out <file>\n```\n", encoding="utf-8"
            )
            see = f"let the script choose and print the path; see {WORKING_FILES_DOC}"
            self.assertEqual(
                [
                    f"skills/alpha/SKILL.md:3 leaves --output to the agent; {see}",
                    f"skills/alpha/SKILL.md:4 leaves --plans to the agent; {see}",
                    f"skills/alpha/SKILL.md:4 leaves --output to the agent; {see}",
                    f"skills/alpha/references/notes.md:2 leaves --output-dir to the agent; {see}",
                    f"skills/alpha/references/notes.md:2 leaves --out to the agent; {see}",
                ],
                output_placeholder_problems(root),
            )

    def test_metadata_format_policy_detects_other_indentation_and_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "deploy-meta").mkdir()
            files = {
                "canonical": '{\n    "required_vars": [],\n    "shared_deps": ["runtime-compatibility.md"]\n}\n',
                "two-space": '{\n  "required_vars": [],\n  "shared_deps": ["runtime-compatibility.md"]\n}\n',
                "expanded": '{\n    "required_vars": [],\n    "shared_deps": [\n        "runtime-compatibility.md"\n'
                "    ]\n}\n",
                "no-newline": '{\n    "required_vars": []\n}',
            }
            for name, text in files.items():
                (root / "deploy-meta" / f"{name}.json").write_text(text, encoding="utf-8", newline="")
            doc = '"Metadata" in docs/adding-a-skill.md'
            self.assertEqual(
                [
                    f"deploy-meta/expanded.json is not in the canonical metadata format; see {doc}",
                    f"deploy-meta/no-newline.json is not in the canonical metadata format; see {doc}",
                    f"deploy-meta/two-space.json is not in the canonical metadata format; see {doc}",
                ],
                metadata_format_problems(root),
            )

    def test_runtime_support_policy_holds_each_declaration_to_what_the_skill_needs(self) -> None:
        full = {"claude-code": "full", "codex": "full", "copilot-cli": "full"}
        user_only = {"level": "partial", "needs": ["user-only-start"], "reason": "Start it interactively."}
        hidden = {"level": "none", "reason": "Nothing starts it."}
        delegating = '---\nname: {0}\ndescription: d\nallowed-tools: ["Agent", "Workflow"]\n---\n'
        metadata = {
            "plain": {"runtime_support": full},
            "undeclared": {},
            "malformed": {"runtime_support": {**full, "codex": {"level": "partial", "needs": ["telepathy"]}}},
            "user-only": {"runtime_support": {**full, "copilot-cli": user_only}},
            "user-only-claimed-full": {"runtime_support": full},
            "delegates": {
                "runtime_support": {
                    "claude-code": "full",
                    "codex": {"level": "partial", "needs": ["workflow"], "reason": "No effort."},
                    "copilot-cli": {"level": "partial", "needs": ["agent-delegation"], "reason": "Inline."},
                }
            },
            "over-claims": {"runtime_support": {**full, "codex": hidden}},
            "hidden": {
                "selectable": False,
                "runtime_support": {"claude-code": hidden, "codex": hidden, "copilot-cli": hidden},
            },
            "hidden-full": {"selectable": False, "runtime_support": full},
            # It invokes delegates, so the step that does inherits what delegates needs, though not its start.
            "invoker": {"runtime_support": full},
            # It starts an agent whose definition declares hooks, which only Claude Code blocks a tool call on when
            # one fails.
            "guarded": {
                "runtime_support": {
                    "claude-code": "full",
                    "codex": "full",
                    "copilot-cli": {"level": "partial", "needs": ["fail-closed-agent-hooks"], "reason": "x"},
                }
            },
            # It names the guarded agent but starts no subagent, so the agent's hooks are not its need.
            "mentions-agent": {"runtime_support": full},
            # A network tool, required or optional, needs network access, which Codex's default sandbox turns off.
            "networked": {"tools": ["gh"], "runtime_support": full},
            "optionally-networked": {
                "optional_tools": ["copilot"],
                "runtime_support": {
                    **full,
                    "codex": {"level": "partial", "needs": ["sandbox-network"], "reason": "No network."},
                },
            },
            # So does a Bash script's git subcommand that reaches a remote, a command substitution's included, though
            # git is never declared as a tool; a local git command and a test suite's fetch do not.
            "fetches": {"runtime_support": full},
            "fetches-in-a-substitution": {
                "runtime_support": {
                    **full,
                    "codex": {"level": "partial", "needs": ["sandbox-network"], "reason": "No network."},
                },
            },
            "reads-git-locally": {"runtime_support": full},
        }
        user_only_md = "---\nname: {0}\ndescription: d\ndisable-model-invocation: true\n---\n"
        plain_md = "---\nname: {0}\ndescription: d\n---\n"
        invoker_md = plain_md + "Then invoke `delegates` once.\n"
        bodies = {
            "user-only": user_only_md,
            "user-only-claimed-full": user_only_md,
            "delegates": delegating,
            "invoker": invoker_md,
            "guarded": '---\nname: {0}\ndescription: d\nallowed-tools: ["Agent"]\n---\nStart `guarded-reviewer`.\n',
            "mentions-agent": plain_md + "Never start `guarded-reviewer`.\n",
        }
        hooked_agent = "---\nname: guarded-reviewer\ndescription: d\nhooks:\n  PreToolUse:\n    - matcher: Bash\n---\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    **{f"deploy-meta/{name}.json": json.dumps(value) for name, value in metadata.items()},
                    **{f"skills/{name}/SKILL.md": bodies.get(name, plain_md).format(name) for name in metadata},
                    "agents/guarded-reviewer.md": hooked_agent,
                    "skills/fetches/scripts/update.sh": 'git -C "$CLONE" fetch --quiet origin\n',
                    "skills/fetches-in-a-substitution/scripts/sync.sh": (
                        "if ! OUTPUT=$(git -c core.quotepath=off ls-remote --tags \\\n  origin 2>&1); then exit 1; fi\n"
                    ),
                    "skills/reads-git-locally/scripts/status.sh": 'git -C "$CLONE" status --porcelain  # fetch later\n',
                    "skills/reads-git-locally/scripts/test_status.sh": 'git -C "$CLONE" fetch origin\n',
                },
            )
            doc = '"Metadata" in docs/adding-a-skill.md'
            hidden_rule = "but a hidden skill is none on every runtime"
            self.assertEqual(
                [
                    "deploy-meta/delegates.json declares copilot-cli needs agent-delegation, "
                    f"but copilot-cli lacks workflow; see {doc}",
                    "deploy-meta/fetches.json declares codex full, but codex lacks sandbox-network, "
                    f"so it is partial or none; see {doc}",
                    "deploy-meta/guarded.json declares codex full, but codex lacks fail-closed-agent-hooks, "
                    f"so it is partial or none; see {doc}",
                    f"deploy-meta/hidden-full.json declares claude-code full, {hidden_rule}",
                    f"deploy-meta/hidden-full.json declares codex full, {hidden_rule}",
                    f"deploy-meta/hidden-full.json declares copilot-cli full, {hidden_rule}",
                    "deploy-meta/invoker.json declares codex full, but codex lacks workflow, so it is partial or none; "
                    f"see {doc}",
                    "deploy-meta/invoker.json declares copilot-cli full, but copilot-cli lacks workflow, "
                    f"so it is partial or none; see {doc}",
                    f"deploy-meta/malformed.json: codex needs unknown capability 'telepathy'; see {doc}",
                    "deploy-meta/networked.json declares codex full, but codex lacks sandbox-network, "
                    f"so it is partial or none; see {doc}",
                    "deploy-meta/over-claims.json declares codex none, but codex offers everything the skill needs, "
                    f"so it is full; see {doc}",
                    f"deploy-meta/undeclared.json declares no runtime_support; see {doc}",
                    "deploy-meta/user-only-claimed-full.json declares copilot-cli full, but copilot-cli lacks "
                    f"user-only-start, so it is partial or none; see {doc}",
                ],
                runtime_support_problems(root),
            )

    def test_deploy_variable_policy_detects_unused_and_undeclared_variables(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "deploy-meta").mkdir()
            (root / "deploy-meta" / "alpha.json").write_text(
                json.dumps({"required_vars": ["REPOS_ROOT", "HOME"]}), encoding="utf-8"
            )
            (root / "skills" / "alpha").mkdir(parents=True)
            (root / "skills" / "alpha" / "SKILL.md").write_text("{{REPOS_ROOT}} {{SOURCE_ROOT}}\n", encoding="utf-8")
            (root / "skills" / "shared.md").write_text("{{REPOS_ROOT}} {{HOME}}\n", encoding="utf-8")
            (root / "source.json").write_text(json.dumps({"shared_assets": {"shared.md": "owner"}}), encoding="utf-8")
            self.assertEqual(
                [
                    "configure never prompts for configured variable SPARE",
                    "configure prompts for unknown variable EXTRA",
                    "skill alpha uses {{SOURCE_ROOT}} without declaring it in required_vars",
                    "skill alpha declares required variable HOME but never uses it",
                    "configured variable SPARE is not required by any skill",
                    "shared asset shared.md uses non-derived variable REPOS_ROOT",
                    "derived variable UNUSED_DERIVED is not used by any skill or shared asset",
                ],
                deploy_variable_problems(
                    root, {"REPOS_ROOT", "SPARE"}, {"REPOS_ROOT", "EXTRA"}, {"HOME", "SOURCE_ROOT", "UNUSED_DERIVED"}
                ),
            )

    def test_script_dependency_policy_detects_an_undeclared_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "deploy-meta").mkdir()
            for skill, metadata in {"user": {"skill_deps": ["core"]}, "core": {"selectable": False}}.items():
                (root / "deploy-meta" / f"{skill}.json").write_text(json.dumps(metadata), encoding="utf-8")
                (root / "skills" / skill / "scripts").mkdir(parents=True)
                (root / "skills" / skill / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")

            def insert(skill: str) -> str:
                return f'sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "{skill}" / "scripts"))\n'

            scripts = root / "skills" / "user" / "scripts"
            looked_up = 'sys.path.insert(0, str(sibling_scripts("looked-up")))\n'
            (scripts / "user.py").write_text(insert("core") + insert("other") + looked_up, encoding="utf-8")
            # A regression suite runs from the source tree, where every skill is present.
            (scripts / "test_user.py").write_text(insert("suite-only"), encoding="utf-8")
            # A skill naming its own scripts directory needs no declaration.
            (root / "skills" / "core" / "scripts" / "core.py").write_text(insert("core"), encoding="utf-8")
            self.assertEqual(
                [
                    "skills/user/scripts/user.py puts looked-up's scripts on sys.path without declaring looked-up "
                    'in skill_deps; see "Paths to a skill\'s own files" in docs/adding-a-skill.md',
                    "skills/user/scripts/user.py puts other's scripts on sys.path without declaring other in "
                    'skill_deps; see "Paths to a skill\'s own files" in docs/adding-a-skill.md',
                ],
                script_dependency_problems(root),
            )

    def test_sibling_path_policy_holds_every_module_to_the_lookup(self) -> None:
        bootstrap = 'sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))\n'
        lookup = 'from skill_roots import sibling_scripts\nsys.path.insert(0, str(sibling_scripts("dep")))\n'
        hand_written = 'sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "dep" / "scripts"))\n'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "skills/user/scripts/user.py": bootstrap + lookup,
                    "skills/user/scripts/test_user.py": bootstrap + lookup + hand_written,
                    # A skill may name its own scripts directory; only a sibling's goes through the lookup.
                    "skills/dep/scripts/dep.py": bootstrap + hand_written,
                    f"{REPOSITORY_SKILLS}/local/scripts/local.py": (
                        'sys.path.insert(0, str(ROOT / "skills" / "ejryan89-skill-core" / "scripts"))\n'
                        'sys.path.append(str(ROOT / "skills" / "dep" / "scripts"))\n'
                    ),
                    "tools/tool.py": "sys.path.insert(0, str(sibling_scripts(NAME)))\n",
                    "tests/suite/test_suite.py": (
                        'sys.path.insert(0, str(skill_roots.sibling_scripts("ejryan89-skill-core")))\n'
                        f"TEXT = {hand_written!r}\n"
                    ),
                    "deploy.py": 'sys.path.insert(0, str(sibling_scripts(name="dep")))\n',
                    # A fixture tree is another repository, read and never run from here.
                    "tests/fixtures/repo/skills/x/scripts/x.py": hand_written,
                },
            )
            doc = 'see "Validation" in docs/adding-a-skill.md'
            self.assertEqual(
                [
                    f"{REPOSITORY_SKILLS}/local/scripts/local.py:2 puts dep's scripts on sys.path by a hand-written "
                    f'path; put sibling_scripts("dep") from skill_roots there instead; {doc}',
                    "deploy.py:1 passes sibling_scripts a name it cannot read; pass the skill's name as one string "
                    f"literal; {doc}",
                    "skills/user/scripts/test_user.py:4 puts dep's scripts on sys.path by a hand-written path; put "
                    f'sibling_scripts("dep") from skill_roots there instead; {doc}',
                    "tests/suite/test_suite.py:1 looks up ejryan89-skill-core through sibling_scripts; the line that "
                    "puts it "
                    f"on sys.path already does; {doc}",
                    "tools/tool.py:1 passes sibling_scripts a name it cannot read; pass the skill's name as one "
                    f"string literal; {doc}",
                ],
                sibling_path_problems(root),
            )


TRIGGER_DOC = 'see "Skill evaluations" in docs/skill-evaluations.md'
MISSING_ALPHA = (
    "skills/alpha may be started by the model and has no trigger scenario under tests/fixtures/skill-evals/alpha/; "
    + TRIGGER_DOC
)


def trigger_spec(skill: str, requests: int = 3) -> dict[str, Any]:
    return {
        "skill": skill,
        "mode": "trigger",
        "requests": [f"Do the {skill} task, phrasing {number}." for number in range(1, requests + 1)],
        "near_misses": ["Do another task."],
    }


def trigger_tree(root: Path, scenarios: Mapping[str, object]) -> None:
    """A source tree with alpha, which the model may start, and beta, which only the user may, and these scenarios."""
    write_fixture_tree(
        root,
        {
            "skills/alpha/SKILL.md": "---\nname: alpha\ndescription: Alpha. Use it when asked.\n---\n",
            "skills/beta/SKILL.md": "---\nname: beta\ndescription: Beta.\ndisable-model-invocation: true\n---\n",
            **{
                f"tests/fixtures/skill-evals/{folder}/scenario.json": spec
                if isinstance(spec, str)
                else json.dumps(spec)
                for folder, spec in scenarios.items()
            },
        },
    )


def test_trigger_scenario_policy_passes_each_skill_the_model_may_start_holding_one(tmp_path: Path) -> None:
    # A review scenario is the evaluation's own to load, and a user-only skill needs no trigger scenario.
    trigger_tree(tmp_path, {"alpha/description": trigger_spec("alpha"), "alpha/review": {"mode": "initial"}})
    assert trigger_scenario_problems(tmp_path) == []


@pytest.mark.parametrize(
    ("scenarios", "problems"),
    [
        pytest.param({}, [MISSING_ALPHA], id="no scenario"),
        pytest.param(
            {"alpha/description": trigger_spec("alpha", requests=0)},
            [
                f"tests/fixtures/skill-evals/alpha/description holds 0 requests, fewer than 3; {TRIGGER_DOC}",
                MISSING_ALPHA,
            ],
            id="no requests",
        ),
        pytest.param(
            {"alpha/description": trigger_spec("alpha", requests=2)},
            [
                f"tests/fixtures/skill-evals/alpha/description holds 2 requests, fewer than 3; {TRIGGER_DOC}",
                MISSING_ALPHA,
            ],
            id="too few requests",
        ),
        pytest.param(
            {"alpha/description": trigger_spec("alpha"), "ghost/description": trigger_spec("ghost")},
            [
                "tests/fixtures/skill-evals/ghost/description names ghost, which is no skill under skills/; "
                + TRIGGER_DOC
            ],
            id="no such skill",
        ),
        pytest.param(
            {"alpha/description": trigger_spec("alpha"), "beta/description": trigger_spec("beta")},
            [
                "tests/fixtures/skill-evals/beta/description names beta, which sets disable-model-invocation, so no "
                f"request can start it; {TRIGGER_DOC}"
            ],
            id="user-only skill",
        ),
        pytest.param(
            {"alpha/description": trigger_spec("beta")},
            [
                'tests/fixtures/skill-evals/alpha/description names skill "beta", not its folder\'s, alpha; '
                + TRIGGER_DOC,
                MISSING_ALPHA,
            ],
            id="another folder's skill",
        ),
        pytest.param(
            {"alpha/description": {**trigger_spec("alpha"), "near_misses": []}},
            [f"tests/fixtures/skill-evals/alpha/description holds no near miss; {TRIGGER_DOC}", MISSING_ALPHA],
            id="no near miss",
        ),
        pytest.param(
            {"alpha/description": trigger_spec("alpha"), "alpha/broken": "{not json"},
            [
                "tests/fixtures/skill-evals/alpha/broken/scenario.json cannot be read: Expecting property name "
                "enclosed in double quotes: line 1 column 2 (char 1)"
            ],
            id="unreadable scenario",
        ),
    ],
)
def test_trigger_scenario_policy_refuses_a_missing_or_unloadable_scenario(
    scenarios: Mapping[str, object], problems: list[str], tmp_path: Path
) -> None:
    trigger_tree(tmp_path, scenarios)
    assert trigger_scenario_problems(tmp_path) == problems
