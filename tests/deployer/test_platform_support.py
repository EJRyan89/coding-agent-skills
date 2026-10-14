from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

from git_test_base import GitTestCase
from harness import DeployerTestCase

from deployer import platform_support
from deployer.errors import DeployError

if platform_support.WINDOWS:

    class FindBashTests(DeployerTestCase):
        def setUp(self) -> None:
            super().setUp()
            self.program_files = self.root / "Program Files"
            self.program_files.mkdir()
            environment = {key: value for key, value in os.environ.items() if key != "GIT_BASH"}
            environment["ProgramFiles"] = str(self.program_files)
            patcher = mock.patch.dict(os.environ, environment, clear=True)
            patcher.start()
            self.addCleanup(patcher.stop)

        def make_bash(self, git_root: Path) -> Path:
            bash = git_root / "bin" / "bash.exe"
            bash.parent.mkdir(parents=True)
            bash.write_bytes(b"")
            return bash

        def which(self, git: str | None) -> Any:
            wsl_bash = r"C:\Windows\System32\bash.exe"
            return mock.patch(
                "deployer.platform_support.shutil.which",
                side_effect=lambda name: {"git": git, "bash": wsl_bash}.get(name),
            )

        def test_git_bash_variable_wins(self) -> None:
            configured = self.make_bash(self.root / "Custom Git")
            self.make_bash(self.program_files / "Git")
            with mock.patch.dict(os.environ, {"GIT_BASH": str(configured)}):
                self.assertEqual(str(configured), platform_support.find_bash())

        def test_program_files_install_is_found(self) -> None:
            bash = self.make_bash(self.program_files / "Git")
            with self.which(None):
                self.assertEqual(str(bash), platform_support.find_bash())

        def test_bash_beside_the_git_on_path_is_found_for_scoop_installs(self) -> None:
            git_root = self.root / "scoop" / "apps" / "git" / "2.56.0"
            bash = self.make_bash(git_root)
            exec_path = f"{platform_support.normalize(git_root)}/mingw64/libexec/git-core\n"
            with (
                self.which(r"C:\Users\YourName\scoop\shims\git.exe"),
                mock.patch(
                    "deployer.platform_support.run_tool", return_value=platform_support.ToolResult(0, exec_path)
                ) as run_tool,
            ):
                found = platform_support.find_bash()
            if found is None:
                self.fail("Git Bash beside the git on PATH is found")
            self.assertEqual(bash, Path(found))
            run_tool.assert_called_once_with([r"C:\Users\YourName\scoop\shims\git.exe", "--exec-path"])

        def test_other_bash_on_path_is_never_used(self) -> None:
            with self.which(None):
                self.assertIsNone(platform_support.find_bash())

        def test_git_without_bash_beside_it_is_not_enough(self) -> None:
            exec_path = f"{platform_support.normalize(self.root / 'MinGit')}/mingw64/libexec/git-core\n"
            for result in (platform_support.ToolResult(0, exec_path), platform_support.ToolResult(1, "")):
                with (
                    self.subTest(returncode=result.returncode),
                    self.which("git.exe"),
                    mock.patch("deployer.platform_support.run_tool", return_value=result),
                ):
                    self.assertIsNone(platform_support.find_bash())

    class FindPwshTests(DeployerTestCase):
        def test_pwsh_is_found_only_on_the_path_given(self) -> None:
            install = self.root / "PowerShell 7"
            install.mkdir()
            (install / "pwsh.exe").write_bytes(b"")
            (self.root / "powershell.exe").write_bytes(b"")
            found = platform_support.find_pwsh(str(install))
            if found is None:
                self.fail("pwsh.exe on the search path is found")
            self.assertEqual(install / "pwsh.exe", Path(found))
            # Windows PowerShell is never a stand-in, and an empty or missing PATH does not fall back to this process's.
            for search_path in (str(self.root), str(self.root / "missing"), ""):
                with self.subTest(search_path=search_path):
                    self.assertIsNone(platform_support.find_pwsh(search_path))

    class FindExecutableTests(DeployerTestCase):
        """The deployer finds a program by skill-core's locator, the one the skills' scripts use."""

        def setUp(self) -> None:
            super().setUp()
            (self.root / "empty path").mkdir()
            self.links = self.root / "local app data" / "Microsoft" / "WinGet" / "Links"
            self.links.mkdir(parents=True)
            patcher = mock.patch.dict(
                os.environ, {"PATH": str(self.root / "empty path"), "LOCALAPPDATA": str(self.root / "local app data")}
            )
            patcher.start()
            self.addCleanup(patcher.stop)

        def test_a_program_only_in_wingets_links_folder_is_found_there(self) -> None:
            program = self.links / "fixture-deployer-tool.exe"
            program.write_bytes(b"")
            self.assertEqual(str(program), platform_support.find_executable("fixture-deployer-tool"))
            self.assertIsNone(platform_support.find_executable("fixture-absent-tool"))

