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
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

import git_client

from tools import release_notes

NOTES = "docs/upgrade-notes.md"
INTRO = "# Upgrade notes\n\nWhat each release asks of a user.\n"
RELEASING = (
    "# Releasing\n\n## Versioning\n\n**Levels**, from `1.0.0` on:\n\n"
    "- **Patch.** Fixes.\n- **Minor.** Additions.\n- **Major.** The user must act.\n\n"
    "**Before `1.0.0`**, minor carries changes that would be major later.\n\n## Before tagging\n"
)
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

    def test_a_wrapped_heading_and_wrapped_fields_are_read_whole(self) -> None:
        # A squash merge may carry a body hard-wrapped, as git and GitHub wrap commit messages, so a heading or a
        # list item runs on over the following lines until a blank line or the next list item.
        wrapped = (
            "## Upgrade note\n\n### A changed document is reviewed as its extracted\ntext\n\n"
            "- Level: minor: the manifest gains optional\nitems.\n"
            "- Contract: the format tables in\n`docs/contract.md`: `record.location`,\nand a coverage gap.\n"
            "  - an indented item stays its own line\n"
            "- User action: none.\n\nA paragraph after a blank line\nstays apart.\n"
        )
        entries = release_notes.body_entries(wrapped)
        self.assertEqual(["A changed document is reviewed as its extracted text"], [e.heading for e in entries])
        self.assertEqual(
            {
                "Level": "minor: the manifest gains optional items.",
                "Contract": "the format tables in `docs/contract.md`: `record.location`, and a coverage gap.",
                "User action": "none.",
            },
            entries[0].fields,
        )
        self.assertEqual({"docs/contract.md", "record.location"}, entries[0].contract_items)
        self.assertEqual(
            "### A changed document is reviewed as its extracted text\n\n"
            "- Level: minor: the manifest gains optional items.\n"
            "- Contract: the format tables in `docs/contract.md`: `record.location`, and a coverage gap.\n"
            "  - an indented item stays its own line\n"
            "- User action: none.\n\nA paragraph after a blank line stays apart.",
            entries[0].text,
        )

    def test_a_field_with_nothing_on_its_line_stays_empty(self) -> None:
        entries = release_notes.body_entries("## Upgrade note\n\n### An entry\n\n- Contract:\n- User action: none\n")
        self.assertEqual({"Contract": "", "User action": "none"}, entries[0].fields)

    def test_a_body_that_says_none_or_has_no_section_has_no_entries(self) -> None:
        self.assertEqual([], release_notes.body_entries("## Upgrade note\n\nNone\n\n## Validation\n"))
        self.assertEqual([], release_notes.body_entries("## Changes\n\n### A heading\n\n- Level: minor.\n"))
        self.assertEqual([], release_notes.body_entries(""))

    def test_heading_whitespace_and_crlf_keep_the_same_section_boundaries(self) -> None:
        body = BODY.replace("## Upgrade note\r\n", "## Upgrade note \t\r\n")
        body = body.replace("## Validation\r\n", "## Validation \t\r\n")
        self.assertEqual(release_notes.body_entries(BODY), release_notes.body_entries(body))
        self.assertEqual([], release_notes.body_entries(body.replace("## Upgrade note \t", " ## Upgrade note")))


class FixtureRepository(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "repository with spaces"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "releasing.md").write_text(RELEASING, encoding="utf-8")
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
    "## v0.2.0\n\nLevel: minor.\n\n"
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

    def test_the_entries_are_the_checkouts_whatever_repository_git_dir_names(self) -> None:
        self.history()
        other = self.root.parent / "another repository"
        subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
        subprocess.run(["git", "-C", str(other), "commit", "-q", "--allow-empty", "-m", "Another (#9)"], check=True)
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git")}):
            entries = release_notes.range_entries(self.root, "v0.1.0", "HEAD")
        self.assertEqual(["#5", "#3"], [item.pull_request for item in entries])

    def test_an_entry_released_with_the_previous_tag_is_not_read_again(self) -> None:
        self.commit("Move a heading (#2)", notes=f"{INTRO}\n## Unreleased\n\n{RELEASED.replace('## v0.1.0', '')}")
        self.assertEqual([], release_notes.range_entries(self.root, "v0.1.0", "HEAD"))


