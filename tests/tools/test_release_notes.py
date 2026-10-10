"""Regression suite for tools/release_notes.py, over a fixture repository whose commits carry upgrade notes."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools import release_notes

NOTES = "docs/upgrade-notes.md"
INTRO = "# Upgrade notes\n\nWhat each release asks of a user.\n"
RELEASED = (
    "## v0.1.0\n\n### The first release\n\n- Level: minor.\n- Contract: none\n- User action: none\n- Pull request: #1\n"
)
HAND_ENTRY = (
    "### An entry written by hand\n\n- Level: patch. A fix.\n- Contract: none\n"
    "- User action: none\n- Pull request: #2\n"
)
BODY = (
    "Closes #3\r\n\r\nWhy the change.\r\n\r\n## Changes\r\n\r\n- A change.\r\n\r\n### Not an entry\r\n\r\n"
    "## Upgrade note\r\n\r\n<!-- ### An example in the template\r\n\r\n- Level: patch. -->\r\n\r\n"
    "### A skill is added\r\n\r\n- Level: minor. Additive: a new skill.\r\n- Contract: `skills/beta`\r\n"
    "- User action: none\r\n\r\n## Validation\r\n\r\n- [x] Validation passes\r\n"
)


class BodyEntriesTests(unittest.TestCase):
    def test_entries_come_only_from_the_upgrade_note_section_and_not_from_its_comments(self) -> None:
        entries = release_notes.body_entries(BODY)
        self.assertEqual(["A skill is added"], [entry.heading for entry in entries])
        self.assertEqual(
            {"Level": "minor. Additive: a new skill.", "Contract": "`skills/beta`", "User action": "none"},
            entries[0].fields,
        )
        self.assertEqual({"skills/beta"}, entries[0].contract_items)

    def test_a_body_that_says_none_or_has_no_section_has_no_entries(self) -> None:
        self.assertEqual([], release_notes.body_entries("## Upgrade note\n\nNone\n\n## Validation\n"))
        self.assertEqual([], release_notes.body_entries("## Changes\n\n### A heading\n\n- Level: minor.\n"))
        self.assertEqual([], release_notes.body_entries(""))


class FixtureRepository(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        self.root = home / "repository with spaces"
        self.root.mkdir()
        (home / "empty").write_text("", encoding="utf-8")
        environment = {
            "GIT_CONFIG_GLOBAL": str(home / "empty"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
        patcher = mock.patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.git("init", "-q", "-b", "main")
        self.commit("Release v0.1.0 (#1)", notes=f"{INTRO}\n## Unreleased\n\n{RELEASED}")
        self.git("tag", "v0.1.0")

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), *arguments], check=True, capture_output=True, text=True, encoding="utf-8"
        ).stdout

    def commit(self, subject: str, body: str = "", notes: str | None = None, change: str = "a") -> None:
        if notes is not None:
            (self.root / "docs").mkdir(exist_ok=True)
            (self.root / NOTES).write_bytes(notes.encode("utf-8"))
        (self.root / f"{change}.txt").write_text(f"{subject}\n", encoding="utf-8")
        self.git("add", ".")
        message = self.root.parent / "message.txt"
        message.write_bytes(f"{subject}\n\n{body}".encode())
        self.git("commit", "-q", "-F", str(message))

    def history(self) -> None:
        """A change written by hand under Unreleased, one from a body, one that asks nothing, then a hand edit."""
        self.commit("Fix a thing (#2)", notes=f"{INTRO}\n## Unreleased\n\n{HAND_ENTRY}\n{RELEASED}")
        self.commit("Add a skill (#3)", body=BODY)
        self.commit("Tidy the documents (#4)", body="## Upgrade note\n\nNone\n")
        edited = HAND_ENTRY.replace("A fix.", "A fix, worded again.")
        self.commit("Reword a note (#5)", notes=f"{INTRO}\n## Unreleased\n\n{edited}\n{RELEASED}")

    def run_main(self, *arguments: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = release_notes.main(list(arguments), root=self.root)
        return code, out.getvalue(), err.getvalue()


EXPECTED_SECTION = (
    "## v0.2.0\n\n"
    "### An entry written by hand\n\n- Level: patch. A fix, worded again.\n- Contract: none\n- User action: none\n"
    "- Pull request: #2\n\n"
    "### A skill is added\n\n- Level: minor. Additive: a new skill.\n- Contract: `skills/beta`\n- User action: none\n"
    "- Pull request: #3\n"
)


class RangeEntriesTests(FixtureRepository):
    def test_each_commit_gives_its_body_entries_and_the_entries_it_added_by_hand_newest_first(self) -> None:
        self.history()
        entries = release_notes.range_entries(self.root, "v0.1.0", "HEAD")
        self.assertEqual(["An entry written by hand", "A skill is added"], [item.entry.heading for item in entries])
        self.assertEqual([True, False], [item.from_notes for item in entries])
        self.assertEqual(["#5", "#3"], [item.pull_request for item in entries])
        self.assertIn(f"{NOTES} as commit ", entries[0].where)
        self.assertTrue(entries[1].where.endswith("'s ## Upgrade note"), entries[1].where)

    def test_merge_commits_are_left_out_and_their_side_is_read_once(self) -> None:
        self.git("checkout", "-q", "-b", "side")
        self.commit("Add a skill (#3)", body=BODY)
        self.git("checkout", "-q", "main")
        self.commit("Fix a thing (#2)", notes=f"{INTRO}\n## Unreleased\n\n{HAND_ENTRY}\n{RELEASED}", change="b")
        self.git("merge", "-q", "--no-ff", "-m", f"Merge side\n\n{BODY}", "side")
        headings = [item.entry.heading for item in release_notes.range_entries(self.root, "v0.1.0", "HEAD")]
        self.assertEqual(["An entry written by hand", "A skill is added"], headings)

    def test_an_entry_released_with_the_previous_tag_is_not_read_again(self) -> None:
        self.commit("Move a heading (#2)", notes=f"{INTRO}\n## Unreleased\n\n{RELEASED.replace('## v0.1.0', '')}")
        self.assertEqual([], release_notes.range_entries(self.root, "v0.1.0", "HEAD"))


class MainTests(FixtureRepository):
    def test_it_prints_the_section_from_the_last_tag_by_default(self) -> None:
        self.history()
        self.assertEqual((0, EXPECTED_SECTION, ""), self.run_main("v0.2.0"))

    def test_from_and_to_choose_the_range(self) -> None:
        self.history()
        self.git("tag", "v0.2.0")
        self.commit("After the release (#6)", body=BODY.replace("A skill is added", "Later"))
        self.assertEqual((0, EXPECTED_SECTION, ""), self.run_main("v0.2.0", "--to", "v0.2.0"))
        code, printed, _ = self.run_main("v0.3.0", "--from", "v0.2.0")
        self.assertEqual(0, code)
        self.assertEqual(["## v0.3.0", "### Later"], [line for line in printed.split("\n") if line.startswith("#")])

    def test_write_inserts_the_section_above_the_newest_version_and_refuses_it_twice(self) -> None:
        self.history()
        (self.root / NOTES).write_bytes(f"{INTRO}\n{RELEASED}".encode())
        code, printed, _ = self.run_main("v0.2.0", "--write")
        self.assertEqual((0, f"Wrote ## v0.2.0 from v0.1.0..HEAD into {NOTES}.\n"), (code, printed))
        written = (self.root / NOTES).read_bytes().decode("utf-8")
        self.assertNotIn("\r", written)
        self.assertEqual(f"{INTRO}\n{EXPECTED_SECTION}\n{RELEASED}", written)
        self.assertEqual(
            (1, "", f"ERROR: {NOTES} already has a ## v0.2.0 section\n"), self.run_main("v0.2.0", "--write")
        )

    def test_a_range_git_cannot_read_exits_2_and_a_malformed_version_is_a_usage_error(self) -> None:
        code, printed, error = self.run_main("v0.2.0", "--from", "v9.9.9")
        self.assertEqual((2, ""), (code, printed))
        self.assertTrue(error.startswith("ERROR: git log "), error)
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(io.StringIO()):
            release_notes.main(["0.2.0"], root=self.root)
        self.assertEqual(2, raised.exception.code)


class InsertSectionTests(unittest.TestCase):
    def test_a_file_with_no_version_gets_the_section_at_its_end(self) -> None:
        self.assertEqual(f"{INTRO}\n## v0.1.0\n", release_notes.insert_section(INTRO, "v0.1.0", "## v0.1.0\n"))


class ReleaseProcedureTests(unittest.TestCase):
    def test_the_release_procedure_writes_the_section_before_tagging(self) -> None:
        root = Path(__file__).resolve().parents[2]
        text = (root / "docs" / "releasing.md").read_text(encoding="utf-8")
        before_tagging = text.split("## Before tagging", 1)[1].split("\n## ", 1)[0]
        self.assertIn("python tools/release_notes.py vX.Y.Z --write", before_tagging)
        # The template carries the heading the script reads, with its example in a comment no reader takes for one.
        template = (root / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
        self.assertIn("\n## Upgrade note\n", template)
        self.assertEqual([], release_notes.body_entries(template))


if __name__ == "__main__":
    unittest.main()
