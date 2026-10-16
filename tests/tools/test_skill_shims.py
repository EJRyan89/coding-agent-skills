"""Regression suite for tools/skill_shims.py: writing and checking the .agents/skills shims of repository skills."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        self.write("source.json", "{}\n")
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

    def test_a_user_only_skill_gets_the_codex_policy_file_beside_its_shim(self) -> None:
        self.write(
            ".claude/skills/widget-check/SKILL.md",
            SKILL.replace("---\n\n#", "disable-model-invocation: true\n---\n\n#"),
        )
        policy = Path(".agents/skills/widget-check/agents/openai.yaml")
        self.assertEqual((0, f"WROTE {SHIM_PATH.as_posix()}\nWROTE {policy.as_posix()}\n"), self.run_main("--write"))
        # The policy Codex reads instead of the frontmatter, written out independently of the deployer's constant.
        self.assertEqual(b"policy:\n  allow_implicit_invocation: false\n", (self.root / policy).read_bytes())
        self.assertEqual((0, ""), self.run_main())
        (self.root / policy).write_text("policy:\n  allow_implicit_invocation: true\n", encoding="utf-8")
        self.assertEqual(
            (1, f"{policy.as_posix()} differs from what python tools/skill_shims.py --write writes; run it\n"),
            self.run_main(),
        )
        (self.root / policy).unlink()
        self.assertEqual(
            (1, f"{policy.as_posix()} is missing from the shim; run python tools/skill_shims.py --write\n"),
            self.run_main(),
        )
        self.assertEqual((0, f"WROTE {policy.as_posix()}\n"), self.run_main("--write"))

    def test_a_file_the_writer_does_not_write_is_reported_and_kept(self) -> None:
        # A model-invocable skill's shim holds SKILL.md alone, so a policy file there changes how Codex starts it.
        self.write(SHIM_PATH.as_posix(), SHIM)
        self.write(".agents/skills/widget-check/agents/openai.yaml", "policy:\n  allow_implicit_invocation: true\n")
        self.write(".agents/skills/widget-check/scripts/tool.py", "print('unread')\n")
        self.assertEqual(
            (
                1,
                ".agents/skills/widget-check/agents/openai.yaml is no file python tools/skill_shims.py --write writes; "
                "delete it\n"
                ".agents/skills/widget-check/scripts/tool.py is no file python tools/skill_shims.py --write writes; "
                "delete it\n",
            ),
            self.run_main("--write"),
        )
        self.assertTrue((self.root / ".agents/skills/widget-check/scripts/tool.py").is_file())

    def test_a_folder_without_a_skill_md_and_a_stray_file_are_reported(self) -> None:
        self.write(SHIM_PATH.as_posix(), SHIM)
        self.write(".agents/skills/orphan-folder/README.md", "An orphan.\n")
        self.write(".agents/skills/notes.md", "Not a shim.\n")
        self.assertEqual(
            (
                1,
                ".agents/skills/notes.md is not a shim folder; delete it\n"
                ".agents/skills/orphan-folder has no .claude/skills/orphan-folder/SKILL.md; delete it\n",
            ),
            self.run_main(),
        )
        # A skill's own folder that lost its SKILL.md is a missing shim, which --write restores.
        (self.root / SHIM_PATH).unlink()
        self.assertIn(
            ".claude/skills/widget-check/SKILL.md has no .agents/skills/widget-check/SKILL.md shim", self.run_main()[1]
        )

    def test_write_can_be_limited_to_named_skills(self) -> None:
        self.write(".claude/skills/other/SKILL.md", "---\nname: other\n---\n")
        self.assertEqual([SHIM_PATH], skill_shims.write(self.root, ["widget-check"]))
        self.assertFalse((self.root / ".agents/skills/other/SKILL.md").exists())

    def test_a_root_outside_a_checkout_is_refused_and_no_adapter_is_rewritten(self) -> None:
        # A home holds .claude/skills and the adapters the deployer owns under .agents/skills, but no source.json.
        home = self.root.parent / "Fake Home"
        adapter = "the deployer's adapter\n"
        for relative, text in ((".claude/skills/widget-check/SKILL.md", SKILL), (SHIM_PATH.as_posix(), adapter)):
            (home / relative).parent.mkdir(parents=True, exist_ok=True)
            (home / relative).write_text(text, encoding="utf-8")

        def refused() -> tuple[str | int | None, str]:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                skill_shims.main(["--write", "--root", str(home)])
            return raised.exception.code, stderr.getvalue()

        code, stderr = refused()
        self.assertEqual(2, code)
        self.assertIn(f"argument --root: {home} is not a checkout of this repository", stderr)
        (home / "source.json").write_text("{}\n", encoding="utf-8")
        with mock.patch.object(Path, "home", return_value=home):
            code, stderr = refused()
        self.assertEqual(2, code)
        self.assertIn(f"lies in the skills directory {home / '.claude' / 'skills'}", stderr)
        self.assertEqual(adapter, (home / SHIM_PATH).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