class MainTests(FixtureRepository):
    def test_the_summary_names_the_largest_leading_level_before_the_entries(self) -> None:
        for values, expected in (
            (["Patch. A fix."], "patch"),
            (["patch.", "MINOR: an addition.", "patch."], "minor"),
            (["minor.", "Major. A migration.", "patch."], "major"),
        ):
            with self.subTest(values=values):
                body = "## Upgrade note\n\n" + "\n\n".join(
                    f"### Entry {index}\n\n- Level: {value}\n- Contract: none\n- User action: none"
                    for index, value in enumerate(values)
                )
                self.commit(f"A {expected} update (#7)", body=body)
                code, printed, error = self.run_main("v1.2.3", "--from", "HEAD^")
                self.assertEqual((0, ""), (code, error))
                self.assertTrue(printed.startswith(f"## v1.2.3\n\nLevel: {expected}.\n\n### Entry 0\n"), printed)
                self.assertEqual(values, [entry.fields["Level"] for entry in release_notes.notes_entries(printed)])

    def test_an_empty_range_states_that_it_has_no_upgrade_note_entries(self) -> None:
        self.commit("Internal cleanup", body="## Upgrade note\n\nNone\n")
        self.assertEqual((0, "## v0.2.0\n\nLevel: none (no upgrade-note entries).\n", ""), self.run_main("v0.2.0"))

    def test_the_summary_uses_the_order_in_the_versioning_document(self) -> None:
        self.history()
        path = self.root / "docs" / "releasing.md"
        path.write_text(RELEASING.replace("**Patch.**", "**Later.**"), encoding="utf-8")
        code, printed, error = self.run_main("v0.2.0")
        self.assertEqual((2, ""), (code, printed))
        self.assertIn("patch", error)
        path.write_text(
            RELEASING.replace(
                "- **Patch.** Fixes.\n- **Minor.** Additions.",
                "- **Minor.** Additions.\n- **Patch.** Fixes.",
            ),
            encoding="utf-8",
        )
        code, printed, error = self.run_main("v0.2.0")
        self.assertEqual((0, ""), (code, error))
        self.assertTrue(printed.startswith("## v0.2.0\n\nLevel: patch.\n\n"), printed)

    def test_pre_release_minor_keeps_the_breaking_explanation_and_action(self) -> None:
        body = BODY.replace(
            "minor. Additive: a new skill.", "minor: before `1.0.0`, this would be major later."
        ).replace("- User action: none", "- User action: migrate the configuration")
        self.commit("A breaking update (#7)", body=body)
        code, printed, error = self.run_main("v0.2.0")
        self.assertEqual((0, ""), (code, error))
        self.assertTrue(printed.startswith("## v0.2.0\n\nLevel: minor.\n\n"), printed)
        self.assertIn("- Level: minor: before `1.0.0`, this would be major later.", printed)
        self.assertIn("- User action: migrate the configuration", printed)

    def test_malformed_or_missing_entry_levels_refuse_output_and_leave_the_file_alone(self) -> None:
        before = (self.root / NOTES).read_bytes()
        for value in ("", "breaking.", "0.5.0", "`minor`", "minor2.", "minor_update"):
            with self.subTest(value=value):
                body = BODY.replace("- Level: minor. Additive: a new skill.", f"- Level: {value}")
                self.commit(f"An invalid update with {value!r} (#7)", body=body)
                code, printed, error = self.run_main("v0.2.0", "--from", "HEAD^", "--write")
                self.assertEqual((2, ""), (code, printed))
                self.assertTrue(error.startswith("ERROR: "), error)
                self.assertIn("Level", error)
                self.assertEqual(before, (self.root / NOTES).read_bytes())
        self.commit("A missing field (#7)", body=BODY.replace("- Level: minor. Additive: a new skill.\r\n", ""))
        self.assertEqual(2, self.run_main("v0.2.0", "--from", "HEAD^")[0])

    def test_a_missing_or_empty_level_list_is_an_explicit_error(self) -> None:
        self.history()
        path = self.root / "docs" / "releasing.md"
        path.write_text("# Releasing\n\n## Versioning\n", encoding="utf-8")
        code, printed, error = self.run_main("v0.2.0")
        self.assertEqual((2, ""), (code, printed))
        self.assertIn("Levels", error)
        path.unlink()
        code, printed, error = self.run_main("v0.2.0")
        self.assertEqual((2, ""), (code, printed))
        self.assertIn("releasing.md", error)

    def test_it_prints_the_section_from_the_last_tag_by_default(self) -> None:
        self.history()
        self.assertEqual((0, EXPECTED_SECTION, ""), self.run_main("v0.2.0"))

    def test_the_default_start_is_the_last_release_not_a_nearer_tag_of_another_name(self) -> None:
        # A pre-release or any other tag is not a release, so the section still starts at the release before it.
        self.commit("Fix a thing (#2)", notes=f"{INTRO}\n## Unreleased\n\n{HAND_ENTRY}\n{RELEASED}")
        self.git("tag", "v0.2.0-rc.1")
        self.commit("Add a skill (#3)", body=BODY)
        self.git("tag", "nightly")
        self.commit("Tidy the documents (#4)", body="## Upgrade note\n\nNone\n")
        code, printed, error = self.run_main("v0.2.0")
        self.assertEqual((0, ""), (code, error))
        self.assertEqual(
            ["## v0.2.0", "### A skill is added", "### An entry written by hand"],
            [line for line in printed.split("\n") if line.startswith("#")],
        )
        # A release is ordered by its numbers, not by how near it is: the highest release reachable is the start.
        self.git("tag", "v0.0.9", "HEAD~2")
        self.assertEqual("v0.1.0", release_notes.last_release_before(self.root, "HEAD"))

    def test_without_a_release_before_to_the_start_must_be_given(self) -> None:
        self.git("tag", "-d", "v0.1.0")
        self.git("tag", "v0.1.0-rc.1", "HEAD")
        self.commit("Add a skill (#3)", body=BODY)
        code, printed, error = self.run_main("v0.1.0")
        self.assertEqual((2, ""), (code, printed))
        self.assertEqual("ERROR: no release tag reaches HEAD^; name the previous release with --from\n", error)
        self.assertEqual(0, self.run_main("v0.1.0", "--from", "v0.1.0-rc.1")[0])

    def test_the_version_must_be_a_release_tag(self) -> None:
        for version in ("v0.2.0-rc.1", f"v0.{chr(0x662)}.0", "v0.2"):
            with self.subTest(version=version):
                with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(io.StringIO()) as error:
                    release_notes.main([version], root=self.root)
                self.assertEqual(2, raised.exception.code)
                self.assertIn("the version must be a release tag such as v1.2.3", error.getvalue())

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

    def test_a_notes_file_that_cannot_be_read_or_written_exits_2_naming_it_and_is_left_alone(self) -> None:
        self.history()
        notes = self.root / NOTES
        notes.unlink()
        self.assertEqual(
            (2, ""), self.run_main("v0.2.0", "--write")[:2], "a missing notes file is unreadable, not refused"
        )
        self.assertIn(f"ERROR: cannot read {NOTES}: ", self.run_main("v0.2.0", "--write")[2])
        self.assertFalse(notes.exists())
        notes.mkdir()
        code, printed, error = self.run_main("v0.2.0", "--write")
        self.assertEqual((2, ""), (code, printed))
        self.assertTrue(error.startswith(f"ERROR: cannot read {NOTES}: "), error)
        notes.rmdir()
        notes.write_bytes(b"\xff\xfe not UTF-8\n")
        code, printed, error = self.run_main("v0.2.0", "--write")
        self.assertEqual((2, ""), (code, printed))
        self.assertTrue(error.startswith(f"ERROR: cannot read {NOTES}: "), error)
        self.assertEqual(b"\xff\xfe not UTF-8\n", notes.read_bytes())
        notes.write_bytes(f"{INTRO}\n{RELEASED}".encode())
        with mock.patch.object(Path, "write_text", side_effect=PermissionError("denied")):
            code, printed, error = self.run_main("v0.2.0", "--write")
        self.assertEqual((2, "", f"ERROR: cannot write {NOTES}: denied\n"), (code, printed, error))

    def test_a_releasing_document_that_cannot_be_read_exits_2_naming_it(self) -> None:
        self.history()
        releasing = self.root / "docs" / "releasing.md"
        for content in (None, b"\xff not UTF-8\n"):
            with self.subTest(content=content):
                if content is None:
                    releasing.unlink()
                else:
                    releasing.write_bytes(content)
                code, printed, error = self.run_main("v0.2.0")
                self.assertEqual((2, ""), (code, printed))
                self.assertTrue(error.startswith("ERROR: cannot read docs/releasing.md: "), error)

    def test_a_range_git_cannot_read_exits_2_and_a_malformed_version_is_a_usage_error(self) -> None:
        code, printed, error = self.run_main("v0.2.0", "--from", "v9.9.9")
        self.assertEqual((2, ""), (code, printed))
        self.assertTrue(error.startswith("ERROR: git log "), error)
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(io.StringIO()):
            release_notes.main(["0.2.0"], root=self.root)
        self.assertEqual(2, raised.exception.code)


