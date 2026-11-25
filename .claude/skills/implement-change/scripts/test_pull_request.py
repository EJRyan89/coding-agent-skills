from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import override
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "ejryan89-skill-core" / "scripts"))
# The git fixture template and the folder removal the repository-skill and tools suites share.
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tests" / "tools"))

import pull_request
import whole_number
from fixture_template import FixtureTemplate, remove_tree, repository_shape
from git_client import GitClient, GitError, GitResult, subprocess_runner
from github_client import CommandResult, GitHubClient
from test_time_in_proportion import assert_time_in_proportion
from type_check_roots import every_job_reasons, file_type_check_jobs, imported_folders, strict_type_check_areas

REPOSITORY = Path(__file__).resolve().parents[4]
GIT_CONFIG: Mapping[str, str] = MappingProxyType({"init.defaultBranch": "main"})
TEMPLATE = (
    "<!-- Name each issue in a Closes #N line of its own. -->\n"
    "Closes #\n"
    "\n"
    "<!-- The problem, in a paragraph. -->\n"
    "\n"
    "## Changes\n"
    "\n"
    "-\n"
    "\n"
    "## Upgrade note\n"
    "\n"
    "<!-- One entry per change.\n\n### What changed\n\n- Level: patch\n-->\n"
    "\n"
    "None\n"
)
FILLED = (
    "Closes #12\n"
    "Closes #34\n"
    "\n"
    "Fixes the quoting of a title.\n"
    "\n"
    "## Changes\n"
    "\n"
    "- `tool.py`: reads the title from a file.\n"
    "\n"
    "## Upgrade note\n"
    "\n"
    "None\n"
)
# The fixture template with a `## Forms considered` section, its guidance in a comment and its placeholder `None`.
FORMS_TEMPLATE = TEMPLATE.replace(
    "## Upgrade note\n",
    "## Forms considered\n\n<!-- For a fix, replace None with the forms the fix weighed. -->\n\nNone\n\n"
    "## Upgrade note\n",
)
FORMS_WEIGHED = "The construct is a title's quoting; each shell's quoting was read, and `test_title` fails reverted."


def with_forms(body: str, forms: str) -> str:
    """`body` with a `## Forms considered` section holding `forms` before its upgrade note."""
    return body.replace("## Upgrade note\n", f"## Forms considered\n\n{forms}\n\n## Upgrade note\n")


# A title the title rules allow, holding each character either shell could misread.
AWKWARD_TITLE = (
    'fix(skills): grant each skill\'s scripts by name, keep `$HOME` and "$(x)", and \u2018quote\u2019 \u201atwice\u201b'
)


def git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(directory), *arguments], capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(arguments)} failed: {result.stderr}")
    return result.stdout.strip()


def printing(stdout: str, command: Sequence[str], timeout: float) -> GitResult:
    """A git that prints `stdout` and succeeds, whatever it is asked."""
    return GitResult(0, stdout, "")


