"""Regression suite for tools/skill_reference.py: generating docs/skills.md and reporting where it drifts."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools import skill_reference

INITIAL_REFERENCE = "# Skills\n\nIntro.\n\n<!-- generated:summary -->\n<!-- /generated:summary -->\n"
README = (
    "# Fixture\n\n## Included skills\n\n| Skill | What it does |\n|---|---|\n"
    "| [`alpha`](docs/skills.md#alpha) | Sweeps. |\n| `suite` | A bundle: [`beta`](docs/skills.md#beta). |\n\n"
    "See [Skills](docs/skills.md).\n\n## Other\n"
)


class SkillReferenceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="skill-reference-test.")
        self.root = Path(self._temporary.name).resolve() / "Repo With Spaces"
        (self.root / "docs").mkdir(parents=True)
        self.write_json("source.json", {"id": "test/skills", "bundles": {"suite": {"members": ["beta"]}}})
        self.add_skill(
            "alpha",
            'description: "Sweeps every repository under {{REPOS_ROOT}}, \\"carefully\\". Use it when asked to sweep."\nargument-hint: "[--flag X]"',
            {"required_vars": ["REPOS_ROOT"], "opt_in": True, "tools": ["copilot"]},
        )
        self.add_skill(
            "beta",
            "description: 'Beta''s plain description.'\ndisable-model-invocation: true",
            {"skill_deps": ["gamma"], "tools": ["gh"], "optional_tools": ["dotnet-format"]},
        )
        self.add_skill("gamma", "description: Internal support.", {"selectable": False, "tools": ["copilot"]})
        (self.root / "README.md").write_text(README, encoding="utf-8")
        self.reference = self.root / "docs" / "skills.md"
        self.reference.write_text(INITIAL_REFERENCE, encoding="utf-8")

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def write_json(self, relative: str, document: object) -> None:
        (self.root / relative).write_text(json.dumps(document), encoding="utf-8")

    def add_skill(self, name: str, frontmatter: str, metadata: dict[str, object], body: str = "Body.\n") -> None:
        directory = self.root / "skills" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(f"---\nname: {name}\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
        (self.root / "deploy-meta").mkdir(exist_ok=True)
        self.write_json(f"deploy-meta/{name}.json", {"required_vars": [], "shared_deps": [], **metadata})

    def text(self) -> str:
        return self.reference.read_text(encoding="utf-8")

    def add_prose(self, name: str, prose: str) -> None:
        marker = f"<!-- /generated:{name} -->"
        self.reference.write_text(self.text().replace(marker, f"{marker}\n\n{prose}"), encoding="utf-8")

    def written_and_explained(self) -> None:
        skill_reference.write(self.root)
        self.add_prose("alpha", "Alpha's flag explained.")
        self.add_prose("beta", "Beta explained.")

    def test_write_adds_a_generated_section_for_each_selectable_skill(self) -> None:
        skill_reference.write(self.root)
        text = self.text()

        self.assertLess(text.index("## `alpha`"), text.index("## `beta`"))
        self.assertNotIn("gamma", text)
        self.assertIn(
            "| [`alpha`](#alpha) | You or the agent | Opt-in | `copilot` (optional), the `REPOS_ROOT` setting |", text
        )
        self.assertIn("| [`beta`](#beta) | You | `suite` bundle | `dotnet-format` (optional), `gh` |", text)
        self.assertIn('Sweeps every repository under `REPOS_ROOT`, "carefully".', text)
        self.assertIn("```text\n/alpha [--flag X]\n```", text)
        self.assertIn(
            "Opt-in: deploy it with `--include alpha`. Needs `copilot` (optional) and the `REPOS_ROOT` setting.", text
        )
        self.assertIn("Beta's plain description.", text)
        self.assertIn("```text\n/beta\n```", text)
        self.assertIn(
            "Started by you. Installed with the `suite` bundle. Needs `dotnet-format` (optional) and `gh`. "
            "Takes no arguments.",
            text,
        )
        # A skill lists only the tools it declares: beta does not run gamma's copilot through its dependency.
        self.assertNotIn("copilot", text[text.index("## `beta`"):])

    def test_a_section_without_hand_written_prose_is_reported(self) -> None:
        skill_reference.write(self.root)
        self.add_prose("alpha", "Alpha's flag explained.")

        self.assertEqual(
            ["docs/skills.md: section `beta` has no hand-written explanation after its generated block"],
            skill_reference.problems(self.root),
        )

    def test_a_current_reference_has_no_problems_and_write_leaves_it_unchanged(self) -> None:
        self.written_and_explained()
        before = self.reference.read_bytes()

        self.assertEqual([], skill_reference.problems(self.root))
        skill_reference.write(self.root)
        self.assertEqual(before, self.reference.read_bytes())

    def test_a_frontmatter_change_makes_the_reference_stale_until_written(self) -> None:
        self.written_and_explained()
        skill_md = self.root / "skills" / "alpha" / "SKILL.md"
        skill_md.write_text(skill_md.read_text(encoding="utf-8").replace("[--flag X]", "[--other Y]"), encoding="utf-8")

        self.assertEqual(
            ["docs/skills.md: generated content is stale; run python tools/skill_reference.py --write"],
            skill_reference.problems(self.root),
        )
        skill_reference.write(self.root)
        self.assertEqual([], skill_reference.problems(self.root))
        self.assertIn("/alpha [--other Y]", self.text())
        self.assertIn("Alpha's flag explained.", self.text())

    def test_a_new_skill_is_reported_and_write_adds_its_section_in_order(self) -> None:
        self.written_and_explained()
        self.add_skill("aardvark", "description: First. Use it when testing.", {})
        (self.root / "README.md").write_text(README.replace("| [`alpha`]", "| `aardvark` | First. |\n| [`alpha`]"), encoding="utf-8")

        found = skill_reference.problems(self.root)
        self.assertIn("docs/skills.md: no section for `aardvark`", found)
        skill_reference.write(self.root)
        text = self.text()
        self.assertLess(text.index("## `aardvark`"), text.index("## `alpha`"))
        self.assertEqual(
            ["docs/skills.md: section `aardvark` has no hand-written explanation after its generated block"],
            skill_reference.problems(self.root),
        )

    def test_a_section_for_a_removed_skill_is_reported_and_kept(self) -> None:
        self.written_and_explained()
        self.reference.write_text(self.text() + "\n## `retired`\n\nGone.\n", encoding="utf-8")

        self.assertIn(
            "docs/skills.md: section `retired` names no selectable skill; remove it",
            skill_reference.problems(self.root),
        )
        skill_reference.write(self.root)
        self.assertIn("## `retired`\n\nGone.", self.text())

    def test_reading_arguments_requires_an_argument_hint(self) -> None:
        self.add_skill("beta", "description: Beta. Use it when testing.", {"skill_deps": ["gamma"], "tools": ["gh"]}, body="Use $ARGUMENTS.\n")
        self.written_and_explained()

        self.assertEqual(
            ["skills/beta/SKILL.md reads $ARGUMENTS but declares no argument-hint"],
            skill_reference.problems(self.root),
        )

    def test_a_model_invocable_description_must_say_when_to_use_the_skill(self) -> None:
        # The description is all the model has when deciding to start a skill on its own; a user-only skill's
        # description never triggers anything, so beta needs no such clause.
        self.add_skill("alpha", "description: Sweeps every repository.", {"required_vars": ["REPOS_ROOT"]})
        self.written_and_explained()

        self.assertEqual(
            ["skills/alpha/SKILL.md: the model may start it, so its description must say what it does, then when to "
             "use it (\"Use it when ...\"); see \"Description\" in docs/adding-a-skill.md"],
            skill_reference.problems(self.root),
        )
        for description in ("Sweeps. Use it when asked.", "Sweeps; use this skill before a release.",
                            "Sweeps. Use whenever a branch is merged.", "Sweeps. Use after deploying."):
            with self.subTest(description=description):
                self.assertTrue(skill_reference.says_when(description))
        for description in ("Sweeps every repository.", "Sweeps when asked.", "Uses whatever is configured."):
            with self.subTest(description=description):
                self.assertFalse(skill_reference.says_when(description))

    def test_the_readme_must_list_every_menu_item_and_link_the_reference(self) -> None:
        self.written_and_explained()
        (self.root / "README.md").write_text(
            "## Included skills\n\n| Skill | What it does |\n|---|---|\n| `alpha` | Sweeps `suite` too. |\n\n"
            "## Other\n| `suite` | Elsewhere. |\n",
            encoding="utf-8",
        )

        self.assertEqual(
            [
                "README.md: 'Included skills' has no row for `suite`",
                "README.md: 'Included skills' does not link docs/skills.md",
            ],
            skill_reference.problems(self.root),
        )

    def test_a_readme_row_for_a_removed_skill_is_reported(self) -> None:
        self.written_and_explained()
        readme = self.root / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8").replace("| `suite`", "| `retired` | Gone. |\n| `suite`"), encoding="utf-8")

        self.assertEqual(
            ["README.md: 'Included skills' row `retired` names no selectable skill or bundle; remove it"],
            skill_reference.problems(self.root),
        )

    def test_a_readme_link_to_a_missing_reference_section_is_reported(self) -> None:
        self.written_and_explained()
        readme = self.root / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8").replace("skills.md#beta", "skills.md#bta"), encoding="utf-8")

        self.assertEqual(
            ["README.md: 'Included skills' links docs/skills.md#bta, which has no section"],
            skill_reference.problems(self.root),
        )

    def test_a_reference_without_a_summary_block_is_reported(self) -> None:
        self.reference.write_text("# Skills\n", encoding="utf-8")

        found = skill_reference.problems(self.root)
        self.assertEqual(1, len(found))
        self.assertIn("no summary block", found[0])
        with self.assertRaisesRegex(skill_reference.ReferenceError, "no summary block"):
            skill_reference.write(self.root)

    def test_a_folded_description_is_read_as_one_line(self) -> None:
        self.add_skill(
            "alpha",
            "description: >-\n  Folded across\n  two lines.\nargument-hint: '[--flag X]'",
            {"required_vars": ["REPOS_ROOT"], "opt_in": True, "tools": ["copilot"]},
        )
        skill_reference.write(self.root)
        self.assertIn("<!-- generated:alpha -->\nFolded across two lines.\n", self.text())

    def test_frontmatter_values_this_tool_cannot_read_are_refused(self) -> None:
        for frontmatter, message in (
            ("description:\n  nested: value", "description: a nested mapping is not supported"),
            ("description: [a, b]", "description must be a single value"),
            ('description: "unterminated', "description: a double-quoted value is not closed"),
        ):
            with self.subTest(frontmatter=frontmatter):
                self.add_skill("alpha", frontmatter, {"required_vars": ["REPOS_ROOT"], "opt_in": True})
                with self.assertRaisesRegex(skill_reference.ReferenceError, message):
                    skill_reference.problems(self.root)
        skill_md = self.root / "skills" / "alpha" / "SKILL.md"
        skill_md.write_text("---\nname: alpha\ndescription: Open.\n", encoding="utf-8")
        with self.assertRaisesRegex(skill_reference.ReferenceError, "not closed"):
            skill_reference.read_frontmatter(skill_md)

    def test_the_command_exits_by_outcome(self) -> None:
        def run(*arguments: str) -> tuple[int, str, str]:
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = skill_reference.main(["--root", str(self.root), *arguments])
            return code, stdout.getvalue(), stderr.getvalue()

        code, stdout, _ = run()
        self.assertEqual(1, code)
        self.assertIn("no section for `alpha`", stdout)
        code, stdout, _ = run("--write")
        self.assertEqual(1, code)
        self.assertIn("section `alpha` has no hand-written explanation", stdout)
        self.add_prose("alpha", "Alpha's flag explained.")
        self.add_prose("beta", "Beta explained.")
        self.assertEqual((0, "", ""), run())
        self.reference.write_text("# Skills\n", encoding="utf-8")
        code, _, stderr = run("--write")
        self.assertEqual(2, code)
        self.assertIn("FAILED", stderr)


if __name__ == "__main__":
    unittest.main()
