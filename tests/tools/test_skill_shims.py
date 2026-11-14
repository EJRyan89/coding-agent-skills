"""Regression suite for tools/skill_shims.py: writing and checking the .agents/skills shims of repository skills."""

from __future__ import annotations

import contextlib
import errno
import io
import shutil
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from presence_denial import denied as stat_denied

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
    "Read and follow `../../../.claude/skills/widget-check/SKILL.md`, a path relative to this file, as the "
    "authoritative workflow.\n"
    "Run its commands from the repository root, the folder that holds `.agents` and `.claude`, and resolve every "
    "relative path it names from there.\n"
    "A command that writes a shim under `.agents` needs an approval to run outside Codex's sandbox, which keeps "
    "`.agents` read-only.\n"
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

    def test_the_shim_resolves_the_skills_paths_from_the_repository_root_not_its_folder(self) -> None:
        # The skill's commands, such as python -B tools/widgets.py, name files from the repository root.
        self.run_main("--write")
        body = (self.root / SHIM_PATH).read_text(encoding="utf-8").split("---\n", 2)[2]
        self.assertIn("Run its commands from the repository root", body)
        self.assertNotIn("from `../../../.claude/skills/widget-check/`", body)
        self.assertIn("needs an approval to run outside Codex's sandbox", body)

    def test_check_reports_a_missing_shim_and_writes_nothing(self) -> None:
        self.assertEqual(
            (
                1,
                "STALE .claude/skills/widget-check/SKILL.md has no .agents/skills/widget-check/SKILL.md shim; "
                "run python tools/skill_shims.py --write\n",
            ),
            self.run_main(),
        )
        self.assertFalse((self.root / SHIM_PATH).exists())

    @unittest.skipIf(sys.platform == "win32", "a symbolic link to a file needs a privilege on Windows")
    def test_a_shim_that_is_a_link_to_nothing_is_missing_and_written_in_the_links_place_never_through_it(
        self,
    ) -> None:
        target = self.root.parent / "elsewhere" / "SKILL.md"
        target.parent.mkdir()
        (self.root / SHIM_PATH).parent.mkdir(parents=True)
        (self.root / SHIM_PATH).symlink_to(target)
        self.assertEqual(
            (
                1,
                "STALE .claude/skills/widget-check/SKILL.md has no .agents/skills/widget-check/SKILL.md shim; "
                "run python tools/skill_shims.py --write\n",
            ),
            self.run_main(),
        )
        self.assertEqual((0, f"WROTE {SHIM_PATH.as_posix()}\n"), self.run_main("--write"))
        self.assertFalse((self.root / SHIM_PATH).is_symlink())
        self.assertEqual(SHIM.encode("utf-8"), (self.root / SHIM_PATH).read_bytes())
        self.assertFalse(target.exists())

    def test_a_frontmatter_change_makes_the_shim_stale_until_written_again(self) -> None:
        self.write(SHIM_PATH.as_posix(), SHIM)
        self.write(".claude/skills/widget-check/SKILL.md", SKILL.replace("Use it when asked.", "Use it often."))
        stale = (
            "STALE .agents/skills/widget-check/SKILL.md differs from what python tools/skill_shims.py --write writes; "
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
            (1, "STALE .agents/skills/retired/SKILL.md has no .claude/skills/retired/SKILL.md; delete it\n"),
            self.run_main("--write"),
        )
        self.assertTrue((self.root / ".agents/skills/retired/SKILL.md").is_file())

    def test_a_skill_without_closed_frontmatter_gets_no_shim(self) -> None:
        self.write(".claude/skills/widget-check/SKILL.md", "---\nname: widget-check\n\n# Never closed\n")
        self.assertEqual(
            (1, "STALE .claude/skills/widget-check/SKILL.md has no closed frontmatter for its shim to carry\n"),
            self.run_main("--write"),
        )
        self.assertFalse((self.root / SHIM_PATH).exists())

    def test_a_name_or_description_the_shared_reader_refuses_gets_no_shim(self) -> None:
        nested = "name:\n" + "".join(" " * depth + "a:\n" for depth in range(1, 50))
        for key, frontmatter_lines in (
            ("name", "name: widget-check\nname: widget-other\n"),
            ("description", "name: widget-check\ndescription: One.\ndescription: Two.\n"),
            ("name", nested),
        ):
            with self.subTest(frontmatter=frontmatter_lines[:40]):
                self.write(".claude/skills/widget-check/SKILL.md", f"---\n{frontmatter_lines}---\n\n# Widget\n")
                code, output = self.run_main("--write")
                self.assertEqual(1, code)
                prefix = "STALE .claude/skills/widget-check/SKILL.md has frontmatter its shim cannot carry: "
                self.assertTrue(output.startswith(prefix + key), output)
                self.assertFalse((self.root / SHIM_PATH).exists())

    def test_a_directory_in_a_skills_place_ends_the_run_as_failed(self) -> None:
        (self.root / ".claude/skills/widget-check/SKILL.md").unlink()
        (self.root / ".claude/skills/widget-check/SKILL.md").mkdir()
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code, output = self.run_main()
        self.assertEqual((2, ""), (code, output))
        self.assertTrue(errors.getvalue().startswith("FAILED "), errors.getvalue())
        self.assertIn("not a file", errors.getvalue())

    def test_a_non_ascii_skill_md_saved_with_a_byte_order_mark_gives_the_shim_its_text(self) -> None:
        skill, shim = (text.replace("Check widgets", "Check café widgets") for text in (SKILL, SHIM))
        (self.root / ".claude/skills/widget-check/SKILL.md").write_bytes(b"\xef\xbb\xbf" + skill.encode("utf-8"))
        self.assertEqual((0, f"WROTE {SHIM_PATH.as_posix()}\n"), self.run_main("--write"))
        self.assertEqual(shim.encode("utf-8"), (self.root / SHIM_PATH).read_bytes())
        # Saved again with CRLF endings, it gives the same shim, which is still current.
        crlf = skill.replace("\n", "\r\n").encode("utf-8")
        (self.root / ".claude/skills/widget-check/SKILL.md").write_bytes(crlf)
        self.assertEqual((0, ""), self.run_main())
        (self.root / SHIM_PATH).unlink()
        self.assertEqual((0, f"WROTE {SHIM_PATH.as_posix()}\n"), self.run_main("--write"))
        self.assertEqual(shim.encode("utf-8"), (self.root / SHIM_PATH).read_bytes())

    def test_a_skill_md_that_is_not_utf8_ends_the_run_as_failed_never_as_stale(self) -> None:
        (self.root / ".claude/skills/widget-check/SKILL.md").write_bytes(b"---\nname: widget-check\n\xff\n---\n")
        for argv in ((), ("--write",)):
            with self.subTest(argv=argv):
                errors = io.StringIO()
                with contextlib.redirect_stderr(errors):
                    code, output = self.run_main(*argv)
                self.assertEqual((2, ""), (code, output))
                self.assertTrue(
                    errors.getvalue().startswith("FAILED .claude/skills/widget-check/SKILL.md is not UTF-8: "),
                    errors.getvalue(),
                )
                self.assertFalse((self.root / SHIM_PATH).exists())

    def test_a_shim_path_misplaced_or_unexamined_ends_the_run_as_failed_never_as_a_missing_shim(self) -> None:
        shims = self.root / ".agents/skills"
        shim = self.root / SHIM_PATH
        policy = shim.parent / "agents/openai.yaml"
        user_only = SKILL.replace("---\n\n#", "disable-model-invocation: true\n---\n\n#")
        not_a_file, not_a_folder = f"[Errno {errno.EISDIR}] not a file", f"[Errno {errno.ENOTDIR}] not a directory"
        denied = f"[Errno {errno.EACCES}] Access is denied"
        # Each row: the skill's SKILL.md, the shim files written first, the path given a folder, a file, or a refused
        # stat, the arguments, and the error the FAILED line names it with.
        rows = [
            (SKILL, (), shim, "folder", (), not_a_file),
            (SKILL, (), shim, "folder", ("--write",), not_a_file),
            (user_only, (SHIM_PATH,), policy, "folder", (), not_a_file),
            (SKILL, (), shims, "file", (), not_a_folder),
            (SKILL, (SHIM_PATH,), shims, "denied", (), denied),
            (SKILL, (SHIM_PATH,), shim, "denied", (), denied),
            (SKILL, (SHIM_PATH,), shim, "denied", ("--write",), denied),
        ]
        for skill, written, path, place, arguments, error in rows:
            with self.subTest(path=path.relative_to(self.root).as_posix(), place=place, arguments=arguments):
                self.write(".claude/skills/widget-check/SKILL.md", skill)
                for relative in written:
                    self.write(relative.as_posix(), SHIM)
                if place == "folder":
                    path.mkdir(parents=True)
                elif place == "file":
                    self.write(path.relative_to(self.root).as_posix(), "../.claude/skills\n")
                errors = io.StringIO()
                with contextlib.redirect_stderr(errors), stat_denied(path) if place == "denied" else nullcontext():
                    code, output = self.run_main(*arguments)
                self.assertEqual((2, "", f"FAILED {error}: {str(path)!r}\n"), (code, output, errors.getvalue()))
                if shims.is_dir():
                    shutil.rmtree(shims)
                else:
                    shims.unlink()

    def test_a_skill_shim_or_stray_entry_it_cannot_examine_or_list_fails_never_is_told_to_be_deleted(self) -> None:
        denied = f"[Errno {errno.EACCES}] Access is denied"
        # Each row: the files written, the path refused, whether its stat is refused too or only its listing.
        rows: list[tuple[tuple[str, ...], str, bool]] = [
            ((SHIM_PATH.as_posix(), ".agents/skills/stray/notes.md"), ".agents/skills/stray", True),
            ((SHIM_PATH.as_posix(), ".agents/skills/gone/SKILL.md"), ".agents/skills/gone/SKILL.md", True),
            ((SHIM_PATH.as_posix(),), ".claude/skills", False),
            ((SHIM_PATH.as_posix(),), ".claude/skills/widget-check/SKILL.md", True),
            (
                (SHIM_PATH.as_posix(), ".agents/skills/widget-check/agents/extra.txt"),
                ".agents/skills/widget-check/agents",
                False,
            ),
        ]
        for written, refused, stat in rows:
            with self.subTest(refused=refused, stat=stat):
                for relative in written:
                    self.write(relative, SHIM)
                path = self.root / refused
                errors = io.StringIO()
                with contextlib.redirect_stderr(errors), stat_denied(path, stat=stat):
                    code, output = self.run_main()
                self.assertEqual((2, "", f"FAILED {denied}: {str(path)!r}\n"), (code, output, errors.getvalue()))
                shutil.rmtree(self.root / ".agents")

    def test_a_repository_with_no_skills_folder_has_no_skills_and_its_shims_are_strays(self) -> None:
        # The command line refuses such a root as no checkout; the validation policy asks problems() directly.
        self.write(SHIM_PATH.as_posix(), SHIM)
        shutil.rmtree(self.root / ".claude")
        self.assertEqual(
            [f"{SHIM_PATH.as_posix()} has no .claude/skills/widget-check/SKILL.md; delete it"],
            skill_shims.problems(self.root),
        )

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
            (1, f"STALE {policy.as_posix()} differs from what python tools/skill_shims.py --write writes; run it\n"),
            self.run_main(),
        )
        (self.root / policy).unlink()
        self.assertEqual(
            (1, f"STALE {policy.as_posix()} is missing from the shim; run python tools/skill_shims.py --write\n"),
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
                "STALE .agents/skills/widget-check/agents/openai.yaml is no file python tools/skill_shims.py --write "
                "writes; delete it\n"
                "STALE .agents/skills/widget-check/scripts/tool.py is no file python tools/skill_shims.py --write "
                "writes; delete it\n",
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
                "STALE .agents/skills/notes.md is not a shim folder; delete it\n"
                "STALE .agents/skills/orphan-folder has no .claude/skills/orphan-folder/SKILL.md; delete it\n",
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
