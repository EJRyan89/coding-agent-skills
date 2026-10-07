from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from harness import DeployerTestCase

from deployer import platform_support


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


class HiddenWindowTests(unittest.TestCase):
    def test_console_programs_start_without_a_window(self) -> None:
        self.assertEqual({"creationflags": subprocess.CREATE_NO_WINDOW}, platform_support.hidden_window())


class ShellPathTests(unittest.TestCase):
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


class StandardCommandTests(unittest.TestCase):
    def test_windows_adds_python_windows_powershell_and_cygpath(self) -> None:
        self.assertEqual(frozenset({"python", "powershell", "cygpath"}), platform_support.STANDARD_COMMANDS)


class HomeDirectoryTests(unittest.TestCase):
    PROFILE = r"D:\Profiles\Some One"

    def test_the_profile_folder_is_the_home_whatever_home_says(self) -> None:
        for home in (None, "C:/Temp/other-home", "/c/Temp/other-home", self.PROFILE):
            with self.subTest(home=home):
                environment = {"USERPROFILE": self.PROFILE, **({"HOME": home} if home else {})}
                self.assertEqual(Path(self.PROFILE), platform_support.home_directory(environment))

    def test_without_a_profile_variable_the_python_home_is_used(self) -> None:
        with mock.patch("deployer.platform_support.Path.home", return_value=Path("C:/Fallback")):
            self.assertEqual(Path("C:/Fallback"), platform_support.home_directory({"HOME": "C:/Temp/other"}))

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


class NameKeyTests(unittest.TestCase):
    def test_windows_compares_item_names_without_regard_to_case(self) -> None:
        self.assertEqual(platform_support.name_key("Guide.md"), platform_support.name_key("guide.md"))
        self.assertEqual(platform_support.name_key("ALPHA"), platform_support.name_key("alpha"))
        self.assertEqual("guide.md", platform_support.name_key("GUIDE.MD"))
        self.assertNotEqual(platform_support.name_key("guide.md"), platform_support.name_key("guide-md"))


if __name__ == "__main__":
    unittest.main()
