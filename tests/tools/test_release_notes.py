"""Regression suite for tools/release_notes.py, over a fixture repository whose commits carry upgrade notes."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import git_client
from fixture_template import FixtureTemplate, remove_tree, repository_shape
from github_client import CommandResult, GitHubClient
from markdown_sections import markdown_section
from test_time_in_proportion import assert_time_in_proportion

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
# An entry two pull requests each wrote under one heading, with their own text.
SHARED_ENTRY = (
    "### Refusals end with the step to take\n\n- Level: minor. {text}\n- Contract: none\n- User action: none\n"
)


class BodyEntriesTests(unittest.TestCase):
    def test_an_entry_s_heading_is_read_as_markdown_reads_it(self) -> None:
        # changed: an entry was a block starting `### `, named by the text after it, so an indented heading, one with a
        # tab after its marks, and one with closing marks gave no entry or a name holding the marks.
        for written in (" ### Named", "###\tNamed", "### Named ###"):
            with self.subTest(written=written):
                [entry] = release_notes.notes_entries(f"{written}\n\n- Level: patch\n- Contract: none\n")
                self.assertEqual(("Named", "patch"), (entry.heading, entry.fields["Level"]))

    def test_entries_come_only_from_the_upgrade_note_section_and_not_from_its_comments(self) -> None:
        entries = release_notes.body_entries(BODY)
        self.assertEqual(["A skill is added"], [entry.heading for entry in entries])
        self.assertEqual(
            {"Level": "minor. Additive: a new skill.", "Contract": "`skills/beta`", "User action": "none"},
            entries[0].fields,
        )
        self.assertEqual({"skills/beta"}, entries[0].contract_items)

    def test_a_contract_item_is_a_code_span_that_pairs_whole_backtick_runs(self) -> None:
        # The contract line of the revert declaration's upgrade note in docs/upgrade-notes.md. changed: the
        # single-backtick pattern this read before split the double-backtick span at its inner backticks, read the
        # prose between it and the next span as an item, and missed `docs/implementing-changes.md`.
        body = (
            "## Upgrade note\n\n### A revert is declared\n\n- Level: patch.\n"
            "- Contract: the revert declaration, ``Reverts `<path>` to its state before <commit>``, as "
            '"Pull requests" in `docs/implementing-changes.md` states.\n- User action: none.\n'
        )
        [entry] = release_notes.body_entries(body)
        self.assertEqual(
            {"Reverts `<path>` to its state before <commit>", "docs/implementing-changes.md"}, entry.contract_items
        )

    def test_an_entry_a_comment_holds_and_a_comment_in_a_heading_are_not_read(self) -> None:
        body = (
            "## Upgrade note\n\n<!--\n### Hidden\n\n- Level: patch\n-->\n### Shown <!-- note --> here\n\n"
            "- Level: patch. A fix.\n- Contract: none\n- User action: none\n"
        )
        (entry,) = release_notes.body_entries(body)
        self.assertEqual("Shown  here", entry.heading)
        self.assertEqual("### Shown  here\n\n- Level: patch. A fix.\n- Contract: none\n- User action: none", entry.text)

    def test_a_comment_code_shows_is_read_as_github_renders_it(self) -> None:
        # GitHub (`gh api markdown`) renders each `<!--` and `-->` here inside a code span, as text.
        fields = "- Level: patch. Reads `-->` too.\n- Contract: none\n- User action: none\n"
        body = f"## Upgrade note\n\n### A reader keeps `<!-- x -->` shown in code\n\n{fields}"
        (entry,) = release_notes.body_entries(body)
        self.assertEqual("A reader keeps `<!-- x -->` shown in code", entry.heading)
        (entry,) = release_notes.body_entries(f"## Upgrade note\n\n### Sample `<!--` opener\n\n{fields}")
        self.assertEqual("Sample `<!--` opener", entry.heading)
        self.assertEqual("patch. Reads `-->` too.", entry.fields["Level"])

    def test_an_unclosed_opener_in_the_last_field_is_read_as_text(self) -> None:
        # GitHub (`gh api markdown`) shows a `<!--` no `-->` follows inside a list item as text, at the body's end too.
        body = "## Upgrade note\n\n### A fix\n\n- Level: patch. A fix.\n- Contract: none\n- User action: none <!-- x"
        (entry,) = release_notes.body_entries(body)
        self.assertEqual("none <!-- x", entry.fields["User action"])

    def test_a_body_eight_times_as_long_is_read_in_about_eight_times_the_time(self) -> None:
        # A linear reading takes about eight times as long and a quadratic one about 64 times; the bound sits halfway.
        unit = "### Entry `<!--` x\n\n- Level: patch. <!-- c --> `-->`\n- Contract: none\n- User action: none\n\n"
        small, large = f"## Upgrade note\n\n{unit * 40}", f"## Upgrade note\n\n{unit * 320}"
        assert_time_in_proportion(release_notes.body_entries, small, large, 8**1.5)

    def test_a_wrapped_line_starts_a_block_only_where_markdown_reads_one(self) -> None:
        # A list item is read as CommonMark reads one: one to nine ASCII digits, then a space or a tab, and below a
        # paragraph's text only a bullet or an item numbered 1 starts a list. Only spaces and tabs are whitespace.
        nbsp, em = chr(0xA0), chr(0x2003)
        for continued in (
            "2. more",
            "1234567890. more",
            f"{chr(0x663)}. more",
            f"1.{nbsp}more",
            f"{em}- more",
            f"{em}# more",
            "    - more",
            # Past MAX_BLOCK_INDENT columns a heading or a quote is the paragraph's text, as markdown_sections'
            # `blocks` reads it.
            "    # more",
            "    > more",
        ):
            with self.subTest(continued=continued):
                joined = f"Some text {continued.lstrip(' ')}"
                self.assertEqual([joined], release_notes.unwrapped(f"Some text\n{continued}"))
        for started in ("1. more", "- more", "  * more", "# more", "#", "> more", "| a |", "---", " * * *", "___"):
            with self.subTest(started=started):
                self.assertEqual(["Some text", started], release_notes.unwrapped(f"Some text\n{started}"))
        # Below a list item, any item starts its own line, and a line of no-break spaces is text, not a blank line.
        self.assertEqual(["1. first", "2. second"], release_notes.unwrapped("1. first\n2. second"))
        # A line the item holds is read from the column of its content: a heading or a quote up to MAX_BLOCK_INDENT
        # columns past it starts its own line, and one further in continues the item.
        for started in ("     # Heading", "    > quoted"):
            with self.subTest(started=started):
                self.assertEqual(["- item", started], release_notes.unwrapped(f"- item\n{started}"))
        self.assertEqual(["- item # deep"], release_notes.unwrapped("- item\n      # deep"))
        self.assertEqual([f"Some text {nbsp} more"], release_notes.unwrapped(f"Some text\n{nbsp}\nmore"))

    def test_a_wrapped_line_joins_only_the_paragraph_or_list_item_that_holds_it(self) -> None:
        # The lines are read as markdown_sections' `blocks` reads them, not from the line above: an item after an
        # item's paragraph is the list's next item, and a numbered line inside an item's paragraph continues it.
        self.assertEqual(["1. a", "", "   para", "2. b"], release_notes.unwrapped("1. a\n\n   para\n2. b"))
        self.assertEqual(["- a 2. b"], release_notes.unwrapped("- a\n  2. b"))
        # A paragraph on the line right below a heading is the heading's wrapped words; one after a blank line is not.
        self.assertEqual(["# H more"], release_notes.unwrapped("# H\nmore"))
        self.assertEqual(["# H", "", "more"], release_notes.unwrapped("# H\n\nmore"))
        # Only a heading's wrapped words run on below it: a paragraph right below a code block is its own line.
        self.assertEqual(["Text", "", "    code", "more"], release_notes.unwrapped("Text\n\n    code\nmore"))
        # A fence's lines, a table's rows, and a quote's lines each stay their own.
        self.assertEqual(
            ["Text", "```", "x", "y", "```", "| a |", "| b |", "> q", "> r"],
            release_notes.unwrapped("Text\n```\nx\ny\n```\n| a |\n| b |\n> q\n> r"),
        )

    def test_a_fence_closes_only_at_a_fence_line_markdown_reads_as_its_close(self) -> None:
        # A shorter fence line or one of the other character is the fence's text, and the fence stays open past it.
        for opener, inner in (("````", "```"), ("~~~", "```"), ("```", "~~~")):
            with self.subTest(opener=opener, inner=inner):
                self.assertEqual(
                    ["Text", opener, "x", inner, "y", opener, "more"],
                    release_notes.unwrapped(f"Text\n{opener}\nx\n{inner}\ny\n{opener}\nmore"),
                )
        # A line right after a closing fence, a longer one among them, starts a paragraph of its own.
        for closer in ("```", "`````"):
            with self.subTest(closer=closer):
                self.assertEqual(
                    ["```", "x", closer, "more words"], release_notes.unwrapped(f"```\nx\n{closer}\nmore\nwords")
                )

    def test_a_heading_keeps_any_unicode_space_markdown_keeps_and_a_number_is_ascii(self) -> None:
        nbsp = chr(0xA0)
        entries = release_notes.body_entries(f"## Upgrade note\n\n### Changed{nbsp} \t\n\n- Level: patch.\n")
        self.assertEqual([f"Changed{nbsp}"], [entry.heading for entry in entries])
        commit = release_notes.Commit
        self.assertEqual("#12", commit("a" * 40, "fix: a change (#12) \t", "").pull_request)
        self.assertIsNone(commit("a" * 40, f"fix: a change (#{chr(0x663)})", "").pull_request)

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

    def test_a_second_level_line_in_a_fence_stays_in_the_entry(self) -> None:
        body = (
            "## Upgrade note\n\n### Thing\n\n- Level: patch, because.\n- Contract: none\n- User action: run\n\n"
            "```text\n## not a heading\n```\n\n## Validation\n"
        )
        entries = release_notes.body_entries(body)
        self.assertEqual(1, len(entries))
        self.assertTrue(entries[0].text.endswith("```text\n## not a heading\n```"), entries[0].text)

    def test_a_field_line_in_a_fence_is_the_fences_text_and_no_field(self) -> None:
        # A fence on a list item's line holds the lines indented as far as the item's content; one written at the
        # margin leaves the item and ends the fence, as GitHub renders it.
        for opener, indent, closer in (("```markdown", "", "```"), ("~~~~", "", "~~~~"), ("- ```text", "  ", "  ```")):
            with self.subTest(opener=opener):
                body = (
                    "## Upgrade note\n\n### X\n\n- Level: patch, a documentation fix.\n- Contract: none.\n"
                    f"- User action: none. Example:\n\n{opener}\n{indent}- Level: major, an example.\n"
                    f"{indent}- Pull request: #99\n{closer}\n\n## Validation\n"
                )
                [entry] = release_notes.body_entries(body)
                self.assertEqual(
                    {"Level": "patch, a documentation fix.", "Contract": "none.", "User action": "none. Example:"},
                    entry.fields,
                )
                self.assertIn("- Level: major, an example.", entry.text)
                sourced = release_notes.SourcedEntry(entry, "a body", "#7", from_notes=False)
                self.assertTrue(sourced.rendered().endswith("\n- Pull request: #7"), sourced.rendered())

    def test_heading_whitespace_and_crlf_keep_the_same_section_boundaries(self) -> None:
        body = BODY.replace("## Upgrade note\r\n", "## Upgrade note \t\r\n")
        body = body.replace("## Validation\r\n", "## Validation \t\r\n")
        self.assertEqual(release_notes.body_entries(BODY), release_notes.body_entries(body))
        self.assertEqual([], release_notes.body_entries(body.replace("## Upgrade note \t", " ## Upgrade note")))


def fixture_git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout


def fixture_commit(root: Path, subject: str, body: str = "", notes: str | None = None, change: str = "a") -> None:
    """Commit a change to `change`.txt, and `notes` as the upgrade notes when given, with the message read from a file
    beside the repository."""
    if notes is not None:
        (root / "docs").mkdir(exist_ok=True)
        (root / NOTES).write_bytes(notes.encode("utf-8"))
    (root / f"{change}.txt").write_text(f"{subject}\n", encoding="utf-8")
    fixture_git(root, "add", ".")
    message = root.parent / "message.txt"
    message.write_bytes(f"{subject}\n\n{body}".encode())
    fixture_git(root, "commit", "-q", "-F", str(message))


def build_release(root: Path) -> None:
    """A repository on main whose one commit, tagged v0.1.0, released one upgrade-note entry."""
    fixture_git(root, "init", "-q", "-b", "main")
    (root / "docs").mkdir()
    (root / "docs" / "releasing.md").write_text(RELEASING, encoding="utf-8")
    fixture_commit(root, "Release v0.1.0 (#1)", notes=f"{INTRO}\n## Unreleased\n\n{RELEASED}")
    fixture_git(root, "tag", "v0.1.0")


def build_history(root: Path) -> None:
    """The release, then a change written by hand under Unreleased, one from a body, one that asks nothing, and a
    hand edit."""
    RELEASE.copy(root)
    fixture_commit(root, "Fix a thing (#2)", notes=f"{INTRO}\n## Unreleased\n\n{HAND_ENTRY}\n{RELEASED}")
    fixture_commit(root, "Add a skill (#3)", body=BODY)
    fixture_commit(root, "Tidy the documents (#4)", body="## Upgrade note\n\nNone\n")
    edited = HAND_ENTRY.replace("A fix.", "A fix, worded again.")
    fixture_commit(root, "Reword a note (#5)", notes=f"{INTRO}\n## Unreleased\n\n{edited}\n{RELEASED}")


# Building the repositories starts a dozen git processes, so each process builds each once and each test gets a copy.
RELEASE = FixtureTemplate("repository with spaces", build_release)
HISTORY = FixtureTemplate("repository with spaces", build_history)


class FixtureTemplateTests(unittest.TestCase):
    def test_each_copied_repository_matches_a_freshly_built_one(self) -> None:
        for template, build in ((RELEASE, build_release), (HISTORY, build_history)):
            with self.subTest(build=build.__name__):
                fresh, copied = (Path(tempfile.mkdtemp(prefix="release-notes-template-")).resolve() for _ in "ab")
                self.addCleanup(remove_tree, fresh)
                self.addCleanup(remove_tree, copied)
                (fresh / "repository").mkdir()
                build(fresh / "repository")
                template.copy(copied / "repository")
                self.assertEqual(repository_shape(fresh, ["repository"]), repository_shape(copied, ["repository"]))


class FixtureRepository(unittest.TestCase):
    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = RELEASE.copy(Path(temporary.name) / "repository with spaces")

    def git(self, *arguments: str) -> str:
        return fixture_git(self.root, *arguments)

    def commit(self, subject: str, body: str = "", notes: str | None = None, change: str = "a") -> None:
        fixture_commit(self.root, subject, body, notes, change)

    def history(self) -> None:
        """Replace the release with a copy of the history after it."""
        remove_tree(self.root)
        HISTORY.copy(self.root)

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

    def test_the_commits_touching_the_notes_are_the_ranges_non_merge_commits_that_change_the_file(self) -> None:
        # Before the range: a commit changing the notes. In it: one changing the notes, one changing another file,
        # and a merge whose side changes another part of the notes, so the merge changes the file against both parents.
        notes = f"{INTRO}\n## Unreleased\n\n{HAND_ENTRY}\n{RELEASED}"
        self.commit("Before the range (#2)", notes=notes)
        start = self.git("rev-parse", "HEAD").strip()
        self.commit("Edit the notes (#3)", notes=notes.replace("What each release", "What every release"))
        edited = self.git("rev-parse", "HEAD").strip()
        self.commit("Change a file (#4)", change="other")
        self.git("checkout", "-q", "-b", "side", start)
        side_notes = notes.replace("- Pull request: #1", "- Pull request: #1, #9")
        self.commit("Edit the notes on a side (#5)", notes=side_notes, change="side")
        side = self.git("rev-parse", "HEAD").strip()
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m", "Merge side", "side")
        self.assertEqual({edited, side}, release_notes.notes_commits(self.root, start, "HEAD"))

    def test_a_message_holding_any_byte_but_nul_is_read_whole(self) -> None:
        # The separators an earlier reader split on, and text that looks like the next commit's name, sit in the
        # subject and the body; git refuses a NUL, the one byte the listing splits on.
        for index, separator in enumerate(("\x1f", "\x1e", f"\n{'0' * 40}\n")):
            with self.subTest(separator=repr(separator)):
                subject = f"Add a skill {separator.strip()} (#6)"
                body = BODY.replace("\r\n", "\n").replace("Why the change.", f"Why{separator}the change.")
                self.commit(subject, body=body, change=f"change {index}")
                [commit] = release_notes.commits(self.root, "HEAD~1", "HEAD")
                self.assertEqual((subject, f"{subject}\n\n{body}"), (commit.subject, commit.message))
                entries = release_notes.range_entries(self.root, "HEAD~1", "HEAD")
                self.assertEqual(["#6"], [item.pull_request for item in entries])
                self.assertEqual(["A skill is added"], [item.entry.heading for item in entries])
                self.assertEqual(0, self.run_main("v0.2.0", "--from", "HEAD~1")[0])

    def test_a_listing_that_is_not_whole_commits_is_a_release_notes_error(self) -> None:
        for listed in (f"{'a' * 40}\0subject\0", f"{'a' * 40}\0subject\0message", "\0"):
            runner = mock.Mock(return_value=git_client.GitResult(0, listed, ""))
            with (
                self.subTest(listed=listed),
                mock.patch.object(release_notes.CLIENT, "runner", runner),
                self.assertRaisesRegex(release_notes.ReleaseNotesError, "a listing that is not whole commits"),
            ):
                release_notes.commits(self.root, "v0.1.0", "HEAD")

    def test_two_pull_requests_whose_entries_share_a_heading_both_reach_the_section_and_are_named(self) -> None:
        older = SHARED_ENTRY.format(text="Refusals of the configuration end with the step.")
        newer = SHARED_ENTRY.format(text="Refusals of the journal end with the step.")
        self.commit("Older refusals (#2)", body=f"## Upgrade note\n\n{older}", change="older")
        self.commit("Newer refusals (#3)", body=f"## Upgrade note\n\n{newer}", change="newer")
        entries = release_notes.range_entries(self.root, "v0.1.0", "HEAD")
        self.assertEqual(["#3", "#2"], [item.pull_request for item in entries])
        self.assertEqual([newer.rstrip("\n"), older.rstrip("\n")], [item.entry.text for item in entries])
        code, printed, error = self.run_main("v0.2.0")
        self.assertEqual(0, code)
        self.assertIn(f"{newer}- Pull request: #3\n\n{older}- Pull request: #2\n", printed)
        newest, oldest = (self.git("rev-parse", "--short=7", ref).strip() for ref in ("HEAD", "HEAD^"))
        self.assertEqual(
            f'SHARED "Refusals end with the step to take": commit {newest}\'s ## Upgrade note; '
            f"commit {oldest}'s ## Upgrade note\n",
            error,
        )

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
            (["v0.2.0", "--check", "--to", "HEAD", "--notes", "unread notes.md"], "missing_entries"),
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


COUNTS_LINE = "Defects fixed: 1 (regression 0, latent 1, hardening 0, no origin label 0). Auditor gaps: 0."


def counting_client(items: Sequence[Mapping[str, object]] | None = None) -> GitHubClient:
    """A client whose milestone v0.2.0 holds the items: by default one fixed latent bug, which counts as COUNTS_LINE."""
    items = [issue(1, "bug", "latent")] if items is None else items
    answers = {
        tuple(MILESTONES): json.dumps([[milestone("v0.2.0", 7, 0, len(items))]]),
        tuple(ISSUES): json.dumps([list(items)]),
    }
    return GitHubClient(lambda command: CommandResult(0, answers[tuple(command)], ""), sleeper=lambda _: None)


class CheckTests(FixtureRepository):
    """--check, run at the tag: the section origin/main holds against the entries origin/main's commits carry, and the
    release notes about to be published against that section and the line --counts prints."""

    def held_notes(self, counts: str = COUNTS_LINE, ref: str = "origin/main") -> str:
        """Release notes as Tagging writes them: the section the ref holds, with the counts line below its level."""
        section = markdown_section(self.git("show", f"{ref}:{NOTES}"), "## v0.2.0")
        if section is None:
            self.fail(f"{ref} holds no ## v0.2.0 section")
        level, rest = section.lstrip("\n").split("\n", 1)
        return f"## v0.2.0\n\n{level}\n\n{counts}\n{rest}\nWhat is included, and what was deferred.\n"

    def check(
        self, *arguments: str, notes: str | bytes | None = None, client: GitHubClient | None = None
    ) -> tuple[int, str, str]:
        """--check with a notes file holding the notes given, or those held_notes writes, and a counting client."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "release notes.md"
        text = self.held_notes() if notes is None else notes
        path.write_bytes(text if isinstance(text, bytes) else text.encode())
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = release_notes.main(
                ["v0.2.0", "--check", *arguments, "--notes", str(path)],
                root=self.root,
                client=client or counting_client(),
            )
        return code, out.getvalue(), err.getvalue()

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
                "origin/main, and the release notes open with it and carry the line --counts prints.\n",
                "",
            ),
            self.check(),
        )
        # Notes written on Windows end their lines with CRLF.
        self.assertEqual(0, self.check(notes=self.held_notes().replace("\n", "\r\n"))[0])

    def test_a_pull_request_merged_after_the_section_was_written_is_behind_and_names_its_entry(self) -> None:
        self.released_section()
        self.commit("Fix late (#8)", body=f"## Upgrade note\n\n{LATE_ENTRY}", change="late")
        self.git("update-ref", "refs/remotes/origin/main", "main")
        code, printed, error = self.check()
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
        self.assertEqual(0, self.check()[0])

    def test_a_section_merging_the_entries_that_share_a_heading_into_one_is_current(self) -> None:
        # The release preparer merges the entries a SHARED line names into one entry under their heading.
        self.commit("Older (#2)", body="## Upgrade note\n\n" + SHARED_ENTRY.format(text="Old."), change="older")
        self.commit("Newer (#3)", body="## Upgrade note\n\n" + SHARED_ENTRY.format(text="New."), change="newer")
        merged = SHARED_ENTRY.format(text="Old and new.") + "- Pull request: #2, #3\n"
        section = f"## v0.2.0\n\nLevel: minor.\n\n{merged}\n"
        (self.root / NOTES).write_bytes(f"{INTRO}\n{section}{RELEASED}".encode())
        self.commit("Release v0.2.0 (#7)", body="## Upgrade note\n\nNone\n")
        self.git("update-ref", "refs/remotes/origin/main", "main")
        self.assertEqual(0, self.check()[0])

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
        code, printed, _ = self.check()
        self.assertEqual(1, code)
        self.assertIn("### Merged late", printed)

    def test_it_reads_origin_main_not_the_checkout_and_to_chooses_another_ref(self) -> None:
        self.released_section()
        self.commit("Fix late (#8)", body=f"## Upgrade note\n\n{LATE_ENTRY}", change="late")
        self.assertEqual(0, self.check()[0])
        self.assertEqual(1, self.check("--to", "HEAD")[0])

    def test_a_ref_without_the_section_exits_2_and_check_and_write_are_one_or_the_other(self) -> None:
        self.history()
        self.git("update-ref", "refs/remotes/origin/main", "main")
        self.assertEqual(
            (2, "", f"ERROR: {NOTES} at origin/main has no ## v0.2.0 section; write it with --write first\n"),
            self.check(notes="## v0.2.0\n"),
        )
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(io.StringIO()):
            release_notes.main(["v0.2.0", "--check", "--write"], root=self.root)
        self.assertEqual(2, raised.exception.code)

    def test_a_ref_git_cannot_read_exits_2_with_gits_reason_never_as_a_missing_section(self) -> None:
        self.history()
        code, printed, errors = self.check("--from", "v0.1.0", "--to", "no-such-ref", notes="## v0.2.0\n")
        self.assertEqual((2, ""), (code, printed))
        self.assertTrue(errors.startswith(f"ERROR: git ls-tree -z --name-only no-such-ref -- {NOTES} failed: "), errors)
        self.assertNotIn("has no ## v0.2.0 section", errors)

    def test_check_reads_the_release_notes_and_notes_go_only_with_check(self) -> None:
        for arguments, message in (
            (["v0.2.0", "--check"], "--check reads the release notes too: give --notes FILE"),
            (["v0.2.0", "--notes", "notes.md"], "--notes is read only with --check"),
            (["v0.2.0", "--write", "--notes", "notes.md"], "--notes is read only with --check"),
            (["v0.2.0", "--counts", "--notes", "notes.md"], "--notes is read only with --check"),
        ):
            err = io.StringIO()
            with (
                self.subTest(arguments=arguments),
                contextlib.redirect_stderr(err),
                self.assertRaises(SystemExit) as raised,
            ):
                release_notes.main(arguments, root=self.root, client=counting_client())
            self.assertEqual(2, raised.exception.code)
            self.assertRegex(err.getvalue(), f"error: {re.escape(message)}\n\\Z")

    def test_notes_without_the_line_counts_prints_are_incomplete(self) -> None:
        self.released_section()
        lacking = (
            f"NOTES_INCOMPLETE the release notes' ## v0.2.0 section lacks the line --counts prints: {COUNTS_LINE}\n"
        )
        stale = COUNTS_LINE.replace("Auditor gaps: 0.", "Auditor gaps: 1.")
        for notes in (
            self.held_notes(counts=""),
            self.held_notes(counts=stale),
            self.held_notes(counts=f"{COUNTS_LINE} More."),
            f"{self.held_notes(counts='')}\n## What is included\n\n{COUNTS_LINE}\n",
        ):
            with self.subTest(notes=notes):
                self.assertEqual((1, lacking, ""), self.check(notes=notes))

    def test_the_line_is_held_whatever_its_numbers_so_bugs_with_no_origin_label_do_not_hold_the_tag(self) -> None:
        self.released_section()
        client = counting_client([issue(1, "bug"), issue(2, "bug"), issue(3, "enhancement", "auditor-gap")])
        unlabeled = "Defects fixed: 2 (regression 0, latent 0, hardening 0, no origin label 2). Auditor gaps: 1."
        self.assertEqual(0, self.check(notes=self.held_notes(counts=unlabeled), client=client)[0])
        self.assertEqual(1, self.check(client=client)[0])

    def test_notes_that_do_not_open_with_the_section_or_lack_one_of_its_entries_are_incomplete(self) -> None:
        self.released_section()
        held = self.held_notes()
        lacking = (
            "NOTES_INCOMPLETE the release notes' ## v0.2.0 section lacks the entry ### A skill is added that ## v0.2.0 "
            f"in {NOTES} at origin/main holds\n"
        )
        for notes, expected in (
            (f"Some words first.\n\n{held}", "NOTES_INCOMPLETE the release notes do not open with ## v0.2.0\n"),
            (f"## v0.3.0\n\n{held}", "NOTES_INCOMPLETE the release notes do not open with ## v0.2.0\n"),
            (held.replace("### A skill is added", "### Another heading"), lacking),
            (held.replace("### A skill is added", "A skill is added"), lacking),
        ):
            with self.subTest(notes=notes):
                self.assertEqual((1, expected, ""), self.check(notes=notes))
        # Blank lines before the heading open nothing else.
        self.assertEqual(0, self.check(notes=f"\n\n{held}")[0])

    def test_notes_without_the_level_line_the_section_holds_are_incomplete(self) -> None:
        self.released_section()
        held = self.held_notes()
        lacking = (
            "NOTES_INCOMPLETE the release notes' ## v0.2.0 section lacks the line Level: minor. that ## v0.2.0 in "
            f"{NOTES} at origin/main holds\n"
        )
        for notes in (
            held.replace("Level: minor.\n", ""),
            held.replace("Level: minor.", "Level: patch."),
            held.replace("Level: minor.", "Level: minor. More."),
            held.replace("\nLevel: minor.\n", "\n").replace("What is included", "## Below\n\nLevel: minor.\n\nWhat"),
        ):
            with self.subTest(notes=notes):
                self.assertEqual((1, lacking, ""), self.check(notes=notes))

    def test_the_notes_are_held_to_the_level_line_the_section_holds_and_to_none_when_it_holds_none(self) -> None:
        entry = "### A thing is fixed\n\n- Level: patch. A fix.\n- Contract: none\n- User action: none\n"
        self.commit("Fix a thing (#2)", body=f"## Upgrade note\n\n{entry}", change="thing")
        base = self.git("rev-parse", "HEAD").strip()
        published = f"{entry}- Pull request: #2\n"
        for held_level, notes_level in (
            # A section written before the summary line, or by hand, holds no Level line, so the notes need none.
            ("", ""),
            # Blanks after the held line are not part of it.
            ("Level: patch.  \n\n", "Level: patch.\n\n"),
            # The stated limit: a held line indented by a blank is not read as the section's level.
            (" Level: patch.\n\n", ""),
        ):
            with self.subTest(held_level=held_level):
                self.git("reset", "-q", "--hard", base)
                section = f"## v0.2.0\n\n{held_level}{published}\n"
                (self.root / NOTES).write_bytes(f"{INTRO}\n{section}{RELEASED}".encode())
                self.commit("Release v0.2.0 (#7)", body="## Upgrade note\n\nNone\n")
                self.git("update-ref", "refs/remotes/origin/main", "main")
                notes = f"## v0.2.0\n\n{notes_level}{COUNTS_LINE}\n\n{published}"
                self.assertEqual(0, self.check(notes=notes)[0])

    def test_blanks_around_a_line_and_a_byte_order_mark_are_not_read_as_missing(self) -> None:
        # Windows PowerShell 5's Set-Content -Encoding UTF8 writes a byte order mark before the heading.
        self.released_section()
        held = self.held_notes()
        for notes in (
            f"﻿{held}",
            self.held_notes(counts=f"{COUNTS_LINE}  "),
            self.held_notes(counts=f"\t{COUNTS_LINE}"),
            held.replace("## v0.2.0\n", "## v0.2.0 \n", 1),
            f" \n\t\n{held}",
            held.replace("Level: minor.", "Level: minor.\t"),
        ):
            with self.subTest(notes=notes):
                self.assertEqual(0, self.check(notes=notes)[0])

    def test_notes_that_cannot_be_read_or_counts_gh_cannot_answer_exit_2_never_current(self) -> None:
        self.released_section()
        code, printed, error = self.check(notes=b"## v0.2.0\n\xff\n")
        self.assertEqual((2, ""), (code, printed))
        self.assertTrue(error.startswith('ERROR: cannot read the release notes "'), error)
        failing = GitHubClient(lambda command: CommandResult(1, "", "HTTP 404: Not Found"), sleeper=lambda _: None)
        code, printed, error = self.check(client=failing)
        self.assertEqual((2, ""), (code, printed))
        self.assertTrue(error.startswith(f"ERROR: cannot read {MILESTONES[2]}: "), error)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = release_notes.main(
                ["v0.2.0", "--check", "--notes", str(self.root / "no such notes.md")],
                root=self.root,
                client=counting_client(),
            )
        self.assertEqual((2, ""), (code, out.getvalue()))
        self.assertTrue(err.getvalue().startswith('ERROR: cannot read the release notes "'), err.getvalue())

    def test_a_section_that_is_behind_is_reported_before_the_notes_are_read(self) -> None:
        self.released_section()
        self.commit("Fix late (#8)", body=f"## Upgrade note\n\n{LATE_ENTRY}", change="late")
        self.git("update-ref", "refs/remotes/origin/main", "main")
        failing = GitHubClient(lambda command: CommandResult(1, "", "unreachable"), sleeper=lambda _: None)
        code, printed, error = self.check(notes=b"\xff", client=failing)
        self.assertEqual((1, ""), (code, error))
        self.assertTrue(printed.startswith("BEHIND 1 entry: "), printed)


