"""Regression tests for the one program locator `deploy.py check` and the skills' extraction share."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import executable_locator
from executable_locator import find_executable, find_executables, link_folders, located_command, path_programs

# A name no machine's PATH holds, so only the fixture's folders can answer it.
PROGRAM = "fixture-locator-tool"


class LocatorTestCase(unittest.TestCase):
    """A sanitized environment: PATH is one empty folder and the local application data folder is a fresh one, so
    nothing on the machine running the test is found."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="executable locator ")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.path_folder = root / "on path"
        self.local = root / "local app data"
        self.links = self.local / "Microsoft" / "WinGet" / "Links"
        self.path_folder.mkdir()
        self.links.mkdir(parents=True)
        environment = mock.patch.dict(
            os.environ, {"PATH": str(self.path_folder), "PATHEXT": ".COM;.EXE", "LOCALAPPDATA": str(self.local)}
        )
        environment.start()
        self.addCleanup(environment.stop)

    def program(self, folder: Path) -> Path:
        program = folder / f"{PROGRAM}.exe"
        program.write_bytes(b"")
        return program


class FindExecutableTests(LocatorTestCase):
    def test_a_program_on_path_is_found_there(self) -> None:
        program = self.program(self.path_folder)
        found = find_executable(PROGRAM)
        self.assertIsNotNone(found)
        self.assertEqual(program, Path(str(found)))

    def test_a_program_only_in_wingets_links_folder_is_found_there(self) -> None:
        program = self.program(self.links)
        self.assertEqual(str(program), find_executable(PROGRAM))

    def test_path_comes_before_the_links_folder(self) -> None:
        on_path = self.program(self.path_folder)
        self.program(self.links)
        self.assertEqual(on_path, Path(str(find_executable(PROGRAM))))

    def test_a_program_in_neither_is_not_found(self) -> None:
        self.assertIsNone(find_executable(PROGRAM))

    def test_a_folder_in_the_links_folder_is_not_a_program(self) -> None:
        (self.links / f"{PROGRAM}.exe").mkdir()
        self.assertIsNone(find_executable(PROGRAM))

    def test_without_an_absolute_local_application_data_folder_nothing_relative_is_searched(self) -> None:
        # Were the folder taken relative to the working directory, a program there would be found.
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.local)
        self.program(self.links)
        for value in ("", "relative"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"LOCALAPPDATA": value}):
                self.assertEqual([], link_folders())
                self.assertIsNone(find_executable(PROGRAM))
        with mock.patch.dict(os.environ):
            del os.environ["LOCALAPPDATA"]
            self.assertEqual([], link_folders())

    def test_the_working_directory_is_not_searched(self) -> None:
        # shutil.which searches it first on Windows; PATH does not name it, so a program planted there is not run.
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.local)
        self.program(self.local)
        self.assertIsNone(find_executable(PROGRAM))


class FindExecutablesTests(LocatorTestCase):
    def test_every_candidate_in_search_order_path_first_then_the_links_folder(self) -> None:
        second = self.path_folder.parent / "second on path"
        second.mkdir()
        first_program = self.program(self.path_folder)
        second_program = self.program(second)
        linked = self.program(self.links)
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join((str(self.path_folder), str(second)))}):
            found = find_executables(PROGRAM)
            self.assertEqual([first_program, second_program, linked], [Path(path) for path in found])
            self.assertEqual(found[0], find_executable(PROGRAM), "the first candidate is the one found")

    def test_a_links_folder_that_is_also_on_path_yields_its_program_once(self) -> None:
        linked = self.program(self.links)
        with mock.patch.dict(os.environ, {"PATH": str(self.links)}):
            self.assertEqual([linked], [Path(path) for path in find_executables(PROGRAM)])

    def test_nothing_found_is_an_empty_list(self) -> None:
        self.assertEqual([], find_executables(PROGRAM))


class LocatedCommandTests(LocatorTestCase):
    def test_the_program_is_replaced_by_the_absolute_path_the_locator_finds(self) -> None:
        linked = self.program(self.links)
        command = located_command([PROGRAM, "--flag", "value with spaces"])
        self.assertEqual((linked, ["--flag", "value with spaces"]), (Path(command[0]), command[1:]))
        self.assertTrue(Path(command[0]).is_absolute())
        on_path = self.program(self.path_folder)
        self.assertEqual(on_path, Path(located_command([PROGRAM])[0]), "PATH comes first")

    def test_a_program_named_by_a_path_is_left_as_it_is(self) -> None:
        self.assertEqual([sys.executable, "-c", "pass"], located_command([sys.executable, "-c", "pass"]))

    def test_a_missing_program_raises_file_not_found_as_starting_it_would(self) -> None:
        with self.assertRaises(FileNotFoundError) as raised:
            located_command([PROGRAM, "--version"])
        self.assertEqual(PROGRAM, raised.exception.filename)


class PathProgramsTests(unittest.TestCase):
    def test_every_match_on_path_in_order_by_the_extension_rules_of_shutil_which(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first, second, third = (Path(temporary) / f"folder {index}" for index in range(3))
            for folder in (first, second, third):
                folder.mkdir()
            (first / "copilot").write_bytes(b"no extension, so not a Windows command")
            (first / "copilot.cmd").write_bytes(b"fixture")
            (first / "copilot.exe").write_bytes(b"fixture")
            (second / "copilot.exe").mkdir()
            (third / "copilot.bat").write_bytes(b"fixture")
            path = os.pathsep.join(("", str(first), str(second), str(first), str(third)))
            with mock.patch.dict(os.environ, {"PATH": path, "PATHEXT": ".EXE;.BAT;.CMD"}):
                self.assertEqual(
                    [str(first / "copilot.EXE"), str(first / "copilot.CMD"), str(third / "copilot.BAT")],
                    path_programs("copilot"),
                    "PATHEXT's order within a folder, PATH's across them; a folder once; no directory",
                )
                self.assertEqual(shutil.which("copilot", path=str(first)), path_programs("copilot")[0])
                self.assertEqual([str(third / "copilot.bat")], path_programs("copilot.bat"), "a name with one")
            with mock.patch.dict(os.environ, {"PATH": "", "PATHEXT": ".EXE"}):
                self.assertEqual([], path_programs("copilot"))


class LinkFoldersTests(LocatorTestCase):
    def test_windows_links_the_folder_winget_installs_portable_commands_into(self) -> None:
        with mock.patch.object(executable_locator.sys, "platform", "win32"):
            self.assertEqual([self.local / "Microsoft" / "WinGet" / "Links"], link_folders())

    def test_another_platform_has_no_link_folder_yet(self) -> None:
        self.program(self.links)
        with mock.patch.object(executable_locator.sys, "platform", "linux"):
            self.assertEqual([], link_folders())
            self.assertIsNone(find_executable(PROGRAM))


if __name__ == "__main__":
    unittest.main()
