from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import atomic_file
import csharp_layout as layout
import dotnet_solution
import whole_number
from run_dotnet_format import Completed, Services

LINKED_FILE = "the file is a symbolic link or junction, which the script does not write through"
READ_ONLY_FILE = "the file is read-only; make it writable to fix it"
OUTSIDE_THROUGH_LINK = "a symbolic link or junction leads it outside the repository root"


def linked_folder(name: str) -> str:
    return f"its folder {name} is a symbolic link or junction, which the script does not write through"


def link_directory(target: Path, link: Path) -> None:
    """A real link to a directory: a junction on Windows, which needs no privilege, else a symbolic link."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def outside_directory(test: unittest.TestCase) -> Path:
    """A throwaway directory beside the test's repository root, outside it."""
    directory = tempfile.TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    return Path(directory.name).resolve()


@contextlib.contextmanager
def recording_paths(*methods: str) -> Iterator[list[str]]:
    """Record each path the named `Path` methods are called on, while they still do what they do."""
    touched: list[str] = []

    def recording(original: Callable[..., object]) -> Callable[..., object]:
        def record(path: Path, *arguments: object) -> object:
            touched.append(str(path))
            return original(path, *arguments)

        return record

    with contextlib.ExitStack() as stack:
        for method in methods:
            stack.enter_context(mock.patch.object(Path, method, recording(getattr(Path, method))))
        yield touched