class GitOutputTests(unittest.TestCase):
    """A git command that fails is named by git's reason on the error's one line, or by its exit code when it printed
    nothing."""

    def test_a_failed_git_is_named_by_its_reason_on_one_line_or_its_exit_code(self) -> None:
        for stderr, reason in (
            ("fatal: bad revision 'x'\nhint: fetch it\n", "fatal: bad revision 'x' hint: fetch it"),
            ("", "exit code 128"),
            (" \n", "exit code 128"),
        ):
            failed = git_client.GitResult(128, "", stderr)
            with self.subTest(reason=reason), mock.patch.object(release_notes, "git", return_value=failed):
                with self.assertRaises(release_notes.ReleaseNotesError) as raised:
                    release_notes.git_output(Path("checkout"), "log", "x")
                self.assertEqual(f"git log x failed: {reason}", str(raised.exception))


class InsertSectionTests(unittest.TestCase):
    def test_a_version_heading_indented_or_with_a_tab_after_its_marks_is_a_section(self) -> None:
        # changed: the version heading's own pattern read neither, where CommonMark reads both as a heading.
        for written in (" ## v0.1.0", "##\tv0.1.0"):
            with self.subTest(written=written):
                notes = f"{INTRO}\n{written}\n\nOld.\n"
                self.assertEqual(
                    f"{INTRO}\n## v0.2.0\n\n{written}\n\nOld.\n",
                    release_notes.insert_section(notes, "v0.2.0", "## v0.2.0\n"),
                )

    def test_entries_are_cut_at_each_heading_the_text_holds_itself(self) -> None:
        # changed: an entry's heading indented or with a tab after its marks did not start one; a heading a list item
        # or a code block holds still does not.
        text = "intro\n ### Indented\nx\n###\tTabbed\ny\n- item\n  ### Held\n```\n### Fenced\n```"
        self.assertEqual(
            ["intro", " ### Indented\nx", "###\tTabbed\ny\n- item\n  ### Held\n```\n### Fenced\n```"],
            release_notes.notes_blocks(text),
        )

    def test_a_file_with_no_version_gets_the_section_at_its_end(self) -> None:
        self.assertEqual(f"{INTRO}\n## v0.1.0\n", release_notes.insert_section(INTRO, "v0.1.0", "## v0.1.0\n"))

    def test_a_heading_in_other_unicode_digits_is_no_versions_section(self) -> None:
        # Arabic-Indic three is a digit to `\d` but not to release_tags, so the section goes at the end, not above it.
        notes = f"{INTRO}\n## v\u0663.0.0\n"
        self.assertEqual(f"{notes}\n## v0.1.0\n", release_notes.insert_section(notes, "v0.1.0", "## v0.1.0\n"))

    def test_a_version_heading_inside_a_fence_is_neither_a_section_nor_the_place_to_insert(self) -> None:
        fenced = f"{INTRO}\n```text\n## v0.2.0\n## v0.1.0\n```\n\n"
        notes = f"{fenced}## v0.1.0\n\nOld.\n"
        self.assertEqual(
            f"{fenced}## v0.2.0\n\n## v0.1.0\n\nOld.\n", release_notes.insert_section(notes, "v0.2.0", "## v0.2.0\n")
        )
        self.assertEqual(
            f"{fenced.rstrip()}\n\n## v0.1.0\n", release_notes.insert_section(fenced, "v0.1.0", "## v0.1.0\n")
        )
        with self.assertRaises(release_notes.ReleaseNotesError):
            release_notes.insert_section(notes, "v0.1.0", "## v0.1.0\n")

    def test_a_version_heading_after_a_lone_cr_is_found_and_each_break_is_kept(self) -> None:
        # CommonMark ends a line at a lone CR, so the heading is a section, and the new one goes above it, the text
        # around it unchanged.
        notes = "# Upgrade notes\r\rIntro.\r\n## v0.1.0\r\rOld.\r"
        self.assertEqual(
            "# Upgrade notes\r\rIntro.\r\n## v0.2.0\n## v0.1.0\r\rOld.\r",
            release_notes.insert_section(notes, "v0.2.0", "## v0.2.0"),
        )
        with self.assertRaises(release_notes.ReleaseNotesError):
            release_notes.insert_section(notes, "v0.1.0", "## v0.1.0\n")