LATE_ENTRY = "### Merged late\n\n- Level: patch. A fix.\n- Contract: none\n- User action: none\n"


class DefectTests(FixtureRepository):
    """A ValueError the module did not raise as its own failure is a defect: it reaches run_main, never an ERROR line
    that reads as a fault of the repository."""

    def test_a_value_error_from_a_defect_is_not_reported_as_an_error_line(self) -> None:
        self.history()
        defect = ValueError("not enough values to unpack (expected 3, got 1)")
        for arguments, failing in (
            (["v0.2.0"], "commits"),
            (["v0.2.0", "--write"], "commits"),
            (["v0.2.0", "--check", "--to", "HEAD"], "missing_entries"),
            (["v0.2.0"], "last_release_before"),
        ):
            with (
                self.subTest(arguments=arguments, failing=failing),
                mock.patch.object(release_notes, failing, side_effect=defect),
                self.assertRaisesRegex(ValueError, "not enough values to unpack"),
            ):
                self.run_main(*arguments)

    def test_a_level_release_levels_refuses_stays_an_error_line(self) -> None:
        self.history()
        with mock.patch.object(release_notes, "largest_level", side_effect=ValueError("no Level")):
            self.assertEqual((2, "", "ERROR: no Level\n"), self.run_main("v0.2.0"))