def source(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def rules(text: str) -> list[tuple[int, str]]:
    """(one-based line, rule) for each violation."""
    return [(violation.line + 1, violation.rule) for violation in layout.check_text(text)]


# Every kind of literal and comment, each containing braces, quotes, and directive-like lines that are not code.
TRICKY = source(
    "class C",  # 1
    "{",  # 2
    "    #region Literals",  # 3
    "",  # 4
    "    void M()",  # 5
    "    {",  # 6
    '        var a = "{ \\" }";',  # 7
    '        var b = @"{ "" }',  # 8
    "#endregion",  # 9  inside a verbatim string
    '";',  # 10
    '        var c = $"{{ {d["k"]:N2} }} {(x ? "}" : "{")}";',
    '        var e = $@"{{ {f} ',  # 12
    '#endregion }}";',  # 13 inside an interpolated verbatim string
    '        var g = """',  # 14
    "            { #endregion",  # 15
    '            "" }',  # 16
    '            """;',  # 17
    '        var h = $$"""{{{i}}} { }""";',  # 18
    "        char j = '{', k = '\\'', l = '\"';",  # 19
    "        /* {",  # 20
    "#endregion",  # 21 inside a block comment
    "        */ // }",  # 22
    "        var m = global::System.Math.PI;",  # 23
    '        var n = $"{global::System.Math.PI}";',  # 24
    "    }",  # 25
    "",  # 26
    "    #endregion",  # 27
    "}",  # 28
)


class LexerTests(unittest.TestCase):
    def test_literals_and_comments_hide_braces_and_directives(self) -> None:
        scan = layout.Lexer(TRICKY).lex()
        directives = [(event.line + 1, event.name) for event in scan.events if event.kind == "directive"]
        braces = [(event.line + 1, event.kind) for event in scan.events if event.kind != "directive"]
        self.assertEqual([(3, "region"), (27, "endregion")], directives)
        self.assertEqual([(2, "open"), (6, "open"), (25, "close"), (28, "close")], braces)
        self.assertEqual({9, 10, 13, 15, 16, 17, 21, 22}, {line + 1 for line in scan.continuation})
        self.assertEqual([], rules(TRICKY))

    def test_multi_dollar_raw_strings_open_holes_only_at_their_brace_count(self) -> None:
        text = source('var s = $$$"""{{ {{{x}}} }}""";', "{")
        self.assertEqual([(1, "open")], [(event.line, event.kind) for event in layout.Lexer(text).lex().events])


# Where Lexer.code stopped, its line, its events as (kind, line, name), and its continuation lines.
Lexed = tuple[int, int, list[tuple[str, int, str]], list[int]]


def lexed(text: str, hole: int = 0) -> Lexed:
    """Lexer.code run directly from the start of text: where it stopped, its line, its events, its continuations."""
    lexer = layout.Lexer(text)
    lexer.code(hole=hole)
    events = [(event.kind, event.line, event.name) for event in lexer.scan.events]
    return lexer.i, lexer.line, events, sorted(lexer.scan.continuation)


class CodeTests(unittest.TestCase):
    """Lexer.code called directly, for each arm, with where it stops and every event and continuation it records."""

    def test_a_directive_counts_only_at_the_start_of_a_line(self) -> None:
        lexer = layout.Lexer("x\n#region")
        lexer.i = 1
        lexer.code(hole=0)
        self.assertEqual([("directive", 1, "region")], [(e.kind, e.line, e.name) for e in lexer.scan.events])
        lexer = layout.Lexer("x#region")
        lexer.i = 1
        lexer.code(hole=0)
        self.assertEqual([], lexer.scan.events)


class RuleTests(unittest.TestCase):
    def test_endregion_must_close_in_the_scope_its_region_opened(self) -> None:
        text = source(
            "class C",  # 1
            "{",  # 2
            "    #region Outer",  # 3
            "",  # 4
            "    void A()",  # 5
            "    {",  # 6
            "    #endregion",  # 7  deeper than its #region
            "",  # 8
            "        #region Inner",  # 9
            "    }",  # 10
            "",  # 11
            "    void B()",  # 12
            "    {",  # 13
            "        #endregion",  # 14 same depth as #region Inner, but another method
            "    }",  # 15
            "    #region Last",  # 16
            "",  # 17
            "    int f;",  # 18
            "}",  # 19
            "#endregion",  # 20 shallower than its #region
        )
        found = [(violation.line + 1, violation.rule, violation.severity) for violation in layout.check_text(text)]
        self.assertEqual(
            [(7, "region-scope", "error"), (14, "region-scope", "error"), (20, "region-scope", "error")], found
        )
        self.assertIn("(line 9)", layout.check_text(text)[1].message)

    def test_conditional_branches_do_not_double_count_braces(self) -> None:
        text = source(
            "#region All",
            "#if NET48",
            "class C : Base {",
            "#else",
            "class C {",
            "#endif",
            "    int f;",
            "}",
            "",
            "#endregion",
        )
        self.assertEqual([], rules(text))

    def test_endregion_description_and_spacing_rules(self) -> None:
        text = source(
            "class C",  # 1
            "{",  # 2
            "    #region A",  # 3
            "",  # 4
            "    int a;",  # 5
            "",  # 6
            "    #endregion A",  # 7  description, and no blank line before #region B
            "    #region B",  # 8
            "",  # 9
            "    int b;",  # 10
            "",  # 11
            "    #endregion",  # 12 two blank lines before #region C
            "",  # 13
            "",  # 14
            "    #region C",  # 15
            "",  # 16
            "    #region Nested",  # 17
            "",  # 18
            "    int c;",  # 19
            "",  # 20
            "    #endregion",  # 21 directly before another #endregion: allowed
            "    #endregion // done",  # 22 description, and a blank line before the closing brace
            "",  # 23
            "}",  # 24
        )
        self.assertEqual(
            [
                (7, "endregion-description"),
                (7, "region-spacing"),
                (12, "region-spacing"),
                (22, "endregion-brace-spacing"),
                (22, "endregion-description"),
            ],
            rules(text),
        )
        fixed, applied = layout.fix_text(text)
        self.assertEqual(5, len(applied))
        self.assertEqual([], rules(fixed))
        self.assertEqual(
            source(
                "class C",
                "{",
                "    #region A",
                "",
                "    int a;",
                "",
                "    #endregion",
                "",
                "    #region B",
                "",
                "    int b;",
                "",
                "    #endregion",
                "",
                "    #region C",
                "",
                "    #region Nested",
                "",
                "    int c;",
                "",
                "    #endregion",
                "    #endregion",
                "}",
            ),
            fixed,
        )
        self.assertEqual((fixed, []), layout.fix_text(fixed))

    def test_endregion_at_end_of_file_without_newline(self) -> None:
        text = "#region A\nclass C { }\n\n#endregion trailing"
        fixed, applied = layout.fix_text(text)
        self.assertEqual("#region A\nclass C { }\n\n#endregion", fixed)
        self.assertEqual(["endregion-description"], [violation.rule for violation in applied])


class FileTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def main(self, *arguments: str) -> tuple[int, list[list[str]]]:
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = layout.main(list(arguments))
        self.assertEqual("", errors.getvalue())
        return status, [line.split("\t") for line in output.getvalue().splitlines()]

    def file_list(self, *names: str) -> Path:
        path = self.root / "files.txt"
        path.write_text("".join(f"{name}\0" for name in names), encoding="utf-8")
        return path

    def test_check_and_fix_preserve_bom_crlf_and_legacy_encoding(self) -> None:
        crlf = self.root / "Src" / "With Space.cs"
        crlf.parent.mkdir()
        crlf_text = "class C\r\n{\r\n    #region A\r\n\r\n    int a;\r\n\r\n    #endregion A\r\n\r\n}\r\n"
        crlf.write_bytes(b"\xef\xbb\xbf" + crlf_text.encode("utf-8"))
        legacy = self.root / "Legacy.cs"
        legacy.write_bytes("// caf\xe9\n#region A\n\n#endregion\n#region B\n\n#endregion\n".encode("cp1252"))
        clean = self.root / "Clean.cs"
        clean.write_bytes(b"class D { }\n")
        file_list = self.root / "files.txt"
        file_list.write_text("Src/With Space.cs\0Legacy.cs\0Clean.cs\0", encoding="utf-8")

        status, lines = self.main("check", "--repo-root", str(self.root), "--file-list", str(file_list))
        self.assertEqual(1, status, "violations are findings")
        self.assertEqual(
            [
                [
                    "VIOLATION",
                    "Src/With Space.cs",
                    "7",
                    "warning",
                    "endregion-brace-spacing",
                    "expected no blank lines between #endregion and the closing brace, found 1",
                ],
                [
                    "VIOLATION",
                    "Src/With Space.cs",
                    "7",
                    "warning",
                    "endregion-description",
                    "#endregion must not have a description; remove the trailing text",
                ],
                [
                    "VIOLATION",
                    "Legacy.cs",
                    "4",
                    "warning",
                    "region-spacing",
                    "expected exactly 1 blank line between #endregion and the next #region, found 0",
                ],
                ["SUMMARY", "3", "2", "endregion-brace-spacing,endregion-description,region-spacing"],
            ],
            lines,
        )

        status, lines = self.main("check", "--repo-root", str(self.root), "--file-list", str(file_list), "--fix")
        self.assertEqual(0, status, "every violation was fixed")
        self.assertEqual(
            [
                ["FIXED", "Src/With Space.cs", "7", "endregion-brace-spacing"],
                ["FIXED", "Src/With Space.cs", "7", "endregion-description"],
                ["FIXED", "Legacy.cs", "4", "region-spacing"],
                ["SUMMARY", "0", "0", "-"],
            ],
            lines,
        )
        self.assertEqual(
            b"\xef\xbb\xbf" + b"class C\r\n{\r\n    #region A\r\n\r\n    int a;\r\n\r\n    #endregion\r\n}\r\n",
            crlf.read_bytes(),
        )
        self.assertEqual(
            "// caf\xe9\n#region A\n\n#endregion\n\n#region B\n\n#endregion\n".encode("cp1252"), legacy.read_bytes()
        )
        self.assertEqual(b"class D { }\n", clean.read_bytes())

    def test_scope_violations_are_reported_but_not_fixed(self) -> None:
        path = self.root / "Scope.cs"
        original = b"class C\n{\n    #region A\n\n    void M()\n    {\n    #endregion\n    }\n}\n"
        path.write_bytes(original)
        file_list = self.root / "files.txt"
        file_list.write_text("Scope.cs\0", encoding="utf-8")
        status, lines = self.main("check", "--repo-root", str(self.root), "--file-list", str(file_list), "--fix")
        self.assertEqual(1, status, "a violation --fix cannot fix remains")
        self.assertEqual(["VIOLATION", "Scope.cs", "7", "error", "region-scope"], lines[0][:5])
        self.assertEqual(["SUMMARY", "1", "1", "region-scope"], lines[1])
        self.assertEqual(original, path.read_bytes())

    def test_a_clean_check_exits_0(self) -> None:
        (self.root / "Clean.cs").write_bytes(b"class C\n{\n    #region A\n\n    int a;\n\n    #endregion\n}\n")
        for extra in ((), ("--fix",)):
            with self.subTest(extra=extra):
                status, lines = self.main(
                    "check", "--repo-root", str(self.root), "--file-list", str(self.file_list("Clean.cs")), *extra
                )
                self.assertEqual((0, [["SUMMARY", "0", "0", "-"]]), (status, lines))

    def test_a_file_whose_byte_order_mark_does_not_fit_its_bytes_is_unreadable_and_the_rest_are_checked(self) -> None:
        # An editor saving one Windows-1252 character into a UTF-8 file with a byte order mark leaves this behind.
        bad = b'\xef\xbb\xbfclass A { string s = "\xff"; }\n'
        odd = b"\xff\xfe" + "class B { }\n".encode("utf-16-le") + b"\x00"
        (self.root / "Bad.cs").write_bytes(bad)
        (self.root / "Odd.cs").write_bytes(odd)
        (self.root / "Good.cs").write_bytes(b"#region R\n\n#endregion R\n")
        file_list = self.file_list("Good.cs", "Bad.cs", "Odd.cs")
        status, lines = self.main("check", "--repo-root", str(self.root), "--file-list", str(file_list))
        self.assertEqual(1, status)
        self.assertEqual(
            [
                [
                    "UNREADABLE",
                    "Bad.cs",
                    "it starts with a UTF-8 byte order mark, but byte 25 is not UTF-8 text (invalid start byte)",
                ],
                [
                    "UNREADABLE",
                    "Odd.cs",
                    "it starts with a UTF-16-LE byte order mark, but byte 26 is not UTF-16-LE text (truncated data)",
                ],
                ["VIOLATION", "Good.cs", "3", "warning", "endregion-description"],
                ["SUMMARY", "1", "1", "endregion-description"],
            ],
            [fields[:5] for fields in lines],
        )
        status, lines = self.main("check", "--repo-root", str(self.root), "--file-list", str(file_list), "--fix")
        self.assertEqual(1, status, "an unreadable file is never clean")
        self.assertEqual(["UNREADABLE", "UNREADABLE", "FIXED", "SUMMARY"], [fields[0] for fields in lines])
        self.assertEqual(["SUMMARY", "0", "0", "-"], lines[-1])
        self.assertEqual((bad, odd), ((self.root / "Bad.cs").read_bytes(), (self.root / "Odd.cs").read_bytes()))
        self.assertEqual(b"#region R\n\n#endregion\n", (self.root / "Good.cs").read_bytes())

    def test_a_file_that_cannot_be_read_is_unreadable_with_the_reason(self) -> None:
        path = self.root / "With Space.cs"
        path.write_bytes(b"class A {}\n")
        with (
            mock.patch.object(Path, "read_bytes", side_effect=PermissionError(13, "Permission denied")),
            self.assertRaises(layout.Unreadable) as raised,
        ):
            layout.read_source(path)
        self.assertEqual("it cannot be read: Permission denied", str(raised.exception))

    def test_a_listed_file_that_does_not_exist_fails_before_any_check_or_fix(self) -> None:
        fixable = self.root / "Fixable.cs"
        original = b"#region A\n\n#endregion A\n"
        fixable.write_bytes(original)
        file_list = self.file_list("Fixable.cs", "Gone/Missing.cs")
        for extra in ((), ("--fix",)):
            with self.subTest(extra=extra):
                status, lines = self.main("check", "--repo-root", str(self.root), "--file-list", str(file_list), *extra)
                self.assertEqual((1, [[f"FAILED {self.root / 'Gone' / 'Missing.cs'} does not exist"]]), (status, lines))
                self.assertEqual(original, fixable.read_bytes())

    def test_an_unreadable_file_list_fails(self) -> None:
        missing = self.root / "absent.txt"
        status, lines = self.main("check", "--repo-root", str(self.root), "--file-list", str(missing))
        self.assertEqual(1, status)
        self.assertEqual(1, len(lines))
        self.assertTrue(lines[0][0].startswith("FAILED [Errno 2] "), lines)
        self.assertIn("absent.txt", lines[0][0])

        undecodable = self.root / "files.txt"
        undecodable.write_bytes(b"caf\xe9.cs\0")
        for command in (("check",), ("config", "--solution", "App.sln")):
            with self.subTest(command=command[0]):
                status, lines = self.main(*command, "--repo-root", str(self.root), "--file-list", str(undecodable))
                self.assertEqual((1, [[f"FAILED {undecodable} is not UTF-8 text"]]), (status, lines))

    def test_an_empty_file_list_fails_as_the_formatter_does(self) -> None:
        empty = self.root / "files.txt"
        empty.write_text("", encoding="utf-8")
        for command in (("check",), ("config", "--solution", "App.sln")):
            with self.subTest(command=command[0]):
                status, lines = self.main(*command, "--repo-root", str(self.root), "--file-list", str(empty))
                self.assertEqual((1, [[f"FAILED {empty} lists no files"]]), (status, lines))

    def test_a_fix_whose_write_fails_part_way_leaves_the_file_as_it_was(self) -> None:
        fixable = self.root / "Fixable.cs"
        original = b"\xef\xbb\xbf#region A\n\n#endregion A\n"
        fixable.write_bytes(original)
        file_list = self.file_list("Fixable.cs")
        write = os.write
        full = OSError(28, "No space left on device")

        def part_way(descriptor: int, data: bytes | memoryview) -> int:
            write(descriptor, bytes(data[:4]))
            raise full

        with mock.patch.object(os, "write", side_effect=part_way):
            status, lines = self.main("check", "--repo-root", str(self.root), "--file-list", str(file_list), "--fix")
        self.assertEqual((1, [["FAILED [Errno 28] No space left on device"]]), (status, lines))
        self.assertEqual(original, fixable.read_bytes())
        self.assertEqual(["Fixable.cs", "files.txt"], sorted(path.name for path in self.root.iterdir()))

    def read_only(self, path: Path) -> None:
        path.chmod(stat.S_IREAD)
        self.addCleanup(path.chmod, stat.S_IREAD | stat.S_IWRITE)

    def test_fix_refuses_each_read_only_file_wherever_it_is_listed_and_fixes_the_rest(self) -> None:
        names = ("A.cs", "B.cs", "C.cs")
        original, fixed = b"#region A\n\n#endregion A\n", b"#region A\n\n#endregion\n"
        for read_only in ({"A.cs"}, {"B.cs"}, {"C.cs"}, {"A.cs", "C.cs"}, set(names)):
            with self.subTest(read_only=sorted(read_only)):
                for name in names:
                    path = self.root / name
                    if path.exists():
                        path.chmod(stat.S_IREAD | stat.S_IWRITE)
                    path.write_bytes(original)
                    if name in read_only:
                        self.read_only(path)
                file_list = self.file_list(*names)
                status, lines = self.main(
                    "check", "--repo-root", str(self.root), "--file-list", str(file_list), "--fix"
                )
                self.assertEqual(1, status, "the violations a refused file keeps remain")
                self.assertEqual(
                    [
                        *(["REFUSED", name, READ_ONLY_FILE] for name in names if name in read_only),
                        *(["FIXED", name, "3", "endregion-description"] for name in names if name not in read_only),
                        *(["VIOLATION", name, "3", "warning", "endregion-description"] for name in sorted(read_only)),
                        ["SUMMARY", str(len(read_only)), str(len(read_only)), "endregion-description"],
                    ],
                    [line[:5] if line[0] == "VIOLATION" else line for line in lines],
                )
                for name in names:
                    expected = original if name in read_only else fixed
                    self.assertEqual(expected, (self.root / name).read_bytes(), name)
                self.assertEqual(["A.cs", "B.cs", "C.cs", "files.txt"], sorted(p.name for p in self.root.iterdir()))

    def test_a_read_only_file_with_nothing_to_fix_is_checked_and_not_refused(self) -> None:
        clean = self.root / "Clean.cs"
        clean.write_bytes(b"#region A\n#endregion\n")
        self.read_only(clean)
        status, lines = self.main(
            "check", "--repo-root", str(self.root), "--file-list", str(self.file_list("Clean.cs")), "--fix"
        )
        self.assertEqual((0, [["SUMMARY", "0", "0", "-"]]), (status, lines))

    def test_a_write_that_fails_after_the_checks_ends_the_fix_with_the_earlier_fixes_written(self) -> None:
        names = ("A.cs", "B.cs", "C.cs")
        original = b"#region A\n\n#endregion A\n"
        for name in names:
            (self.root / name).write_bytes(original)
        replace = atomic_file.replace_bytes
        full = OSError(28, "No space left on device")

        def failing_on_b(path: Path, data: bytes, *, mode: int) -> None:
            if path.name == "B.cs":
                raise full
            replace(path, data, mode=mode)

        with mock.patch.object(layout, "replace_bytes", side_effect=failing_on_b):
            status, lines = self.main(
                "check", "--repo-root", str(self.root), "--file-list", str(self.file_list(*names)), "--fix"
            )
        self.assertEqual(
            (1, [["FIXED", "A.cs", "3", "endregion-description"], ["FAILED [Errno 28] No space left on device"]]),
            (status, lines),
        )
        self.assertEqual(b"#region A\n\n#endregion\n", (self.root / "A.cs").read_bytes())
        self.assertEqual(original, (self.root / "B.cs").read_bytes())
        self.assertEqual(original, (self.root / "C.cs").read_bytes())

    def test_a_fix_keeps_a_lone_surrogate_a_file_with_a_byte_order_mark_holds(self) -> None:
        # The bytes ED A0 80 encode U+D800, which only surrogatepass reads and writes back.
        fixable = self.root / "Fixable.cs"
        fixable.write_bytes(b"\xef\xbb\xbf// \xed\xa0\x80\n#region A\n\n#endregion A\n")
        status, lines = self.main(
            "check", "--repo-root", str(self.root), "--file-list", str(self.file_list("Fixable.cs")), "--fix"
        )
        self.assertEqual((0, "FIXED"), (status, lines[0][0]))
        self.assertEqual(b"\xef\xbb\xbf// \xed\xa0\x80\n#region A\n\n#endregion\n", fixable.read_bytes())

    def test_a_fix_keeps_the_file_permissions(self) -> None:
        fixable = self.root / "Fixable.cs"
        fixable.write_bytes(b"#region A\n\n#endregion A\n")
        fixable.chmod(0o640)
        before = stat.S_IMODE(fixable.stat().st_mode)
        status, lines = self.main(
            "check", "--repo-root", str(self.root), "--file-list", str(self.file_list("Fixable.cs")), "--fix"
        )
        self.assertEqual((0, "FIXED"), (status, lines[0][0]))
        self.assertEqual(b"#region A\n\n#endregion\n", fixable.read_bytes())
        self.assertEqual(before, stat.S_IMODE(fixable.stat().st_mode))

    def test_fix_never_writes_a_file_that_is_or_lies_below_a_link(self) -> None:
        original = b"#region A\n\n#endregion A\n"
        for name in ("Linked.cs", "Linked/Inner.cs", "Fixable.cs"):
            (self.root / name).parent.mkdir(exist_ok=True)
            (self.root / name).write_bytes(original)
        file_list = self.file_list("Linked.cs", "Linked/Inner.cs", "Fixable.cs")
        # Links stand in through the stubbed test on every platform; the real-link test below makes one.
        with mock.patch.object(
            layout, "is_link_or_junction", side_effect=lambda path: path.name in {"Linked.cs", "Linked"}
        ):
            status, lines = self.main("check", "--repo-root", str(self.root), "--file-list", str(file_list), "--fix")
        self.assertEqual(1, status, "the violations a refused file keeps remain")
        self.assertEqual(
            [
                ["REFUSED", "Linked.cs", LINKED_FILE],
                ["REFUSED", "Linked/Inner.cs", linked_folder("Linked")],
                ["FIXED", "Fixable.cs", "3", "endregion-description"],
                ["VIOLATION", "Linked.cs", "3", "warning", "endregion-description"],
                ["VIOLATION", "Linked/Inner.cs", "3", "warning", "endregion-description"],
                ["SUMMARY", "2", "2", "endregion-description"],
            ],
            [line[:5] if line[0] == "VIOLATION" else line for line in lines],
        )
        self.assertEqual(original, (self.root / "Linked.cs").read_bytes())
        self.assertEqual(original, (self.root / "Linked" / "Inner.cs").read_bytes())
        self.assertEqual(b"#region A\n\n#endregion\n", (self.root / "Fixable.cs").read_bytes())

    def test_check_without_fix_reads_through_a_link_and_never_asks(self) -> None:
        (self.root / "Linked.cs").write_bytes(b"#region A\n\n#endregion A\n")
        with mock.patch.object(layout, "is_link_or_junction", side_effect=AssertionError("asked without --fix")):
            status, lines = self.main(
                "check", "--repo-root", str(self.root), "--file-list", str(self.file_list("Linked.cs"))
            )
        self.assertEqual((1, ["VIOLATION", "SUMMARY"]), (status, [line[0] for line in lines]))

    def test_fix_never_writes_through_a_real_link(self) -> None:
        real = self.root / "Real"
        real.mkdir()
        original = b"#region A\n\n#endregion A\n"
        (real / "Target.cs").write_bytes(original)
        link_directory(real, self.root / "Linked")
        names = ["Linked/Target.cs"]
        expected = [["REFUSED", "Linked/Target.cs", linked_folder("Linked")]]
        if sys.platform != "win32":  # a symbolic link to a file needs a privilege Windows rarely grants
            (self.root / "File.cs").symlink_to(real / "Target.cs")
            names.append("File.cs")
            expected.append(["REFUSED", "File.cs", LINKED_FILE])
        status, lines = self.main(
            "check", "--repo-root", str(self.root), "--file-list", str(self.file_list(*names)), "--fix"
        )
        self.assertEqual(1, status)
        self.assertEqual(expected, [line for line in lines if line[0] == "REFUSED"])
        self.assertNotIn("FIXED", [line[0] for line in lines])
        self.assertEqual(original, (real / "Target.cs").read_bytes())

    def check_never_leaving_the_root(self, entry: str, outside: Path, reason: str) -> None:
        """Check, then fix, a list holding `entry`, which names a fixable file in `outside`, and an in-root fixable
        file: the entry is refused before anything opens a path outside the root, and the in-root file is fixed."""
        original = b"#region A\n\n#endregion A\n"
        (outside / "Outside.cs").write_bytes(original)
        inside = self.root / "Inside.cs"
        file_list = self.file_list(entry, "Inside.cs")
        root = dotnet_solution.same_path(self.root)
        for extra, verdict in (((), "VIOLATION"), (("--fix",), "FIXED")):
            with self.subTest(entry=entry, extra=extra):
                inside.write_bytes(original)
                with recording_paths("is_file", "read_bytes", "write_bytes") as touched:
                    status, lines = self.main(
                        "check", "--repo-root", str(self.root), "--file-list", str(file_list), *extra
                    )
                self.assertEqual(1, status, "a refused entry is a finding, since its file went unchecked")
                self.assertEqual(["REFUSED", entry], lines[0][:2])
                self.assertTrue(lines[0][2].startswith(reason), lines[0])
                self.assertEqual([verdict, "Inside.cs"], lines[1][:2])
                self.assertNotIn(entry, [line[1] for line in lines[1:] if len(line) > 1])
                self.assertEqual(
                    [],
                    [path for path in touched if not Path(dotnet_solution.same_path(Path(path))).is_relative_to(root)],
                )
                self.assertEqual(original, (outside / "Outside.cs").read_bytes())
        self.assertEqual(b"#region A\n\n#endregion\n", inside.read_bytes())

    def test_an_entry_whose_dot_dot_segments_leave_the_root_is_refused_unread(self) -> None:
        outside = outside_directory(self)
        entry = f"../{outside.name}/Outside.cs"
        self.check_never_leaving_the_root(entry, outside, "the path is outside the examined tree")

    def test_an_absolute_entry_outside_the_root_is_refused_unread(self) -> None:
        outside = outside_directory(self)
        self.check_never_leaving_the_root(
            (outside / "Outside.cs").as_posix(), outside, "the path is outside the examined tree"
        )

    def test_an_entry_a_link_leads_outside_the_root_is_refused_unread(self) -> None:
        outside = outside_directory(self)
        link_directory(outside, self.root / "Linked")
        self.check_never_leaving_the_root("Linked/Outside.cs", outside, OUTSIDE_THROUGH_LINK)

    def test_a_path_whose_link_state_cannot_be_read_is_not_written(self) -> None:
        (self.root / "Fixable.cs").write_bytes(b"#region A\n\n#endregion A\n")
        denied = PermissionError(13, "Permission denied")
        with mock.patch.object(layout, "is_link_or_junction", side_effect=denied):
            status, lines = self.main(
                "check", "--repo-root", str(self.root), "--file-list", str(self.file_list("Fixable.cs")), "--fix"
            )
        self.assertEqual(1, status)
        self.assertEqual(["REFUSED", "Fixable.cs", "Fixable.cs cannot be examined: Permission denied"], lines[0])
        self.assertEqual(b"#region A\n\n#endregion A\n", (self.root / "Fixable.cs").read_bytes())


SDK_PROJECT = '<Project Sdk="Microsoft.NET.Sdk"></Project>\n'
SOLUTION = 'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "App", "src\\App\\App.csproj", "{1}"\r\nEndProject\r\n'


class MSBuild:
    """Answers `dotnet msbuild ./<project> -getItem:...` as MSBuild's evaluation would, with the items given for each
    project by its path from the root, and none for any other, or with the one result given for every project."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.items: dict[str, dict[str, list[dict[str, str]]]] = {}
        self.result: Completed | None = None
        self.calls: list[tuple[list[str], Path, float]] = []

    def __call__(self, arguments: Sequence[str], cwd: Path, timeout: float) -> Completed:
        self.calls.append((list(arguments), cwd, timeout))
        if self.result is not None:
            return self.result
        project = (cwd / arguments[2]).resolve().relative_to(self.root).as_posix()
        items = {"PackageReference": [], "Analyzer": [], **self.items.get(project, {})}
        return Completed(0, json.dumps({"Items": items}, indent=2).encode("utf-8"))

    def evaluated(self) -> list[str]:
        return [(cwd / arguments[2]).resolve().relative_to(self.root).as_posix() for arguments, cwd, _ in self.calls]


PACKAGE_REFERENCE = {"PackageReference": [{"Identity": "Roslynator.Formatting.Analyzers", "Version": "4.12.0"}]}


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        (self.root / "src" / "App").mkdir(parents=True)
        (self.root / "src" / "App" / "App.csproj").write_text(SDK_PROJECT, encoding="utf-8")
        (self.root / "App.sln").write_text(SOLUTION, encoding="utf-8")
        self.file_list = self.root / "files.txt"
        self.changed("src/App/Program.cs")
        self.msbuild = MSBuild(self.root)
        self.services = Services(run=self.msbuild, which=lambda name: name)

    def changed(self, *names: str) -> None:
        self.file_list.write_text("".join(f"{name}\0" for name in names), encoding="utf-8")

    def run_config(self, *extra: str, solution: str = "App.sln") -> tuple[int, dict[str, list[list[str]]]]:
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = layout.main(
                [
                    "config",
                    "--repo-root",
                    str(self.root),
                    "--solution",
                    solution,
                    "--file-list",
                    str(self.file_list),
                    *extra,
                ],
                self.services,
            )
        self.assertEqual("", errors.getvalue())
        grouped: dict[str, list[list[str]]] = {}
        for line in output.getvalue().splitlines():
            kind, *fields = line.split("\t")
            grouped.setdefault(kind, []).append(fields)
        return status, grouped

    def config(self, *extra: str, solution: str = "App.sln") -> dict[str, list[list[str]]]:
        """The output grouped by kind, after checking that only a missing setting left unapplied exits 1."""
        status, grouped = self.run_config(*extra, solution=solution)
        unapplied = "--apply" not in extra and any(state == "missing" for _, _, state in grouped["SETTING"])
        self.assertEqual(1 if unapplied else 0, status)
        return grouped

    def states(self, grouped: dict[str, list[list[str]]]) -> dict[str, str]:
        return {key: state for key, _, state in grouped["SETTING"]}

    def test_a_missing_setting_is_a_finding_until_applied_and_the_rest_are_not(self) -> None:
        status, grouped = self.run_config()
        self.assertEqual(1, status)
        self.assertEqual(7, len(grouped["SETTING"]))
        status, grouped = self.run_config("--apply")
        self.assertEqual((0, 7), (status, len(grouped["ADDED"])))
        status, grouped = self.run_config()
        self.assertEqual((0, [["Roslynator.Formatting.Analyzers", "missing"]]), (status, grouped["PACKAGE"]))
        editorconfig = self.root / ".editorconfig"
        editorconfig.write_text(editorconfig.read_text(encoding="utf-8").replace("= true", "= false"), encoding="utf-8")
        status, grouped = self.run_config()
        self.assertEqual(0, status, "another value is the repository's choice")
        self.assertIn(["insert_final_newline", "true", "other:false"], grouped["SETTING"])

    def test_a_solution_outside_the_root_is_refused_before_it_is_read(self) -> None:
        outside = outside_directory(self)
        (outside / "Other.sln").write_text(SOLUTION, encoding="utf-8")
        link_directory(outside, self.root / "Linked")
        solutions = {
            f"../{outside.name}/Other.sln": "the path is outside the examined tree",
            (outside / "Other.sln").as_posix(): "the path is outside the examined tree",
            "Linked/Other.sln": "a symbolic link or junction leads it outside the repository root",
        }
        for solution, expected in solutions.items():
            with self.subTest(solution=solution):
                status, grouped = self.run_config("--apply", solution=solution)
                self.assertEqual(1, status)
                (line,) = grouped
                self.assertTrue(line.startswith(f"FAILED the solution {solution} is refused: {expected}"), line)
                self.assertEqual([], self.msbuild.calls)
                self.assertFalse((self.root / ".editorconfig").exists())

    def test_an_editorconfig_the_apply_adds_is_readable_by_everyone_and_writable_by_its_owner(self) -> None:
        with mock.patch("csharp_layout.replace_bytes", wraps=atomic_file.replace_bytes) as replace:
            status, grouped = self.run_config("--apply")
        self.assertEqual(0, status, grouped)
        self.assertEqual([0o644], [call.kwargs["mode"] for call in replace.call_args_list])
        self.assertTrue((self.root / ".editorconfig").is_file())

    def test_an_unwritable_editorconfig_fails_after_reporting(self) -> None:
        denied = PermissionError(13, "Permission denied", ".editorconfig")
        with mock.patch.object(tempfile, "mkstemp", side_effect=denied):
            status, grouped = self.run_config("--apply")
        self.assertEqual(1, status)
        self.assertEqual("FAILED [Errno 13] Permission denied: '.editorconfig'", list(grouped)[-1])
        self.assertFalse((self.root / ".editorconfig").exists())
        self.assertEqual(7, len(grouped["SETTING"]))
        self.assertNotIn("ADDED", grouped)

    def test_reports_missing_configuration_and_adds_only_editorconfig_settings(self) -> None:
        grouped = self.config()
        self.assertEqual([["Roslynator.Formatting.Analyzers", "missing"]], grouped["PACKAGE"])
        self.assertEqual({key: "missing" for key, _ in layout.SETTINGS}, self.states(grouped))
        self.assertNotIn("ADDED", grouped)
        self.assertFalse((self.root / ".editorconfig").exists())

        grouped = self.config("--apply")
        self.assertEqual([[".editorconfig", key, value] for key, value in layout.SETTINGS], grouped["ADDED"])
        self.assertEqual(
            "[*.cs]\n" + "".join(f"{key} = {value}\n" for key, value in layout.SETTINGS),
            (self.root / ".editorconfig").read_text(encoding="utf-8"),
        )
        grouped = self.config("--apply")
        self.assertEqual({key: "present" for key, _ in layout.SETTINGS}, self.states(grouped))
        self.assertNotIn("ADDED", grouped)
        self.assertEqual(["missing"], [state for _, state in grouped["PACKAGE"]])
        self.assertFalse(any(path.name == "Directory.Build.props" for path in self.root.rglob("*")))

    def test_respects_existing_values_sections_and_root_files(self) -> None:
        (self.root / ".editorconfig").write_text(
            "[*.cs]\ndotnet_diagnostic.RCS0002.severity = warning\n", encoding="utf-8"
        )
        nested = self.root / "src" / ".editorconfig"
        nested.write_bytes(
            b"\xef\xbb\xbf# comment\r\nroot = true\r\n\r\n[*]\r\ninsert_final_newline = false\r\n\r\n"
            b"[*.{cs,vb}]\r\ndotnet_diagnostic.RCS0041.severity = error\r\n\r\n"
            b"[*.vb]\r\ndotnet_diagnostic.RCS0010.severity = warning\r\n\r\n"
            b"[tests/**.cs]\r\ndotnet_diagnostic.RCS0012.severity = warning"
        )
        (self.root / "src" / "App.sln").write_text(SOLUTION.replace("src\\", ""), encoding="utf-8")
        states = self.states(self.config(solution="src/App.sln"))
        self.assertEqual("other:false", states["insert_final_newline"])
        self.assertEqual("present", states["dotnet_diagnostic.RCS0041.severity"])
        self.assertEqual("missing", states["dotnet_diagnostic.RCS0010.severity"])
        self.assertEqual("missing", states["dotnet_diagnostic.RCS0012.severity"])
        self.assertEqual("missing", states["dotnet_diagnostic.RCS0002.severity"])  # above root = true

        added = self.config("--apply", solution="src/App.sln")["ADDED"]
        self.assertEqual({"src/.editorconfig"}, {target for target, _, _ in added})
        self.assertNotIn("insert_final_newline", {key for _, key, _ in added})
        content = nested.read_bytes()
        self.assertTrue(content.startswith(b"\xef\xbb\xbf# comment\r\n"))
        self.assertTrue(
            content.startswith(
                b"\xef\xbb\xbf# comment\r\nroot = "
                b"true\r\n\r\n[*.cs]\r\ndotnet_diagnostic.RCS0010.severity = warning\r\n"
            ),
            "the added section comes first, so the existing sections still override it",
        )
        self.assertTrue(content.endswith(b"[tests/**.cs]\r\ndotnet_diagnostic.RCS0012.severity = warning"))
        self.assertNotIn(b"\n", content.replace(b"\r\n", b""))
        states = self.states(self.config(solution="src/App.sln"))
        self.assertEqual("other:false", states["insert_final_newline"])
        self.assertEqual({"present"}, {state for key, state in states.items() if key != "insert_final_newline"})

    def test_path_specific_choices_are_judged_per_changed_file_and_never_overridden(self) -> None:
        editorconfig = self.root / ".editorconfig"
        editorconfig.write_text(
            "root = true\n\n[src/**.cs]\ndotnet_diagnostic.RCS0041.severity = none\n", encoding="utf-8"
        )
        key = "dotnet_diagnostic.RCS0041.severity"
        self.assertEqual("other:none", self.states(self.config())[key])
        self.assertNotIn(key, {added for _, added, _ in self.config("--apply").get("ADDED", [])})

        editorconfig.write_text(
            "root = true\n\n[src/**.cs]\ndotnet_diagnostic.RCS0041.severity = none\n", encoding="utf-8"
        )
        self.changed("src/App/Program.cs", "tools/Build.cs")
        self.assertEqual("missing", self.states(self.config())[key])
        self.config("--apply")
        configs = layout.AnalyzerConfigs(self.root)
        self.assertEqual("none", configs.settings(self.root / "src/App/Program.cs")[key.casefold()])
        self.assertEqual("warning", configs.settings(self.root / "tools/Build.cs")[key.casefold()])
        self.assertTrue(editorconfig.read_text(encoding="utf-8").startswith("root = true\n\n[*.cs]\n"))

    def package(self, *extra: str) -> list[str]:
        (line,) = self.config(*extra)["PACKAGE"]
        self.assertEqual("Roslynator.Formatting.Analyzers", line[0])
        return line[1:]

    def test_the_package_is_read_from_the_projects_evaluation_never_from_its_text(self) -> None:
        # A comment, an item under a false condition, and a props file MSBuild does not import all name the package,
        # and the evaluation holds none of them.
        project = self.root / "src" / "App" / "App.csproj"
        project.write_text(
            '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup Condition="false">'
            '<PackageReference Include="Roslynator.Formatting.Analyzers" /></ItemGroup></Project>\n',
            encoding="utf-8",
        )
        (self.root / "src" / "Directory.Build.props").write_text(
            "<Project><!-- TODO: add Roslynator.Formatting.Analyzers once the team agrees --></Project>\n",
            encoding="utf-8",
        )
        (self.root / "Directory.Build.props").write_text(
            '<Project><ItemGroup><PackageReference Include="Roslynator.Formatting.Analyzers" /></ItemGroup>'
            "</Project>\n",
            encoding="utf-8",
        )
        self.assertEqual(["missing"], self.package())
        self.assertEqual(
            [
                (
                    [
                        "dotnet",
                        "msbuild",
                        "./App.csproj",
                        "-nologo",
                        "-nodeReuse:false",
                        "-getItem:PackageReference",
                        "-getItem:Analyzer",
                    ],
                    self.root / "src" / "App",
                    120,
                )
            ],
            self.msbuild.calls,
        )
        cases = (
            ({"PackageReference": [{"Identity": "roslynator.formatting.analyzers"}]}, "present"),
            ({"Analyzer": [{"Filename": "Roslynator.Formatting.Analyzers", "Extension": ".dll"}]}, "present"),
            ({"Analyzer": [{"Filename": "Roslynator.Formatting.Analyzers.CodeFixes"}]}, "missing"),
            ({"PackageReference": [{"Identity": "Roslynator.Analyzers"}]}, "missing"),
        )
        for items, state in cases:
            with self.subTest(items=items):
                self.msbuild.items["src/App/App.csproj"] = items
                self.assertEqual([state], self.package())

    def test_the_package_is_judged_for_each_solution_project_owning_a_listed_file(self) -> None:
        for name in ("Lib", "Other", "Loose"):
            (self.root / "src" / name).mkdir()
            (self.root / "src" / name / f"{name}.csproj").write_text(SDK_PROJECT, encoding="utf-8")
        (self.root / "App.sln").write_text(
            SOLUTION
            + SOLUTION.replace("App", "Lib").replace("{1}", "{2}")
            + SOLUTION.replace("App", "Other").replace("{1}", "{3}"),
            encoding="utf-8",
        )
        self.changed("src/App/Program.cs", "src/Lib/Code.cs", "src/App/More.cs", "src/Loose/Script.cs")
        self.msbuild.items["src/App/App.csproj"] = PACKAGE_REFERENCE
        self.assertEqual(["missing"], self.package())
        self.msbuild.calls.clear()
        self.msbuild.items["src/Lib/Lib.csproj"] = PACKAGE_REFERENCE
        self.assertEqual(["present"], self.package())
        self.assertEqual(["src/App/App.csproj", "src/Lib/Lib.csproj"], self.msbuild.evaluated())

    def test_an_evaluation_that_cannot_tell_leaves_the_package_unknown_with_the_reason(self) -> None:
        cases = (
            (Completed(1, b"error MSB4019: The imported project was not found.\n"), "could not evaluate"),
            (Completed(-1, b"", timed_out=True), "timed out"),
            (Completed(0, b"Welcome to .NET!\nnot the items\n"), "not JSON"),
            (Completed(0, b'{"Items": [1]}'), "not the items"),
        )
        reasons = (
            "MSBuild could not evaluate src/App/App.csproj: it exited with code 1",
            "MSBuild did not evaluate src/App/App.csproj within 120 seconds",
            "MSBuild's evaluation of src/App/App.csproj is not the items it was asked for",
            "MSBuild's evaluation of src/App/App.csproj is not the items it was asked for",
        )
        for (result, case), reason in zip(cases, reasons, strict=True):
            with self.subTest(case=case):
                self.msbuild.result = result
                self.assertEqual(["unknown", reason], self.package())
        self.msbuild.result = Completed(0, b"Welcome to .NET!\n" + json.dumps({"Items": PACKAGE_REFERENCE}).encode())
        self.assertEqual(["present"], self.package(), "text before the evaluation is passed over")

        self.msbuild.result = None
        self.msbuild.calls.clear()
        self.services.which = lambda name: None
        self.assertEqual(
            ["unknown", "dotnet is not installed, so MSBuild cannot evaluate the projects"], self.package()
        )
        self.assertEqual([], self.msbuild.calls)

    def test_a_project_lacking_the_package_is_missing_whatever_another_evaluation_says(self) -> None:
        (self.root / "src" / "Lib").mkdir()
        (self.root / "src" / "Lib" / "Lib.csproj").write_text(SDK_PROJECT, encoding="utf-8")
        (self.root / "App.sln").write_text(SOLUTION + SOLUTION.replace("App", "Lib"), encoding="utf-8")
        self.changed("src/Lib/Code.cs", "src/App/Program.cs")
        failing = self.msbuild.__call__

        def lib_fails(arguments: Sequence[str], cwd: Path, timeout: float) -> Completed:
            result = failing(arguments, cwd, timeout)
            return Completed(1, b"") if cwd.name == "Lib" else result

        self.services.run = lib_fails
        self.assertEqual(["missing"], self.package())
        self.msbuild.items["src/App/App.csproj"] = PACKAGE_REFERENCE
        self.assertEqual(
            ["unknown", "MSBuild could not evaluate src/Lib/Lib.csproj: it exited with code 1"], self.package()
        )

    def severity(self, rule: str, *extra: str) -> str:
        return self.states(self.config(*extra))[f"dotnet_diagnostic.{rule}.severity"]

    def added(self, *extra: str) -> list[list[str]]:
        return self.config("--apply", *extra).get("ADDED", [])

    def test_a_globalconfig_severity_is_read_and_apply_never_overrides_it(self) -> None:
        for text in ("is_global = true\n", "# no is_global: the name makes it global\n"):
            with self.subTest(text=text):
                (self.root / ".globalconfig").write_text(
                    f"{text}dotnet_diagnostic.RCS0063.severity = none\n", encoding="utf-8"
                )
                self.assertEqual("other:none", self.severity("RCS0063"))
                added = self.added()
                self.assertEqual(6, len(added))
                self.assertNotIn("dotnet_diagnostic.RCS0063.severity", {key for _, key, _ in added})
                self.assertEqual("other:none", self.severity("RCS0063"))
                (self.root / ".editorconfig").unlink()

    def test_an_editorconfig_value_wins_over_a_global_configs(self) -> None:
        (self.root / ".globalconfig").write_text("dotnet_diagnostic.RCS0063.severity = none\n", encoding="utf-8")
        (self.root / ".editorconfig").write_text(
            "[*.cs]\ndotnet_diagnostic.RCS0063.severity = warning\n", encoding="utf-8"
        )
        self.assertEqual("present", self.severity("RCS0063"))

    def test_only_the_nearest_globalconfig_at_or_above_the_project_folder_is_read(self) -> None:
        (self.root / ".globalconfig").write_text("dotnet_diagnostic.RCS0063.severity = none\n", encoding="utf-8")
        (self.root / "src" / ".globalconfig").write_text(
            "dotnet_diagnostic.RCS0063.severity = error\n", encoding="utf-8"
        )
        self.assertEqual("present", self.severity("RCS0063"), "the nearer file is the one the SDK adds")
        (self.root / "src" / ".globalconfig").unlink()
        (self.root / "src" / "App" / "Sub").mkdir()
        (self.root / "src" / "App" / "Sub" / ".globalconfig").write_text(
            "dotnet_diagnostic.RCS0041.severity = none\n", encoding="utf-8"
        )
        self.changed("src/App/Sub/Code.cs")
        self.assertEqual("other:none", self.severity("RCS0063"))
        self.assertEqual("missing", self.severity("RCS0041"), "the SDK looks from the project's folder up")

        repository = self.root / "repository"
        (repository / "src").mkdir(parents=True)
        (repository / "src" / "App.csproj").write_text(SDK_PROJECT, encoding="utf-8")
        self.assertEqual({}, layout.AnalyzerConfigs(repository).settings(repository / "src" / "Program.cs"))

    def test_the_higher_global_level_wins_and_one_level_set_twice_is_unset(self) -> None:
        (self.root / ".globalconfig").write_text("dotnet_diagnostic.RCS0063.severity = warning\n", encoding="utf-8")
        inner = self.root / "src" / "App" / ".editorconfig"
        cases = (
            ("global_level = 200\n", "other:none"),
            ("global_level = 100\n", "missing"),
            ("", "present"),
            ("global_level = 99\n", "present"),
            ("global_level = high\n", "present"),
        )
        for level, state in cases:
            with self.subTest(level=level):
                inner.write_text(
                    f"is_global = true\n{level}dotnet_diagnostic.RCS0063.severity = none\n", encoding="utf-8"
                )
                self.assertEqual(state, self.severity("RCS0063"))
        added = self.added()
        self.assertEqual({".editorconfig"}, {target for target, _, _ in added}, "a global file is no chain member")
        self.assertTrue(inner.read_text(encoding="utf-8").startswith("is_global = true\n"))

    def test_a_global_configs_section_applies_only_to_the_file_whose_absolute_path_it_names(self) -> None:
        program = (self.root / "src" / "App" / "Program.cs").as_posix()
        (self.root / ".globalconfig").write_text(
            f"is_global = true\n[{program}]\ndotnet_diagnostic.RCS0063.severity = none\n"
            f"[{program}.bak]\ndotnet_diagnostic.RCS0010.severity = none\n"
            "[*.cs]\ndotnet_diagnostic.RCS0041.severity = none\n",
            encoding="utf-8",
        )
        states = self.states(self.config())
        self.assertEqual("other:none", states["dotnet_diagnostic.RCS0063.severity"])
        self.assertEqual("missing", states["dotnet_diagnostic.RCS0010.severity"])
        self.assertEqual("missing", states["dotnet_diagnostic.RCS0041.severity"], "a glob is not a global section")

    def test_root_true_ends_the_chain_only_in_a_files_preamble(self) -> None:
        (self.root / ".editorconfig").write_text(
            "[*.cs]\ndotnet_diagnostic.CA1822.severity = warning\ndotnet_diagnostic.RCS0063.severity = none\n",
            encoding="utf-8",
        )
        nested = self.root / "src" / ".editorconfig"
        nested.write_text("[*.cs]\nroot = true\nindent_size = 4\n", encoding="utf-8")
        self.assertEqual("other:none", self.severity("RCS0063"), "the compiler ignores root inside a section")
        added = self.added()
        self.assertEqual({".editorconfig"}, {target for target, _, _ in added})
        self.assertNotIn("dotnet_diagnostic.RCS0063.severity", {key for _, key, _ in added})
        self.assertEqual("[*.cs]\nroot = true\nindent_size = 4\n", nested.read_text(encoding="utf-8"))

        nested.write_text("root = true\n\n[*.cs]\nindent_size = 4\n", encoding="utf-8")
        self.assertEqual("missing", self.severity("RCS0063"))
        self.assertEqual({"src/.editorconfig"}, {target for target, _, _ in self.added()})

    def test_apply_reports_an_editorconfig_it_cannot_decode_and_writes_nothing_there(self) -> None:
        editorconfig = self.root / ".editorconfig"
        undecodable = b"\xef\xbb\xbf# caf\xe9\n"
        editorconfig.write_bytes(undecodable)
        status, grouped = self.run_config("--apply")
        self.assertEqual(1, status, "the settings it could not add are still missing")
        ((target, reason),) = grouped["UNREADABLE"]
        self.assertEqual(".editorconfig", target)
        self.assertTrue(reason.startswith("it starts with a UTF-8 byte order mark, but byte 8 "), reason)
        self.assertNotIn("ADDED", grouped)
        self.assertEqual(undecodable, editorconfig.read_bytes())

    def test_a_solution_it_cannot_read_fails_instead_of_searching_only_its_folder(self) -> None:
        (self.root / "App.slnx").write_text(
            '<Solution><Project Path="src/App/App.csproj"></Solution>', encoding="utf-8"
        )
        status, grouped = self.run_config(solution="App.slnx")
        self.assertEqual(1, status)
        (failure,) = [kind for kind in grouped if kind.startswith("FAILED ")]
        self.assertTrue(failure.startswith(f"FAILED {self.root / 'App.slnx'} cannot be read: "), failure)
        self.assertIn("not well-formed", failure)
        self.assertNotIn("SETTING", grouped)

    def test_a_solution_above_the_read_bound_fails_with_the_reason(self) -> None:
        limit = 16
        with mock.patch.object(dotnet_solution, "READ_LIMIT_BYTES", limit):
            status, grouped = self.run_config()
        self.assertEqual(1, status)
        bound = f"the file is larger than the reader's bound of {limit} bytes, so it is not read"
        (failure,) = [kind for kind in grouped if kind.startswith("FAILED ")]
        self.assertEqual(f"FAILED {self.root / 'App.sln'} cannot be read: {bound}", failure)
        self.assertNotIn("SETTING", grouped)

    def test_a_solution_entry_outside_the_repository_is_refused_and_never_touched(self) -> None:
        outside = outside_directory(self)
        (outside / "Out").mkdir()
        (outside / "Out" / "Out.csproj").write_text(SDK_PROJECT, encoding="utf-8")
        (outside / "Directory.Build.props").write_text(
            '<Project><ItemGroup><PackageReference Include="Roslynator.Formatting.Analyzers" /></ItemGroup>'
            "</Project>\n",
            encoding="utf-8",
        )
        entries = {
            f"..\\{outside.name}\\Out\\Out.csproj": "the path is outside the examined tree",
            "\\\\localhost\\nonexistent-share\\X\\X.csproj": "the path names a network share",
            "//localhost/nonexistent-share/Y/Y.csproj": "the path names a network share",
            "\\\\?\\C:\\Device\\Z.csproj": "the path names a device",
        }
        listed = "".join(f'<Project Path="{entry}" />' for entry in entries)
        (self.root / "App.slnx").write_text(
            f'<Solution><Project Path="src\\App\\App.csproj" />{listed}</Solution>', encoding="utf-8"
        )
        touched: list[str] = []
        is_file = Path.is_file

        def recording(path: Path) -> bool:
            touched.append(str(path))
            return is_file(path)

        with mock.patch.object(Path, "is_file", recording):
            grouped = self.config(solution="App.slnx")
        self.assertEqual(list(entries), [entry for entry, _ in grouped["REFUSED"]])
        for (entry, reason), expected in zip(grouped["REFUSED"], entries.values(), strict=True):
            with self.subTest(entry=entry):
                self.assertTrue(reason.startswith(expected), reason)
        self.assertEqual([["Roslynator.Formatting.Analyzers", "missing"]], grouped["PACKAGE"])
        root = dotnet_solution.same_path(self.root)
        self.assertTrue(touched)
        self.assertEqual(
            [], [path for path in touched if not Path(dotnet_solution.same_path(Path(path))).is_relative_to(root)]
        )

    def test_apply_never_writes_an_editorconfig_that_is_or_lies_below_a_link(self) -> None:
        editorconfig = self.root / ".editorconfig"
        editorconfig.write_text("# shared\n", encoding="utf-8")
        with mock.patch.object(layout, "is_link_or_junction", side_effect=lambda path: path.name == ".editorconfig"):
            status, grouped = self.run_config("--apply")
        self.assertEqual(1, status, "the settings it did not add are still missing")
        self.assertEqual([[".editorconfig", LINKED_FILE]], grouped["REFUSED"])
        self.assertNotIn("ADDED", grouped)
        self.assertEqual("# shared\n", editorconfig.read_text(encoding="utf-8"))

    def test_apply_refuses_each_read_only_editorconfig_and_adds_the_rest(self) -> None:
        folders = ("a", "b", "c")
        for folder in folders:
            (self.root / folder).mkdir()
            (self.root / folder / ".editorconfig").write_text("root = true\n", encoding="utf-8")
        read_only = self.root / "b" / ".editorconfig"
        read_only.chmod(stat.S_IREAD)
        self.addCleanup(read_only.chmod, stat.S_IREAD | stat.S_IWRITE)
        self.changed(*(f"{folder}/Program.cs" for folder in folders))
        status, grouped = self.run_config("--apply")
        self.assertEqual(1, status, "the settings it did not add are still missing")
        self.assertEqual([["b/.editorconfig", READ_ONLY_FILE]], grouped["REFUSED"])
        self.assertEqual(
            [[f"{folder}/.editorconfig", key, value] for folder in ("a", "c") for key, value in layout.SETTINGS],
            grouped["ADDED"],
        )
        self.assertEqual("root = true\n", read_only.read_text(encoding="utf-8"))
        for folder in ("a", "c"):
            self.assertIn("[*.cs]", (self.root / folder / ".editorconfig").read_text(encoding="utf-8"))

    def test_apply_never_writes_through_a_real_link(self) -> None:
        real = self.root / "real"
        real.mkdir()
        shared = real / ".editorconfig"
        shared.write_text("root = true\n", encoding="utf-8")
        link_directory(real, self.root / "linked")
        self.changed("linked/Program.cs")
        status, grouped = self.run_config("--apply")
        self.assertEqual(1, status)
        self.assertEqual([["linked/.editorconfig", linked_folder("linked")]], grouped["REFUSED"])
        self.assertNotIn("ADDED", grouped)
        self.assertEqual("root = true\n", shared.read_text(encoding="utf-8"))
        self.assertFalse(list(self.root.glob(".editorconfig")))

    def test_an_entry_outside_the_root_is_refused_and_never_judged_or_written(self) -> None:
        outside = outside_directory(self)
        (outside / "Shared").mkdir()
        shared = outside / "Shared" / ".editorconfig"
        shared.write_text("root = true\n", encoding="utf-8")
        link_directory(outside / "Shared", self.root / "linked")
        entries = {
            f"../{outside.name}/Shared/Program.cs": "the path is outside the examined tree",
            (outside / "Shared" / "Program.cs").as_posix(): "the path is outside the examined tree",
            "linked/Program.cs": OUTSIDE_THROUGH_LINK,
        }
        for entry, reason in entries.items():
            with self.subTest(entry=entry):
                self.changed(entry, "src/App/Program.cs")
                status, grouped = self.run_config("--apply")
                self.assertEqual(1, status, "a refused entry is a finding, since its file went unjudged")
                self.assertEqual([entry], [refused for refused, _ in grouped["REFUSED"]])
                self.assertTrue(grouped["REFUSED"][0][1].startswith(reason), grouped["REFUSED"])
                self.assertEqual({".editorconfig"}, {added[0] for added in grouped["ADDED"]})
                self.assertEqual("root = true\n", shared.read_text(encoding="utf-8"))
                (self.root / ".editorconfig").unlink()

    def test_a_list_every_entry_of_which_is_refused_fails(self) -> None:
        outside = outside_directory(self)
        self.changed(f"../{outside.name}/Program.cs")
        status, grouped = self.run_config("--apply")
        self.assertEqual(1, status)
        self.assertEqual([f"../{outside.name}/Program.cs"], [entry for entry, _ in grouped["REFUSED"]])
        self.assertEqual(["REFUSED", "FAILED no listed file lies inside the repository root"], list(grouped))
        self.assertFalse(list(self.root.glob(".editorconfig")))

    def test_a_single_star_section_does_not_configure_deeper_files(self) -> None:
        settings = "".join(f"{key} = {value}\n" for key, value in layout.SETTINGS)
        (self.root / ".editorconfig").write_text(f"root = true\n\n[src/*.cs]\n{settings}", encoding="utf-8")
        self.assertEqual({"missing"}, set(self.states(self.config()).values()), "src/App/Program.cs is not in src/")
        self.changed("src/Top.cs")
        self.assertEqual({"present"}, set(self.states(self.config()).values()))


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
def test_line_endings_are_counted_alike(newline: str) -> None:
    text = newline.join(("class C", "{", "    #region A", "", "    #endregion", "", "}", ""))
    assert rules(text) == [(5, "endregion-brace-spacing")]


# Each literal holds braces or directive-like lines that would add events if lexed as code, and the brace after it is
# recorded only if the literal ended in the right place: (text, that brace's line, continuation lines).
EDGE_CASE_LITERALS: list[tuple[str, int, list[int]]] = [
    # Verbatim strings: a backslash is literal, and a doubled quote is one quote.
    (r'var a = @"C:\"; {', 0, []),
    ('var a = @"a ""{"" }"; {', 0, []),
    ('var a = @"""#region"""; {', 0, []),
    ('var a = $@"{{ {b} }}"; {', 0, []),
    ('var a = @$"{{ {b} }}"; {', 0, []),
    ('var a = @"\n}\n#endregion\n"; {', 3, [1, 2, 3]),
    ('var a = $@"{new { B = @"}""}" }.B}"; {', 0, []),
    # Raw strings: closed only by a run of at least the opening quotes.
    ('var a = """b "" { c"""; {', 0, []),
    ('var a = """"b """ } c""""; {', 0, []),
    ('var a = """\n    }\n#endregion\n    """; {', 3, [1, 2, 3]),
    ('var a = $$"""{b} {{c}}"""; {', 0, []),
    ('var a = $$"""{{new { B = "}" }.B}}"""; {', 0, []),
    # Nested braces in interpolation holes: only the brace that closes the hole ends it.
    ('var a = $"{new { B = 1 }.B}"; {', 0, []),
    ('var a = $"{b switch { 1 => "}", _ => "{" }}"; {', 0, []),
    ('var a = $"{F(() => { return "}"; })}"; {', 0, []),
    ('var a = $"{$"{new { B = $"{c}" }}"}"; {', 0, []),
    ('var a = $"{\n    new[] { 1 }[0]\n}"; {', 2, [1, 2]),
]


