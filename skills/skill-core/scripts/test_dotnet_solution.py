"""Regression tests for the one reader of a .NET solution's project list."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dotnet_solution
from dotnet_solution import (
    FileRefused,
    decode_text,
    is_solution,
    listed_projects,
    project_path,
    read_bounded,
    read_listed_projects,
    read_text,
    refusal,
    same_path,
    solution_projects,
)
from xml_reader import XmlRefused

SLN = (
    "Microsoft Visual Studio Solution File, Format Version 12.00\r\n"
    'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "Harbor.Web", "src\\Harbor.Web\\Harbor.Web.csproj", "{1}"\r\n'
    "EndProject\r\n"
    'Project("{2150E333-8FDC-42A3-9474-1A3956D46DE8}") = "Solution Items", "Solution Items", "{2}"\r\n'
    "EndProject\r\n"
    'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "Harbor.Core", "src\\Harbor.Core\\Harbor.Core.csproj", '
    '"{3}"\r\n'
    "EndProject\r\n"
)
SLNX = (
    "<Solution>\n"
    '  <Folder Name="/src/">\n'
    '    <Project Path="src\\Harbor.Web\\Harbor.Web.csproj" />\n'
    '    <Project Path="src/Harbor.Core/Harbor.Core.csproj" />\n'
    "  </Folder>\n"
    "</Solution>\n"
)


class SolutionReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dotnet-solution-"))
        self.addCleanup(self.remove_root)

    def remove_root(self) -> None:
        for path in sorted(self.root.rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        self.root.rmdir()

    def test_module_is_the_one_reader(self) -> None:
        self.assertEqual((".sln", ".slnx"), dotnet_solution.SOLUTION_SUFFIXES)

    def test_sln_lists_every_project_entry_as_written_solution_folders_included(self) -> None:
        solution = self.root / "Harbor.sln"
        solution.write_bytes(SLN.encode("utf-8-sig"))
        self.assertEqual(
            ["src\\Harbor.Web\\Harbor.Web.csproj", "Solution Items", "src\\Harbor.Core\\Harbor.Core.csproj"],
            read_listed_projects(solution),
        )
        # A caller that wants project files alone filters them: a solution folder has no path that exists.
        self.assertEqual(
            {
                same_path(self.root / "src/Harbor.Web/Harbor.Web.csproj"),
                same_path(self.root / "Solution Items"),
                same_path(self.root / "src/Harbor.Core/Harbor.Core.csproj"),
            },
            solution_projects(solution),
        )

    def test_utf16_sln_is_read(self) -> None:
        solution = self.root / "Harbor.sln"
        solution.write_bytes(b"\xff\xfe" + SLN.encode("utf-16-le"))
        self.assertEqual(3, len(read_listed_projects(solution)))
        self.assertTrue(read_text(solution).startswith("Microsoft Visual Studio"))

    def test_text_a_caller_read_itself_is_decoded_and_listed_as_the_file_is(self) -> None:
        self.assertEqual(SLN, decode_text(b"\xff\xfe" + SLN.encode("utf-16-le")))
        self.assertEqual(SLN, decode_text(SLN.encode("utf-8-sig")))
        self.assertEqual(SLN, decode_text(SLN.encode("utf-8")))
        solution = self.root / "Harbor.sln"
        solution.write_bytes(SLN.encode("utf-8"))
        self.assertEqual(read_listed_projects(solution), listed_projects(SLN, solution))
        self.assertEqual(
            ["src\\Harbor.Web\\Harbor.Web.csproj", "src/Harbor.Core/Harbor.Core.csproj"],
            listed_projects(SLNX, self.root / "Harbor.SLNX"),
        )
        with self.assertRaises(XmlRefused):
            listed_projects("<!DOCTYPE x>" + SLNX, self.root / "Harbor.slnx")

    def test_slnx_lists_each_project_element_path_in_any_folder(self) -> None:
        solution = self.root / "Harbor.slnx"
        solution.write_text(SLNX, encoding="utf-8")
        self.assertEqual(
            ["src\\Harbor.Web\\Harbor.Web.csproj", "src/Harbor.Core/Harbor.Core.csproj"],
            read_listed_projects(solution),
        )
        self.assertEqual(2, len(solution_projects(solution)))

    def test_a_slnx_it_cannot_read_is_refused_and_an_empty_one_lists_nothing(self) -> None:
        solution = self.root / "Harbor.slnx"
        for text, reason in (
            ("<!DOCTYPE x>" + SLNX, "DOCTYPE"),
            ('<Solution><Project Path="a.csproj"></Solution>', ""),
        ):
            with self.subTest(reason=reason or "malformed"):
                solution.write_text(text, encoding="utf-8")
                for reader in (read_listed_projects, solution_projects):
                    with self.assertRaises(XmlRefused) as refused:
                        reader(solution)
                    self.assertIn(reason, str(refused.exception))
        solution.write_text("<Solution />", encoding="utf-8")
        self.assertEqual(([], set()), (read_listed_projects(solution), solution_projects(solution)))

    def test_is_solution_matches_either_suffix_in_any_case(self) -> None:
        self.assertTrue(is_solution(Path("Harbor.SLN")))
        self.assertTrue(is_solution(Path("Harbor.slnx")))
        self.assertFalse(is_solution(Path("Harbor.csproj")))

    def test_same_path_normalizes_separators_and_case_as_the_platform_does(self) -> None:
        self.assertEqual(same_path(Path("a/./b")), same_path(Path("a") / "b"))
        if os.name == "nt":
            self.assertEqual(same_path(Path("A/B")), same_path(Path("a\\b")))


class BoundedReadTests(unittest.TestCase):
    """Every read opens only a regular file and refuses one above the one bound, with the file and the reason."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dotnet-solution-bound-"))
        self.addCleanup(self.remove_root)

    def remove_root(self) -> None:
        for path in sorted(self.root.rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() and not path.is_symlink() else path.unlink()
        self.root.rmdir()

    def test_the_bound_is_the_documented_one(self) -> None:
        self.assertEqual(67108864, dotnet_solution.READ_LIMIT_BYTES)

    def test_a_regular_file_within_the_bound_is_read_and_one_above_it_is_refused(self) -> None:
        path = self.root / "Lib.csproj"
        path.write_bytes(b"x" * 16)
        too_large = "the file is larger than the reader's bound of 16 bytes, so it is not read"
        with mock.patch.object(dotnet_solution, "READ_LIMIT_BYTES", 16):
            self.assertEqual(b"x" * 16, read_bounded(path))
            self.assertEqual("x" * 16, read_text(path))
            path.write_bytes(b"x" * 17)
            for reader in (read_bounded, read_text):
                with self.subTest(reader=reader.__name__), self.assertRaises(FileRefused) as refused:
                    reader(path)
                self.assertEqual((path, too_large), (refused.exception.path, refused.exception.reason))
                self.assertEqual(f"{path} cannot be read: {too_large}", str(refused.exception))
            # A file whose size the system understates, as a growing file's or a special file's, is cut at the bound.
            small = os.stat_result((stat.S_IFREG | 0o644, 0, 0, 1, 0, 0, 1, 0, 0, 0))
            with mock.patch.object(Path, "stat", return_value=small), self.assertRaises(FileRefused) as refused:
                read_bounded(path)
            self.assertEqual(too_large, refused.exception.reason)

    def test_a_folder_a_missing_file_and_a_device_are_refused_as_an_os_error(self) -> None:
        refusals = [
            (self.root, "not a regular file, so the reader does not open it"),
            (self.root / "Missing.sln", None),
        ]
        # A device is made where the platform makes one; the refusal never opens it, so /dev/zero cannot hold the run.
        if Path("/dev/zero").exists():
            (self.root / "Zero.sln").symlink_to("/dev/zero")
            refusals.append((self.root / "Zero.sln", "not a regular file, so the reader does not open it"))
        # A caller that reports a file it cannot read, as an OSError, reports a refused one the same way.
        self.assertTrue(issubclass(FileRefused, OSError))
        for path, reason in refusals:
            with self.subTest(path=path.name), self.assertRaises(FileRefused) as refused:
                read_text(path)
            self.assertEqual(path, refused.exception.path)
            if reason is not None:
                self.assertEqual(reason, refused.exception.reason)

    def test_a_solution_above_the_bound_is_refused_never_read_as_one_that_lists_no_project(self) -> None:
        for name, text in (("Harbor.sln", SLN), ("Harbor.slnx", SLNX)):
            solution = self.root / name
            solution.write_text(text, encoding="utf-8")
            with mock.patch.object(dotnet_solution, "READ_LIMIT_BYTES", 8):
                for reader in (read_listed_projects, solution_projects):
                    with self.subTest(solution=name, reader=reader.__name__), self.assertRaises(FileRefused) as refused:
                        reader(solution)
                    self.assertIn("larger than the reader's bound of 8 bytes", refused.exception.reason)


class NamedPathTests(unittest.TestCase):
    """The lexical rule for a path a solution or project names, which both .NET skills apply."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dotnet-solution-paths-")).resolve()
        self.addCleanup(self.root.rmdir)

    def test_a_named_path_is_made_absolute_and_judged_without_reaching_the_file_system(self) -> None:
        tree = self.root / "repo"
        project_dir = tree / "App"
        written = {
            "..\\Lib\\Lib.csproj": None,
            "Inner/../Inner.csproj": None,
            "..\\..\\outside\\Outside.csproj": "outside the examined tree",
            "..\\..\\repo-sibling\\Sibling.csproj": "outside the examined tree",
            "\\\\example.invalid\\share\\x.csproj": "network share",
            "//example.invalid/share/x.csproj": "network share",
            "\\\\?\\C:\\x\\x.csproj": "device",
        }
        with mock.patch.object(Path, "stat", side_effect=AssertionError("reached the file system")):
            for text, reason in written.items():
                with self.subTest(written=text):
                    refused = refusal(project_path(project_dir, text), tree)
                    if reason is None:
                        self.assertIsNone(refused)
                    else:
                        self.assertIn(reason, refused or "")
            # A local device form is refused on every platform: as a device where it names one, else as a share.
            self.assertIsNotNone(refusal(project_path(project_dir, "\\\\.\\pipe\\x.csproj"), tree))
        self.assertEqual(tree / "Lib" / "Lib.csproj", project_path(project_dir, "..\\Lib\\Lib.csproj"))


if __name__ == "__main__":
    unittest.main()