class CheckTests(FixtureRepository):
    """--check, run at the tag: the section origin/main holds against the entries origin/main's commits carry."""

    def released_section(self) -> None:
        """The release pull request: a branch writes the section at its head, and main takes it as one squash
        commit, so the commit the section was written at never reaches main."""
        self.history()
        self.git("checkout", "-q", "-b", "release")
        (self.root / NOTES).write_bytes(f"{INTRO}\n{RELEASED}".encode())
        self.assertEqual(0, self.run_main("v0.2.0", "--write")[0])
        self.commit("Write the section")
        written = (self.root / NOTES).read_bytes()
        self.git("checkout", "-q", "main")
        (self.root / NOTES).write_bytes(written)
        self.commit("Release v0.2.0 (#7)", body="## Upgrade note\n\nNone\n")
        self.git("update-ref", "refs/remotes/origin/main", "main")

    def test_a_section_that_holds_every_entry_on_origin_main_is_current(self) -> None:
        self.released_section()
        self.assertEqual(
            (
                0,
                f"CURRENT ## v0.2.0 in {NOTES} at origin/main holds every upgrade-note entry from v0.1.0 to "
                "origin/main.\n",
                "",
            ),
            self.run_main("v0.2.0", "--check"),
        )

    def test_a_pull_request_merged_after_the_section_was_written_is_behind_and_names_its_entry(self) -> None:
        self.released_section()
        self.commit("Fix late (#8)", body=f"## Upgrade note\n\n{LATE_ENTRY}", change="late")
        self.git("update-ref", "refs/remotes/origin/main", "main")
        code, printed, error = self.run_main("v0.2.0", "--check")
        self.assertEqual((1, ""), (code, error))
        self.assertTrue(printed.startswith("BEHIND 1 entry: the commits from v0.1.0 to origin/main carry "), printed)
        self.assertTrue(printed.endswith(f"\n\n{LATE_ENTRY}- Pull request: #8\n"), printed)
        # Carrying the entry into the section through a pull request makes it current, and an entry the section holds
        # beyond the range's, as the releaser adds from the preparation issue, is left alone.
        notes = (self.root / NOTES).read_text(encoding="utf-8")
        carried = notes.replace(
            "## v0.1.0", f"{LATE_ENTRY}- Pull request: #8\n\n### An obligation\n\n- Level: patch.\n\n## v0.1.0"
        )
        (self.root / NOTES).write_bytes(carried.encode())
        self.commit("Carry the late entry (#9)")
        self.git("update-ref", "refs/remotes/origin/main", "main")
        self.assertEqual(0, self.run_main("v0.2.0", "--check")[0])

    def test_a_pull_request_merged_before_the_squash_is_behind_too(self) -> None:
        self.history()
        self.git("checkout", "-q", "-b", "release")
        (self.root / NOTES).write_bytes(f"{INTRO}\n{RELEASED}".encode())
        self.run_main("v0.2.0", "--write")
        self.commit("Write the section")
        written = (self.root / NOTES).read_bytes()
        self.git("checkout", "-q", "main")
        self.commit("Fix late (#8)", body=f"## Upgrade note\n\n{LATE_ENTRY}", change="late")
        (self.root / NOTES).write_bytes(written)
        self.commit("Release v0.2.0 (#7)")
        self.git("update-ref", "refs/remotes/origin/main", "main")
        code, printed, _ = self.run_main("v0.2.0", "--check")
        self.assertEqual(1, code)
        self.assertIn("### Merged late", printed)

    def test_it_reads_origin_main_not_the_checkout_and_to_chooses_another_ref(self) -> None:
        self.released_section()
        self.commit("Fix late (#8)", body=f"## Upgrade note\n\n{LATE_ENTRY}", change="late")
        self.assertEqual(0, self.run_main("v0.2.0", "--check")[0])
        self.assertEqual(1, self.run_main("v0.2.0", "--check", "--to", "HEAD")[0])

    def test_a_ref_without_the_section_exits_2_and_check_and_write_are_one_or_the_other(self) -> None:
        self.history()
        self.git("update-ref", "refs/remotes/origin/main", "main")
        self.assertEqual(
            (2, "", f"ERROR: {NOTES} at origin/main has no ## v0.2.0 section; write it with --write first\n"),
            self.run_main("v0.2.0", "--check"),
        )
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(io.StringIO()):
            release_notes.main(["v0.2.0", "--check", "--write"], root=self.root)
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
        # The tag waits for the check that the committed section holds every entry main carries.
        tagging = text.split("\n## Tagging", 1)[1].split("\n## ", 1)[0]
        self.assertIn("python tools/release_notes.py vX.Y.Z --check", tagging)
        # The template carries the heading the script reads, with its example in a comment no reader takes for one.
        template = (root / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
        self.assertIn("\n## Upgrade note\n", template)
        self.assertEqual([], release_notes.body_entries(template))


class GitProgramTests(unittest.TestCase):
    def test_git_runs_through_skill_cores_client_on_the_checkout_never_by_name(self) -> None:
        # The client's runner starts git at the path the locator finds and without the repository-selecting
        # variables; skill-core's suites hold both.
        self.assertIs(git_client.subprocess_runner, release_notes.CLIENT.runner)
        commands: list[list[str]] = []

        def runner(command: list[str], timeout: float) -> git_client.GitResult:
            commands.append(list(command))
            return git_client.GitResult(0, "out", "")

        with mock.patch.object(release_notes.CLIENT, "runner", runner):
            result = release_notes.git(Path("a checkout"), "log")
        self.assertEqual([["git", "-C", "a checkout", "log"]], commands)
        self.assertEqual("out", result.stdout)

    def test_git_that_cannot_run_or_prints_other_than_utf8_is_a_release_notes_error(self) -> None:
        def missing(command: list[str], timeout: float) -> git_client.GitResult:
            raise git_client.GitError(git_client.MISSING_GIT, kind="prerequisite")

        def latin(command: list[str], timeout: float) -> git_client.GitResult:
            return git_client.GitResult(0, b"caf\xe9".decode("utf-8", "surrogateescape"), "")

        for runner, message in ((missing, git_client.MISSING_GIT), (latin, "not UTF-8")):
            with (
                self.subTest(message=message),
                mock.patch.object(release_notes.CLIENT, "runner", runner),
                self.assertRaisesRegex(release_notes.ReleaseNotesError, message),
            ):
                release_notes.git(Path("a checkout"), "log")