else:

    def make_program(directory: Path, name: str) -> Path:
        """An executable file named `name`, as a package manager installs a program off Windows."""
        program = directory / name
        program.parent.mkdir(parents=True, exist_ok=True)
        program.write_bytes(b"#!/bin/sh\nexit 0\n")
        program.chmod(0o755)
        return program

    class UnixProgramTests(DeployerTestCase):
        """Off Windows, Bash, PowerShell, and every other program are the ones PATH names, with no link folder."""

        def setUp(self) -> None:
            super().setUp()
            self.bin = self.root / "usr bin"
            environment = {"PATH": str(self.bin), "GIT_BASH": str(self.root / "elsewhere" / "bash")}
            patcher = mock.patch.dict(os.environ, environment)
            patcher.start()
            self.addCleanup(patcher.stop)

        def test_the_system_bash_on_path_is_found_and_git_bash_is_not_asked_for(self) -> None:
            make_program(self.root / "elsewhere", "bash")
            bash = make_program(self.bin, "bash")
            self.assertEqual(str(bash), platform_support.find_bash())

        def test_without_bash_on_path_none_is_found(self) -> None:
            make_program(self.root / "elsewhere", "bash")
            self.assertIsNone(platform_support.find_bash())

        def test_pwsh_is_found_and_windows_powershell_never_stands_in(self) -> None:
            make_program(self.bin, "powershell")
            self.assertIsNone(platform_support.find_powershell())
            pwsh = make_program(self.bin, "pwsh")
            self.assertEqual(str(pwsh), platform_support.find_powershell())
            self.assertEqual(str(pwsh), platform_support.find_pwsh(str(self.bin)))

        def test_a_program_is_found_on_path_and_nowhere_else(self) -> None:
            program = make_program(self.bin, "fixture-deployer-tool")
            self.assertEqual(str(program), platform_support.find_executable("fixture-deployer-tool"))
            self.assertIsNone(platform_support.find_executable("fixture-absent-tool"))


class HiddenWindowTests(GitTestCase):
    def test_console_programs_start_without_a_window(self) -> None:
        # CREATE_NO_WINDOW on Windows; elsewhere a program opens no window by starting, so nothing is asked.
        expected = {"creationflags": 0x08000000} if platform_support.WINDOWS else {}
        self.assertEqual(expected, platform_support.hidden_window())


class LocatorTests(GitTestCase):
    def test_it_is_skill_cores_lookup(self) -> None:
        with mock.patch("executable_locator.find_executable", return_value="C:/located.exe") as located:
            self.assertEqual("C:/located.exe", platform_support.find_executable("tool"))
        located.assert_called_once_with("tool")


class SupportedPlatformTests(GitTestCase):
    def test_windows_and_linux_are_supported_and_macos_is_refused_by_name(self) -> None:
        for platform in ("win32", "linux"):
            with self.subTest(platform=platform), mock.patch.object(platform_support.sys, "platform", platform):
                platform_support.ensure_supported()
        with (
            mock.patch.object(platform_support.sys, "platform", "darwin"),
            self.assertRaises(DeployError) as raised,
        ):
            platform_support.ensure_supported()
        self.assertEqual(
            ("ERROR: The deployer runs on Windows and Linux only (detected platform: darwin).",),
            raised.exception.lines,
        )

    def test_this_platform_is_windows_only_on_windows(self) -> None:
        self.assertEqual(sys.platform == "win32", platform_support.WINDOWS)


