"""Regression tests for the one decoder of the paths a git diff's file headers name."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from diff_paths import DiffPathError, header_path, unquote
from git_client import GitClient
from git_test_base import GitTestCase

SPACED = "tools/report age.py"
ACCENTED = "tools/résumé.py"


class RealDiffTests(GitTestCase):
    """Each header a real `git diff` writes names the path it was given."""

    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.checkout = Path(temporary.name)
        self.git = GitClient()
        self.git.output(["init", "--quiet"], directory=self.checkout)

    def commit(self, files: dict[str, str]) -> None:
        for path, text in files.items():
            (self.checkout / path).parent.mkdir(parents=True, exist_ok=True)
            (self.checkout / path).write_bytes(text.encode("utf-8"))
        self.git.output(["add", "--all"], directory=self.checkout)
        self.git.output(["commit", "--quiet", "--message", "change"], directory=self.checkout)

    def headers(self, prefixes: tuple[str, ...]) -> list[str]:
        diff = self.git.output(["diff", "HEAD~1", "HEAD"], directory=self.checkout)
        return [line for line in diff.split("\n") if line.startswith(prefixes)]

    def test_a_changed_path_with_a_space_keeps_git_s_tab_off(self) -> None:
        self.commit({SPACED: "DAYS = 30\n"})
        self.commit({SPACED: "DAYS = 7\n"})
        headers = self.headers(("--- ", "+++ "))
        # The fixture is what git writes: the name, then a tab.
        self.assertEqual(["--- a/tools/report age.py\t", "+++ b/tools/report age.py\t"], headers)
        self.assertEqual([SPACED, SPACED], [header_path(line[4:]) for line in headers])

    def test_a_quoted_path_is_decoded(self) -> None:
        self.commit({ACCENTED: "DAYS = 30\n"})
        self.commit({ACCENTED: "DAYS = 7\n"})
        headers = self.headers(("--- ", "+++ "))
        self.assertEqual(
            ['--- "a/tools/r\\303\\251sum\\303\\251.py"', '+++ "b/tools/r\\303\\251sum\\303\\251.py"'], headers
        )
        self.assertEqual([ACCENTED, ACCENTED], [header_path(line[4:]) for line in headers])

    def test_an_added_and_a_deleted_file_have_no_path_on_their_empty_side(self) -> None:
        self.commit({SPACED: "DAYS = 30\n"})
        (self.checkout / SPACED).unlink()
        self.commit({ACCENTED: "DAYS = 7\n"})
        diff = self.git.output(["diff", "--no-renames", "HEAD~1", "HEAD"], directory=self.checkout)
        headers = [line for line in diff.split("\n") if line.startswith(("--- ", "+++ "))]
        self.assertEqual([SPACED, None, None, ACCENTED], [header_path(line[4:]) for line in headers])

    def test_a_renamed_quoted_path_is_decoded_from_its_rename_lines(self) -> None:
        self.commit({SPACED: "DAYS = 30\nWEEKS = 4\nMONTHS = 1\n"})
        (self.checkout / SPACED).rename(self.checkout / ACCENTED)
        self.git.output(["add", "--all"], directory=self.checkout)
        self.git.output(["commit", "--quiet", "--message", "rename"], directory=self.checkout)
        headers = self.headers(("rename from ", "rename to "))
        self.assertEqual(["rename from tools/report age.py", 'rename to "tools/r\\303\\251sum\\303\\251.py"'], headers)
        self.assertEqual([SPACED, ACCENTED], [unquote(line.split(" ", 2)[2]) for line in headers])


class DecoderTests(GitTestCase):
    def test_each_escape_git_writes_is_undone(self) -> None:
        self.assertEqual('a\x07\x08\t\n\x0b\x0c\r"\\b', unquote('"a\\a\\b\\t\\n\\v\\f\\r\\"\\\\b"'))
        self.assertEqual("é", unquote('"\\303\\251"'))
        # With core.quotePath off, git writes a byte above ASCII as is and still quotes a name with a quote.
        self.assertEqual('Ā"', unquote('"Ā\\""'))

    def test_a_byte_that_is_not_utf_8_is_one_replacement_character(self) -> None:
        self.assertEqual("a�b", unquote('"a\\377b"'))

    def test_an_unquoted_value_is_as_given(self) -> None:
        self.assertEqual('a "b" \\c', unquote('a "b" \\c'))

    def test_a_header_s_line_end_tab_and_prefix_are_left_off_and_a_trailing_space_is_kept(self) -> None:
        self.assertEqual("x/a b.py", header_path("b/x/a b.py\t\r\n"))
        self.assertEqual("x/a b ", header_path("a/x/a b \t"))
        self.assertEqual("x/a.py", header_path("x/a.py"))
        self.assertEqual("x\ty.py", header_path('"b/x\\ty.py"'))
        self.assertIsNone(header_path("/dev/null"))

    def test_a_quoted_value_git_does_not_write_is_refused(self) -> None:
        for value in ('"b/a.py', '"', '"a"b"', '"a\\"', '"a\\q"', '"a\\12"', '"a\\400"', '"a\\08x"'):
            with self.subTest(value=value), self.assertRaisesRegex(DiffPathError, "^Malformed quoted diff path: "):
                unquote(value)


if __name__ == "__main__":
    unittest.main()
