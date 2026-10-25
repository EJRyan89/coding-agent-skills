"""Regression suite for tools/new_skill.py: scaffolding a skill's files and its skill reference section."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))
# The policies a scaffolded script header must pass.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "validation"))

import frontmatter as fm
from module_closure import copy_with_imports
from skill_grants import skill_grant_problems
from skill_layout import script_dependency_problems, sibling_path_problems

from tools import new_skill, skill_reference, skill_shims

README = (
    "# Fixture\n\n## Included skills\n\n| Skill | What it does |\n|---|---|\n"
    "| [`alpha`](docs/skills.md#alpha) | Alpha. |\n| `suite` | A bundle. |\n\nSee [Skills](docs/skills.md) and "
    "[Runtime support](docs/skills.md#runtime-support).\n"
)
REFERENCE = (
    "# Skills\n\n<!-- generated:summary -->\n<!-- /generated:summary -->\n\n"
    "<!-- generated:runtime-support -->\n<!-- /generated:runtime-support -->\n"
)
FULL = {"claude-code": "full", "codex": "full", "copilot-cli": "full"}
DESCRIPTION = 'Report "widgets" in a repository with spaces — use it when asked about them.'
# The grants a fresh scaffold of widget-report writes, which no step uses yet and validation's grant check refuses.
OWN_SCRIPT = '{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/widget_report.py" *'
GRANTS_REMAINING = [
    *(
        f"skills/widget-report/SKILL.md grants {tool}, which no step uses yet; use it in a step or remove it from "
        "allowed-tools"
        for tool in ("Bash", "PowerShell", "Read")
    ),
    *(
        f"skills/widget-report/SKILL.md grants {tool}({OWN_SCRIPT}), which matches no command in its fences or code "
        "spans yet; show the command it covers or remove the grant"
        for tool in ("Bash", "PowerShell")
    ),
]


class FixtureRepositoryTestCase(unittest.TestCase):
    """A fixture source with two shipped skills, one in a bundle, and a current skill reference."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="new-skill-test.")
        self.root = Path(self._temporary.name).resolve() / "Repo With Spaces"
        for directory in ("docs", "skills/alpha", "skills/beta", "deploy-meta", ".claude/skills"):
            (self.root / directory).mkdir(parents=True)
        (self.root / "source.json").write_text(
            json.dumps({"id": "test/skills", "bundles": {"suite": {"members": ["beta"]}}}), encoding="utf-8"
        )
        for name in ("alpha", "beta"):
            (self.root / "skills" / name / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: {name.capitalize()}. Use it when testing.\n---\n\nBody.\n",
                encoding="utf-8",
            )
            (self.root / "deploy-meta" / f"{name}.json").write_text(
                json.dumps({"required_vars": [], "shared_deps": []}), encoding="utf-8"
            )
        (self.root / "README.md").write_text(README, encoding="utf-8")
        (self.root / "docs" / "skills.md").write_text(REFERENCE, encoding="utf-8")
        skill_reference.write(self.root)
        for name in ("alpha", "beta"):
            self.add_prose(name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def add_prose(self, name: str) -> None:
        reference = self.root / "docs" / "skills.md"
        marker = f"<!-- /generated:{name} -->"
        text = reference.read_text(encoding="utf-8")
        reference.write_text(text.replace(marker, f"{marker}\n\nExplained."), encoding="utf-8")

    def snapshot(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }


class NewSkillTestCase(FixtureRepositoryTestCase):
    def test_it_writes_frontmatter_metadata_and_the_generated_reference_section(self) -> None:
        remaining = new_skill.scaffold(self.root, "widget-report", DESCRIPTION, argument_hint="ORG [--months N]")

        skill_md = self.root / "skills" / "widget-report" / "SKILL.md"
        self.assertEqual(
            "---\nname: widget-report\n"
            'description: "Report \\"widgets\\" in a repository with spaces — use it when asked about them."\n'
            'argument-hint: "ORG [--months N]"\n'
            'allowed-tools: ["Bash({{PYTHON}} -B \\"${CLAUDE_SKILL_DIR}/scripts/widget_report.py\\" *)", '
            '"PowerShell({{PYTHON}} -B \\"${CLAUDE_SKILL_DIR}/scripts/widget_report.py\\" *)", "Read"]\n'
            "---\n\n# Widget report\n",
            skill_md.read_text(encoding="utf-8"),
        )
        frontmatter, _ = skill_reference.read_frontmatter(skill_md)
        self.assertEqual(DESCRIPTION, frontmatter["description"])
        # The default grant names the skill's script, ending at its closing quote, so no `..` path matches it.
        own_scripts = '{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/widget_report.py" *'
        self.assertEqual(
            [f"Bash({own_scripts})", f"PowerShell({own_scripts})", "Read"],
            fm.read(skill_md).value("allowed-tools"),
        )
        self.assertEqual(
            # The default grant runs Python through the interpreter token, so the skill declares it.
            {"required_vars": ["PYTHON"], "shared_deps": ["runtime-compatibility.md"], "runtime_support": FULL},
            json.loads((self.root / "deploy-meta" / "widget-report.json").read_text(encoding="utf-8")),
        )
        reference = (self.root / "docs" / "skills.md").read_text(encoding="utf-8")
        self.assertIn("## `widget-report`", reference)
        self.assertIn("```text\n/widget-report ORG [--months N]\n```", reference)
        self.assertEqual(
            [
                "docs/skills.md: section `widget-report` has no hand-written explanation after its generated block",
                "README.md: 'Included skills' has no row for `widget-report`",
                *GRANTS_REMAINING,
            ],
            remaining,
        )

    def test_remaining_names_every_grant_and_tool_validation_still_refuses(self) -> None:
        remaining = new_skill.scaffold(self.root, "widget-report", DESCRIPTION, tools=["gh"])
        skill_md = self.root / "skills" / "widget-report" / "SKILL.md"
        refused = skill_grant_problems(self.root, [skill_md])
        self.assertEqual(5, len(refused), refused)
        # Each grant validation refuses is named by a REMAINING line, by the grant the check names.
        for problem in refused:
            granted = problem.split(" grants ", 1)[1].split(", which ", 1)[0]
            with self.subTest(grant=granted):
                self.assertTrue([line for line in remaining if f" grants {granted}, which " in line], remaining)
        self.assertIn(
            "deploy-meta/widget-report.json declares the tool gh, which nothing the skill runs yet runs; run it from "
            "a script or a step, or remove it from tools",
            remaining,
        )
        # A step that only says "read" leaves the Read grant named; one that names the tool clears it.
        body = skill_md.read_text(encoding="utf-8")
        run = 'then run `{{PYTHON}} -B "${CLAUDE_SKILL_DIR}/scripts/widget_report.py" --all`.\n'
        skill_md.write_text(body + "\nRead the input, " + run, encoding="utf-8")
        line = len(body.splitlines()) + 2
        self.assertEqual(
            [
                f"skills/widget-report/SKILL.md grants Read, which line {line} only implies; name the tool in the "
                "step that uses it, or remove it from allowed-tools"
            ],
            new_skill.grant_remaining(self.root, Path("skills/widget-report/SKILL.md")),
        )
        # Once a step runs the script through both grants and names the tool that reads a file, no grant remains.
        skill_md.write_text(body + "\n`Read` the input, " + run, encoding="utf-8")
        self.assertEqual([], new_skill.grant_remaining(self.root, Path("skills/widget-report/SKILL.md")))
        self.assertEqual([], skill_grant_problems(self.root, [skill_md]))

    def test_finishing_the_remaining_items_leaves_the_reference_current(self) -> None:
        new_skill.scaffold(self.root, "widget-report", DESCRIPTION)
        self.add_prose("widget-report")
        readme = self.root / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8").replace(
                "| `suite` | A bundle. |", "| `suite` | A bundle. |\n| `widget-report` | Widgets. |"
            ),
            encoding="utf-8",
        )

        self.assertEqual([], skill_reference.problems(self.root))

    def test_options_reach_the_frontmatter_and_metadata(self) -> None:
        new_skill.scaffold(
            self.root,
            "tidy",
            "Tidy things.",
            user_only=True,
            opt_in=True,
            tools=["gh", "gh"],
            allowed_tools=["Bash(gh auth status)", "PowerShell(gh auth status)"],
        )

        self.assertIn(
            'allowed-tools: ["Bash(gh auth status)", "PowerShell(gh auth status)"]\n'
            "disable-model-invocation: true\n---",
            (self.root / "skills" / "tidy" / "SKILL.md").read_text(encoding="utf-8"),
        )
        # The canonical layout that validation holds every deploy-meta file to.
        self.assertEqual(
            '{\n    "required_vars": [],\n    "shared_deps": ["runtime-compatibility.md"],\n    "tools": ["gh"],\n'
            '    "opt_in": true,\n    "runtime_support": {"claude-code": "full", "codex": "full", "copilot-cli": '
            '{"level": "partial", "needs": ["user-only-start"], "reason": "A headless copilot -p session cannot start '
            'it; start it from an interactive session."}}\n}\n',
            (self.root / "deploy-meta" / "tidy.json").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "Started by you. Opt-in: deploy it with `--include tidy`. Needs `gh`. Takes no arguments.",
            (self.root / "docs" / "skills.md").read_text(encoding="utf-8"),
        )

    def test_a_skill_granted_agent_and_workflow_is_scaffolded_partial_where_a_runtime_lacks_them(self) -> None:
        new_skill.scaffold(self.root, "fan-out", DESCRIPTION, allowed_tools=["Read", "Agent", "Workflow"])

        self.assertEqual(
            {
                "claude-code": "full",
                "codex": {
                    "level": "partial",
                    "needs": ["workflow"],
                    "reason": "It uses Claude Code's Workflow tool, which this runtime lacks.",
                },
                "copilot-cli": {
                    "level": "partial",
                    "needs": ["workflow"],
                    "reason": "It uses Claude Code's Workflow tool, which this runtime lacks.",
                },
            },
            json.loads((self.root / "deploy-meta" / "fan-out.json").read_text(encoding="utf-8"))["runtime_support"],
        )

    def test_refusals_change_nothing(self) -> None:
        before = self.snapshot()
        # Each case's keyword arguments for scaffold, with or without a description.
        cases: tuple[tuple[str, dict[str, Any], str], ...] = (
            ("Bad_Name", {}, "not a valid skill name"),
            ("con", {}, "not a valid skill name"),
            ("claude-helper", {}, "reserved word 'claude', which the Agent Skills frontmatter rules forbid"),
            ("anthropic", {}, "reserved word 'anthropic'"),
            (
                "fresh",
                {"description": "Emit <tag> markup. Use it when testing."},
                "XML tag '<tag>', which the Agent Skills frontmatter rules forbid",
            ),
            ("alpha", {}, "already a skill"),
            ("suite", {}, "already a skill, a bundle"),
            ("fresh", {"tools": ["jq"]}, "unknown tool jq"),
            ("fresh", {"skill_deps": ["absent"]}, "--skill-dep absent names no skill in the source"),
            # A skill given a sibling also depends on ejryan89-skill-core, which this fixture source lacks.
            ("fresh", {"skill_deps": ["alpha"]}, "--skill-dep ejryan89-skill-core names no skill in the source"),
            ("fresh", {"description": "Two\nlines."}, "one non-empty line"),
            ("fresh", {"description": "  "}, "one non-empty line"),
            ("fresh", {"argument_hint": "A\nB"}, "one non-empty line"),
            ("fresh", {"allowed_tools": [""]}, "at least one tool"),
            ("fresh", {"description": "Fresh."}, "say what it does, then when to use it"),
            ("fresh", {"allowed_tools": ["Bash", "Read"]}, "grants Bash for every command"),
            ("fresh", {"allowed_tools": ["PowerShell(*)"]}, r"grants PowerShell\(\*\) for every command"),
            ("fresh", {"allowed_tools": ["Bash(git status)"]}, r"Bash\(git status\) has no PowerShell twin"),
            (
                "fresh",
                {
                    "allowed_tools": [
                        'Bash(python -B "${CLAUDE_SKILL_DIR}/scripts/*)',
                        'PowerShell(python -B "${CLAUDE_SKILL_DIR}/scripts/*)',
                    ]
                },
                r"scripts/\*\) leaves a path open before its \*",
            ),
            (
                "fresh",
                {
                    "allowed_tools": [
                        'Bash(python -B "${CLAUDE_SKILL_DIR}/../core/scripts/*)',
                        'PowerShell(python -B "${CLAUDE_SKILL_DIR}/../core/scripts/*)',
                    ]
                },
                r"leaves a path open before its \*",
            ),
            (
                "fresh",
                {"allowed_tools": ["Bash(python -B *)", "PowerShell(python -B *)"]},
                r"Bash\(python -B \*\) names no script or git or gh subcommand before its \*",
            ),
        )
        for name, arguments, message in cases:
            with self.subTest(name=name, arguments=arguments):
                description = arguments.pop("description", "Fresh. Use it when testing.")
                with self.assertRaisesRegex(new_skill.ScaffoldError, message):
                    new_skill.scaffold(self.root, name, description, **arguments)
                self.assertEqual(before, self.snapshot())
        (self.root / "skills" / "orphan").mkdir()
        with self.assertRaisesRegex(new_skill.ScaffoldError, "existing path"):
            new_skill.scaffold(self.root, "orphan", "Orphan. Use it when testing.")
        (self.root / "docs" / "skills.md").unlink()
        with self.assertRaisesRegex(new_skill.ScaffoldError, "does not exist"):
            new_skill.scaffold(self.root, "fresh", "Fresh. Use it when testing.")
        self.assertFalse((self.root / "skills" / "fresh").exists())

    def test_a_reference_without_a_generated_block_is_refused_before_anything_is_written(self) -> None:
        reference = self.root / "docs" / "skills.md"
        for block in ("summary", "runtime-support"):
            with self.subTest(block=block):
                reference.write_text(
                    REFERENCE.replace(f"<!-- generated:{block} -->\n<!-- /generated:{block} -->\n", ""),
                    encoding="utf-8",
                )
                before = self.snapshot()
                with self.assertRaisesRegex(skill_reference.SkillReferenceError, f"no {block} block"):
                    new_skill.scaffold(self.root, "fresh", "Fresh. Use it when testing.")
                self.assertEqual(before, self.snapshot())
        reference.write_bytes(b"\xff\xfe not UTF-8\n")
        before = self.snapshot()
        with self.assertRaisesRegex(new_skill.ScaffoldError, "not UTF-8"):
            new_skill.scaffold(self.root, "fresh", "Fresh. Use it when testing.")
        self.assertEqual(before, self.snapshot())

    def test_a_write_that_fails_partway_removes_what_it_wrote(self) -> None:
        before = self.snapshot()
        with (
            mock.patch.object(skill_reference, "write", side_effect=OSError("disk full")),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            new_skill.scaffold(self.root, "fresh", "Fresh. Use it when testing.")
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.root / "skills" / "fresh").exists())

    def test_the_description_limit_and_repository_skill_names_are_refused(self) -> None:
        (self.root / ".claude" / "skills" / "implement-change").mkdir()
        before = self.snapshot()
        # The limit pinned as a literal: the Agent Skills limit, which Copilot CLI enforces when it loads a skill.
        clause = " Use it when testing."
        longest = "x" * (1024 - len(clause)) + clause
        for name, description, message in (
            ("fresh", longest + "x", "--description is 1025 characters; runtime adapters allow at most 1024"),
            ("implement-change", "Fresh. Use it when testing.", "already a repository skill under .claude/skills"),
        ):
            for scaffold in (new_skill.scaffold, new_skill.scaffold_repository):
                if scaffold is new_skill.scaffold_repository and name == "implement-change":
                    continue
                with self.subTest(name=name, scaffold=scaffold.__name__):
                    with self.assertRaisesRegex(new_skill.ScaffoldError, message):
                        scaffold(self.root, name, description)
                    self.assertEqual(before, self.snapshot())
        self.assertEqual(1024, len(longest))
        new_skill.scaffold(self.root, "fresh", longest)
        new_skill.scaffold_repository(self.root, "fresher", longest)

    def test_the_command_reports_what_it_wrote_and_what_remains(self) -> None:
        def run(*arguments: str) -> tuple[int, str, str]:
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = new_skill.main(["--root", str(self.root), *arguments])
            return code, stdout.getvalue(), stderr.getvalue()

        code, stdout, _ = run("widget-report", "--description", DESCRIPTION, "--tool", "gh")
        self.assertEqual(0, code)
        self.assertEqual(
            [
                "CREATED skills/widget-report/SKILL.md",
                "CREATED deploy-meta/widget-report.json",
                "UPDATED docs/skills.md",
                "REMAINING docs/skills.md: section `widget-report` has "
                "no hand-written explanation after its generated block",
                "REMAINING README.md: 'Included skills' has no row for `widget-report`",
                *(f"REMAINING {line}" for line in GRANTS_REMAINING),
                "REMAINING deploy-meta/widget-report.json declares the tool gh, which nothing the skill runs yet "
                "runs; run it from a script or a step, or remove it from tools",
            ],
            stdout.splitlines(),
        )
        code, stdout, stderr = run("widget-report", "--description", DESCRIPTION)
        self.assertEqual((2, ""), (code, stdout))
        self.assertIn("FAILED 'widget-report' is already a skill", stderr)

    def test_a_root_outside_a_checkout_is_refused_and_nothing_is_scaffolded(self) -> None:
        # A source.json alone does not make a checkout: it also holds the repository skills under .claude/skills.
        (self.root / ".claude" / "skills").rmdir()
        before = self.snapshot()
        for arguments in (["--repository"], []):
            with self.subTest(arguments=arguments):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                    new_skill.main(["widget-check", "--description", DESCRIPTION, "--root", str(self.root), *arguments])
                self.assertEqual(2, raised.exception.code)
                self.assertIn(f"argument --root: {self.root} is not a checkout of this repository", stderr.getvalue())
                self.assertEqual(before, self.snapshot())