@pytest.mark.parametrize(
    ("text", "line", "continuation"), EDGE_CASE_LITERALS, ids=[case[0] for case in EDGE_CASE_LITERALS]
)
def test_edge_case_literals_end_where_csharp_ends_them(text: str, line: int, continuation: list[int]) -> None:
    scan = layout.Lexer(text).lex()
    assert [(line, "open")] == [(event.line, event.kind) for event in scan.events]
    assert continuation == sorted(scan.continuation)


OUTSIDE_A_HOLE: list[tuple[str, Lexed]] = [
    ("{}", (2, 0, [("open", 0, ""), ("close", 0, "")], [])),
    ("#region A", (9, 0, [("directive", 0, "region")], [])),
    ("#", (1, 0, [("directive", 0, "")], [])),
    (" \t\f\v#if X\n}", (11, 1, [("directive", 0, "if"), ("close", 1, "")], [])),
    ("x #region A", (11, 0, [], [])),
    ("{ #endregion", (12, 0, [("open", 0, "")], [])),
    ("x\n  #endregion", (14, 1, [("directive", 1, "endregion")], [])),
    ("x\r\n#region\r#endregion\n", (22, 3, [("directive", 1, "region"), ("directive", 2, "endregion")], [])),
    ("// { #region\n{", (14, 1, [("open", 1, "")], [])),
    ("/* {\n#region */ }", (17, 1, [("close", 1, "")], [1])),
    ("/* {", (4, 0, [], [])),
    ("'{' '}' '\\'' { ", (15, 0, [("open", 0, "")], [])),
    ('"{" @"}" { ', (11, 0, [("open", 0, "")], [])),
    ("([{)]}", (6, 0, [("open", 0, ""), ("close", 0, "")], [])),
    (")) :: : {", (9, 0, [("open", 0, "")], [])),
    ("a::b }", (6, 0, [("close", 0, "")], [])),
]


