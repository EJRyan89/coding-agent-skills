"""Regression tests for the one decoder of the paths a git diff's file headers name."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from diff_paths import HUNK_HEADER, DiffPathError, header_path, header_text, quote, snapshot_path, unquote
from git_client import GitClient

SPACED = "tools/report age.py"
ACCENTED = "tools/résumé.py"


class RealDiffTests(unittest.TestCase):
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

    def test_a_quoted_path_with_a_space_keeps_git_s_tab_off_before_it_is_decoded(self) -> None:
        quoted_spaced = "tools/résumé age.py"
        self.commit({quoted_spaced: "DAYS = 30\n"})
        self.commit({quoted_spaced: "DAYS = 7\n"})
        headers = self.headers(("--- ", "+++ "))
        # The fixture is what git writes: the quoted name, then the tab it ends a name holding a space with.
        self.assertEqual(
            ['--- "a/tools/r\\303\\251sum\\303\\251 age.py"\t', '+++ "b/tools/r\\303\\251sum\\303\\251 age.py"\t'],
            headers,
        )
        self.assertEqual([quoted_spaced, quoted_spaced], [header_path(line[4:]) for line in headers])

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


class DecoderTests(unittest.TestCase):
    def test_each_escape_git_writes_is_undone(self) -> None:
        self.assertEqual('a\x07\x08\t\n\x0b\x0c\r"\\b', unquote('"a\\a\\b\\t\\n\\v\\f\\r\\"\\\\b"'))
        self.assertEqual("é", unquote('"\\303\\251"'))
        # With core.quotePath off, git writes a byte above ASCII as is and still quotes a name with a quote.
        self.assertEqual('Ā"', unquote('"Ā\\""'))

    def test_a_byte_that_is_not_utf_8_is_one_replacement_character(self) -> None:
        self.assertEqual("a�b", unquote('"a\\377b"'))

    def test_an_unquoted_value_is_as_given(self) -> None:
        self.assertEqual('a "b" \\c', unquote('a "b" \\c'))

    def test_a_path_is_quoted_as_git_quotes_it_and_read_back_whole(self) -> None:
        # Literals as git writes them, from RealDiffTests and git's quote_c_style.
        self.assertEqual("a/x/a b.py", quote("a/x/a b.py"))
        self.assertEqual('"a/tools/r\\303\\251sum\\303\\251.py"', quote(f"a/{ACCENTED}"))
        self.assertEqual('"x\\a\\b\\t\\n\\v\\f\\r\\"\\\\\\001\\177"', quote('x\x07\x08\t\n\x0b\x0c\r"\\\x01\x7f'))
        for path in ("x/a b.py", ACCENTED, 'x/"q"\\r\x01\x7f.py', "x/\ufffd.py"):
            self.assertEqual(path, unquote(quote(path)))

    def test_a_header_s_text_names_its_path_as_git_writes_it(self) -> None:
        self.assertEqual("/dev/null", header_text(None))
        self.assertEqual("a/tools/report age.py\t", header_text(f"a/{SPACED}"))
        self.assertEqual('"b/tools/r\\303\\251sum\\303\\251 age.py"\t', header_text("b/tools/résumé age.py"))
        self.assertEqual("b/x.py", header_text("b/x.py"))
        for path in (f"a/{SPACED}", f"b/{ACCENTED}", "a/x/trailing "):
            self.assertEqual(path[2:], header_path(header_text(path)))
        self.assertIsNone(header_path(header_text(None)))

    def test_a_header_s_line_end_tab_and_prefix_are_left_off_and_a_trailing_space_is_kept(self) -> None:
        self.assertEqual("x/a b.py", header_path("b/x/a b.py\t\r\n"))
        self.assertEqual("x/a b ", header_path("a/x/a b \t"))
        self.assertEqual("x/a.py", header_path("x/a.py"))
        self.assertEqual("x\ty.py", header_path('"b/x\\ty.py"'))
        self.assertIsNone(header_path("/dev/null"))


def link_folder(link: Path, target: Path) -> None:
    """A link at `link` to the folder `target`: a junction on Windows, which needs no privilege, else a symlink."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


