"""Regression suite for tools/skill_shims.py: writing and checking the .agents/skills shims of repository skills."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools import skill_shims

SKILL = (
    "---\nname: widget-check\ndescription: >-\n  Check widgets in a repository with spaces. Use it when asked.\n"
    'allowed-tools: ["Bash(python -B tools/widgets.py*)", "PowerShell(python -B tools/widgets.py*)"]\n---\n\n'
    "# Widget check\n\nThe workflow, which the shim never copies.\n"
)
# The shim as Codex and Copilot CLI read it, written out independently of the tool under test.
SHIM = (
    "---\nname: widget-check\ndescription: >-\n  Check widgets in a repository with spaces. Use it when asked.\n"
    'allowed-tools: ["Bash(python -B tools/widgets.py*)", "PowerShell(python -B tools/widgets.py*)"]\n---\n\n'
    "Read and follow `../../../.claude/skills/widget-check/SKILL.md` as the authoritative workflow.\n"
    "Resolve all relative paths and supporting resources from `../../../.claude/skills/widget-check/`.\n"
)
SHIM_PATH = Path(".agents/skills/widget-check/SKILL.md")


class SkillShimsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="skill-shims-test.")
        self.root = Path(self._temporary.name).resolve() / "Repo With Spaces"
        self.write(".claude/skills/widget-check/SKILL.md", SKILL)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def run_main(self, *argv: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = skill_shims.main([*argv, "--root", str(self.root)])
        return code, output.getvalue()

    def test_write_creates_the_shim_from_the_frontmatter_and_check_then_passes(self) -> None:
        self.assertEqual((0, f"WROTE {SHIM_PATH.as_posix()}\n"), self.run_main("--write"))
        self.assertEqual(SHIM.encode("utf-8"), (self.root / SHIM_PATH).read_bytes())
        self.assertEqual((0, ""), self.run_main())

    def test_check_reports_a_missing_shim_and_writes_nothing(self) -> None:
        self.assertEqual(
            (
                1,
                ".claude/skills/widget-check/SKILL.md has no .agents/skills/widget-check/SKILL.md shim; "
                "run python tools/skill_shims.py --write\n",
            ),
            self.run_main(),
        )
        self.assertFalse((self.root / SHIM_PATH).exists())

    def test_a_frontmatter_change_makes_the_shim_stale_until_written_again(self) -> None:
        self.write(SHIM_PATH.as_posix(), SHIM)
        self.write(".claude/skills/widget-check/SKILL.md", SKILL.replace("Use it when asked.", "Use it often."))
        stale = (
            ".agents/skills/widget-check/SKILL.md differs from what python tools/skill_shims.py --write writes; "
            "run it\n"
        )
        self.assertEqual((1, stale), self.run_main())
        self.assertEqual((0, f"WROTE {SHIM_PATH.as_posix()}\n"), self.run_main("--write"))
        self.assertEqual(
            SHIM.replace("Use it when asked.", "Use it often.").encode("utf-8"), (self.root / SHIM_PATH).read_bytes()
        )

    def test_a_shim_with_crlf_line_endings_is_current_and_left_alone(self) -> None:
        self.write(SHIM_PATH.as_posix(), SHIM.replace("\n", "\r\n"))
        self.assertEqual((0, ""), self.run_main("--write"))
        self.assertIn(b"\r\n", (self.root / SHIM_PATH).read_bytes())

    def test_a_shim_whose_body_was_edited_is_rewritten(self) -> None:
        self.write(SHIM_PATH.as_posix(), SHIM + "\nAn extra instruction.\n")
        self.assertEqual((0, f"WROTE {SHIM_PATH.as_posix()}\n"), self.run_main("--write"))
        self.assertEqual(SHIM.encode("utf-8"), (self.root / SHIM_PATH).read_bytes())

    def test_a_stray_shim_is_reported_and_kept(self) -> None:
        self.write(SHIM_PATH.as_posix(), SHIM)
        self.write(".agents/skills/retired/SKILL.md", "---\nname: retired\n---\n")
        self.assertEqual(
            (1, ".agents/skills/retired/SKILL.md has no .claude/skills/retired/SKILL.md; delete it\n"),
            self.run_main("--write"),
        )
        self.assertTrue((self.root / ".agents/skills/retired/SKILL.md").is_file())

    def test_a_skill_without_closed_frontmatter_gets_no_shim(self) -> None:
        self.write(".claude/skills/widget-check/SKILL.md", "---\nname: widget-check\n\n# Never closed\n")
        self.assertEqual(
            (1, ".claude/skills/widget-check/SKILL.md has no closed frontmatter for its shim to carry\n"),
            self.run_main("--write"),
        )
        self.assertFalse((self.root / SHIM_PATH).exists())

    def test_write_can_be_limited_to_named_skills(self) -> None:
        self.write(".claude/skills/other/SKILL.md", "---\nname: other\n---\n")
        self.assertEqual([SHIM_PATH], skill_shims.write(self.root, ["widget-check"]))
        self.assertFalse((self.root / ".agents/skills/other/SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()