@pytest.mark.parametrize(("text", "expected"), OUTSIDE_A_HOLE, ids=[text for text, _ in OUTSIDE_A_HOLE])
def test_code_outside_a_hole_records_braces_and_directives_to_the_end(text: str, expected: Lexed) -> None:
    assert expected == lexed(text)


HOLE_ENDS: list[tuple[str, int, Lexed]] = [
    ("} rest {", 1, (1, 0, [], [])),
    ("x}}}", 2, (3, 0, [], [])),
    ("x}", 3, (2, 0, [], [])),
    ("{a}}{", 1, (4, 0, [], [])),
    ("{{a}}}", 1, (6, 0, [], [])),
    ("abc", 1, (3, 0, [], [])),
    ("a\n#region\nb}", 1, (12, 2, [], [1, 2])),
    ("#region }", 1, (9, 0, [], [])),
    ("/* } */ '}' \"}\" } x", 1, (17, 0, [], [])),
]


@pytest.mark.parametrize(("text", "hole", "expected"), HOLE_ENDS, ids=[text for text, _, _ in HOLE_ENDS])
def test_a_hole_returns_after_its_closing_braces(text: str, hole: int, expected: Lexed) -> None:
    assert expected == lexed(text, hole)


FORMAT_CLAUSES: list[tuple[str, int, Lexed]] = [
    ("x:N2} rest", 1, (5, 0, [], [])),
    ("x:{a}} rest", 1, (5, 0, [], [])),
    ("x:a\nb}}} rest", 2, (7, 1, [], [1])),
    # Inside parentheses or brackets a colon is code, so a brace in a string after it does not end the hole.
    ('(b ? x : "}")} rest', 1, (14, 0, [], [])),
    ('[b ? x : "}"]} rest', 1, (14, 0, [], [])),
    # Once they close, a colon starts the format clause again.
    ('(a):"}"} rest', 1, (6, 0, [], [])),
    ('[a]:"}"} rest', 1, (6, 0, [], [])),
    # A closing parenthesis or bracket with none open leaves the count at zero.
    ('):"}"} rest', 1, (4, 0, [], [])),
    (']]:"}"} rest', 1, (5, 0, [], [])),
    ("{a:b}} rest", 1, (6, 0, [], [])),
    ("a::b{c}} rest", 1, (8, 0, [], [])),
    ("a ? b : c} rest", 1, (10, 0, [], [])),
]