class SnapshotPathTests(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.outer = Path(temporary.name) / "outer folder"
        self.root = self.outer / "snapshot"
        (self.root / "tools").mkdir(parents=True)
        (self.root / "tools" / "inside.py").write_text("x = 1\n", encoding="utf-8")
        (self.outer / "outside.py").write_text("def private_name():\n    pass\n", encoding="utf-8")

    def test_a_path_inside_the_snapshot_is_where_it_lands(self) -> None:
        for path in ("tools/inside.py", "tools/not yet here.py", "tools/ab:c.py", "tools/..x.py"):
            with self.subTest(path=path):
                self.assertEqual(self.root.resolve().joinpath(*path.split("/")), snapshot_path(self.root, path))

    def test_a_path_that_could_land_outside_the_snapshot_is_refused_on_every_platform(self) -> None:
        for path in (
            "tools\\..\\..\\outside.py",
            "tools\\inside.py",
            "../outside.py",
            "tools/../../outside.py",
            "tools/./inside.py",
            "tools//inside.py",
            "tools/",
            "",
            "/etc/outside.py",
            "//server/share/outside.py",
            "C:/outside.py",
            "C:outside.py",
            "tools/D:outside.py",
            "tools/in\nside.py",
            "tools/in\x00side.py",
        ):
            with self.subTest(path=path):
                self.assertIsNone(snapshot_path(self.root, path))

    def test_this_platform_s_absolute_path_to_a_file_outside_is_refused(self) -> None:
        outside = (self.outer / "outside.py").resolve()
        self.assertTrue(outside.is_file())
        for path in (str(outside), outside.as_posix()):
            with self.subTest(path=path):
                self.assertIsNone(snapshot_path(self.root, path))

    def test_a_path_through_a_link_out_of_the_snapshot_is_refused(self) -> None:
        link_folder(self.root / "linked", self.outer)
        self.assertTrue((self.root / "linked" / "outside.py").is_file())
        self.assertIsNone(snapshot_path(self.root, "linked/outside.py"))


class UnresolvableSnapshotPathTests(unittest.TestCase):
    def test_a_path_whose_resolution_raises_is_refused(self) -> None:
        # Windows' non-strict realpath re-raises some errors, such as a name too long, so the error is raised here
        # for the one path, leaving the root to resolve.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "snapshot"
        root.mkdir()
        real_resolve = Path.resolve

        def resolve(path: Path, strict: bool = False) -> Path:
            if path.name == "unresolvable.py":
                raise OSError(206, "The filename or extension is too long")
            return real_resolve(path, strict)

        with mock.patch.object(Path, "resolve", resolve):
            self.assertIsNone(snapshot_path(root, "tools/unresolvable.py"))
            self.assertEqual(root.resolve() / "tools" / "inside.py", snapshot_path(root, "tools/inside.py"))


@pytest.mark.parametrize(
    ("line", "first"), [("@@ -1,3 +4,5 @@ def f():", "4"), ("@@ -7 +9 @@", "9"), ("@@ -0,0 +1 @@", "1")]
)
def test_a_hunk_header_gives_its_new_side_s_first_line_with_or_without_a_count(line: str, first: str) -> None:
    match = HUNK_HEADER.match(line)
    assert match is not None
    assert first == (match.group(1) if match else None)


@pytest.mark.parametrize("line", ["+@@ -1 +1 @@", "@@ +1 -1 @@", "@@@ -1 -1 +1 @@@", "@@ -a +1 @@"])
def test_a_line_that_is_not_a_hunk_header_does_not_match(line: str) -> None:
    assert HUNK_HEADER.match(line) is None


@pytest.mark.parametrize("value", ['"b/a.py', '"', '"a"b"', '"a\\"', '"a\\q"', '"a\\12"', '"a\\400"', '"a\\08x"'])
def test_a_quoted_value_git_does_not_write_is_refused(value: str) -> None:
    with pytest.raises(DiffPathError, match=r"^Malformed quoted diff path: "):
        unquote(value)