class SkillDependencyTestCase(FixtureRepositoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        # ejryan89-skill-core, holding the real lookup, and a sibling with a module a dependent script imports.
        (self.root / "skills" / "alpha" / "scripts").mkdir(parents=True)
        copy_with_imports(self.root / "skills" / "ejryan89-skill-core" / "scripts", "skill_roots")
        (self.root / "skills" / "alpha" / "scripts" / "alpha_values.py").write_text(
            'VALUE = "alpha found"\n', encoding="utf-8"
        )
        (self.root / "skills" / "ejryan89-skill-core" / "SKILL.md").write_text(
            "---\nname: ejryan89-skill-core\ndescription: Shared modules.\ndisable-model-invocation: true\n"
            "user-invocable: false\n---\n\nBody.\n",
            encoding="utf-8",
        )
        (self.root / "deploy-meta" / "ejryan89-skill-core.json").write_text(
            json.dumps({"required_vars": [], "shared_deps": [], "selectable": False}), encoding="utf-8"
        )

    def run_main(self, *arguments: str) -> tuple[int, list[str]]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = new_skill.main(["--root", str(self.root), *arguments])
        return code, stdout.getvalue().splitlines()

    def test_a_sibling_reaches_skill_deps_and_a_script_header_that_runs_and_passes_the_policies(self) -> None:
        code, lines = self.run_main("widget-report", "--description", DESCRIPTION, "--skill-dep", "alpha")
        self.assertEqual(0, code)
        header = [line.removeprefix("SCRIPT_HEADER ") for line in lines if line.startswith("SCRIPT_HEADER ")]
        self.assertEqual(
            [
                'sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))',
                "from skill_roots import sibling_scripts",
                'sys.path.insert(0, str(sibling_scripts("alpha")))',
            ],
            header,
        )
        self.assertEqual(
            ["alpha", "ejryan89-skill-core"],
            json.loads((self.root / "deploy-meta" / "widget-report.json").read_text(encoding="utf-8"))["skill_deps"],
        )
        script = self.root / "skills" / "widget-report" / "scripts" / "widget_report.py"
        script.parent.mkdir()
        start = ["import sys", "from pathlib import Path", *header]
        script.write_text("\n".join([*start, "from alpha_values import VALUE", "print(VALUE)", ""]), encoding="utf-8")
        result = subprocess.run([sys.executable, "-B", str(script)], capture_output=True, text=True, check=False)
        self.assertEqual((0, "alpha found\n"), (result.returncode, result.stdout), result.stderr)
        self.assertEqual([], sibling_path_problems(self.root))
        self.assertEqual([], script_dependency_problems(self.root))

    def test_skill_core_alone_starts_a_script_with_its_line_only(self) -> None:
        code, lines = self.run_main("widget-report", "--description", DESCRIPTION, "--skill-dep", "ejryan89-skill-core")
        self.assertEqual(0, code)
        self.assertEqual(
            [
                'SCRIPT_HEADER sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / '
                '"scripts"))'
            ],
            [line for line in lines if line.startswith("SCRIPT_HEADER ")],
        )
        self.assertEqual(
            ["ejryan89-skill-core"],
            json.loads((self.root / "deploy-meta" / "widget-report.json").read_text(encoding="utf-8"))["skill_deps"],
        )

    def test_a_skill_with_no_dependencies_prints_no_header(self) -> None:
        code, lines = self.run_main("widget-report", "--description", DESCRIPTION)
        self.assertEqual(0, code)
        self.assertEqual([], [line for line in lines if line.startswith("SCRIPT_HEADER")])
        self.assertNotIn(
            "skill_deps",
            json.loads((self.root / "deploy-meta" / "widget-report.json").read_text(encoding="utf-8")),
        )


class NewRepositorySkillTestCase(FixtureRepositoryTestCase):
    def run_main(self, *arguments: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = new_skill.main(["--root", str(self.root), "--repository", *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_a_shim_write_that_fails_partway_removes_the_skill_and_the_shim_it_wrote(self) -> None:
        def write_then_fail(root: Path, names: list[str]) -> list[Path]:
            shim = root / ".agents" / "skills" / "widget-check" / "SKILL.md"
            shim.parent.mkdir(parents=True)
            shim.write_text("half a shim\n", encoding="utf-8")
            raise OSError("disk full")

        before = self.snapshot()
        with (
            mock.patch.object(skill_shims, "write", side_effect=write_then_fail),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            new_skill.scaffold_repository(self.root, "widget-check", DESCRIPTION)
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.root / ".claude" / "skills" / "widget-check").exists())
        self.assertFalse((self.root / ".agents" / "skills" / "widget-check").exists())

    def test_it_writes_the_skill_and_its_shim_and_nothing_a_shipped_skill_needs(self) -> None:
        before = self.snapshot()
        written = new_skill.scaffold_repository(self.root, "widget-check", DESCRIPTION, argument_hint="TARGET")

        self.assertEqual(
            [Path(".claude/skills/widget-check/SKILL.md"), Path(".agents/skills/widget-check/SKILL.md")], written
        )
        frontmatter = (
            "---\nname: widget-check\n"
            'description: "Report \\"widgets\\" in a repository with spaces — use it when asked about them."\n'
            'argument-hint: "TARGET"\n---\n'
        )
        self.assertEqual(
            frontmatter + "\n# Widget check\n",
            (self.root / ".claude/skills/widget-check/SKILL.md").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            frontmatter + "\nRead and follow `../../../.claude/skills/widget-check/SKILL.md`, a path relative to "
            "this file, as the authoritative workflow.\nRun its commands from the repository root, the folder that "
            "holds `.agents` and `.claude`, and resolve every relative path it names from there.\nA command that "
            "writes a shim under `.agents` needs an approval to run outside Codex's sandbox, which keeps `.agents` "
            "read-only.\n",
            (self.root / ".agents/skills/widget-check/SKILL.md").read_text(encoding="utf-8"),
        )
        self.assertEqual([], skill_shims.problems(self.root))
        after = self.snapshot()
        self.assertEqual(before, {path: data for path, data in after.items() if path in before})
        self.assertEqual(2, len(after) - len(before))

    def test_given_grants_and_user_only_reach_the_skill_and_its_shim(self) -> None:
        grants = ["Bash(python -B tools/widgets.py *)", "PowerShell(python -B tools/widgets.py *)"]
        new_skill.scaffold_repository(self.root, "widget-check", "Check widgets.", user_only=True, allowed_tools=grants)

        expected = (
            'allowed-tools: ["Bash(python -B tools/widgets.py *)", "PowerShell(python -B tools/widgets.py *)"]\n'
            "disable-model-invocation: true\n---\n"
        )
        for path in (".claude/skills/widget-check/SKILL.md", ".agents/skills/widget-check/SKILL.md"):
            with self.subTest(path=path):
                self.assertIn(expected, (self.root / path).read_text(encoding="utf-8"))
        # Codex reads a user-only skill's policy from beside the shim's SKILL.md, not from its frontmatter.
        self.assertEqual(
            b"policy:\n  allow_implicit_invocation: false\n",
            (self.root / ".agents/skills/widget-check/agents/openai.yaml").read_bytes(),
        )

    def test_refusals_change_nothing(self) -> None:
        (self.root / ".claude/skills/taken").mkdir(parents=True)
        (self.root / ".agents/skills/shimmed").mkdir(parents=True)
        before = self.snapshot()
        cases: tuple[tuple[str, dict[str, Any], str], ...] = (
            ("Bad_Name", {}, "not a valid skill name"),
            ("claude-helper", {}, "reserved word 'claude'"),
            ("alpha", {}, "already a shipped or repository skill"),
            ("taken", {}, "already a shipped or repository skill"),
            ("shimmed", {}, ".agents/skills/shimmed already exists"),
            ("fresh", {"description": "Fresh."}, "say what it does, then when to use it"),
            ("fresh", {"allowed_tools": ["Bash"]}, "grants Bash for every command"),
            ("fresh", {"allowed_tools": ["Bash(git status)"]}, r"Bash\(git status\) has no PowerShell twin"),
            (
                "fresh",
                {"allowed_tools": ["Bash(python -B tools/widgets.py*)", "PowerShell(python -B tools/widgets.py*)"]},
                r"leaves a path open before its \*",
            ),
        )
        for name, arguments, message in cases:
            with self.subTest(name=name, arguments=arguments):
                description = arguments.pop("description", "Fresh. Use it when testing.")
                with self.assertRaisesRegex(new_skill.ScaffoldError, message):
                    new_skill.scaffold_repository(self.root, name, description, **arguments)
                self.assertEqual(before, self.snapshot())
        for option in (["--tool", "gh"], ["--opt-in"], ["--skill-dep", "alpha"]):
            with self.subTest(option=option):
                code, stdout, stderr = self.run_main("fresh", "--description", "Fresh. Use it when testing.", *option)
                self.assertEqual((2, ""), (code, stdout))
                self.assertIn("FAILED a repository skill has no deploy metadata", stderr)
                self.assertEqual(before, self.snapshot())

    def test_the_command_reports_what_it_wrote(self) -> None:
        self.assertEqual(
            (0, "CREATED .claude/skills/widget-check/SKILL.md\nCREATED .agents/skills/widget-check/SKILL.md\n", ""),
            self.run_main("widget-check", "--description", DESCRIPTION),
        )

    def test_the_command_names_each_grant_no_step_uses_yet(self) -> None:
        code, stdout, _ = self.run_main(
            "widget-check",
            "--description",
            DESCRIPTION,
            "--allowed-tools",
            "Bash(python -B tools/widgets.py *),PowerShell(python -B tools/widgets.py *)",
        )
        self.assertEqual(0, code)
        self.assertEqual(
            [
                "CREATED .claude/skills/widget-check/SKILL.md",
                "CREATED .agents/skills/widget-check/SKILL.md",
                *(
                    f"REMAINING .claude/skills/widget-check/SKILL.md grants {tool}, which no step uses yet; use it "
                    "in a step or remove it from allowed-tools"
                    for tool in ("Bash", "PowerShell")
                ),
                *(
                    f"REMAINING .claude/skills/widget-check/SKILL.md grants {tool}(python -B tools/widgets.py *), "
                    "which matches no command in its fences or code spans yet; show the command it covers or remove "
                    "the grant"
                    for tool in ("Bash", "PowerShell")
                ),
            ],
            stdout.splitlines(),
        )