@pytest.mark.parametrize(("text", "hole", "expected"), FORMAT_CLAUSES, ids=[text for text, _, _ in FORMAT_CLAUSES])
def test_a_colon_at_a_holes_top_level_starts_its_format_clause(text: str, hole: int, expected: Lexed) -> None:
    assert expected == lexed(text, hole)


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param([], id="no-command"),
        pytest.param(["check", "--file-list", "f"], id="check-without-repo-root"),
        pytest.param(["config", "--repo-root", ".", "--file-list", "f"], id="config-without-solution"),
        pytest.param(
            ["config", "--repo-root", ".", "--solution", "A.sln", "--file-list", "f", "--fix"], id="config-with-fix"
        ),
    ],
)
def test_usage_errors_exit_2_with_usage_on_stderr(arguments: list[str]) -> None:
    output, errors = io.StringIO(), io.StringIO()
    with (
        contextlib.redirect_stdout(output),
        contextlib.redirect_stderr(errors),
        pytest.raises(SystemExit) as raised,
    ):
        layout.main(arguments)
    assert raised.value.code == 2
    assert output.getvalue() == ""
    assert "usage:" in errors.getvalue()


@pytest.mark.parametrize(
    ("section", "path", "expected"),
    [
        ("*", "A.cs", True),
        ("*.{cs,vb}", "src/A.cs", True),
        ("*.vb", "A.cs", False),
        ("*.cs", "src/deep/A.cs", True),
        # `*` never crosses a path separator; `**` does.
        ("src/*.cs", "src/A.cs", True),
        ("src/*.cs", "src/deep/A.cs", False),
        ("src/**.cs", "src/deep/A.cs", True),
        ("tests/**.cs", "src/A.cs", False),
        ("**/A.cs", "A.cs", True),
        ("**/A.cs", "src/deep/A.cs", True),
        ("src/**/A.cs", "src/A.cs", True),
        ("src/**/A.cs", "src/deep/A.cs", True),
        # A separator anywhere, including a leading one, anchors the glob to the .editorconfig directory.
        ("/A.cs", "A.cs", True),
        ("/A.cs", "src/A.cs", False),
        ("src/A.cs", "other/src/A.cs", False),
        ("?.cs", "src/A.cs", True),
        ("src?A.cs", "src/A.cs", False),
        ("[AB].cs", "B.cs", True),
        ("[AB].cs", "C.cs", False),
        ("[!AB].cs", "C.cs", True),
        ("[!AB].cs", "A.cs", False),
        ("[a-c].cs", "b.cs", True),
        ("[a-c].cs", "d.cs", False),
        ("[*].cs", "*.cs", True),
        ("[*].cs", "A.cs", False),
        ("[a/b].cs", "x/[a/b].cs", True),  # a bracket holding a separator is literal and anchors nothing
        ("[a/b].cs", "a.cs", False),
        ("{a,{b,c}}.cs", "c.cs", True),
        ("{a,{b,c}}.cs", "d.cs", False),
        ("{single}.cs", "{single}.cs", True),
        ("{single}.cs", "single.cs", False),
        ("{src/A,B}.cs", "src/A.cs", True),
        ("{src/A,B}.cs", "deep/B.cs", False),
        ("File{1..3}.cs", "File2.cs", True),
        ("File{1..3}.cs", "File4.cs", False),
        ("File{3..1}.cs", "File1.cs", True),
        ("File{-3..-1}.cs", "File-2.cs", True),
        ("File{-3..-1}.cs", "File-4.cs", False),
        ("{a{1..2},b}.cs", "b.cs", True),
        ("{a{1..2},b}.cs", "a3.cs", False),
        (r"\*.cs", "*.cs", True),
        (r"\*.cs", "A.cs", False),
        ("[.cs", "[.cs", True),
        ("{.cs", "{.cs", True),
        ("*.CS", "A.cs", False),
    ],
)
def test_section_matching_follows_the_editorconfig_glob_specification(section: str, path: str, expected: bool) -> None:
    assert expected == layout.section_matches(section, path)


