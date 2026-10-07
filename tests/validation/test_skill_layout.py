"""Fixture tests for tests/validation/skill_layout.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from skill_layout import (
    MAXIMUM_INLINE_EXECUTABLE_LINES,
    WORKING_FILES_DOC,
    ExecutableFence,
    deploy_variable_problems,
    embedded_program_problems,
    find_executable_fences,
    fixture_source_problems,
    metadata_format_problems,
    output_placeholder_problems,
    script_dependency_problems,
    script_language_problems,
    skill_path_problems,
    unsupported_script_problems,
)
from validation_support import REPOSITORY_SKILLS, SKILL_GUIDE, write_fixture_tree


class SkillLayoutFixtures(unittest.TestCase):
    def test_embedded_script_policy_recognizes_long_executable_fences(self) -> None:
        short_example = ["```bash", "echo one", "echo two", "```"]
        embedded_program = ["```python", *[f"line_{n}()" for n in range(6)], "```"]
        self.assertEqual(2, find_executable_fences(short_example)[0].body_line_count)
        self.assertGreater(
            find_executable_fences(embedded_program)[0].body_line_count,
            MAXIMUM_INLINE_EXECUTABLE_LINES,
        )
        self.assertEqual([], find_executable_fences(["```text", *["x"] * 9, "```"]))
        with self.assertRaisesRegex(AssertionError, "Unclosed executable"):
            find_executable_fences(["```sh", "echo open"])
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
            for name in ("scripts/run.js", "scripts/tool.MJS", "scripts/test_tool.cjs", "types.ts", "scripts/ok.py"):
                path = root / "skills" / "demo" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("\n", encoding="utf-8")
            reason = "a skill's executable files are Bash, Python, and PowerShell"
            self.assertEqual(
                [
                    f"skills/demo/scripts/run.js: {reason}",
                    f"skills/demo/scripts/test_tool.cjs: {reason}",
                    f"skills/demo/scripts/tool.MJS: {reason}",
                    f"skills/demo/types.ts: {reason}",
                ],
                unsupported_script_problems(root),
            )

    def test_script_language_policy_detects_a_missing_interpreter_and_a_stale_guide(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "docs").mkdir()
            guide = root / "docs" / "adding-a-skill.md"
            guide.write_text(
                "# Adding a skill\n\nA skill's executable files are Bash, Python, and PowerShell: `.bash`, `.sh`, "
                "`.py`, `.ps1`, and `.js` files. JavaScript and TypeScript (`.mjs`) are rejected.\n",
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
                script_language_problems(root, {"bash", "python"}),
            )
            guide.write_text("# Adding a skill\n", encoding="utf-8")
            self.assertEqual(
                [f"{SKILL_GUIDE} does not state which languages a skill's executable files may use"],
                script_language_problems(root, {"bash", "python", "pwsh"}),
            )

    def test_skill_path_policy_detects_install_paths_bare_scripts_and_undeclared_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "deploy-meta").mkdir()
            (root / "agents").mkdir()
            skills = {"alpha": {"skill_deps": ["core"]}, "beta": {}, "core": {}, "analyze-skill-cost": {}}
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
            # analyze-skill-cost describes the scripts/ convention of the skills it audits, so that span is its own.
            (root / "skills" / "analyze-skill-cost" / "SKILL.md").write_text(
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
                    f"skills/analyze-skill-cost/SKILL.md:1 names `scripts/x.py` by a bare relative path; see {doc}",
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
                "analyze-skill-cost": {},
                "core": {},
                "hollow": {},
                "inner": {"skill_deps": ["core"]},
            }.items():
                write(f"deploy-meta/{skill}.json", json.dumps(metadata))
            write(
                "skills/alpha/SKILL.md",
                "Names `scripts/`, which only analyze-skill-cost may.\n"  # 1
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
            write("skills/analyze-skill-cost/SKILL.md", "Recommend `scripts/`.\n")
            write("skills/core/SKILL.md", "# core\n")
            write("skills/hollow/notes.md", "Read {{HOME}}/.claude/skills/core/x.\n")
            write("skills/group/inner/SKILL.md", "Read `scripts/run.py`.\n")
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
                    f"skills/group/inner/SKILL.md:1 names `scripts/run.py` by a bare relative path; see {doc}",
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

            def insert(skill: str) -> str:
                return f'sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "{skill}" / "scripts"))\n'

            scripts = root / "skills" / "user" / "scripts"
            (scripts / "user.py").write_text(insert("core") + insert("other"), encoding="utf-8")
            # A regression suite runs from the source tree, where every skill is present.
            (scripts / "test_user.py").write_text(insert("suite-only"), encoding="utf-8")
            # A skill naming its own scripts directory needs no declaration.
            (root / "skills" / "core" / "scripts" / "core.py").write_text(insert("core"), encoding="utf-8")
            self.assertEqual(
                [
                    "skills/user/scripts/user.py puts other's scripts on sys.path without declaring other in "
                    'skill_deps; see "Paths to a skill\'s own files" in docs/adding-a-skill.md'
                ],
                script_dependency_problems(root),
            )


if __name__ == "__main__":
    unittest.main()