class DraftTests(unittest.TestCase):
    def test_each_issue_gets_its_own_closes_line_in_place_of_the_bare_one(self) -> None:
        body = pull_request.draft_body(TEMPLATE, [12, 34])
        self.assertIn("-->\nCloses #12\nCloses #34\n\n<!-- The problem", body)
        self.assertNotIn("Closes #\n", body)
        self.assertIn("## Changes\n", body)
        self.assertIn("## Upgrade note\n", body)

    def test_no_issue_removes_the_bare_closes_line(self) -> None:
        body = pull_request.draft_body(TEMPLATE, [])
        self.assertNotRegex(body, r"(?m)^Closes")
        self.assertIn("## Changes\n", body)

    def test_a_comment_holding_a_line_separator_or_a_lone_cr_keeps_the_closes_line_in_its_place(self) -> None:
        # Markdown ends a line at a lone CR and at no line separator, and the comment's blanked copy keeps both, so
        # the template and its blanked copy hold the same lines.
        for inside in ("\u2028", "\r"):
            with self.subTest(inside=repr(inside)):
                template = f"<!-- one{inside}two -->\nCloses #\n## Changes\n"
                self.assertEqual(
                    f"<!-- one{inside}two -->\nCloses #5\n## Changes\n", pull_request.draft_body(template, [5])
                )

    def test_a_template_with_no_closes_line_gets_them_at_the_top(self) -> None:
        self.assertEqual("Closes #7\n\n## Changes\n", pull_request.draft_body("## Changes\n", [7]))

    def test_this_repository_s_template_drafts_a_body_the_check_still_refuses(self) -> None:
        template = (REPOSITORY / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
        body = pull_request.draft_body(template, [1166])
        self.assertRegex(body, r"(?m)^Closes #1166$")
        problems = pull_request.body_problems(body, template)
        self.assertTrue(any(line.startswith("PLACEHOLDER ") and "empty list item" in line for line in problems))
        self.assertTrue(any("template comment" in line for line in problems))


class CommentBlankingTests(unittest.TestCase):
    """The bytes blank_comments writes, pinned against literals, so a reading that blanks comments another way fails."""

    def test_each_character_of_a_hidden_comment_but_its_line_breaks_becomes_a_space(self) -> None:
        self.assertEqual("a       \r\n      d\n", pull_request.blank_comments("a <!-- b\r\nc --> d\n"))
        self.assertEqual("          \nCloses #5\n", pull_request.blank_comments("<!-- x -->\nCloses #5\n"))
        self.assertEqual(
            "one\r    \n \r       \nthree\n", pull_request.blank_comments("one\r<!--\n-\rtwo -->\nthree\n")
        )

    def test_a_comment_marker_code_shows_is_read_as_github_renders_it(self) -> None:
        # GitHub (`gh api markdown`) renders each `<!--` and `-->` here inside a code span or a fence, as text, so the
        # closing line, the empty list item, and the template's comment shown in a fence are the body's own text.
        body = FILLED.replace(
            "Fixes the quoting of a title.", "Fixes `<!--` in one place and\nCloses #5 in `-->` another."
        )
        self.assertEqual(["CLOSES_LINE 5 Closes #5 in `-->` another."], pull_request.body_problems(body, TEMPLATE))
        body = FILLED.replace("- `tool.py`", "```\n<!--\n```\n-\n```\n-->\n```\n- `tool.py`")
        self.assertEqual(["PLACEHOLDER 11 an empty list item"], pull_request.body_problems(body, TEMPLATE))
        comment = "<!-- One entry per change.\n\n### What changed\n\n- Level: patch\n-->"
        body = FILLED.replace("None\n", f"```text\n{comment}\n```\n\nNone\n")
        self.assertEqual([], pull_request.body_problems(body, TEMPLATE))

    def test_an_unclosed_opener_in_a_paragraph_blanks_nothing(self) -> None:
        # GitHub (`gh api markdown`) shows a `<!--` no `-->` follows inside a paragraph as text, at the body's end too.
        self.assertEqual(
            "Fixes a <!-- in prose\nCloses #5 too", pull_request.blank_comments("Fixes a <!-- in prose\nCloses #5 too")
        )
        body = FILLED + "\nFixes a <!-- in prose\nCloses #5 too"
        self.assertEqual(["CLOSES_LINE 15 Closes #5 too"], pull_request.body_problems(body, TEMPLATE))

    def test_a_body_eight_times_as_long_is_blanked_in_about_eight_times_the_time(self) -> None:
        # A linear reading takes about eight times as long and a quadratic one about 64 times; the bound sits halfway.
        unit = "Text `<!--` x <!-- c --> y\n- `-->`\n"
        assert_time_in_proportion(pull_request.blank_comments, unit * 100, unit * 800, 8**1.5)

    def test_a_heading_a_hidden_comment_holds_is_not_read(self) -> None:
        self.assertEqual(["## A", "## C"], pull_request.headings("## A\n<!-- ## B -->\n## C\n"))


class BodyCheckTests(unittest.TestCase):
    def test_a_filled_body_has_no_problem_and_names_its_issues(self) -> None:
        self.assertEqual([], pull_request.body_problems(FILLED, TEMPLATE))
        self.assertEqual([12, 34], pull_request.closed_issues(FILLED))

    def test_each_leftover_placeholder_is_named_with_its_line(self) -> None:
        body = FILLED.replace("Closes #34\n", "Closes #\n").replace("- `tool.py`", "-\n- `tool.py`")
        self.assertEqual(
            ["PLACEHOLDER 2 a Closes line with no issue", "PLACEHOLDER 8 an empty list item"],
            pull_request.body_problems(body, TEMPLATE),
        )

    def test_an_empty_list_item_is_read_as_markdown_reads_one(self) -> None:
        # A no-break space after a dash, or an Arabic-Indic digit, opens no list item, so the line is text; a tab
        # and a carriage return around a marker leave it empty.
        for text in ("-\u00a0", "\u0661.", "-1.", "1234567890."):
            body = FILLED.replace("- `tool.py`", f"{text}\n- `tool.py`")
            self.assertEqual([], pull_request.body_problems(body, TEMPLATE), text)
        body = FILLED.replace("- `tool.py`", "\t2)\t\r\n- `tool.py`")
        self.assertEqual(["PLACEHOLDER 8 an empty list item"], pull_request.body_problems(body, TEMPLATE))

    def test_a_placeholder_is_numbered_where_markdown_ends_a_line(self) -> None:
        # A body saved with lone CRs: each line is read on its own, and numbered as an editor numbers it.
        body = FILLED.replace("Closes #34\n", "Closes #\n").replace("\n", "\r")
        self.assertEqual(["PLACEHOLDER 2 a Closes line with no issue"], pull_request.body_problems(body, TEMPLATE))
        comment = "<!-- One entry per change.\n\n### What changed\n\n- Level: patch\n-->"
        body = FILLED.replace("\n", "\r").replace("None\r", f"{comment}\rNone\r")
        self.assertEqual(["PLACEHOLDER 12 a template comment"], pull_request.body_problems(body, TEMPLATE))

    def test_each_template_comment_is_numbered_alike_at_lf_crlf_and_a_lone_cr(self) -> None:
        # Pinned before the check took its line starts from text_lines.line_starts, and unchanged by it.
        comment = "<!-- One entry per change.\n\n### What changed\n\n- Level: patch\n-->"
        body = FILLED.replace("None\n", f"{comment}\nNone\n").replace(
            "Fixes the quoting of a title.", "<!-- The problem, in a paragraph. -->"
        )
        for line_break in ("\n", "\r\n", "\r"):
            with self.subTest(line_break=line_break):
                self.assertEqual(
                    ["PLACEHOLDER 4 a template comment", "PLACEHOLDER 12 a template comment"],
                    pull_request.body_problems(body.replace("\n", line_break), TEMPLATE.replace("\n", line_break)),
                )

    def test_a_body_of_template_comments_eight_times_as_long_is_checked_in_about_eight_times_the_time(self) -> None:
        # A linear check takes about eight times as long. Numbering each comment's line from the body's start is
        # quadratic, and beside the check's linear work took about 38 times as long; the bound, about 23, sits between.
        unit = "<!-- The problem, in a paragraph. -->\n"
        check = partial(pull_request.body_problems, template=TEMPLATE)
        assert_time_in_proportion(check, unit * 800, unit * 6400, 8**1.5)

    def test_a_template_comment_holding_a_lone_cr_keeps_the_body_s_lines(self) -> None:
        template = "<!-- one\rtwo -->\nCloses #\n## Changes\n"
        body = "<!-- one\rtwo -->\nCloses #5\n## Changes\n"
        self.assertEqual(["PLACEHOLDER 1 a template comment"], pull_request.body_problems(body, template))

    def test_a_template_comment_left_in_the_body_is_named(self) -> None:
        body = FILLED.replace("None\n", "<!-- One entry per change.\n\n### What changed\n\n- Level: patch\n-->\nNone\n")
        self.assertEqual(["PLACEHOLDER 12 a template comment"], pull_request.body_problems(body, TEMPLATE))

    def test_an_author_s_own_comment_is_not_a_placeholder(self) -> None:
        body = FILLED.replace("None\n", "<!-- my note -->\nNone\n")
        self.assertEqual([], pull_request.body_problems(body, TEMPLATE))

    def test_prose_that_starts_with_a_closing_word_is_not_a_closes_line(self) -> None:
        body = FILLED.replace("Fixes the quoting of a title.", "Fixes the quoting that #1162 found.")
        self.assertEqual([], pull_request.body_problems(body, TEMPLATE))

    def test_a_placeholder_inside_an_author_s_comment_is_ignored(self) -> None:
        body = FILLED.replace("None\n", "<!--\n-\nCloses #\n-->\nNone\n")
        self.assertEqual([], pull_request.body_problems(body, TEMPLATE))

    def test_each_template_heading_the_body_drops_is_named(self) -> None:
        body = FILLED.replace("## Upgrade note\n", "## Upgrade notes\n")
        self.assertEqual(["HEADING_MISSING ## Upgrade note"], pull_request.body_problems(body, TEMPLATE))

    def test_a_heading_only_inside_a_template_comment_is_not_required(self) -> None:
        self.assertNotIn("### What changed", " ".join(pull_request.body_problems(FILLED, TEMPLATE)))


class FormsCheckTests(unittest.TestCase):
    """A fix's `## Forms considered` section, read by the reader validation's body check holds a fix body to."""

    template = (REPOSITORY / ".github" / "pull_request_template.md").read_text(encoding="utf-8")

    def forms_lines(self, title: str, forms: str | None) -> list[str]:
        body = FILLED if forms is None else with_forms(FILLED, forms)
        return pull_request.forms_lines(title, body, self.template)

    def test_a_fix_body_still_holding_the_template_s_none_is_refused(self) -> None:
        for title in ("fix: read the title", "fix(skills)!: read the title", "Fix: read the title"):
            with self.subTest(title=title):
                self.assertEqual(["FORMS_CONSIDERED placeholder"], self.forms_lines(title, "None"))

    def test_a_drafted_fix_body_left_as_the_template_wrote_it_is_refused(self) -> None:
        body = pull_request.draft_body(self.template, [2500])
        self.assertEqual(["FORMS_CONSIDERED placeholder"], pull_request.forms_lines("fix: x", body, self.template))

    def test_a_fix_body_with_no_section_or_only_a_filler_is_refused(self) -> None:
        self.assertEqual(["FORMS_CONSIDERED missing"], self.forms_lines("fix: x", None))
        self.assertEqual(["FORMS_CONSIDERED empty"], self.forms_lines("fix: x", "TBD"))

    def test_a_fix_body_with_its_own_forms_passes(self) -> None:
        self.assertEqual([], self.forms_lines("fix: read the title", FORMS_WEIGHED))

    def test_a_body_that_is_not_a_fix_s_is_left_alone(self) -> None:
        for title in ("docs: say what a fix owes", "feat: add a check", "fixes: read the title", "a fix: no type"):
            with self.subTest(title=title):
                self.assertEqual([], self.forms_lines(title, "None"))
                self.assertEqual([], self.forms_lines(title, None))

    def test_a_template_with_no_forms_section_asks_none_of_a_fix(self) -> None:
        self.assertEqual([], pull_request.forms_lines("fix: x", FILLED, TEMPLATE))


class TitleCheckTests(unittest.TestCase):
    def test_one_line_is_the_title_with_its_line_break_dropped(self) -> None:
        self.assertEqual((AWKWARD_TITLE, []), pull_request.read_title(AWKWARD_TITLE + "\r\n"))

    def test_a_title_file_saved_with_a_byte_order_mark_reads_as_typed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "title.txt"
            path.write_bytes(b"\xef\xbb\xbf" + AWKWARD_TITLE.encode("utf-8") + b"\n")
            self.assertEqual((AWKWARD_TITLE, []), pull_request.read_title(pull_request.read_file(path)))

    def test_an_empty_title_and_one_of_several_lines_are_refused(self) -> None:
        self.assertEqual(("", ["TITLE_EMPTY"]), pull_request.read_title(" \n"))
        self.assertEqual(["TITLE_LINES 2"], pull_request.read_title("fix: a\nsecond\n")[1])
        self.assertEqual(["TITLE_LINES 2"], pull_request.read_title("fix: a\rsecond\n")[1])

    def test_a_title_holding_a_line_separator_is_one_line(self) -> None:
        # git ends a commit message's line at LF, never at a line separator, so a title holding one is one line.
        self.assertEqual(("fix: a\u2028b", []), pull_request.read_title("fix: a\u2028b\n"))


# The squash commit's title is the pull request's title, and GitHub closes an issue a commit message names, so a
# closing reference in the title, in any form closing_references reads or one that starts a reference, is refused.
@pytest.mark.parametrize(
    "title",
    [
        "fix: resolves #5 and fixes owner/repo#6",
        "fix: closes #5",
        "fix(scope): Fixes: #5",
        "fix: closes:#5",
        "fix: **Closes** #5",
        "fix: closes GH-5",
        "fix: resolves https://github.com/o/r/issues/5",
        "fix: resolves <http://www.github.com/o/r/issues/5>",
        "fix: closes #abc",
        "fix: closes https://github.com/o/r/pull/5",
    ],
)
def test_a_title_holding_a_closing_reference_is_refused(title: str) -> None:
    assert pull_request.read_title(title + "\n") == (title, [f"TITLE_CLOSES {title}"])


@pytest.mark.parametrize(
    "title",
    [
        AWKWARD_TITLE,
        "fix(deployer): read the title (#4242)",
        "fix: the closes check reads #12 now",
        "ci: run on GH-hosted runners",
        "docs: what prefixes #7 means",
        "fix: closes the file it opens",
    ],
)
def test_a_title_with_no_closing_reference_is_accepted(title: str) -> None:
    assert pull_request.read_title(title) == (title, [])


class TemplateLookupTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="implement-change-test-"))
        self.addCleanup(remove_tree, self.root)

    def test_the_template_is_found_where_github_reads_it_in_any_case(self) -> None:
        for folder, name in (
            (".github", "pull_request_template.md"),
            ("", "PULL_REQUEST_TEMPLATE.md"),
            ("docs", "Pull_Request_Template.md"),
        ):
            with self.subTest(folder=folder, name=name):
                directory = self.root / folder
                directory.mkdir(exist_ok=True)
                (directory / name).write_text("## Changes\n", encoding="utf-8")
                self.assertEqual(directory / name, pull_request.find_template(self.root))
                (directory / name).unlink()

    def test_no_template_is_none(self) -> None:
        self.assertIsNone(pull_request.find_template(self.root))

    def test_a_template_folder_that_is_not_there_is_passed_over_for_the_next(self) -> None:
        (self.root / "docs").mkdir()
        (self.root / "docs" / "pull_request_template.md").write_text("## Changes\n", encoding="utf-8")
        self.assertEqual(self.root / "docs" / "pull_request_template.md", pull_request.find_template(self.root))

    def test_a_file_named_like_a_template_folder_holds_no_template(self) -> None:
        (self.root / ".github").write_text("not a folder\n", encoding="utf-8")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "pull_request_template.md").write_text("## Changes\n", encoding="utf-8")
        self.assertEqual(self.root / "docs" / "pull_request_template.md", pull_request.find_template(self.root))

    def test_a_folder_named_like_the_template_is_passed_over_for_the_real_one(self) -> None:
        (self.root / ".github" / pull_request.TEMPLATE_NAME).mkdir(parents=True)
        (self.root / "docs").mkdir()
        (self.root / "docs" / pull_request.TEMPLATE_NAME).write_text("## Changes\n", encoding="utf-8")
        self.assertEqual(self.root / "docs" / pull_request.TEMPLATE_NAME, pull_request.find_template(self.root))

    def test_a_folder_named_like_the_template_alone_is_no_template(self) -> None:
        (self.root / ".github" / pull_request.TEMPLATE_NAME).mkdir(parents=True)
        self.assertIsNone(pull_request.find_template(self.root))

    def test_a_template_whose_stat_fails_is_refused_rather_than_passed_over_for_the_next(self) -> None:
        template = self.root / ".github" / "pull_request_template.md"
        template.parent.mkdir()
        template.write_text("## Changes\n", encoding="utf-8")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "pull_request_template.md").write_text("## Changes\n", encoding="utf-8")
        real_stat, real_is_file = Path.stat, Path.is_file

        def denied(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            if path == template:
                raise PermissionError(13, "Permission denied", str(path))
            return real_stat(path, follow_symlinks=follow_symlinks)

        def is_file(path: Path, *rest: object, **options: object) -> bool:
            return False if path == template else real_is_file(path)

        with (
            mock.patch.object(Path, "stat", autospec=True, side_effect=denied),
            mock.patch.object(Path, "is_file", autospec=True, side_effect=is_file),
            self.assertRaises(pull_request.PullRequestError) as refused,
        ):
            pull_request.find_template(self.root)
        self.assertEqual(
            f'cannot read "{template}": [Errno 13] Permission denied: {str(template)!r}', str(refused.exception)
        )

    def test_a_template_folder_whose_stat_fails_is_refused_rather_than_passed_over(self) -> None:
        github = self.root / ".github"
        github.mkdir()
        (github / "pull_request_template.md").write_text("## Changes\n", encoding="utf-8")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "pull_request_template.md").write_text("## Other\n", encoding="utf-8")
        real_stat = Path.stat

        def denied(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            if path == github:
                raise PermissionError(13, "Permission denied", str(path))
            return real_stat(path, follow_symlinks=follow_symlinks)

        with (
            mock.patch.object(Path, "stat", autospec=True, side_effect=denied),
            self.assertRaises(pull_request.PullRequestError) as refused,
        ):
            pull_request.find_template(self.root)
        self.assertEqual(
            f'cannot read "{github}": [Errno 13] Permission denied: {str(github)!r}', str(refused.exception)
        )


def build_repository(root: Path) -> None:
    """A bare origin whose main holds the template, and its clone `work tree` on a branch with one commit of its own."""
    origin = root / "origin.git"
    git(root, "init", "--quiet", "--bare", str(origin))
    clone = root / "work tree"
    git(root, "clone", "--quiet", str(origin), str(clone))
    (clone / ".github").mkdir()
    (clone / ".github" / "pull_request_template.md").write_text(TEMPLATE, encoding="utf-8")
    git(clone, "add", "--all")
    git(clone, "commit", "--quiet", "-m", "template")
    git(clone, "push", "--quiet", "origin", "main")
    git(clone, "switch", "--quiet", "-c", "fix/title")
    (clone / "tool.py").write_text("print('title')\n", encoding="utf-8")
    git(clone, "add", "--all")
    git(clone, "commit", "--quiet", "-m", "change")


# Building the origin and its clone starts nine git processes, so each process builds them once and each test gets a
# copy, whose clone names the copy's own origin.
REPOSITORY_TEMPLATE = FixtureTemplate("implement-change", build_repository)


class FixtureTemplateTests(unittest.TestCase):
    git_config = GIT_CONFIG

    def test_a_copy_matches_a_fresh_build_and_a_push_from_it_reaches_only_its_own_origin(self) -> None:
        fresh, copied = (Path(tempfile.mkdtemp(prefix="implement-change-template-")).resolve() for _ in range(2))
        self.addCleanup(remove_tree, fresh)
        self.addCleanup(remove_tree, copied)
        build_repository(fresh)
        REPOSITORY_TEMPLATE.copy(copied)
        repositories = ["origin.git", "work tree"]
        self.assertEqual(repository_shape(fresh, repositories), repository_shape(copied, repositories))
        template_main = git(REPOSITORY_TEMPLATE.root / "origin.git", "rev-parse", "main")
        git(copied / "work tree", "push", "--quiet", "origin", "fix/title:main")
        pushed = git(copied / "work tree", "rev-parse", "HEAD")
        self.assertEqual(pushed, git(copied / "origin.git", "rev-parse", "main"))
        self.assertEqual(template_main, git(REPOSITORY_TEMPLATE.root / "origin.git", "rev-parse", "main"))


class Repository(unittest.TestCase):
    """A clone of a bare origin whose main holds the template, on a branch with one commit of its own."""

    git_config = GIT_CONFIG

    @override
    def setUp(self) -> None:
        self.temporary = Path(tempfile.mkdtemp(prefix="implement-change-test-")).resolve()
        self.addCleanup(remove_tree, self.temporary)
        REPOSITORY_TEMPLATE.copy(self.temporary)
        self.root = self.temporary / "work tree"
        self.files = self.temporary / "files"
        self.files.mkdir()
        self.title = self.files / "title.txt"
        self.title.write_text(AWKWARD_TITLE + "\n", encoding="utf-8")
        self.body = self.files / "body.md"
        self.body.write_text(FILLED, encoding="utf-8")
        self.gh_calls: list[list[str]] = []
        self.gh_result = CommandResult(0, "https://github.com/o/r/pull/9\n", "")

    def commit(self, message: str) -> None:
        git(self.root, "add", "--all")
        git(self.root, "commit", "--quiet", "-m", message)

    def advance_origin(self) -> None:
        other = self.temporary / "other"
        git(self.temporary, "clone", "--quiet", str(self.temporary / "origin.git"), str(other))
        (other / "later.txt").write_text("later\n", encoding="utf-8")
        git(other, "add", "--all")
        git(other, "commit", "--quiet", "-m", "later")
        git(other, "push", "--quiet", "origin", "main")

    def fake_gh(self, command: Sequence[str]) -> CommandResult:
        self.gh_calls.append(list(command))
        return self.gh_result

    def run_script(self, *arguments: str, git_client: GitClient | None = None) -> tuple[int, list[str]]:
        output = io.StringIO()
        github = GitHubClient(runner=self.fake_gh)
        with contextlib.redirect_stdout(output):
            code = pull_request.main(["--repository", str(self.root), *arguments], git=git_client, github=github)
        return code, output.getvalue().splitlines()

    def failing_git(self, *marker: str, result: GitResult | None = None) -> GitClient:
        """Real git, except that the command whose arguments hold `marker` in order gives back `result`, or does not
        finish when `result` is None."""

        def runner(command: Sequence[str], timeout: float) -> GitResult:
            for start in range(len(command) - len(marker) + 1):
                if tuple(command[start : start + len(marker)]) == marker:
                    if result is None:
                        raise GitError(f"{' '.join(command)} did not finish within 1 seconds", kind="timeout")
                    return result
            return subprocess_runner(command, timeout)

        return GitClient(runner=runner)

    def files_arguments(self) -> list[str]:
        return ["--title-file", str(self.title), "--body-file", str(self.body), "--base", "origin/main"]


class DraftCommandTests(Repository):
    def test_draft_writes_the_body_in_a_new_folder_and_names_the_title_file(self) -> None:
        code, lines = self.run_script("draft", "--closes", "12", "--closes", "34")
        self.assertEqual(0, code, lines)
        self.assertEqual('TEMPLATE ".github/pull_request_template.md"', lines[0])
        self.assertEqual(["CLOSES 12", "CLOSES 34"], lines[1:3])
        self.assertTrue(lines[3].startswith('BODY "') and lines[4].startswith('TITLE "'), lines)
        body = Path(lines[3][len('BODY "') : -1])
        title = Path(lines[4][len('TITLE "') : -1])
        self.addCleanup(remove_tree, body.parent)
        self.assertTrue(body.parent.name.startswith("implement-change-"))
        self.assertEqual(body.parent, title.parent)
        self.assertFalse(title.exists())
        self.assertEqual(pull_request.draft_body(TEMPLATE, [12, 34]), body.read_text(encoding="utf-8"))

    def test_draft_without_a_template_fails_and_writes_nothing(self) -> None:
        # A folder in the template's place is no template, as GitHub reads it, so the refusal names the file it looked
        # for rather than leaving a reader to guess why the folder did not count.
        template = self.root / ".github" / "pull_request_template.md"
        for place in ("missing", "folder"):
            with self.subTest(place=place):
                template.unlink()
                if place == "folder":
                    template.mkdir()
                code, lines = self.run_script("draft")
                failed = (
                    "FAILED no pull request template, a pull_request_template.md file, in .github/, the top folder, "
                )
                self.assertEqual((1, [f"{failed}or docs/"]), (code, lines))
                if place == "folder":
                    template.rmdir()
                template.write_text(TEMPLATE, encoding="utf-8")

    def test_an_issue_that_is_not_a_positive_whole_number_is_a_usage_error_naming_it(self) -> None:
        # Each value int() or a lower bound of 0 would read, and one past the digit limit.
        for value in ["#12", "0", "00", "-1", "+1", " 1", "1 ", "1_0", "\u0661", "1" + "0" * 5000]:
            with self.subTest(value=value[:8]):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                    self.run_script("draft", "--closes", value)
                self.assertEqual(2, raised.exception.code)
                self.assertIn(
                    f"argument --closes: must be a whole number of at least 1 in ASCII digits, not {value!r}",
                    stderr.getvalue(),
                )

    def test_an_issue_written_with_leading_zeros_is_read_as_its_number(self) -> None:
        code, lines = self.run_script("draft", "--closes", "012", "--closes", "0001")
        self.assertEqual(0, code, lines)
        self.assertEqual(["CLOSES 12", "CLOSES 1"], lines[1:3])
        body = Path(lines[3][len('BODY "') : -1])
        self.addCleanup(remove_tree, body.parent)
        self.assertIn("Closes #12\nCloses #1\n", body.read_text(encoding="utf-8"))


# The job names validation gives the type checks, as literals.
SHARED_JOB = "static type check (mypy deployer, tools, deploy.py, tests, conftest.py)"
ALPHA_JOB = "static type check (mypy skills/alpha/scripts)"
EVERY_JOB = "RUN python -B tests/run_validation.py -k 'static type check'"
# A strict list, and a mypy_path naming the one shared skill's scripts other roots import.
TYPE_CHECK_PYPROJECT = (
    '[tool.validation]\nmypy-strict = ["tools", "tests"]\n\n'
    '[tool.mypy]\nmypy_path = ["$MYPY_CONFIG_FILE_DIR/skills/core/scripts"]\n'
)


class TypeChecksCommandTests(Repository):
    """The base holds an ordinary skill's scripts, a shared skill's scripts mypy_path lists, a module under tools/ and
    deployer/, a suite under tests/, and the checkers' configuration and pins; each test changes some of them."""

    FILES = (
        "skills/alpha/SKILL.md",
        "skills/alpha/scripts/run.py",
        "skills/alpha/scripts/test_run.py",
        "skills/core/SKILL.md",
        "skills/core/scripts/shared.py",
        "tools/gate.py",
        "deployer/plan.py",
        "tests/unit/test_gate.py",
        "requirements/requirements-dev.in",
        "requirements/requirements-dev.txt",
    )

    @override
    def setUp(self) -> None:
        super().setUp()
        self.write("pyproject.toml", TYPE_CHECK_PYPROJECT)
        for name in self.FILES:
            self.write(name, f"# {name}\n")
        self.commit("roots")
        git(self.root, "push", "--quiet", "origin", "HEAD:main")

    def write(self, name: str, text: str) -> None:
        path = self.root.joinpath(*name.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def type_checks(self) -> tuple[int, list[str]]:
        code, lines = self.run_script("type-checks", "--base", "origin/main")
        base = git(self.root, "rev-parse", "origin/main")
        self.assertEqual(f"BASE origin/main {base}", lines[0], lines)
        return code, lines[1:]

    def test_each_job_a_changed_python_file_reaches_is_named_and_one_command_runs_them_all(self) -> None:
        # A committed change to an ordinary skill's script reaches its own run, and an untracked suite under tests/
        # the shared run and the strict run of tests.
        self.write("skills/alpha/scripts/run.py", "# changed\n")
        self.commit("change")
        self.write("tests/unit/test_new.py", "")
        jobs = [ALPHA_JOB, SHARED_JOB, "static type check (mypy --strict tests)"]
        command = "python -B tests/run_validation.py " + " ".join(f"-k '{job}'" for job in jobs)
        self.assertEqual((0, [*(f"TYPE_CHECK {job}" for job in jobs), f"RUN {command}"]), self.type_checks())

    def test_an_uncommitted_change_to_a_tracked_file_is_read(self) -> None:
        self.write("skills/alpha/scripts/run.py", "# changed\n")
        self.assertEqual(
            (0, [f"TYPE_CHECK {ALPHA_JOB}", f"RUN python -B tests/run_validation.py -k '{ALPHA_JOB}'"]),
            self.type_checks(),
        )

    def test_an_uncommitted_rename_counts_the_folder_it_leaves(self) -> None:
        # Read as a rename, the move would name only where the module went, which no type check reads.
        (self.root / "notes").mkdir()
        git(self.root, "mv", "skills/alpha/scripts/run.py", "notes/run.py")
        self.assertEqual(
            (0, [f"TYPE_CHECK {ALPHA_JOB}", f"RUN python -B tests/run_validation.py -k '{ALPHA_JOB}'"]),
            self.type_checks(),
        )

    def test_a_file_that_configures_every_check_or_that_other_roots_import_reaches_every_job(self) -> None:
        cases = {
            "pyproject.toml": "configures or pins every type check",
            "requirements/requirements-dev.txt": "configures or pins every type check",
            "requirements/requirements-dev.in": "configures or pins every type check",
            "skills/core/scripts/shared.py": "is in skills/core/scripts, which other type-check roots import",
            "tools/gate.py": "is in tools, which other type-check roots import",
            "deployer/plan.py": "is in deployer, which other type-check roots import",
        }
        for name, why in cases.items():
            with self.subTest(name=name):
                path = self.root.joinpath(*name.split("/"))
                path.write_text(path.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
                self.assertEqual((0, [f"TYPE_CHECK_ALL {name} {why}", EVERY_JOB]), self.type_checks())
                git(self.root, "checkout", "--quiet", "--", name)

    @unittest.skipIf(sys.platform == "win32", "Windows refuses a line break in a file name")
    def test_a_line_break_in_a_file_name_prints_no_line_of_its_own(self) -> None:
        # Printed raw, the name would end its fact line and start one the session reads as the command to run.
        self.write("tools/a\nRUN evil.py", "")
        self.assertEqual(
            (0, ["TYPE_CHECK_ALL tools/a RUN evil.py is in tools, which other type-check roots import", EVERY_JOB]),
            self.type_checks(),
        )

    def test_a_line_separator_in_the_base_s_branch_prints_no_line_of_its_own(self) -> None:
        # Git takes U+2028 in a branch's name; printed raw, it would end the BASE line and start a RUN line of its own.
        branch = "ma\u2028RUN-evil"
        git(self.root, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
        base = git(self.root, "rev-parse", "HEAD")
        self.assertEqual(
            (0, [f"BASE origin/ma RUN-evil {base}", "NO_TYPE_CHECK"]),
            self.run_script("type-checks", "--base", f"origin/{branch}"),
        )

    def test_a_python_file_the_base_holds_and_the_branch_does_not_reaches_nothing(self) -> None:
        # Only the files changed since the merge base count, so a base that moved on adds none of its own.
        other = self.temporary / "other"
        git(self.temporary, "clone", "--quiet", str(self.temporary / "origin.git"), str(other))
        (other / "tools" / "later.py").write_text("", encoding="utf-8")
        git(other, "add", "--all")
        git(other, "commit", "--quiet", "-m", "later")
        git(other, "push", "--quiet", "origin", "main")
        self.write("notes.md", "")
        self.assertEqual((0, ["NO_TYPE_CHECK"]), self.type_checks())

    def test_a_configuration_that_cannot_be_read_is_a_failed_line(self) -> None:
        pyproject = self.root / "pyproject.toml"
        strict = '[tool.validation]\nmypy-strict = ["tools"]\n'
        for text, reason in (
            ("[tool.validation\n", f'FAILED cannot read "{pyproject}": '),
            (
                '[tool.validation]\nmypy-strict = "tools"\n',
                "FAILED pyproject.toml: [tool.validation] mypy-strict is not a list of paths",
            ),
            (
                strict,
                "FAILED pyproject.toml: [tool.mypy] mypy_path is not a list of paths starting $MYPY_CONFIG_FILE_DIR/",
            ),
            (
                f'{strict}[tool.mypy]\nmypy_path = ["skills/core/scripts"]\n',
                "FAILED pyproject.toml: [tool.mypy] mypy_path is not a list of paths starting $MYPY_CONFIG_FILE_DIR/",
            ),
        ):
            with self.subTest(text=text):
                pyproject.write_text(text, encoding="utf-8")
                code, lines = self.run_script("type-checks", "--base", "origin/main")
                self.assertEqual(1, code)
                self.assertTrue(lines[-1].startswith(reason), lines)
        pyproject.unlink()
        code, lines = self.run_script("type-checks", "--base", "origin/main")
        self.assertEqual(1, code)
        self.assertTrue(lines[-1].startswith(f'FAILED cannot read "{pyproject}": '), lines)

    def test_a_base_that_cannot_be_fetched_is_a_failed_line(self) -> None:
        code, lines = self.run_script("type-checks", "--base", "nowhere/main")
        self.assertEqual(1, code)
        self.assertTrue(lines[-1].startswith("FAILED cannot fetch main from nowhere: "), lines)


def test_a_job_name_holding_a_single_quote_is_refused_rather_than_quoted() -> None:
    with pytest.raises(pull_request.PullRequestError, match="single quote"):
        pull_request.run_command(["static type check (mypy skills/it's/scripts)"])


@pytest.mark.parametrize("character", ["\n", "\u2028", "\t", "\x1b"])
def test_a_job_name_holding_a_line_break_or_a_control_character_is_refused_rather_than_flattened(
    character: str,
) -> None:
    # Flattened onto the RUN line, the name would select another job, or none.
    with pytest.raises(pull_request.PullRequestError, match="line break or a control character"):
        pull_request.run_command([f"static type check (mypy skills/a{character}b/scripts)"])


def test_this_repository_s_roots_map_to_the_jobs_continuous_integration_runs() -> None:
    # A suite reaches the strict run of tests, the one a suite reaching a name its module does not export fails.
    strict = strict_type_check_areas(REPOSITORY / "pyproject.toml")
    assert strict is not None
    changed = ["tests/test_conftest.py", "skills/repo-cleanup/scripts/repo_cleanup.py"]
    assert file_type_check_jobs(REPOSITORY, changed, strict) == [
        SHARED_JOB,
        "static type check (mypy --strict tests)",
        "static type check (mypy skills/repo-cleanup/scripts)",
    ]


def test_this_repository_s_shared_modules_and_checker_files_reach_every_job() -> None:
    # A module other roots import breaks their runs, not only its own folder's, so its change reaches every job; an
    # ordinary skill's script, which nothing else imports, does not.
    imported = imported_folders(REPOSITORY / "pyproject.toml")
    assert imported is not None
    changed = [
        "skills/ejryan89-skill-core/scripts/path_containment.py",
        "tools/mutation_gate.py",
        "deployer/plan.py",
        "pyproject.toml",
        "requirements/requirements-dev.txt",
        ".claude/skills/implement-change/scripts/pull_request.py",
    ]
    assert [name for name, _ in every_job_reasons(changed, imported)] == changed[:5]


class CheckCommandTests(Repository):
    def test_a_ready_branch_prints_its_facts_and_ready(self) -> None:
        code, lines = self.run_script("check", *self.files_arguments())
        base = git(self.root, "rev-parse", "origin/main")
        self.assertEqual(
            (0, [f"TITLE {AWKWARD_TITLE}", "CLOSES 12", "CLOSES 34", f"BASE origin/main {base}", "READY"]),
            (code, lines),
        )

    def test_a_base_that_moved_on_is_fetched_and_the_branch_named_behind(self) -> None:
        self.advance_origin()
        code, lines = self.run_script("check", *self.files_arguments())
        base = git(self.root, "rev-parse", "origin/main")
        self.assertEqual(1, code)
        self.assertIn(f"BEHIND origin/main {base}", lines)
        self.assertNotIn("READY", lines)
        git(self.root, "rebase", "--quiet", "origin/main")
        self.assertEqual(0, self.run_script("check", *self.files_arguments())[0])

    def test_a_branch_with_no_commit_of_its_own_is_refused(self) -> None:
        git(self.root, "reset", "--quiet", "--hard", "origin/main")
        code, lines = self.run_script("check", *self.files_arguments())
        self.assertEqual((1, "NO_COMMITS origin/main"), (code, lines[-1]))

    def test_a_line_separator_in_the_base_s_branch_keeps_each_fact_and_problem_on_its_line(self) -> None:
        # The base's name is on the BASE fact and the NO_COMMITS problem; printed raw, its U+2028 would end each and
        # start a RUN line of its own.
        branch = "ma\u2028RUN-evil"
        git(self.root, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
        head = git(self.root, "rev-parse", "HEAD")
        files = ["--title-file", str(self.title), "--body-file", str(self.body), "--base", f"origin/{branch}"]
        self.assertEqual(
            (
                1,
                [
                    f"TITLE {AWKWARD_TITLE}",
                    "CLOSES 12",
                    "CLOSES 34",
                    f"BASE origin/ma RUN-evil {head}",
                    "NO_COMMITS origin/ma RUN-evil",
                ],
            ),
            self.run_script("check", *files),
        )

    def test_uncommitted_changes_are_counted(self) -> None:
        # A name holding a space is one path, whatever byte the listing ends it with.
        (self.root / "scratch notes.txt").write_text("scratch\n", encoding="utf-8")
        (self.root / "tool.py").write_text("print('changed')\n", encoding="utf-8")
        code, lines = self.run_script("check", *self.files_arguments())
        self.assertEqual((1, "UNCOMMITTED 2"), (code, lines[-1]))

    def test_checking_for_uncommitted_changes_never_writes_the_index(self) -> None:
        # A file whose timestamp moved but whose content did not: a status with optional locks refreshes its index
        # entry and writes the index back under its lock, which a commit in the checkout at that moment needs.
        tool = self.root / "tool.py"
        moved = tool.stat().st_mtime + 5
        os.utime(tool, (moved, moved))
        index = self.root / ".git" / "index"
        before = (index.read_bytes(), index.stat().st_mtime_ns)
        code, lines = self.run_script("check", *self.files_arguments())
        self.assertEqual((0, "READY"), (code, lines[-1]))
        self.assertEqual(before, (index.read_bytes(), index.stat().st_mtime_ns))

    def test_an_untracked_file_is_counted_whatever_status_show_untracked_files_says(self) -> None:
        # Untracked files are counted one by one, as the UNCOMMITTED line documents, whether the user's configuration
        # hides them from a status or lists each file in an untracked folder.
        (self.root / "tool.py").write_text("print('changed')\n", encoding="utf-8")
        (self.root / "scratch").mkdir()
        (self.root / "scratch" / "one.txt").write_text("one\n", encoding="utf-8")
        (self.root / "scratch" / "two.txt").write_text("two\n", encoding="utf-8")
        for setting in ("no", "normal", "all"):
            with self.subTest(setting):
                git(self.root, "config", "status.showUntrackedFiles", setting)
                code, lines = self.run_script("check", *self.files_arguments())
                self.assertEqual((1, "UNCOMMITTED 2"), (code, lines[-1]))

    def test_a_staged_rename_counts_the_path_it_left_and_the_one_it_made(self) -> None:
        git(self.root, "mv", "tool.py", "renamed.py")
        code, lines = self.run_script("check", *self.files_arguments())
        self.assertEqual((1, "UNCOMMITTED 2"), (code, lines[-1]))

    def test_a_status_git_cannot_read_is_named_with_git_s_reason(self) -> None:
        broken = self.failing_git("status", result=GitResult(128, "", "fatal: index file corrupt\n"))
        code, lines = self.run_script("check", *self.files_arguments(), git_client=broken)
        self.assertEqual((1, "FAILED git status: fatal: index file corrupt"), (code, lines[-1]))

    def test_a_title_closing_issues_is_refused_and_each_issue_it_closes_named(self) -> None:
        self.title.write_text("fix: resolves #5 and fixes owner/repo#6\n", encoding="utf-8")
        self.body.write_text(FILLED.replace("Closes #12\nCloses #34\n", "Closes #7\n"), encoding="utf-8")
        code, lines = self.run_script("check", *self.files_arguments())
        self.assertEqual(1, code)
        self.assertEqual(["CLOSES 5", "CLOSES 6", "CLOSES 7"], [line for line in lines if line.startswith("CLOSES")])
        self.assertIn("TITLE_CLOSES fix: resolves #5 and fixes owner/repo#6", lines)
        self.assertNotIn("READY", lines)

    def test_body_and_title_problems_are_each_named(self) -> None:
        self.title.write_text("", encoding="utf-8")
        self.body.write_text(FILLED.replace("Closes #34", "Closes #34, #35"), encoding="utf-8")
        code, lines = self.run_script("check", *self.files_arguments())
        self.assertEqual(1, code)
        self.assertIn("TITLE_EMPTY", lines)
        self.assertFalse([line for line in lines if line.startswith("TITLE ")], lines)
        self.assertIn("CLOSES_LINE 2 Closes #34, #35", lines)

    def use_forms_template(self) -> None:
        """Commit the fixture template with a `## Forms considered` section, so the branch stays clean."""
        (self.root / ".github" / "pull_request_template.md").write_text(FORMS_TEMPLATE, encoding="utf-8")
        self.commit("add the forms section")

    def test_a_fix_body_still_holding_the_template_s_none_is_refused(self) -> None:
        self.use_forms_template()
        self.body.write_text(with_forms(FILLED, "None"), encoding="utf-8")
        code, lines = self.run_script("check", *self.files_arguments())
        self.assertEqual((1, "FORMS_CONSIDERED placeholder"), (code, lines[-1]), lines)
        self.assertNotIn("READY", lines)

    def test_a_fix_body_with_its_own_forms_is_ready(self) -> None:
        self.use_forms_template()
        self.body.write_text(with_forms(FILLED, FORMS_WEIGHED), encoding="utf-8")
        code, lines = self.run_script("check", *self.files_arguments())
        self.assertEqual((0, "READY"), (code, lines[-1]), lines)

    def test_a_body_that_is_not_a_fix_s_keeps_the_template_s_none(self) -> None:
        self.use_forms_template()
        self.title.write_text("docs: say what a fix owes\n", encoding="utf-8")
        self.body.write_text(with_forms(FILLED, "None"), encoding="utf-8")
        code, lines = self.run_script("check", *self.files_arguments())
        self.assertEqual((0, "READY"), (code, lines[-1]), lines)

    def test_a_fetch_that_fails_is_a_failed_line(self) -> None:
        arguments = [*self.files_arguments()[:-1], "nowhere/main"]
        code, lines = self.run_script("check", *arguments)
        self.assertEqual(1, code)
        self.assertTrue(lines[-1].startswith("FAILED cannot fetch main from nowhere: "), lines)

    def test_a_failure_spanning_lines_is_one_failed_line(self) -> None:
        refusal = pull_request.PullRequestError("git fetch: fatal: one\nfatal:\u2028two")
        with mock.patch.object(pull_request, "run", side_effect=refusal):
            code, lines = self.run_script("check", *self.files_arguments())
        self.assertEqual((1, ["FAILED git fetch: fatal: one fatal: two"]), (code, lines))

    def test_an_unreadable_file_is_a_failed_line(self) -> None:
        self.body.unlink()
        code, lines = self.run_script("check", *self.files_arguments())
        self.assertEqual(1, code)
        self.assertTrue(lines[-1].startswith(f'FAILED cannot read "{self.body}": '), lines)

    def test_a_base_git_cannot_read_is_a_failed_line_with_git_s_reason(self) -> None:
        broken = GitResult(128, "", "fatal: bad object refs/remotes/origin/main\n")
        code, lines = self.run_script(
            "check", *self.files_arguments(), git_client=self.failing_git("rev-parse", "--verify", result=broken)
        )
        self.assertEqual((1, ["FAILED git rev-parse: fatal: bad object refs/remotes/origin/main"]), (code, lines))

    def test_a_base_read_that_does_not_finish_is_a_failed_line_not_a_missing_commit(self) -> None:
        code, lines = self.run_script(
            "check", *self.files_arguments(), git_client=self.failing_git("rev-parse", "--verify")
        )
        self.assertEqual(1, code)
        self.assertTrue(lines[-1].startswith("FAILED git rev-parse: git -C "), lines)
        self.assertIn("did not finish", lines[-1])

    def test_a_base_that_names_no_commit_after_the_fetch_is_a_failed_line(self) -> None:
        missing = GitResult(1, "", "")
        code, lines = self.run_script(
            "check", *self.files_arguments(), git_client=self.failing_git("rev-parse", "--verify", result=missing)
        )
        self.assertEqual((1, ["FAILED origin/main names no commit after the fetch"]), (code, lines))

    def test_only_an_exit_of_1_that_says_nothing_names_no_commit(self) -> None:
        for result, line in (
            (GitResult(128, "", ""), "FAILED git rev-parse: git rev-parse failed with exit code 128"),
            (GitResult(1, "", "fatal: ambiguous argument\n"), "FAILED git rev-parse: fatal: ambiguous argument"),
        ):
            with self.subTest(result=result):
                git_client = self.failing_git("rev-parse", "--verify", result=result)
                self.assertEqual((1, [line]), self.run_script("check", *self.files_arguments(), git_client=git_client))

    def test_a_holding_test_git_warns_about_is_a_failed_line_not_behind(self) -> None:
        warned = GitResult(1, "", "warning: ignoring broken ref refs/heads/side\n")
        git_client = self.failing_git("merge-base", "--is-ancestor", result=warned)
        self.assertEqual(
            (1, ["FAILED git merge-base: warning: ignoring broken ref refs/heads/side"]),
            self.run_script("check", *self.files_arguments(), git_client=git_client),
        )

    def test_the_top_folder_loses_only_the_line_break_git_ends_it_with(self) -> None:
        for printed, top in (("/srv/tree X \n", "/srv/tree X "), ("/srv/treeX\r\n", "/srv/treeX")):
            with self.subTest(printed=printed):
                git_client = GitClient(runner=partial(printing, printed))
                self.assertEqual(Path(top), pull_request.top_of(git_client, self.root))

    def test_a_folder_outside_any_repository_is_a_failed_line(self) -> None:
        outside = self.temporary / "outside"
        outside.mkdir()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = pull_request.main(["--repository", str(outside), "draft"])
        self.assertEqual(
            (1, [f'FAILED "{outside}" is not in a git repository']), (code, output.getvalue().splitlines())
        )

    def test_a_top_folder_git_cannot_read_is_a_failed_line_with_git_s_reason(self) -> None:
        code, lines = self.run_script("draft", git_client=self.failing_git("--show-toplevel"))
        self.assertEqual(1, code)
        self.assertTrue(lines[-1].startswith("FAILED git rev-parse: git -C "), lines)
        self.assertIn("did not finish", lines[-1])

    def test_a_base_with_no_remote_is_a_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            self.run_script("check", *self.files_arguments()[:-1], "main")
        self.assertEqual(2, raised.exception.code)


class OpenCommandTests(Repository):
    def test_a_branch_not_pushed_is_refused_and_gh_never_runs(self) -> None:
        code, lines = self.run_script("open", *self.files_arguments())
        self.assertEqual((1, "UNPUSHED no upstream"), (code, lines[-1]))
        self.assertEqual([], self.gh_calls)
        git(self.root, "push", "--quiet", "-u", "origin", "HEAD")
        (self.root / "tool.py").write_text("print('again')\n", encoding="utf-8")
        self.commit("after the push")
        code, lines = self.run_script("open", *self.files_arguments())
        self.assertEqual((1, "UNPUSHED origin/fix/title"), (code, lines[-1]))
        self.assertEqual([], self.gh_calls)

    def test_a_detached_head_has_no_upstream(self) -> None:
        git(self.root, "switch", "--quiet", "--detach")
        code, lines = self.run_script("open", *self.files_arguments())
        self.assertEqual((1, "UNPUSHED no upstream"), (code, lines[-1]))
        self.assertEqual([], self.gh_calls)

    def test_an_upstream_not_fetched_yet_is_unpushed(self) -> None:
        git(self.root, "push", "--quiet", "-u", "origin", "HEAD")
        git(self.root, "update-ref", "-d", "refs/remotes/origin/fix/title")
        code, lines = self.run_script("open", *self.files_arguments())
        self.assertEqual((1, "UNPUSHED origin/fix/title"), (code, lines[-1]))
        self.assertEqual([], self.gh_calls)

    def test_a_branch_git_cannot_read_is_a_failed_line_not_unpushed(self) -> None:
        git(self.root, "push", "--quiet", "-u", "origin", "HEAD")
        broken = GitResult(128, "", "fatal: your current branch appears to be broken\n")
        for marker in (("symbolic-ref",), ("for-each-ref",)):
            with self.subTest(marker=marker):
                code, lines = self.run_script(
                    "open", *self.files_arguments(), git_client=self.failing_git(*marker, result=broken)
                )
                self.assertEqual(1, code)
                self.assertEqual(f"FAILED git {marker[0]}: fatal: your current branch appears to be broken", lines[-1])
                self.assertEqual([], self.gh_calls)

    def test_an_upstream_git_cannot_read_is_a_failed_line_not_unpushed(self) -> None:
        git(self.root, "push", "--quiet", "-u", "origin", "HEAD")
        upstream = "refs/remotes/origin/fix/title^{commit}"
        code, lines = self.run_script("open", *self.files_arguments(), git_client=self.failing_git(upstream))
        self.assertEqual(1, code)
        self.assertTrue(lines[-1].startswith("FAILED git rev-parse: git -C "), lines)
        self.assertIn("did not finish", lines[-1])
        self.assertEqual([], self.gh_calls)

    def test_a_check_problem_stops_the_open(self) -> None:
        git(self.root, "push", "--quiet", "-u", "origin", "HEAD")
        self.body.write_text(FILLED.replace("Closes #34", "Closes #"), encoding="utf-8")
        code, lines = self.run_script("open", *self.files_arguments())
        self.assertEqual((1, "PLACEHOLDER 2 a Closes line with no issue"), (code, lines[-1]))
        self.assertEqual([], self.gh_calls)

    def test_a_fix_body_still_holding_the_template_s_none_stops_the_open(self) -> None:
        (self.root / ".github" / "pull_request_template.md").write_text(FORMS_TEMPLATE, encoding="utf-8")
        self.commit("add the forms section")
        git(self.root, "push", "--quiet", "-u", "origin", "HEAD")
        self.body.write_text(with_forms(FILLED, "None"), encoding="utf-8")
        code, lines = self.run_script("open", *self.files_arguments())
        self.assertEqual((1, "FORMS_CONSIDERED placeholder"), (code, lines[-1]), lines)
        self.assertEqual([], self.gh_calls)

    def test_a_pushed_ready_branch_opens_with_the_title_passed_unchanged(self) -> None:
        git(self.root, "push", "--quiet", "-u", "origin", "HEAD")
        code, lines = self.run_script("open", *self.files_arguments())
        self.assertEqual((0, "PULL_REQUEST https://github.com/o/r/pull/9"), (code, lines[-1]), lines)
        self.assertNotIn("READY", lines)
        self.assertEqual(
            [["gh", "pr", "create", "--title", AWKWARD_TITLE, "--body-file", str(self.body), "--base", "main"]],
            self.gh_calls,
        )

    def test_a_gh_failure_is_a_failed_line(self) -> None:
        git(self.root, "push", "--quiet", "-u", "origin", "HEAD")
        self.gh_result = CommandResult(1, "", 'a pull request for branch "fix/title" already exists\n')
        code, lines = self.run_script("open", *self.files_arguments())
        self.assertEqual(1, code)
        self.assertTrue(lines[-1].startswith("FAILED gh pr create: "), lines)
        self.assertIn("already exists", lines[-1])


class EntryPointTests(unittest.TestCase):
    def test_the_script_runs_as_a_program(self) -> None:
        ran = subprocess.run(
            [sys.executable, "-B", str(Path(pull_request.__file__)), "--help"],
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        self.assertEqual(0, ran.returncode, ran.stderr)
        self.assertIn("draft", ran.stdout)


@pytest.mark.parametrize("line", ["Closes #1" + "0" * 5000, "This fixes #1" + "0" * 5000 + "."])
def test_a_closing_number_past_the_digit_limit_fails_by_name(line: str) -> None:
    body = FILLED.replace("Closes #34", line)
    with pytest.raises(pull_request.PullRequestError) as raised:
        pull_request.closed_issues(body)
    assert f"a closing reference names an issue number of more than {whole_number.DIGIT_LIMIT} digits" == str(
        raised.value
    )


# GitHub closes an issue a closing reference names anywhere in the body, not only on a line that opens with one, so
# each form is reported closed and refused until it is a `Closes #<n>` line of its own.
PROSE_REFERENCES = "Closes #5\nThis also fixes #12.\n- Resolves #13\n* closes: #14\n> Fixes #15\n"


def test_every_closing_reference_github_reads_is_reported_and_refused_unless_it_is_a_closes_line() -> None:
    body = FILLED.replace("Closes #12\nCloses #34\n", PROSE_REFERENCES)
    assert pull_request.closed_issues(body) == [5, 12, 13, 14, 15]
    assert pull_request.body_problems(body, TEMPLATE) == [
        "CLOSES_LINE 2 This also fixes #12.",
        "CLOSES_LINE 3 - Resolves #13",
        "CLOSES_LINE 4 * closes: #14",
        "CLOSES_LINE 5 > Fixes #15",
    ]


@pytest.mark.parametrize(
    "prose",
    [
        "As before, this resolves o/r#7 too.",
        "It fixes https://github.com/o/r/issues/7 as well.",
        "1. Closed #7 with the rest.",
        "| Fixed #7 | here |",
        "Closes #7 and refactors.",
    ],
)
def test_a_closing_reference_in_any_other_line_is_refused(prose: str) -> None:
    body = FILLED.replace("Fixes the quoting of a title.", prose)
    [problem] = pull_request.body_problems(body, TEMPLATE)
    assert problem == f"CLOSES_LINE 4 {prose}"
    assert 7 in pull_request.closed_issues(body)


# Neither a number with no closing keyword nor a keyword with no reference closes anything.
@pytest.mark.parametrize(
    "prose", ["The bug in #7 is fixed.", "Fixes the quoting that #7 found.", "prefixes #7", "ci: GH-hosted runners"]
)
def test_text_that_closes_nothing_is_neither_reported_nor_refused(prose: str) -> None:
    body = FILLED.replace("Fixes the quoting of a title.", prose)
    assert pull_request.body_problems(body, TEMPLATE) == []
    assert pull_request.closed_issues(body) == [12, 34]


# The squash commit carries the body as written, so a reference in a comment the author wrote closes its issue, and
# is reported and refused; a template comment left in the body is a placeholder, read for nothing else.
@pytest.mark.parametrize("prose", ["<!-- fixes #7 -->", "<!-- Resolves GH-7 -->"])
def test_a_closing_reference_in_an_author_s_comment_is_reported_and_refused(prose: str) -> None:
    body = FILLED.replace("Fixes the quoting of a title.", prose)
    assert pull_request.body_problems(body, TEMPLATE) == [f"CLOSES_LINE 4 {prose}"]
    assert pull_request.closed_issues(body) == [12, 34, 7]


# A `Closes #<n>` line in a comment the author wrote or a code block closes its issue on merge, since the squash commit
# carries it, but GitHub (`gh api markdown`) renders no link for it, so it is reported closed and refused as hidden.
@pytest.mark.parametrize(
    ("hidden", "line"),
    [("<!--\nCloses #7\n-->", 7), ("```\nCloses #7\n```", 7), ("~~~text\nCloses #7\n~~~", 7), ("    Closes #7", 6)],
)
def test_a_closes_line_the_rendered_body_does_not_show_is_reported_and_refused_as_hidden(
    hidden: str, line: int
) -> None:
    body = FILLED.replace("Fixes the quoting of a title.\n", f"Fixes the quoting of a title.\n\n{hidden}\n")
    assert pull_request.body_problems(body, TEMPLATE) == [f"CLOSES_HIDDEN {line} Closes #7"]
    assert pull_request.closed_issues(body) == [12, 34, 7]


def test_a_template_comment_left_beside_a_closes_line_leaves_the_line_a_closes_line() -> None:
    body = FILLED.replace("Closes #34", "Closes #34 <!-- The problem, in a paragraph. -->")
    assert pull_request.body_problems(body, TEMPLATE) == ["PLACEHOLDER 2 a template comment"]


def test_a_closing_number_past_the_digit_limit_in_the_title_alone_fails_by_name() -> None:
    with pytest.raises(pull_request.PullRequestError, match="more than"):
        pull_request.closed_issues(FILLED, "fix: closes #1" + "0" * 5000)


def test_a_title_alone_names_the_issues_it_closes() -> None:
    assert pull_request.closed_issues("", "fix: resolves GH-5") == [5]


def test_a_closing_reference_in_a_template_comment_left_in_the_body_is_only_a_placeholder() -> None:
    template = TEMPLATE.replace("<!-- The problem, in a paragraph. -->", "<!-- e.g. fixes #99 -->")
    body = FILLED.replace("Fixes the quoting of a title.", "<!-- e.g. fixes #99 -->")
    assert pull_request.body_problems(body, template) == ["PLACEHOLDER 4 a template comment"]


# Every spelling closing_references reads is refused on a line of its own unless it is `Closes #<n>`.
@pytest.mark.parametrize(
    "line",
    [
        "Closes GH-12",
        "Closes <https://github.com/o/r/issues/12>",
        "**Closes** #12",
        "CLOSES:#12",
        "Closes http://github.com/o/r/issues/12",
        "Closes https://www.github.com/o/r/issues/12",
    ],
)
def test_each_spelling_the_reader_reads_is_refused_as_a_closing_line(line: str) -> None:
    body = FILLED.replace("Closes #34", line)
    assert [f"CLOSES_LINE 2 {line}"] == pull_request.body_problems(body, TEMPLATE)
    assert pull_request.closed_issues(body) == [12]


@pytest.mark.parametrize(
    "line", ["Closes #12, #13", "Closes #12 and #13", "Fixes #12", "closes #12", "Resolves: #12", "Closes #12."]
)
def test_a_closing_line_must_name_one_issue_with_closes(line: str) -> None:
    body = FILLED.replace("Closes #34", line)
    assert [f"CLOSES_LINE 2 {line}"] == pull_request.body_problems(body, TEMPLATE)
    assert pull_request.closed_issues(body) == [12]


# GitHub closes an issue by its repository's name or its URL too, so neither form passes for `Closes #<n>`.
@pytest.mark.parametrize(
    "line", ["Closes o/r#12", "Fixes https://github.com/o/r/issues/12", "resolves: https://github.com/o/r/issues/12"]
)
def test_a_closing_line_by_repository_or_url_must_be_written_closes_number(line: str) -> None:
    body = FILLED.replace("Closes #34", line)
    assert [f"CLOSES_LINE 2 {line}"] == pull_request.body_problems(body, TEMPLATE)


def test_a_failed_git_call_is_named_by_its_subcommand_behind_git_s_options() -> None:
    def run(command: Sequence[str], timeout: float) -> GitResult:
        return GitResult(1, "", "fatal: no\n")

    with pytest.raises(pull_request.PullRequestError, match=r"^git rev-parse: fatal: no$"):
        pull_request.git_output(GitClient(run), Path("r"), ["-c", "a=b", "rev-parse", "HEAD"])


def test_a_quiet_git_call_git_cannot_answer_is_labelled_with_its_subcommand() -> None:
    def run(command: Sequence[str], timeout: float) -> GitResult:
        return GitResult(128, "", "fatal: bad config\n")

    with pytest.raises(pull_request.PullRequestError, match=r"^git symbolic-ref: fatal: bad config$"):
        pull_request.quiet_output(GitClient(run), Path("r"), ["-c", "a=b", "symbolic-ref", "--quiet", "HEAD"])
