"""Regression suite for docs/implementing-changes.md: the profile must carry every section implement-change names."""

from __future__ import annotations

import base64
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

import frontmatter
from git_test_base import GitTestCase

from deployer import platform_support

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROFILE = REPOSITORY_ROOT / "docs" / "implementing-changes.md"
SKILL = REPOSITORY_ROOT / ".claude" / "skills" / "implement-change" / "SKILL.md"
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
# A title the "Pull requests" rules allow, holding each character either shell could misread.
AWKWARD_TITLE = (
    'fix(skills): grant each skill\'s scripts by name, keep `$HOME` and "$(x)", and \u2018quote\u2019 \u201atwice\u201b'
)
# PowerShell ends a single-quoted string at any of these, and reads two of them as one.
POWERSHELL_QUOTES = re.compile("['\u2018\u2019\u201a\u201b]")


def headings(text: str) -> dict[str, str]:
    """Map each second-level heading to its body."""
    parts = re.split(r"^## (.+)$", text, flags=re.MULTILINE)
    return {parts[index].strip(): parts[index + 1].strip() for index in range(1, len(parts), 2)}


class ImplementingChangesProfileTest(GitTestCase):
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

    def test_step_8_checks_the_written_title_and_body_then_brings_current_and_pushes_before_opening(self) -> None:
        section = SKILL.read_text(encoding="utf-8").split("## 8. Pull request", 1)[1]
        steps = [
            "1. Write the title",
            "2. Run *Validation*'s title and body checks",
            "3. Bring the branch up to date",
            "4. Push the branch",
            "5. Open the pull request",
            "gh pr create",
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

    def pull_request_command(self) -> str:
        """Step 8's command, the one fence its section holds, with the body file named."""
        section = SKILL.read_text(encoding="utf-8").split("## 8. Pull request", 1)[1]
        [command] = re.findall(r"^```bash\n(gh pr create .+)\n```$", section, flags=re.MULTILINE)
        self.assertIn("'<title>'", command)
        return command.replace("'<file>'", "'body.md'")

    def test_step_8_passes_a_title_with_quotes_backticks_and_dollars_unchanged_in_both_shells(self) -> None:
        section = SKILL.read_text(encoding="utf-8").split("## 8. Pull request", 1)[1]
        # The step states each shell's escape, which the commands below apply.
        self.assertIn("In Bash, write each `'` in the title as `'\\''`.", section)
        self.assertIn(
            "In PowerShell, write it as `''`, and double each `\u2018`, `\u2019`, `\u201a`, and `\u201b`", section
        )
        command = self.pull_request_command()
        bash, powershell = platform_support.find_bash(), platform_support.find_powershell()
        if bash is None or powershell is None:
            raise AssertionError("Git Bash and PowerShell are required for repository validation.")
        with tempfile.TemporaryDirectory() as directory:
            # Each shell's gh prints the argument after --title, so the test sees what the real one would receive.
            in_bash = command.replace("'<title>'", "'" + AWKWARD_TITLE.replace("'", "'\\''") + "'")
            script = Path(directory) / "pull-request.sh"
            script.write_bytes(f'gh() {{ printf "%s" "$4"; }}\n{in_bash}\n'.encode())
            ran = subprocess.run([bash, str(script)], capture_output=True, check=False, timeout=60)
            self.assertEqual((0, AWKWARD_TITLE), (ran.returncode, ran.stdout.decode("utf-8")), ran.stderr)
        doubled = POWERSHELL_QUOTES.sub(lambda quote: quote.group() * 2, AWKWARD_TITLE)
        in_powershell = command.replace("'<title>'", f"'{doubled}'")
        program = (
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
            "function gh { [Console]::Out.Write($args[3]) }\n"
            f"{in_powershell}\n"
        )
        ran = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                base64.b64encode(program.encode("utf-16-le")).decode("ascii"),
            ],
            capture_output=True,
            check=False,
            timeout=60,
        )
        self.assertEqual((0, AWKWARD_TITLE), (ran.returncode, ran.stdout.decode("utf-8")), ran.stderr)

    def test_skill_body_names_no_repository_command(self) -> None:
        lines = SKILL.read_text(encoding="utf-8").splitlines()
        found = frontmatter.split(lines)
        if found is None:
            self.fail("implement-change has no frontmatter")
        body = "\n".join(lines[found[1] :])
        for literal in ("tools/worktrees.py", "run_validation", "deploy.py", "change-skill", "releasing.md"):
            with self.subTest(literal=literal):
                self.assertNotIn(literal, body)


if __name__ == "__main__":
    unittest.main()
