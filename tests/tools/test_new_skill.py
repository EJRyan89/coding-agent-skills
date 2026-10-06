"""Regression suite for tools/new_skill.py: scaffolding a skill's files and its skill reference section."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from deployer import frontmatter as fm
from tools import new_skill, skill_reference

README = (
    "# Fixture\n\n## Included skills\n\n| Skill | What it does |\n|---|---|\n"
    "| [`alpha`](docs/skills.md#alpha) | Alpha. |\n| `suite` | A bundle. |\n\nSee [Skills](docs/skills.md).\n"
)
REFERENCE = "# Skills\n\n<!-- generated:summary -->\n<!-- /generated:summary -->\n"
DESCRIPTION = 'Report "widgets" in a repository with spaces — use it when asked about them.'


class NewSkillTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="new-skill-test.")
        self.root = Path(self._temporary.name).resolve() / "Repo With Spaces"
        for directory in ("docs", "skills/alpha", "skills/beta", "deploy-meta"):
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

    def test_it_writes_frontmatter_metadata_and_the_generated_reference_section(self) -> None:
        remaining = new_skill.scaffold(self.root, "widget-report", DESCRIPTION, argument_hint="ORG [--months N]")

        skill_md = self.root / "skills" / "widget-report" / "SKILL.md"
        self.assertEqual(
            "---\nname: widget-report\n"
            'description: "Report \\"widgets\\" in a repository with spaces — use it when asked about them."\n'
            'argument-hint: "ORG [--months N]"\n'
            'allowed-tools: ["Bash(python -B \\"${CLAUDE_SKILL_DIR}/scripts/*)", '
            '"PowerShell(python -B \\"${CLAUDE_SKILL_DIR}/scripts/*)", "Read"]\n---\n\n# Widget report\n',
            skill_md.read_text(encoding="utf-8"),
        )
        frontmatter = skill_reference.read_frontmatter(skill_md)
        self.assertEqual(DESCRIPTION, frontmatter["description"])
        own_scripts = 'python -B "${CLAUDE_SKILL_DIR}/scripts/*'
        self.assertEqual(
            [f"Bash({own_scripts})", f"PowerShell({own_scripts})", "Read"],
            fm.read(skill_md).value("allowed-tools"),
        )
        self.assertEqual(
            {"required_vars": [], "shared_deps": ["runtime-compatibility.md"]},
            json.loads((self.root / "deploy-meta" / "widget-report.json").read_text(encoding="utf-8")),
        )
        reference = (self.root / "docs" / "skills.md").read_text(encoding="utf-8")
        self.assertIn("## `widget-report`", reference)
        self.assertIn("```text\n/widget-report ORG [--months N]\n```", reference)
        self.assertEqual(
            [
                "docs/skills.md: section `widget-report` has no hand-written explanation after its generated block",
                "README.md: 'Included skills' has no row for `widget-report`",
            ],
            remaining,
        )

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
            '    "opt_in": true\n}\n',
            (self.root / "deploy-meta" / "tidy.json").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "Started by you. Opt-in: deploy it with `--include tidy`. Needs `gh`. Takes no arguments.",
            (self.root / "docs" / "skills.md").read_text(encoding="utf-8"),
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
            ("fresh", {"description": "Two\nlines."}, "one non-empty line"),
            ("fresh", {"description": "  "}, "one non-empty line"),
            ("fresh", {"argument_hint": "A\nB"}, "one non-empty line"),
            ("fresh", {"allowed_tools": [""]}, "at least one tool"),
            ("fresh", {"description": "Fresh."}, "say what it does, then when to use it"),
            ("fresh", {"allowed_tools": ["Bash", "Read"]}, "grants Bash for every command"),
            ("fresh", {"allowed_tools": ["PowerShell(*)"]}, r"grants PowerShell\(\*\) for every command"),
            ("fresh", {"allowed_tools": ["Bash(git status)"]}, r"Bash\(git status\) has no PowerShell twin"),
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
            ],
            stdout.splitlines(),
        )
        code, stdout, stderr = run("widget-report", "--description", DESCRIPTION)
        self.assertEqual((2, ""), (code, stdout))
        self.assertIn("FAILED 'widget-report' is already a skill", stderr)


if __name__ == "__main__":
    unittest.main()