def test_a_numeric_range_bound_past_the_digit_limit_fails_by_name() -> None:
    refusal = f"an .editorconfig section's numeric range has a bound of more than {whole_number.DIGIT_LIMIT} digits"
    for section in ("*.{1..1" + "0" * 5000 + "}", "*.{-" + "9" * 5000 + "..1}"):
        with pytest.raises(layout.Failed) as raised:
            layout.section_glob(section)
        assert refusal == str(raised.value)


def test_a_numeric_range_inside_literal_braces_is_read_into_the_globs_ranges() -> None:
    # Braces without a comma are literal, and the range inside them is still the glob's one capturing group.
    pattern, ranges = layout.section_glob("{a{1..3}}.cs")
    assert ranges == ((1, 3),)
    assert layout.section_matches("{a{1..3}}.cs", "{a2}.cs")
    assert not layout.section_matches("{a{1..3}}.cs", "{a4}.cs")
    assert pattern.fullmatch("{a2}.cs") is not None


def test_braces_nested_past_the_shared_depth_limit_fail_by_name() -> None:
    # The limit is pinned as a literal: a glob of 100 nested braces reads, and one of 101 fails, never in a
    # RecursionError, whether the braces hold alternatives or are literal.
    assert layout.section_matches("{a," * 100 + "b" + "}" * 100, "b")
    assert layout.section_matches("{" * 100 + "x" + "}" * 100, "{" * 100 + "x" + "}" * 100)
    for section in ("{a," * 101 + "b" + "}" * 101, "{" * 101 + "x" + "}" * 101, "{a," * 2000 + "b" + "}" * 2000):
        with pytest.raises(layout.Failed) as raised:
            layout.section_glob(section)
        assert str(raised.value) == "an .editorconfig section's braces nest more than 100 deep"


