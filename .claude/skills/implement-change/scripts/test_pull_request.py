from __future__ import annotations

import contextlib
import io
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "skill-core" / "scripts"))

import pull_request
from github_client import CommandResult, GitHubClient

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
# A title the title rules allow, holding each character either shell could misread.
AWKWARD_TITLE = (
    'fix(skills): grant each skill\'s scripts by name, keep `$HOME` and "$(x)", and \u2018quote\u2019 \u201atwice\u201b'
)


def remove_tree(path: Path) -> None:
    def writable(function, target, *_):
        Path(target).chmod(stat.S_IWRITE)
        function(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=writable)
    else:
        shutil.rmtree(path, onerror=writable)


def git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(directory), *arguments], capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(arguments)} failed: {result.stderr}")
    return result.stdout.strip()


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

    def test_a_template_with_no_closes_line_gets_them_at_the_top(self) -> None:
        self.assertEqual("Closes #7\n\n## Changes\n", pull_request.draft_body("## Changes\n", [7]))

    def test_this_repository_s_template_drafts_a_body_the_check_still_refuses(self) -> None:
        template = (REPOSITORY / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
        body = pull_request.draft_body(template, [1166])
        self.assertRegex(body, r"(?m)^Closes #1166$")
        problems = pull_request.body_problems(body, template)
        self.assertTrue(any(line.startswith("PLACEHOLDER ") and "empty list item" in line for line in problems))
        self.assertTrue(any("template comment" in line for line in problems))


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

    def test_a_template_comment_left_in_the_body_is_named(self) -> None:
        body = FILLED.replace("None\n", "<!-- One entry per change.\n\n### What changed\n\n- Level: patch\n-->\nNone\n")
        self.assertEqual(["PLACEHOLDER 12 a template comment"], pull_request.body_problems(body, TEMPLATE))

    def test_an_author_s_own_comment_is_not_a_placeholder(self) -> None:
        body = FILLED.replace("None\n", "<!-- my note -->\nNone\n")
        self.assertEqual([], pull_request.body_problems(body, TEMPLATE))

    def test_a_closing_line_must_name_one_issue_with_closes(self) -> None:
        for line in (
            "Closes #12, #13",
            "Closes #12 and #13",
            "Fixes #12",
            "closes #12",
            "Resolves: #12",
            "Closes #12.",
        ):
            with self.subTest(line=line):
                body = FILLED.replace("Closes #34", line)
                self.assertEqual([f"CLOSES_LINE 2 {line}"], pull_request.body_problems(body, TEMPLATE))
                self.assertEqual([12], pull_request.closed_issues(body))

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


class TitleCheckTests(unittest.TestCase):
    def test_one_line_is_the_title_with_its_line_break_dropped(self) -> None:
        self.assertEqual((AWKWARD_TITLE, []), pull_request.read_title(AWKWARD_TITLE + "\r\n"))

    def test_an_empty_title_and_one_of_several_lines_are_refused(self) -> None:
        self.assertEqual(("", ["TITLE_EMPTY"]), pull_request.read_title(" \n"))
        self.assertEqual(["TITLE_LINES 2"], pull_request.read_title("fix: a\nsecond\n")[1])


class TemplateLookupTests(unittest.TestCase):
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


class Repository(unittest.TestCase):
    """A clone of a bare origin whose main holds the template, on a branch with one commit of its own."""

    git_config = GIT_CONFIG

    def setUp(self) -> None:
        self.temporary = Path(tempfile.mkdtemp(prefix="implement-change-test-")).resolve()
        self.addCleanup(remove_tree, self.temporary)
        origin = self.temporary / "origin.git"
        git(self.temporary, "init", "--quiet", "--bare", str(origin))
        self.root = self.temporary / "work tree"
        git(self.temporary, "clone", "--quiet", str(origin), str(self.root))
        (self.root / ".github").mkdir()
        (self.root / ".github" / "pull_request_template.md").write_text(TEMPLATE, encoding="utf-8")
        self.commit("template")
        git(self.root, "push", "--quiet", "origin", "main")
        git(self.root, "switch", "--quiet", "-c", "fix/title")
        (self.root / "tool.py").write_text("print('title')\n", encoding="utf-8")
        self.commit("change")
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

    def run_script(self, *arguments: str) -> tuple[int, list[str]]:
        output = io.StringIO()
        github = GitHubClient(runner=self.fake_gh)
        with contextlib.redirect_stdout(output):
            code = pull_request.main(["--repository", str(self.root), *arguments], github=github)
        return code, output.getvalue().splitlines()

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
        (self.root / ".github" / "pull_request_template.md").unlink()
        code, lines = self.run_script("draft")
        self.assertEqual((1, ["FAILED no pull request template in .github/, the top folder, or docs/"]), (code, lines))

    def test_an_issue_that_is_not_a_positive_number_is_a_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            self.run_script("draft", "--closes", "#12")
        self.assertEqual(2, raised.exception.code)


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

    def test_uncommitted_changes_are_counted(self) -> None:
        (self.root / "scratch.txt").write_text("scratch\n", encoding="utf-8")
        (self.root / "tool.py").write_text("print('changed')\n", encoding="utf-8")
        code, lines = self.run_script("check", *self.files_arguments())
        self.assertEqual((1, "UNCOMMITTED 2"), (code, lines[-1]))

    def test_body_and_title_problems_are_each_named(self) -> None:
        self.title.write_text("", encoding="utf-8")
        self.body.write_text(FILLED.replace("Closes #34", "Closes #34, #35"), encoding="utf-8")
        code, lines = self.run_script("check", *self.files_arguments())
        self.assertEqual(1, code)
        self.assertIn("TITLE_EMPTY", lines)
        self.assertIn("CLOSES_LINE 2 Closes #34, #35", lines)

    def test_a_fetch_that_fails_is_a_failed_line(self) -> None:
        arguments = [*self.files_arguments()[:-1], "nowhere/main"]
        code, lines = self.run_script("check", *arguments)
        self.assertEqual(1, code)
        self.assertTrue(lines[-1].startswith("FAILED cannot fetch main from nowhere: "), lines)

    def test_an_unreadable_file_is_a_failed_line(self) -> None:
        self.body.unlink()
        code, lines = self.run_script("check", *self.files_arguments())
        self.assertEqual(1, code)
        self.assertTrue(lines[-1].startswith(f'FAILED cannot read "{self.body}": '), lines)

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

    def test_a_check_problem_stops_the_open(self) -> None:
        git(self.root, "push", "--quiet", "-u", "origin", "HEAD")
        self.body.write_text(FILLED.replace("Closes #34", "Closes #"), encoding="utf-8")
        code, lines = self.run_script("open", *self.files_arguments())
        self.assertEqual((1, "PLACEHOLDER 2 a Closes line with no issue"), (code, lines[-1]))
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