class AbsolutePathTests(GitTestCase):
    """Each platform's absolute-path forms, checked on every platform, and the one this platform configures."""

    POSIX_ABSOLUTE = ("/home/you/GitHub", "/srv/repos (dev)/a.b@c", "/a", "/")
    WINDOWS_ABSOLUTE = ("C:/GitHub", "d:/Work Trees (dev)", "C:/")
    NEITHER = ("home/you", "./repos", "", "~/GitHub", "C:GitHub", "C:\\GitHub")

    def test_a_posix_path_is_absolute_from_one_leading_slash(self) -> None:
        for value in self.POSIX_ABSOLUTE:
            with self.subTest(value=value):
                self.assertTrue(platform_support.POSIX_ABSOLUTE.match(value))
        # Two leading slashes name what POSIX leaves to the system, such as a network share.
        for value in (*self.WINDOWS_ABSOLUTE, *self.NEITHER, "//server/share", "//"):
            with self.subTest(value=value):
                self.assertIsNone(platform_support.POSIX_ABSOLUTE.match(value))

    def test_a_posix_root_is_the_slash_alone(self) -> None:
        self.assertTrue(platform_support.POSIX_ROOT.fullmatch("/"))
        for value in ("/home", "//", "/ ", "C:/", ""):
            with self.subTest(value=value):
                self.assertIsNone(platform_support.POSIX_ROOT.fullmatch(value))

    def test_a_windows_path_is_absolute_from_a_drive_letter(self) -> None:
        for value in self.WINDOWS_ABSOLUTE:
            with self.subTest(value=value):
                self.assertTrue(platform_support.WINDOWS_ABSOLUTE.match(value))
        for value in (*self.POSIX_ABSOLUTE, *self.NEITHER):
            with self.subTest(value=value):
                self.assertIsNone(platform_support.WINDOWS_ABSOLUTE.match(value))

    def test_this_platform_configures_its_own_form_only(self) -> None:
        native, foreign = (
            (self.WINDOWS_ABSOLUTE, self.POSIX_ABSOLUTE)
            if platform_support.WINDOWS
            else (self.POSIX_ABSOLUTE, self.WINDOWS_ABSOLUTE)
        )
        for value in native:
            with self.subTest(value=value):
                self.assertTrue(platform_support.is_absolute(value))
        for value in (*foreign, *self.NEITHER):
            with self.subTest(value=value):
                self.assertFalse(platform_support.is_absolute(value))
        roots = ("C:/", "d:/") if platform_support.WINDOWS else ("/",)
        for value in roots:
            with self.subTest(root=value):
                self.assertTrue(platform_support.is_filesystem_root(value))
        self.assertFalse(platform_support.is_filesystem_root("C:/GitHub" if platform_support.WINDOWS else "/home"))
        expected = (
            ("an absolute Windows path", "C:/GitHub")
            if platform_support.WINDOWS
            else ("an absolute path", "/home/you/GitHub")
        )
        self.assertEqual(expected, (platform_support.ABSOLUTE_PATH, platform_support.EXAMPLE_DIRECTORY))


class NormalizeTests(GitTestCase):
    def test_only_windows_separates_with_a_backslash(self) -> None:
        with mock.patch.object(platform_support, "WINDOWS", True):
            self.assertEqual("C:/Tools/a b", platform_support.normalize("C:\\Tools\\a b"))
        with mock.patch.object(platform_support, "WINDOWS", False):
            self.assertEqual("/home/a\\b", platform_support.normalize("/home/a\\b"))

    def test_a_typed_path_loses_its_quotes_and_trailing_slashes_but_keeps_a_root(self) -> None:
        for windows, value, expected in (
            (True, '"C:\\Work Trees\\"', "C:/Work Trees"),
            (True, "C:/", "C:/"),
            (True, " D:/repos/// ", "D:/repos"),
            (False, '"/home/you/Git Hub/"', "/home/you/Git Hub"),
            (False, "/srv/repos///", "/srv/repos"),
            (False, "/", "/"),
            (False, "/a/", "/a"),
        ):
            with self.subTest(value=value), mock.patch.object(platform_support, "WINDOWS", windows):
                self.assertEqual(expected, platform_support.normalize_path_input(value))