@pytest.mark.parametrize(
    ("section", "bounds"),
    [("File{+2..-3}.cs", ((-3, 2),)), ("{1..1}{-0..07}", ((1, 1), (0, 7)))],
)
def test_a_numeric_range_reads_each_bound_with_its_sign(section: str, bounds: tuple[tuple[int, int], ...]) -> None:
    assert bounds == layout.section_glob(section)[1]


@pytest.mark.parametrize(
    ("section", "path", "expected"),
    [
        # `**/` matches zero or more whole directories; `**` elsewhere matches across separators.
        ("**/*.cs", "A.cs", True),
        ("**/*.cs", "x/y/A.cs", True),
        ("**/*.cs", "x/y/A.vb", False),
        ("/**/A.cs", "x/A.cs", True),
        ("src/**/A.cs", "srcx/A.cs", False),
        ("src/**/A.cs", "x/src/A.cs", False),
        ("a/**/b/**/c.cs", "a/b/c.cs", True),
        ("a/**/b/**/c.cs", "a/x/b/y/z/c.cs", True),
        ("a/**/b/**/c.cs", "a/x/c.cs", False),
        ("a**b.cs", "a/x/b.cs", True),
        ("src/**", "src/a/b.cs", True),
        ("*/**/A.cs", "A.cs", False),
        ("*/**/A.cs", "x/y/A.cs", True),
        ("{**/A,B}.cs", "x/y/A.cs", True),
        ("{**/A,B}.cs", "x/B.cs", False),
        # `[!seq]` matches one character outside seq, never a separator.
        ("[!a-c].cs", "d.cs", True),
        ("[!a-c].cs", "b.cs", False),
        ("x[!a]y", "xby", True),
        ("x[!a]y", "x/y", False),
        (r"[!\]].cs", "a.cs", True),
        (r"[!\]].cs", "].cs", False),
        ("[!!].cs", "a.cs", True),
        ("[!!].cs", "!.cs", False),
        (r"[\!a].cs", "!.cs", True),  # an escaped `!` is a member, not a negation
        (r"[\!a].cs", "b.cs", False),
        ("[!].cs", "[!].cs", True),  # an empty bracket is literal
        ("[!].cs", "a.cs", False),
        ("[!/]x", "y/[!/]x", True),  # a negated bracket holding a separator is literal too
    ],
)
def test_double_star_and_negated_bracket_globs(section: str, path: str, expected: bool) -> None:
    assert expected == layout.section_matches(section, path)
