"""Regression tests for the one reader of a .NET solution's project list."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dotnet_solution
from dotnet_solution import (
    decode_text,
    is_solution,
    listed_projects,
    read_listed_projects,
    read_text,
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


if __name__ == "__main__":
    unittest.main()