class LoneCrNotesTests(unittest.TestCase):
    def test_entries_and_their_fields_are_read_where_commonmark_ends_a_line(self) -> None:
        notes = "## Unreleased\r\r### First\r\r- Level: patch.\r- Contract: none\r### Second\r\r- Level: minor."
        entries = release_notes.notes_entries(notes)
        self.assertEqual(["First", "Second"], [entry.heading for entry in entries])
        self.assertEqual(["patch.", "minor."], [entry.fields["Level"] for entry in entries])

    def test_a_wrapped_line_is_joined_across_a_lone_cr(self) -> None:
        self.assertEqual(["Some text more", "", "Next"], release_notes.unwrapped("Some text\rmore\r\rNext"))


class ReleaseProcedureTests(unittest.TestCase):
    def test_the_release_procedure_writes_the_section_before_tagging(self) -> None:
        root = Path(__file__).resolve().parents[2]
        text = (root / "docs" / "releasing.md").read_text(encoding="utf-8")
        before_tagging = text.split("## Before tagging", 1)[1].split("\n## ", 1)[0]
        self.assertIn("python tools/release_notes.py vX.Y.Z --write", before_tagging)
        # The tag waits for the check that the committed section holds every entry main carries.
        tagging = text.split("\n## Tagging", 1)[1].split("\n## ", 1)[0]
        self.assertIn("python tools/release_notes.py vX.Y.Z --check --notes '<file>'", tagging)
        # The notes carry the line --counts prints, and the check the tag waits on holds it.
        self.assertIn("python tools/release_notes.py vX.Y.Z --counts", tagging)
        self.assertLess(
            tagging.index("python tools/release_notes.py vX.Y.Z --check --notes '<file>'"),
            tagging.index("gh release create vX.Y.Z"),
        )
        # The template carries the heading the script reads, with its example in a comment no reader takes for one.
        template = (root / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
        self.assertIn("\n## Upgrade note\n", template)
        self.assertEqual([], release_notes.body_entries(template))


class GitProgramTests(unittest.TestCase):
    def test_git_runs_through_skill_cores_client_on_the_checkout_never_by_name(self) -> None:
        # The client's runner starts git at the path the locator finds and without the repository-selecting
        # variables; ejryan89-skill-core's suites hold both.
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


MILESTONES = ["gh", "api", "repos/{owner}/{repo}/milestones?state=all&per_page=100", "--paginate", "--slurp"]
ISSUES = ["gh", "api", "repos/{owner}/{repo}/issues?milestone=7&state=all&per_page=100", "--paginate", "--slurp"]


def issue(number: int, *labels: str, reason: str | None = "completed") -> dict[str, object]:
    """An issue as GitHub's REST listing gives it; reason "open" makes an open one, whose close reason is null."""
    state = "open" if reason == "open" else "closed"
    return {
        "number": number,
        "labels": [{"name": label} for label in labels],
        "state": state,
        "state_reason": None if reason == "open" else reason,
    }


def pull_request(number: int) -> dict[str, object]:
    return {"number": number, "labels": [], "state": "closed", "state_reason": None, "pull_request": {"url": "u"}}


def milestone(title: str, number: int, opened: int, closed: int) -> dict[str, object]:
    return {"title": title, "number": number, "open_issues": opened, "closed_issues": closed}


class CountsTests(unittest.TestCase):
    """--counts reads the release milestone and its issues through REST and prints the line the release notes carry."""

    def counts(
        self,
        items: Sequence[Mapping[str, object]],
        milestones: Sequence[object] | None = None,
        pages: int = 1,
        again: Sequence[Mapping[str, object]] | None = None,
    ) -> tuple[int, str, str, list[list[str]]]:
        """Run --counts on a milestone whose listing gives items, split into pages, on its first read, and again, or
        the same items, on its second."""
        held = len(items)
        if milestones is None:
            milestones = [milestone("v0.1.0", 6, 0, 3), milestone("v0.2.0", 7, 1, held - 1)]

        def paged(listed: Sequence[Mapping[str, object]]) -> str:
            size = -(-len(listed) // pages) or 1
            return json.dumps([listed[start : start + size] for start in range(0, max(len(listed), 1), size)])

        readings = [paged(items), paged(items if again is None else again)]
        asked: list[list[str]] = []

        def runner(command: Sequence[str]) -> CommandResult:
            asked.append(list(command))
            if tuple(command) == tuple(MILESTONES):
                return CommandResult(0, json.dumps([milestones]), "")
            self.assertEqual(ISSUES, list(command))
            return CommandResult(0, readings.pop(0), "")

        out, err = io.StringIO(), io.StringIO()
        client = GitHubClient(runner, sleeper=lambda _: None)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = release_notes.main(["v0.2.0", "--counts"], root=Path("unread"), client=client)
        return code, out.getvalue(), err.getvalue(), asked

    def test_it_counts_the_fixed_bugs_by_origin_label_and_the_auditor_gaps_on_the_milestone(self) -> None:
        items = [
            issue(1, "bug", "regression"),
            issue(2, "bug", "regression"),
            issue(3, "bug", "latent"),
            issue(4, "bug", "hardening"),
            issue(5, "bug"),
            issue(6, "enhancement", "auditor-gap"),
            issue(7, "enhancement"),
            issue(8, "bug", "latent", reason="not_planned"),
            issue(9, "enhancement", "auditor-gap", reason="duplicate"),
            issue(10, "documentation"),
            issue(11, "enhancement", "auditor-gap", reason="open"),
            issue(12, "bug", "regression", reason="open"),
            issue(13, "bug", "latent", reason=None),
            issue(14, "question", reason="not_planned"),
            issue(15, reason="not_planned"),
            pull_request(16),
        ]
        code, printed, error, asked = self.counts(items, pages=3)
        expected = "Defects fixed: 6 (regression 2, latent 2, hardening 1, no origin label 1). Auditor gaps: 2.\n"
        self.assertEqual((0, expected, ""), (code, printed, error))
        self.assertEqual([MILESTONES, ISSUES, ISSUES], asked)

    def test_a_milestone_with_no_issue_counts_zero(self) -> None:
        self.assertEqual(
            (0, "Defects fixed: 0 (regression 0, latent 0, hardening 0, no origin label 0). Auditor gaps: 0.\n", ""),
            self.counts([], milestones=[milestone("v0.2.0", 7, 0, 0)])[:3],
        )

    def test_a_title_no_milestone_has_is_refused_rather_than_counted_as_zero(self) -> None:
        # gh issue list --milestone searches, and search answers [] for an unknown title.
        for milestones in (
            [milestone("v0.1.0", 6, 0, 3)],
            [],
            [milestone("v0.2.0", 7, 0, 0), milestone("v0.2.0", 8, 0, 0)],
        ):
            with self.subTest(milestones=milestones):
                code, printed, error, _ = self.counts([], milestones=milestones)
                self.assertEqual((2, ""), (code, printed))
                self.assertRegex(error, r"^ERROR: [0-9]+ milestones are titled v0\.2\.0; --counts reads exactly one\n$")

    def test_a_listing_that_disagrees_with_the_milestone_s_own_counts_is_refused(self) -> None:
        # A search-backed listing stops at its limit; the milestone's open and closed counts, pull requests included,
        # say how many the listing must hold.
        for opened, closed in ((0, 3), (1, 0)):
            with self.subTest(opened=opened, closed=closed):
                code, printed, error, _ = self.counts(
                    [issue(1, "bug"), pull_request(2)], milestones=[milestone("v0.2.0", 7, opened, closed)]
                )
                self.assertEqual((2, ""), (code, printed))
                self.assertEqual(
                    f"ERROR: milestone v0.2.0 holds {opened + closed} issues and pull requests, but its listing "
                    "gave 2; count them by hand\n",
                    error,
                )

    def test_a_listing_that_repeats_a_number_is_refused_even_when_the_totals_hold(self) -> None:
        # The milestone holds a latent bug and a regression; an issue joining or leaving during the paginated read can
        # shift a page, so the latent bug comes on both pages and the regression on neither, and the totals agree.
        for items, repeated in (
            ([issue(3, "bug", "latent"), issue(3, "bug", "latent")], "#3"),
            ([issue(3, "bug", "latent"), pull_request(5), issue(4, "bug", "regression"), pull_request(5)], "#5"),
            ([issue(6, "enhancement"), issue(3, "bug"), issue(6, "enhancement"), issue(3, "bug")], "#3, #6"),
        ):
            with self.subTest(items=items):
                code, printed, error, _ = self.counts(items, pages=2)
                self.assertEqual((2, ""), (code, printed))
                self.assertEqual(
                    f"ERROR: the listing of milestone v0.2.0 gives {repeated} more than once, so an issue moved on "
                    "or off the milestone while gh read it; run it again once none is moving\n",
                    error,
                )

    def test_a_repeated_number_is_named_before_totals_that_disagree(self) -> None:
        # A repeat is the more precise cause, so it is named even when the totals also disagree.
        items = [issue(3, "bug", "latent"), issue(3, "bug", "latent")]
        code, printed, error, _ = self.counts(items, milestones=[milestone("v0.2.0", 7, 0, 3)], pages=2)
        self.assertEqual((2, ""), (code, printed))
        self.assertTrue(error.startswith("ERROR: the listing of milestone v0.2.0 gives #3 more than once"), error)

    def test_a_shift_that_drops_a_row_without_a_repeat_is_refused_by_a_second_read(self) -> None:
        # Newest first, two to a page: the milestone holds issues ten, nine (latent), eight (regression), and seven.
        # Page 1 reads ten and nine; then ten leaves and an old issue one joins, so page 2 reads seven and one. No
        # number repeats, the totals hold, and the regression was never read; the second read, of the milestone as it
        # now is, gives eight and not ten.
        first = [issue(10, "enhancement"), issue(9, "bug", "latent"), issue(7, "enhancement"), issue(1, "question")]
        settled = [issue(9, "bug", "latent"), issue(8, "bug", "regression"), issue(7, "enhancement"), first[3]]
        for items, again, differ in ((first, settled, "#8, #10"), (settled, first, "#8, #10")):
            with self.subTest(differ=differ, first=[item["number"] for item in items]):
                code, printed, error, asked = self.counts(items, pages=2, again=again)
                self.assertEqual((2, ""), (code, printed))
                self.assertEqual(
                    f"ERROR: two listings of milestone v0.2.0 differ in {differ}, so an issue moved on or off the "
                    "milestone while gh read it; run it again once none is moving\n",
                    error,
                )
                self.assertEqual([MILESTONES, ISSUES, ISSUES], asked)

    def test_two_reads_that_give_the_same_numbers_in_another_order_are_counted(self) -> None:
        items = [issue(2, "bug", "regression"), issue(1, "bug", "latent")]
        code, printed, error, _ = self.counts(items, pages=2, again=list(reversed(items)))
        self.assertEqual((0, ""), (code, error))
        self.assertTrue(printed.startswith("Defects fixed: 2 (regression 1, latent 1, "), printed)

    def test_an_item_whose_number_is_not_an_integer_is_refused_rather_than_left_out_of_the_repeats(self) -> None:
        # Repeats and the second read are judged by number, so an item with none would escape both.
        for number in ("5", 5.0, True, None):
            for item in (pull_request(5) | {"number": number}, issue(5, "bug") | {"number": number}):
                with self.subTest(number=number, pull_request="pull_request" in item):
                    code, printed, error, _ = self.counts([item, item], pages=2)
                    self.assertEqual((2, ""), (code, printed))
                    self.assertEqual(
                        f"ERROR: gh answered with an item of milestone v0.2.0 that has no number: {item!r}\n", error
                    )

    def test_a_fixed_issue_whose_labels_cannot_be_counted_is_refused_by_number(self) -> None:
        items = [
            issue(4, "bug", "latent", "regression"),
            issue(5, "enhancement", "hardening"),
            issue(6),
            issue(7, "auditor-gap"),
            issue(8, "bug", "hardening", "latent"),
            issue(9, reason="not_planned"),
            issue(10, reason="open"),
        ]
        code, printed, error, _ = self.counts(items)
        self.assertEqual((2, ""), (code, printed))
        self.assertEqual(
            "ERROR: closed issues on milestone v0.2.0 carry no kind label: #6, #7; "
            "closed issues on milestone v0.2.0 carry an origin label without `bug`: #5; "
            "closed issues on milestone v0.2.0 carry more than one origin label: #4, #8\n",
            error,
        )

    def test_gh_that_fails_is_an_error_line(self) -> None:
        def runner(command: Sequence[str]) -> CommandResult:
            return CommandResult(1, "", "HTTP 404: Not Found")

        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(err):
            code = release_notes.main(["v0.2.0", "--counts"], root=Path("unread"), client=GitHubClient(runner))
        self.assertEqual((2, ""), (code, out.getvalue()))
        self.assertTrue(err.getvalue().startswith(f"ERROR: cannot read {MILESTONES[2]}: "), err.getvalue())

    def test_an_answer_that_is_not_milestones_or_issues_is_an_error_line(self) -> None:
        for milestones, items in (
            ([{"title": "v0.2.0", "number": "7", "open_issues": 0, "closed_issues": 1}], [issue(1, "bug")]),
            ([{"title": "v0.2.0", "number": 7, "open_issues": True, "closed_issues": 1}], [issue(1, "bug")]),
            (None, [{"number": 1}]),
            (None, [{"number": 1, "labels": [{}], "state": "closed", "state_reason": "completed"}]),
            (None, [{"number": 1, "labels": [], "state": "merged", "state_reason": None}]),
            (None, [{"number": 1, "labels": [], "state": "closed", "state_reason": "solved"}]),
        ):
            with self.subTest(milestones=milestones, items=items):
                code, printed, error, _ = self.counts(items, milestones=milestones)
                self.assertEqual((2, ""), (code, printed))
                self.assertTrue(error.startswith("ERROR: gh answered with "), error)

    def test_each_kind_label_lets_a_fixed_issue_be_counted(self) -> None:
        for kind in ("bug", "enhancement", "documentation", "question"):
            with self.subTest(kind=kind):
                code, printed, error, _ = self.counts([issue(1, kind)])
                self.assertEqual((0, ""), (code, error))
                self.assertTrue(printed.startswith(f"Defects fixed: {int(kind == 'bug')} "), printed)

    def test_an_open_gap_counts_and_a_gap_closed_as_not_planned_does_not(self) -> None:
        reopened = {"number": 3, "labels": [{"name": "auditor-gap"}], "state": "open", "state_reason": "reopened"}
        for items, gaps in (
            ([issue(1, "enhancement", "auditor-gap", reason="open")], 1),
            ([reopened], 1),
            ([issue(2, "enhancement", "auditor-gap", reason="not_planned")], 0),
        ):
            with self.subTest(items=items):
                code, printed, error, _ = self.counts(items)
                self.assertEqual((0, ""), (code, error))
                self.assertTrue(printed.endswith(f"Auditor gaps: {gaps}.\n"), printed)

    def test_a_label_that_is_not_an_object_is_an_error_line(self) -> None:
        item = {"number": 1, "labels": ["bug"], "state": "closed", "state_reason": "completed"}
        code, printed, error, _ = self.counts([item])
        self.assertEqual((2, ""), (code, printed))
        self.assertTrue(error.startswith("ERROR: gh answered with an issue that is not "), error)

    def test_a_listing_item_that_is_not_an_object_is_an_error_line(self) -> None:
        for item in ("issue 2", 2, None, [issue(2, "bug")]):
            with self.subTest(item=item):
                items = [issue(1, "bug"), item]
                code, printed, error, _ = self.counts(items)  # type: ignore[arg-type]  # a malformed answer
                self.assertEqual((2, ""), (code, printed))
                self.assertEqual("ERROR: gh answered with an item of milestone v0.2.0 that is not an object\n", error)

    def test_pages_that_are_not_arrays_are_an_error_line(self) -> None:
        for answer in ('{"message": "x"}', '[{"message": "x"}]', '[[], {"message": "x"}]'):
            with self.subTest(answer=answer):
                self.assert_not_pages(answer)

    def assert_not_pages(self, answer: str) -> None:
        def runner(command: Sequence[str]) -> CommandResult:
            return CommandResult(0, answer, "")

        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = release_notes.main(["v0.2.0", "--counts"], root=Path("unread"), client=GitHubClient(runner))
        self.assertEqual(2, code)
        self.assertEqual(
            f"ERROR: gh api {MILESTONES[2]} answered with something that is not pages of items\n", err.getvalue()
        )

    def test_the_default_client_runs_gh_in_the_checkout_it_counts(self) -> None:
        # gh reads the repository from its working directory, so the client --counts builds must run it in root.
        places: list[Path | None] = []

        def runner(arguments: Sequence[str], *, timeout: float = 0, cwd: Path | None = None) -> CommandResult:
            places.append(cwd)
            answer = [[milestone("v0.2.0", 7, 0, 0)]] if "milestones" in arguments[2] else [[]]
            return CommandResult(0, json.dumps(answer), "")

        root = Path("the checkout")
        with mock.patch.object(release_notes, "subprocess_runner", runner), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, release_notes.main(["v0.2.0", "--counts"], root=root))
        self.assertEqual([root, root, root], places)

    def test_counts_takes_no_range_and_no_other_action(self) -> None:
        for arguments in (("--from", "v0.1.0"), ("--to", "HEAD"), ("--write",), ("--check",)):
            err = io.StringIO()
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as raised:
                    release_notes.main(["v0.2.0", "--counts", *arguments], root=Path("unread"))
                self.assertEqual(2, raised.exception.code)
                if arguments[0] in ("--from", "--to"):
                    self.assertRegex(
                        err.getvalue(), r"error: --counts reads the milestone, not a range: drop --from and --to\n\Z"
                    )
