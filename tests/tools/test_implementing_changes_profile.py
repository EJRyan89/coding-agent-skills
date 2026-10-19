"""Regression suite for docs/implementing-changes.md: the profile must carry every section implement-change names."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import frontmatter

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROFILE = REPOSITORY_ROOT / "docs" / "implementing-changes.md"
SKILL = REPOSITORY_ROOT / ".claude" / "skills" / "implement-change" / "SKILL.md"
SCRIPT = ".claude/skills/implement-change/scripts/pull_request.py"
REQUIRED_HEADINGS = (
    "Size gate",
    "Worktree",
    "Validation",
    "Contract files",
    "Documentation",
    "Model guidance",
    "Pull requests",
)
ISSUE_TEMPLATES = REPOSITORY_ROOT / ".github" / "ISSUE_TEMPLATE"


def headings(text: str) -> dict[str, str]:
    """Map each second-level heading to its body."""
    parts = re.split(r"^## (.+)$", text, flags=re.MULTILINE)
    return {parts[index].strip(): parts[index + 1].strip() for index in range(1, len(parts), 2)}


class ImplementingChangesProfileTest(unittest.TestCase):
    def test_profile_has_every_required_heading_with_content(self) -> None:
        found = headings(PROFILE.read_text(encoding="utf-8"))
        for name in REQUIRED_HEADINGS:
            with self.subTest(heading=name):
                self.assertIn(name, found, f"docs/implementing-changes.md lacks a '## {name}' section")
                self.assertTrue(found[name], f"'## {name}' is empty")

    def test_headings_detector_reports_a_missing_section(self) -> None:
        found = headings("# Title\n\n## Size gate\n\nText.\n\n## Worktree\n\n")
        self.assertEqual({"Size gate": "Text.", "Worktree": ""}, found)
        self.assertNotIn("Validation", found)

    def test_skill_names_each_required_heading(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        for name in REQUIRED_HEADINGS:
            with self.subTest(heading=name):
                self.assertIn(f"*{name}*", text)

    def test_skill_finds_the_bug_class_before_the_fix(self) -> None:
        steps = re.findall(r"^## \d+\. (.+)$", SKILL.read_text(encoding="utf-8"), flags=re.MULTILINE)
        self.assertIn("Bug class", steps)
        self.assertLess(steps.index("Bug class"), steps.index("Implement"))

    def test_step_8_drafts_brings_current_checks_pushes_then_opens(self) -> None:
        section = SKILL.read_text(encoding="utf-8").split("## 8. Pull request", 1)[1]
        steps = [
            "1. Draft the body",
            "pull_request.py draft --closes",
            "Write the title",
            "2. Bring the branch up to date",
            "3. Check both files",
            "*Validation*'s title and body checks",
            "pull_request.py check --title-file",
            "4. Push the branch",
            "5. Open the pull request",
            "pull_request.py open --title-file",
        ]
        found = [section.find(step) for step in steps]
        self.assertNotIn(-1, found, dict(zip(steps, found, strict=True)))
        self.assertEqual(sorted(found), found)
        self.assertIn("upgrade note", section)

    def test_skill_hands_over_the_title_and_the_upgrade_note_with_the_plan(self) -> None:
        plan = SKILL.read_text(encoding="utf-8").split("## 5. Plan", 1)[1].split("## 6.", 1)[0]
        self.assertIn("- the pull request's title, and whether the change owes an upgrade note", plan)

    def test_profile_states_both_starts_and_the_base_rules(self) -> None:
        found = headings(PROFILE.read_text(encoding="utf-8"))
        self.assertIn("**In the hub.**", found["Worktree"])
        self.assertIn("**In a worktree a runtime already made**", found["Worktree"])
        self.assertIn("create no second tree", found["Worktree"])
        for rule in ("`git rebase origin/main`", "never rebase or force-push it", "If a push is rejected"):
            with self.subTest(rule=rule):
                self.assertIn(rule, found["Pull requests"])
        self.assertIn("`## Upgrade note`", found["Pull requests"])

    def test_bug_and_enhancement_templates_ask_for_known_siblings(self) -> None:
        for name in ("bug.md", "enhancement.md"):
            with self.subTest(template=name):
                self.assertIn("known siblings", (ISSUE_TEMPLATES / name).read_text(encoding="utf-8"))

    def test_step_8_pastes_no_title_into_a_command(self) -> None:
        section = SKILL.read_text(encoding="utf-8").split("## 8. Pull request", 1)[1]
        commands = re.findall(r"^ *```bash\n *(.+)\n *```$", section, flags=re.MULTILINE)
        self.assertEqual(3, len(commands), commands)
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(command.startswith(f"python -B {SCRIPT} "))
                self.assertNotIn("--title ", command)
        self.assertNotIn("gh pr create", section)

    def test_skill_body_names_no_repository_command(self) -> None:
        lines = SKILL.read_text(encoding="utf-8").splitlines()
        found = frontmatter.split(lines)
        if found is None:
            self.fail("implement-change has no frontmatter")
        body = "\n".join(lines[found[1] :])
        for literal in ("tools/worktrees.py", "run_validation", "deploy.py", "change-skill", "releasing.md"):
            with self.subTest(literal=literal):
                self.assertNotIn(literal, body)