class ShellPathTests(GitTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(platform_support, "WINDOWS", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_git_bash_drive_paths_become_drive_letter_paths(self) -> None:
        for value, expected in (
            ("/c/Work Trees (dev)/repo", "C:/Work Trees (dev)/repo"),
            ("/d", "D:/"),
            ("/d/", "D:/"),
            ("C:/already/native", "C:/already/native"),
            ("relative/dir", "relative/dir"),
            ("/cd/not-a-drive", "/cd/not-a-drive"),
            ("/tmp/x", "/tmp/x"),  # noqa: S108 - a path string the converter must leave alone; nothing is created
        ):
            with self.subTest(value=value):
                self.assertEqual(expected, platform_support.from_shell_path(value))

    def test_drive_letter_paths_become_git_bash_drive_paths(self) -> None:
        for value, expected in (
            ("C:/Tools With Spaces (dev)", "/c/Tools With Spaces (dev)"),
            ("d:/lower", "/d/lower"),
            ("E:\\Back\\Slashes", "/e/Back/Slashes"),
            ("C:", "/c"),
            ("/c/already/shell", "/c/already/shell"),
            ("relative/dir", "relative/dir"),
        ):
            with self.subTest(value=value):
                self.assertEqual(expected, platform_support.to_shell_path(value))

    def test_shell_paths_round_trip(self) -> None:
        native = "C:/Work Trees (dev)/repo"
        self.assertEqual(native, platform_support.from_shell_path(platform_support.to_shell_path(native)))

    def test_off_windows_a_shell_path_is_the_path_itself(self) -> None:
        with mock.patch.object(platform_support, "WINDOWS", False):
            for value in ("/c/Work Trees/repo", "/d", "/home/you", "C:/Tools"):
                with self.subTest(value=value):
                    self.assertEqual(value, platform_support.from_shell_path(value))
                    self.assertEqual(value, platform_support.to_shell_path(value))


class StandardCommandTests(GitTestCase):
    def test_windows_adds_python_windows_powershell_and_cygpath_and_elsewhere_python3(self) -> None:
        expected = {"python", "powershell", "cygpath"} if platform_support.WINDOWS else {"python3"}
        self.assertEqual(frozenset(expected), platform_support.STANDARD_COMMANDS)

    def test_python_is_named_python_on_windows_and_python3_elsewhere(self) -> None:
        self.assertEqual("python" if platform_support.WINDOWS else "python3", platform_support.PYTHON)
        self.assertIn(platform_support.PYTHON, platform_support.STANDARD_COMMANDS)


class InstallHintTests(GitTestCase):
    def test_each_platform_names_its_bash_and_its_installer(self) -> None:
        self.assertEqual("winget install --id Git.Git", platform_support.WINDOWS_INSTALL_HINTS["Git Bash"])
        self.assertEqual(
            "winget install --id koalaman.shellcheck", platform_support.WINDOWS_INSTALL_HINTS["ShellCheck"]
        )
        self.assertEqual(
            {
                "Bash": "sudo apt install bash",
                "ShellCheck": "sudo apt install shellcheck",
                "PowerShell": "sudo snap install powershell --classic",
                "ruff": "python3 -m pip install -r requirements-dev.txt",
                "mypy": "python3 -m pip install -r requirements-dev.txt",
                "PSScriptAnalyzer": "pwsh -Command 'Install-Module PSScriptAnalyzer -Scope CurrentUser -Force'",
            },
            platform_support.LINUX_INSTALL_HINTS,
        )
        # Every hint a caller asks for by a tool's label has an entry on both platforms.
        self.assertEqual(
            {label.replace("Git Bash", "Bash") for label in platform_support.WINDOWS_INSTALL_HINTS},
            set(platform_support.LINUX_INSTALL_HINTS),
        )

    def test_this_platform_uses_its_own_names_and_hints(self) -> None:
        bash, hints = ("Git Bash", "winget") if platform_support.WINDOWS else ("Bash", "sudo apt")
        self.assertEqual(bash, platform_support.BASH_NAME)
        self.assertTrue(platform_support.install_hint(bash).startswith(hints))
        self.assertIn('"Installing the tools"', " ".join(platform_support.INSTALL_HELP))


class HomeDirectoryTests(GitTestCase):
    PROFILE = r"D:\Profiles\Some One"

    def test_on_windows_the_profile_folder_is_the_home_whatever_home_says(self) -> None:
        with mock.patch.object(platform_support, "WINDOWS", True):
            for home in (None, "C:/Temp/other-home", "/c/Temp/other-home", self.PROFILE):
                with self.subTest(home=home):
                    environment = {"USERPROFILE": self.PROFILE, **({"HOME": home} if home else {})}
                    self.assertEqual(Path(self.PROFILE), platform_support.home_directory(environment))

    def test_off_windows_home_is_the_home_whatever_a_profile_variable_says(self) -> None:
        with mock.patch.object(platform_support, "WINDOWS", False):
            for profile in (None, self.PROFILE):
                with self.subTest(profile=profile):
                    environment = {"HOME": "/home/some one", **({"USERPROFILE": profile} if profile else {})}
                    self.assertEqual(Path("/home/some one"), platform_support.home_directory(environment))

    def test_without_the_variable_the_python_home_is_used(self) -> None:
        for windows, environment in ((True, {"HOME": "C:/Temp/other"}), (False, {"USERPROFILE": self.PROFILE})):
            with (
                self.subTest(windows=windows),
                mock.patch.object(platform_support, "WINDOWS", windows),
                mock.patch("deployer.platform_support.Path.home", return_value=Path("C:/Fallback")),
            ):
                self.assertEqual(Path("C:/Fallback"), platform_support.home_directory(environment))

    if platform_support.WINDOWS:

        def test_home_naming_another_directory_than_the_profile_is_reported(self) -> None:
            profile = Path(self.PROFILE)
            for home, expected in (
                (None, None),
                ("", None),
                ("C:/Temp/other-home", "C:/Temp/other-home"),
                ("/c/Temp/other-home", "C:/Temp/other-home"),
                (r"C:\Temp\other-home", "C:/Temp/other-home"),
                (self.PROFILE, None),
                ("d:/profiles/some one/", None),
                ("/d/Profiles/Some One", None),
            ):
                with self.subTest(home=home):
                    environment = {"USERPROFILE": self.PROFILE, **({"HOME": home} if home is not None else {})}
                    self.assertEqual(expected, platform_support.ignored_home_variable(profile, environment))

        def test_a_home_other_than_the_profile_is_never_reported(self) -> None:
            # A canary or fixture home is chosen explicitly, so HOME has no bearing on it.
            environment = {"USERPROFILE": self.PROFILE, "HOME": "C:/Temp/other-home"}
            self.assertIsNone(platform_support.ignored_home_variable(Path("C:/Temp/canary"), environment))

    else:

        def test_home_is_never_ignored_since_it_is_the_home(self) -> None:
            for home, profile in (("/home/some one", None), ("/home/some one", self.PROFILE)):
                with self.subTest(profile=profile):
                    environment = {"HOME": home, **({"USERPROFILE": profile} if profile else {})}
                    resolved = platform_support.home_directory(environment)
                    self.assertIsNone(platform_support.ignored_home_variable(resolved, environment))


class NameKeyTests(GitTestCase):
    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="name-key-test.")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        patcher = mock.patch.dict(platform_support._FOLDS_CASE, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def swapped_case(self, same: bool) -> Any:
        """os.lstat as a file system that does, or does not, fold case answers a case-swapped name."""
        real = os.lstat

        def lstat(path: Any) -> os.stat_result:
            if Path(path).name == "probe.TXT":
                if not same:
                    raise FileNotFoundError(2, "No such file or directory", str(path))
                return real(Path(path).with_name("PROBE.txt"))
            return real(path)

        return mock.patch.object(platform_support.os, "lstat", side_effect=lstat)

    def test_without_a_directory_names_compare_without_regard_to_case(self) -> None:
        self.assertEqual(platform_support.name_key("Guide.md"), platform_support.name_key("guide.md"))
        self.assertEqual(platform_support.name_key("ALPHA"), platform_support.name_key("alpha"))
        self.assertEqual("guide.md", platform_support.name_key("GUIDE.MD"))
        self.assertNotEqual(platform_support.name_key("guide.md"), platform_support.name_key("guide-md"))

    def test_a_directory_that_folds_case_folds_its_names(self) -> None:
        (self.directory / "PROBE.txt").write_bytes(b"")
        with self.swapped_case(same=True):
            self.assertTrue(platform_support.folds_case(self.directory))
        self.assertEqual("guide.md", platform_support.name_key("Guide.MD", self.directory))

    def test_a_case_sensitive_directory_folds_nothing(self) -> None:
        (self.directory / "PROBE.txt").write_bytes(b"")
        with self.swapped_case(same=False):
            self.assertFalse(platform_support.folds_case(self.directory))
        self.assertEqual("Guide.MD", platform_support.name_key("Guide.MD", self.directory))
        self.assertNotEqual(
            platform_support.name_key("Guide.md", self.directory), platform_support.name_key("guide.md", self.directory)
        )

    def test_a_directory_with_nothing_to_ask_folds_and_is_asked_again(self) -> None:
        (self.directory / "1234").write_bytes(b"")  # no letter whose case could be swapped
        self.assertTrue(platform_support.folds_case(self.directory))
        self.assertTrue(platform_support.folds_case(self.directory / "missing"))
        self.assertEqual({}, platform_support._FOLDS_CASE)
        (self.directory / "PROBE.txt").write_bytes(b"")
        with self.swapped_case(same=False):
            self.assertFalse(platform_support.folds_case(self.directory))

    def test_this_file_system_gives_its_own_answer(self) -> None:
        # A temporary directory is on NTFS on Windows, which folds case, and on a case-sensitive file system on Linux.
        (self.directory / "Probe.txt").write_bytes(b"")
        self.assertEqual(platform_support.WINDOWS, platform_support.folds_case(self.directory))

    if not platform_support.WINDOWS:

        def test_two_entries_whose_names_differ_only_in_case_are_two_files(self) -> None:
            (self.directory / "Guide.md").write_bytes(b"one")
            (self.directory / "gUIDE.MD").write_bytes(b"two")
            self.assertFalse(platform_support.folds_case(self.directory))


class ProgramNameTests(GitTestCase):
    def test_a_program_is_named_without_the_extension_windows_lets_its_caller_add(self) -> None:
        cases = {
            "git.exe": "git",
            "GIT.EXE": "GIT",
            "Pwsh.Exe": "Pwsh",
            "git": "git",
            # Only the extension Windows finds a program by is dropped, and only at the end.
            "codex.cmd": "codex.cmd",
            "git.exe.bak": "git.exe.bak",
            "notexe": "notexe",
            ".exe": "",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(expected, platform_support.program_name(name))


class ProcessStatusTests(GitTestCase):
    def test_the_lock_judges_a_holder_by_skill_cores_probe(self) -> None:
        status = platform_support.process_status(os.getpid())
        self.assertTrue(status.alive)
        self.assertIsInstance(status.start_time, int)
        self.assertIs(platform_support.ProcessStatus, sys.modules["process_identity"].ProcessStatus)


class SignalTerminationTests(GitTestCase):
    """Off Windows, Python reports a process a signal ended by the negated signal number."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(platform_support, "WINDOWS", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_signal_is_named(self) -> None:
        self.assertEqual("SIGTERM", platform_support.termination_status(-15))
        self.assertEqual("SIGINT", platform_support.termination_status(-2))
        self.assertEqual("signal 250", platform_support.termination_status(-250))

    def test_a_program_s_own_exit_code_is_not_a_termination(self) -> None:
        for returncode in (0, 1, 2, 127, 255, 2_147_483_652):
            with self.subTest(returncode=returncode):
                self.assertIsNone(platform_support.termination_status(returncode))


class TerminationStatusTests(GitTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(platform_support, "WINDOWS", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_floor_is_the_lowest_ntstatus_with_a_severity_bit(self) -> None:
        self.assertEqual(2_147_483_648, platform_support.TERMINATION_STATUS_FLOOR)

    def test_a_status_at_or_above_the_floor_is_named_in_hexadecimal(self) -> None:
        self.assertEqual("0x80000004", platform_support.termination_status(2_147_483_652))
        self.assertEqual("0x80000000", platform_support.termination_status(2_147_483_648))
        self.assertEqual("0xC0000005", platform_support.termination_status(3_221_225_477))

    def test_a_program_s_own_exit_code_is_not_a_termination(self) -> None:
        for returncode in (0, 1, 2, 127, 255, 2_147_483_647):
            with self.subTest(returncode=returncode):
                self.assertIsNone(platform_support.termination_status(returncode))


if __name__ == "__main__":
    unittest.main()
