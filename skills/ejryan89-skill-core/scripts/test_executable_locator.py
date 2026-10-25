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


WINDOWS = sys.platform == "win32"
# The file a program named PROGRAM is installed as: WinGet's `.exe` on Windows, and the bare name elsewhere.
PROGRAM_FILE = f"{PROGRAM}.exe" if WINDOWS else PROGRAM


class LocatorTestCase(unittest.TestCase):
    """A sanitized environment: PATH is one empty folder and the local application data folder is a fresh one, so
    nothing on the machine running the test is found. Off Windows, where no package manager links commands into a
    folder of its own, `links` is the folder PATH names after the first, the place the locator looks next."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="executable locator ")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.path_folder = root / "on path"
        self.local = root / "local app data"
        self.links = self.local / "Microsoft" / "WinGet" / "Links" if WINDOWS else root / "later on path"
        self.path_folder.mkdir()
        self.local.mkdir()
        self.links.mkdir(parents=True, exist_ok=True)
        variables = (
            {"PATH": str(self.path_folder), "PATHEXT": ".COM;.EXE", "LOCALAPPDATA": str(self.local)}
            if WINDOWS
            else {"PATH": os.pathsep.join((str(self.path_folder), str(self.links))), "LOCALAPPDATA": str(self.local)}
        )
        environment = mock.patch.dict(os.environ, variables)
        environment.start()
        self.addCleanup(environment.stop)

    def program(self, folder: Path) -> Path:
        program = folder / PROGRAM_FILE
        program.write_bytes(b"")
        program.chmod(0o755)
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
        (self.links / PROGRAM_FILE).mkdir()
        self.assertIsNone(find_executable(PROGRAM))

    def test_without_an_absolute_local_application_data_folder_nothing_relative_is_searched(self) -> None:
        # Were the folder taken relative to the working directory, a program there would be found.
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.local)
        self.program(self.links)
        for value in ("", "relative"):
            with (
                self.subTest(value=value),
                mock.patch.dict(os.environ, {"LOCALAPPDATA": value, "PATH": str(self.path_folder)}),
            ):
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

    def test_a_search_path_given_is_searched_in_place_of_path_and_never_the_working_directory(self) -> None:
        second = self.path_folder.parent / "second"
        second.mkdir()
        program = self.program(second)
        self.program(self.path_folder)
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.local)
        self.program(self.local)
        self.assertEqual([program], [Path(path) for path in path_programs(PROGRAM, str(second))])
        self.assertEqual([], path_programs(PROGRAM, str(self.links)))
        self.assertEqual([], path_programs(PROGRAM, ""))

    def test_a_relative_path_entry_is_skipped_rather_than_resolved_against_the_working_directory(self) -> None:
        # Each entry would name a folder holding the program, were it resolved against the working directory.
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.local)
        self.program(self.local)
        nested = self.local / "bin"
        nested.mkdir()
        self.program(nested)
        relative = ["", ".", "bin", os.curdir + os.sep + "bin"]
        if WINDOWS:
            drive = self.local.drive
            relative += [drive + "bin", "\\" + str(nested.relative_to(self.local.anchor))]
        for entry in relative:
            with self.subTest(entry=entry):
                self.assertEqual([], path_programs(PROGRAM, entry))
                with mock.patch.dict(os.environ, {"PATH": os.pathsep.join((entry, str(self.path_folder)))}):
                    self.assertIsNone(find_executable(PROGRAM))
        # The control: the same folder named by its absolute path is searched.
        self.assertEqual([nested / PROGRAM_FILE], [Path(path) for path in path_programs(PROGRAM, str(nested))])


class FindExecutablesTests(LocatorTestCase):
    def test_every_candidate_in_search_order_path_first_then_the_links_folder(self) -> None:
        second = self.path_folder.parent / "second on path"
        second.mkdir()
        first_program = self.program(self.path_folder)
        second_program = self.program(second)
        linked = self.program(self.links)
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join((str(self.path_folder), str(second)))}):
            found = find_executables(PROGRAM)
            # Off Windows the folder after PATH's first is on PATH only as the fixture puts it, so not here.
            expected = [first_program, second_program, linked] if WINDOWS else [first_program, second_program]
            self.assertEqual(expected, [Path(path) for path in found])
            self.assertEqual(found[0], find_executable(PROGRAM), "the first candidate is the one found")

    def test_a_links_folder_that_is_also_on_path_yields_its_program_once(self) -> None:
        linked = self.program(self.links)
        with mock.patch.dict(os.environ, {"PATH": str(self.links)}):
            self.assertEqual([linked], [Path(path) for path in find_executables(PROGRAM)])

    def test_nothing_found_is_an_empty_list(self) -> None:
        self.assertEqual([], find_executables(PROGRAM))

    def test_a_path_folder_spelled_several_ways_is_searched_once(self) -> None:
        program = self.program(self.path_folder)
        folder = str(self.path_folder)
        spellings = (folder, folder + os.sep, str(self.path_folder / ".." / self.path_folder.name))
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join(spellings)}):
            self.assertEqual([program], [Path(path) for path in path_programs(PROGRAM)])
            self.assertEqual([program], [Path(path) for path in find_executables(PROGRAM)])


class LocatedCommandTests(LocatorTestCase):
    def test_the_program_is_replaced_by_the_absolute_path_the_locator_finds(self) -> None:
        linked = self.program(self.links)
        command = located_command([PROGRAM, "--flag", "value with spaces"])
        self.assertEqual((linked, ["--flag", "value with spaces"]), (Path(command[0]), command[1:]))
        self.assertTrue(Path(command[0]).is_absolute())
        on_path = self.program(self.path_folder)
        self.assertEqual(on_path, Path(located_command([PROGRAM])[0]), "PATH comes first")

    def test_a_program_kept_is_reused_until_a_variable_the_lookup_reads_changes(self) -> None:
        kept: executable_locator.Found = {}
        on_path = self.program(self.path_folder)
        first = located_command([PROGRAM, "status"], kept=kept)
        self.assertEqual((on_path, ["status"]), (Path(first[0]), first[1:]))
        on_path.unlink()
        self.assertEqual([first[0], "log"], located_command([PROGRAM, "log"], kept=kept), "kept, not looked up")
        linked = self.program(self.links)
        # The variables a lookup reads; a change to any of them makes the kept program stale.
        self.assertEqual(("PATH", "PATHEXT", "LOCALAPPDATA"), executable_locator.LOOKUP_VARIABLES)
        with mock.patch.dict(os.environ, {"PATH": os.environ["PATH"] + os.pathsep}):
            self.assertEqual(linked, Path(located_command([PROGRAM], kept=kept)[0]), "looked up again")
        with self.assertRaises(FileNotFoundError):
            located_command(["fixture-locator-missing"], kept=kept)
        self.assertNotIn("fixture-locator-missing", [key[0] for key in kept])

    def test_a_program_named_by_a_path_is_left_as_it_is(self) -> None:
        self.assertEqual([sys.executable, "-c", "pass"], located_command([sys.executable, "-c", "pass"]))

    def test_a_missing_program_raises_file_not_found_as_starting_it_would(self) -> None:
        with self.assertRaises(FileNotFoundError) as raised:
            located_command([PROGRAM, "--version"])
        self.assertEqual(PROGRAM, raised.exception.filename)


class PathProgramsTests(unittest.TestCase):
    if not WINDOWS:

        def test_every_executable_match_on_path_in_order(self) -> None:
            with tempfile.TemporaryDirectory() as temporary:
                first, second, third = (Path(temporary) / f"folder {index}" for index in range(3))
                for folder in (first, second, third):
                    folder.mkdir()
                for program in (first / "copilot", third / "copilot"):
                    program.write_bytes(b"#!/bin/sh\n")
                    program.chmod(0o755)
                (first / "copilot.exe").write_bytes(b"an extension is part of the name here")
                (first / "copilot.exe").chmod(0o755)
                (second / "copilot").mkdir()
                (third / "plain").write_bytes(b"not executable")
                path = os.pathsep.join(("", str(first), str(second), str(first), str(third)))
                with mock.patch.dict(os.environ, {"PATH": path}):
                    self.assertEqual(
                        [str(first / "copilot"), str(third / "copilot")],
                        path_programs("copilot"),
                        "PATH's order across folders; a folder once; no directory",
                    )
                    self.assertEqual(shutil.which("copilot", path=str(first)), path_programs("copilot")[0])
                    self.assertEqual([], path_programs("plain"), "a file that is not executable is no program")

    else:

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
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual([self.local / "Microsoft" / "WinGet" / "Links"], link_folders())

    def test_an_unset_or_relative_local_application_data_folder_links_no_folder(self) -> None:
        environment = {key: value for key, value in os.environ.items() if key.upper() != "LOCALAPPDATA"}
        for local in (None, "", "local app data"):
            variables = environment if local is None else {**environment, "LOCALAPPDATA": local}
            with (
                self.subTest(local=local),
                mock.patch.object(sys, "platform", "win32"),
                mock.patch.dict(os.environ, variables, clear=True),
            ):
                self.assertEqual([], link_folders())

    def test_another_platform_has_no_link_folder_yet(self) -> None:
        self.program(self.links)
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.dict(os.environ, {"PATH": str(self.path_folder)}),
        ):
            self.assertEqual([], link_folders())
            self.assertIsNone(find_executable(PROGRAM))


class EnvironmentPathTests(unittest.TestCase):
    """An environment variable names a path a lookup may use only when the path is absolute."""

    VARIABLE = "FIXTURE_LOCATOR_FOLDER"

    def path(self, value: str | None) -> Path | None:
        environment = {key: item for key, item in os.environ.items() if key != self.VARIABLE}
        if value is not None:
            environment[self.VARIABLE] = value
        with mock.patch.dict(os.environ, environment, clear=True):
            return executable_locator.environment_path(self.VARIABLE)

    def test_an_absolute_path_is_the_path_it_names(self) -> None:
        folder = Path(tempfile.gettempdir()).resolve()
        self.assertEqual(folder, self.path(str(folder)))

    def test_an_unset_empty_or_relative_variable_names_no_path(self) -> None:
        relative = ["", ".", "bin", "Git/bin/bash.exe", "bash.exe"]
        # Windows resolves a drive-relative path against the drive's working directory and a rooted one against the
        # working directory's drive, so neither is absolute there.
        relative += ["C:bin", r"\bin"] if WINDOWS else []
        for value in [None, *relative]:
            with self.subTest(value=value):
                self.assertIsNone(self.path(value))
