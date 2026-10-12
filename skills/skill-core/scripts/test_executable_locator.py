"""Regression tests for the one program locator `deploy.py check` and the skills' extraction share."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import executable_locator
from executable_locator import find_executable, link_folders

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
