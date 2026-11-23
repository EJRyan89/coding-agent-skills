from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import IO, Any
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import bounded_process
import dotnet_format_targets as targets
import dotnet_solution
import folder_walk
import git_client
import github_client
from flat_text import flat_text
from git_client import GitClient, GitError, GitResult
from github_client import CommandResult
from github_client import Runner as GhRunner
from test_folder_walk import reached, recording
from test_private_folder import judged_folder_calls, recording_temporary_folders
from tree_reader import TreeReader, TreeRefused

SDK_PROJECT = '<Project Sdk="Microsoft.NET.Sdk"></Project>\n'
WEB_PROJECT = '<Project Sdk="Microsoft.NET.Sdk.Web"></Project>\n'
# A project in the format before SDK-style ones, as Visual Studio wrote it for .NET Framework.
LEGACY_PROJECT = (
    '<?xml version="1.0" encoding="utf-8"?>\n<Project ToolsVersion="15.0" '
    'xmlns="http://schemas.microsoft.com/developer/msbuild/2003">\n'
    '  <Import Project="$(MSBuildExtensionsPath)\\$(MSBuildToolsVersion)\\Microsoft.Common.props" />\n'
    "</Project>\n"
)
LEGACY_WEB_PROJECT = (
    "<Project><PropertyGroup><ProjectTypeGuids>{349C5851-65DF-11DA-9384-00065B846F21};"
    "{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}</ProjectTypeGuids></PropertyGroup></Project>\n"
)
WEB_APPLICATION_PROJECT = "<Project><PropertyGroup><WebApplication>true</WebApplication></PropertyGroup></Project>\n"
WINFORMS_PROJECT = (
    '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup><Reference Include="System.Web" /></ItemGroup></Project>\n'
)
CSHARP_PROJECT_TYPE = "{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}"


def solution(*projects: str) -> str:
    """A Visual Studio solution listing projects by path, with Windows separators and line endings."""
    lines = ["Microsoft Visual Studio Solution File, Format Version 12.00"]
    for number, project in enumerate(projects):
        name = Path(project.replace("\\", "/")).stem
        lines += [
            f'Project("{CSHARP_PROJECT_TYPE}") = "{name}", "{project}", "{{{number}}}"',
            "EndProject",
        ]
    lines.append('Project("{2150E333-8FDC-42A3-9474-1A3956D46DE8}") = "Solution Items", "Solution Items", "{X}"')
    return "\r\n".join(lines) + "\r\n"


def slnx(*projects: str, folder: str | None = None) -> str:
    """An XML solution listing projects by path, inside a solution folder when one is named."""
    entries = "".join(f'    <Project Path="{project}" />\n' for project in projects)
    if folder is not None:
        entries = f'  <Folder Name="{folder}">\n{entries}  </Folder>\n'
    return f"<Solution>\n{entries}</Solution>\n"


def link_folder(target: Path, link: Path) -> None:
    """A real link to a folder: a junction on Windows, which needs no privilege, else a symbolic link."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def remove_link(link: Path) -> None:
    """Remove a link `link_folder` made, never what it points at."""
    if sys.platform == "win32":
        link.rmdir()
    else:
        link.unlink()


@contextlib.contextmanager
def recording_reaches(touched: list[tuple[str, bool]]) -> Iterator[None]:
    """Record in `touched` each path the file system is reached with, and whether the call follows a link there:
    ejryan89-skill-core's folder-walk `recording` of `os.stat`, `os.lstat`, `os.scandir` with each listed entry's
    metadata calls, and `os.readlink`, and each open through `io.open`, which `Path.open` calls, `builtins.open`, and
    `os.open`, which follows a link unless given `O_NOFOLLOW`."""
    real_io_open, real_os_open = io.open, os.open

    def opened(file: Any, *rest: Any, **options: Any) -> Any:
        if isinstance(file, (str, os.PathLike)):
            touched.append((os.fsdecode(file), True))
        return real_io_open(file, *rest, **options)

    def os_open(path: Any, flags: int, *rest: Any, **options: Any) -> int:
        touched.append((os.fsdecode(path), not flags & getattr(os, "O_NOFOLLOW", 0)))
        return real_os_open(path, flags, *rest, **options)

    with (
        recording(touched),
        mock.patch.object(io, "open", opened),
        mock.patch("builtins.open", opened),
        mock.patch.object(os, "open", os_open),
    ):
        yield


def outside_folder(test: unittest.TestCase) -> Path:
    """A throwaway folder outside the test's repository, holding the SDK-style project E/E.csproj."""
    directory = tempfile.TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    outside = Path(directory.name).resolve()
    (outside / "E").mkdir()
    (outside / "E" / "E.csproj").write_text(SDK_PROJECT, encoding="utf-8")
    return outside


def linked_out(link: Path, target: Path, tree: Path) -> str:
    """The refusal of a path through `link`, whose text is `target`, outside `tree`."""
    return (
        f"its links cannot be followed inside the repository root: {link.as_posix()} is a link to {target.as_posix()},"
        f" outside the examined tree {tree.as_posix()}, which the reader does not follow"
    )


@contextlib.contextmanager
def denied(target: Path, *, stat: bool = True) -> Iterator[None]:
    """`target` cannot be listed, as a file system refuses a folder: os.scandir and os.listdir raise, and Path.glob
    and Path.rglob match nothing in it, as Python 3.15's do where iterdir raises. With `stat`, its stat is refused
    too: Path.stat, os.stat, and os.lstat raise, and Path.exists, is_file, and is_dir and the os.path predicates
    answer False, as from Python 3.14 on."""

    def names(argument: object) -> bool:
        return isinstance(argument, (str, os.PathLike)) and Path(argument) == target

    def refusing(real: Callable[..., Any]) -> Callable[..., Any]:
        def call(path: Any, *args: Any, **kwargs: Any) -> Any:
            if names(path):
                raise PermissionError(errno.EACCES, "Access is denied", str(target))
            return real(path, *args, **kwargs)

        return call

    def answering(real: Callable[..., Any], answer: Any) -> Callable[..., Any]:
        def call(path: Any, *args: Any, **kwargs: Any) -> Any:
            return answer if names(path) else real(path, *args, **kwargs)

        return call

    Wrap = Callable[[Callable[..., Any]], Callable[..., Any]]
    patches: list[tuple[object, str, Wrap]] = [(os, "scandir", refusing), (os, "listdir", refusing)]
    patches += [(Path, name, lambda real: answering(real, iter(()))) for name in ("glob", "rglob")]
    if stat:
        patches += [(Path, "stat", refusing), (os, "stat", refusing), (os, "lstat", refusing)]
        patches += [(Path, name, lambda real: answering(real, False)) for name in ("exists", "is_file", "is_dir")]
        patches += [
            (os.path, name, lambda real: answering(real, False)) for name in ("exists", "lexists", "isfile", "isdir")
        ]
    with contextlib.ExitStack() as stack:
        for owner, name, wrap in patches:
            stack.enter_context(mock.patch.object(owner, name, wrap(getattr(owner, name))))
        yield


class Repository:
    def __init__(self, root: Path) -> None:
        self.root = root

    def git(self, *arguments: str) -> str:
        result = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.autocrlf=false",
                *arguments,
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return result.stdout

    def write(self, relative: str, text: str = "class C { }\n") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return path

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "--quiet", "--no-verify", "-m", message)


class Services(targets.Services):
    """Real Git, the .NET SDK, no gh unless its output is given, and no network fetch."""

    def __init__(self, pull_base: str | None = None, gh_output: bytes | None = None, dotnet: bool = True) -> None:
        self.calls: list[list[str]] = []
        if gh_output is None and pull_base:
            gh_output = json.dumps({"baseRefName": pull_base}).encode() + b"\n"

        self.directories: list[Path] = []

        def git(command: Sequence[str], timeout: float) -> GitResult:
            # Recorded without `-C <directory>`, the way the commands read.
            self.calls.append([command[0], *command[3:]])
            if command[3] == "fetch":
                return GitResult(128, "", "")
            return git_client.subprocess_runner(command, timeout)

        def gh(arguments: Sequence[str]) -> CommandResult:
            self.calls.append(list(arguments))
            if gh_output is None:
                return CommandResult(1, "", "")
            return CommandResult(0, gh_output.decode("utf-8", "surrogateescape"), "")

        def gh_in(directory: Path) -> GhRunner:
            self.directories.append(directory)
            return gh

        super().__init__(
            git=GitClient(git),
            gh=gh_in,
            which=lambda name: (
                name if (name == "gh" and gh_output is not None) or (name == "dotnet" and dotnet) else None
            ),
        )


class ResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repository = Repository(Path(directory.name).resolve())
        self.repository.git("init", "--quiet", "--initial-branch=main")
        self.repository.write("README.md", "fixture\n")
        self.repository.commit("base")
        self.repository.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.repository.git("checkout", "--quiet", "-b", "feature")

    def run_resolve(
        self, cwd: Path | None = None, services: targets.Services | None = None
    ) -> tuple[int, list[list[str]], str]:
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = targets.main(["resolve", "--cwd", str(cwd or self.repository.root)], services or Services())
        lines = [line.split("\t") for line in output.getvalue().splitlines()]
        for fields in lines:
            if fields[0] == "FILE_LIST":
                self.addCleanup(os.remove, fields[1])
        return status, lines, errors.getvalue()

    def resolve(self, cwd: Path | None = None, services: targets.Services | None = None) -> list[list[str]]:
        status, lines, errors = self.run_resolve(cwd, services)
        self.assertEqual((0, ""), (status, errors))
        return lines

    def failed(self, cwd: Path | None = None, services: targets.Services | None = None) -> str:
        """The reason a resolve run's last line, FAILED, gives, after checking it exits 1 and writes no stderr."""
        status, lines, errors = self.run_resolve(cwd, services)
        self.assertEqual((1, ""), (status, errors))
        self.assertEqual([], self.values(lines, "FILE_LIST"))
        self.assertEqual([], self.values(lines, "STOP"))
        last = "\t".join(lines[-1])
        self.assertTrue(last.startswith("FAILED "), last)
        self.assertEqual(1, sum(1 for fields in lines if fields[0].startswith("FAILED")))
        return last[len("FAILED ") :]

    def values(self, lines: list[list[str]], kind: str) -> list[list[str]]:
        return [fields[1:] for fields in lines if fields[0] == kind]

    def test_collects_committed_uncommitted_and_untracked_files_and_skips_aspnet_projects(self) -> None:
        repo = self.repository
        repo.write("App/App.csproj", SDK_PROJECT)
        repo.write("App/Modified.cs")
        repo.write("App/Deleted.cs")
        repo.write("App/Renamed.cs")
        repo.git("add", "-A")
        repo.git("commit", "--quiet", "-m", "existing")
        repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
        repo.write("App/Committed.cs")
        repo.write("Web/Web.csproj", WEB_PROJECT)
        repo.write("Web/Controllers/HomeController.cs")
        repo.write("Legacy/Legacy.csproj", LEGACY_WEB_PROJECT)
        repo.write("Legacy/Page.cs")
        repo.write("Site/Site.csproj", WEB_APPLICATION_PROJECT)
        repo.write("Site/Global.cs")
        repo.write("Forms/Forms.csproj", WINFORMS_PROJECT)
        repo.write("Forms/My Form.cs")
        repo.write("notes.txt", "not C#\n")
        repo.commit("feature")
        repo.write("notes.txt", "still not C#\n")
        (repo.root / "App/Deleted.cs").unlink()
        repo.git("mv", "App/Renamed.cs", "App/RenamedAgain.cs")
        repo.write("App/Modified.cs", "class Modified { }\n")
        repo.write("Loose/Untracked.cs")
        repo.write(
            "App.sln",
            solution(
                "App\\App.csproj",
                "Forms\\Forms.csproj",
                "Web\\Web.csproj",
                "Legacy\\Legacy.csproj",
                "Site\\Site.csproj",
            ),
        )

        lines = self.resolve()

        self.assertEqual(["REPO_ROOT", str(repo.root)], lines[0])
        self.assertEqual([["origin/main"]], self.values(lines, "BASE"))
        # The solution holds projects in the older format, so the global tool formats it and skips the ASP.NET ones.
        self.assertEqual(
            [
                ["Legacy/Page.cs", "Legacy/Legacy.csproj"],
                ["Site/Global.cs", "Site/Site.csproj"],
                ["Web/Controllers/HomeController.cs", "Web/Web.csproj"],
            ],
            self.values(lines, "SKIPPED_ASPNET"),
        )
        expected = [
            "App/Committed.cs",
            "App/Modified.cs",
            "App/RenamedAgain.cs",
            "Forms/My Form.cs",
            "Legacy/Page.cs",
            "Loose/Untracked.cs",
            "Site/Global.cs",
            "Web/Controllers/HomeController.cs",
        ]
        self.assertEqual([[name] for name in expected], self.values(lines, "FILE"))
        (file_list,) = self.values(lines, "FILE_LIST")
        self.assertEqual("".join(f"{name}\0" for name in expected), Path(file_list[0]).read_text(encoding="utf-8"))
        self.assertEqual(expected, targets.read_file_list(Path(file_list[0])))
        self.assertEqual([["App.sln", "7"]], self.values(lines, "SOLUTION"))
        self.assertEqual([["Loose/Untracked.cs"]], self.values(lines, "OUTSIDE_SOLUTION"))
        self.assertEqual("SOLUTION", lines[-1][0])

    def test_web_project_at_repository_root_owns_files_without_a_nearer_project(self) -> None:
        self.repository.write("Root.csproj", WEB_PROJECT)
        self.repository.write("Old/Old.csproj", LEGACY_PROJECT)
        self.repository.write("Pages/Index.cs")
        self.repository.write("Root.sln", solution("Root.csproj", "Old\\Old.csproj"))
        self.repository.commit("web")
        lines = self.resolve()
        self.assertEqual([["Pages/Index.cs", "Root.csproj"]], self.values(lines, "SKIPPED_ASPNET"))
        self.assertEqual([["Pages/Index.cs"]], self.values(lines, "FILE"))
        self.assertEqual([["Root.sln", "1"]], self.values(lines, "SOLUTION"))

    def test_a_changed_file_project_or_solution_it_cannot_examine_or_list_fails_never_reads_as_none(self) -> None:
        # A changed file it cannot stat would read as deleted, and the run as one with no C# change; a project or
        # solution, or a folder it cannot list, as one with none, which drops the ASP.NET skip or the nearest solution.
        # A changed file, project, or solution is judged by its links first, from its own metadata, so its refusal
        # gives the system's reason through that judgment.
        repo = self.repository
        repo.write("Web/Web.csproj", WEB_PROJECT)
        repo.write("Web/Program.cs")
        repo.write("App.sln", solution("Web\\Web.csproj"))
        repo.commit("web")
        root = repo.root
        error = "[Errno 13] Access is denied"
        judged = "its links cannot be followed inside the repository root: Access is denied"
        rows: list[tuple[str, bool, str]] = [
            ("Web/Program.cs", True, f"the changed file Web/Program.cs cannot be examined: {judged}"),
            ("Web/Web.csproj", True, f"{root / 'Web/Web.csproj'} cannot be examined: {judged}"),
            ("Web", False, f"{root / 'Web'} cannot be listed: {error}: {{target!r}}"),
            ("App.sln", True, f"{root / 'App.sln'} cannot be examined: {judged}"),
            (".", False, f"{root} cannot be listed: {error}: {{target!r}}"),
        ]
        for relative, stat, reason in rows:
            target = root / relative if relative != "." else root
            with self.subTest(relative=relative), denied(target, stat=stat):
                self.assertEqual(reason.format(target=str(target)), self.failed())

    def test_a_folder_by_a_project_or_solution_name_is_passed_by_and_the_files_after_it_found(self) -> None:
        repo = self.repository
        repo.write("Lib/Lib.csproj", SDK_PROJECT)
        repo.write("Lib/Code.cs")
        repo.write("App.sln", solution("Lib\\Lib.csproj"))
        repo.commit("solution")
        expected = [fields for fields in self.resolve() if fields[0] != "FILE_LIST"]
        self.assertEqual([["App.sln", "1"]], self.values(expected, "SOLUTION"))
        # Each folder sorts before the file by its kind's name in the same folder.
        (repo.root / "Lib/A.csproj").mkdir()
        (repo.root / "A.sln").mkdir()
        self.assertEqual(expected, [fields for fields in self.resolve() if fields[0] != "FILE_LIST"])

    def test_the_sdks_dotnet_format_is_given_an_aspnet_projects_files_and_only_the_global_tool_skips_them(self) -> None:
        # The SDK's dotnet format formats an ASP.NET project, so its files are skipped only when the solution has the
        # global tool run, and the layout checker sees them either way.
        repo = self.repository
        repo.write("Web/Web.csproj", WEB_PROJECT)
        repo.write("Web/Program.cs")
        repo.write("Site/Site.csproj", WEB_APPLICATION_PROJECT)
        repo.write("Site/Global.cs")
        repo.write("App/App.csproj", SDK_PROJECT)
        repo.write("App/Code.cs")
        repo.write("App.sln", solution("App\\App.csproj", "Web\\Web.csproj"))
        repo.commit("web")
        everything = ["App/Code.cs", "Site/Global.cs", "Web/Program.cs"]
        lines = self.resolve()
        self.assertEqual([], self.values(lines, "SKIPPED_ASPNET"))
        self.assertEqual([["Site/Global.cs"]], self.values(lines, "OUTSIDE_SOLUTION"))
        (file_list,) = self.values(lines, "FILE_LIST")
        self.assertEqual(everything, targets.read_file_list(Path(file_list[0])))
        self.assertEqual([["App.sln", "2"]], self.values(lines, "SOLUTION"))

        repo.write("App.sln", solution("App\\App.csproj", "Web\\Web.csproj", "Site\\Site.csproj"))
        repo.commit("a web application project, in the older format")
        lines = self.resolve()
        self.assertEqual(
            [["Site/Global.cs", "Site/Site.csproj"], ["Web/Program.cs", "Web/Web.csproj"]],
            self.values(lines, "SKIPPED_ASPNET"),
        )
        (file_list,) = self.values(lines, "FILE_LIST")
        self.assertEqual(everything, targets.read_file_list(Path(file_list[0])))
        self.assertEqual([["App.sln", "3"]], self.values(lines, "SOLUTION"))

        (repo.root / "App.sln").unlink()
        repo.write("App.slnx", slnx("App/App.csproj", "Web/Web.csproj", "Site/Site.csproj"))
        repo.commit("slnx")
        self.assertEqual([], self.values(self.resolve(), "SKIPPED_ASPNET"))

    def test_nearest_owning_solution_from_cwd_wins_over_better_scoring_solutions_elsewhere(self) -> None:
        repo = self.repository
        repo.write("Sources/Lib/Lib.csproj", SDK_PROJECT)
        repo.write("Sources/Lib/Code.cs")
        repo.write("Sources/App/App.csproj", SDK_PROJECT)
        repo.write("Sources/App/One.cs")
        repo.write("Sources/App/Two.cs")
        repo.write("Sources/Local.sln", solution("Lib\\Lib.csproj"))
        repo.write("Everything.sln", solution("Sources\\Lib\\Lib.csproj", "Sources\\App\\App.csproj"))
        repo.commit("solutions")
        lines = self.resolve(cwd=repo.root / "Sources" / "Lib")
        self.assertEqual([["Sources/Local.sln", "1"]], self.values(lines, "SOLUTION"))
        self.assertEqual([["Sources/App/One.cs"], ["Sources/App/Two.cs"]], self.values(lines, "OUTSIDE_SOLUTION"))

    def test_without_cwd_it_resolves_from_the_current_directory(self) -> None:
        # The skill runs resolve with no --cwd: a "$PWD" argument would make Claude Code ask before every run.
        repo = self.repository
        repo.write("Sources/Lib/Lib.csproj", SDK_PROJECT)
        repo.write("Sources/Lib/Code.cs")
        repo.write("Sources/Local.sln", solution("Lib\\Lib.csproj"))
        repo.write("Everything.sln", solution("Sources\\Lib\\Lib.csproj"))
        repo.commit("solutions")
        output = io.StringIO()
        with (
            mock.patch.object(Path, "cwd", return_value=repo.root / "Sources" / "Lib"),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(0, targets.main(["resolve"], Services()))
        lines = [line.split("\t") for line in output.getvalue().splitlines()]
        self.addCleanup(os.remove, self.values(lines, "FILE_LIST")[0][0])
        self.assertEqual([["Sources/Local.sln", "1"]], self.values(lines, "SOLUTION"))

    def test_a_nearer_solution_owning_no_changed_file_is_never_chosen(self) -> None:
        repo = self.repository
        repo.write("Sources/Lib/Lib.csproj", SDK_PROJECT)
        repo.write("Sources/Lib/Code.cs")
        repo.write("Sources/Local.sln", solution("Unrelated\\Unrelated.csproj"))
        repo.write("Everything.sln", solution("Sources\\Lib\\Lib.csproj"))
        repo.commit("solutions")
        lines = self.resolve(cwd=repo.root / "Sources" / "Lib")
        self.assertEqual([["Everything.sln", "1"]], self.values(lines, "SOLUTION"))
        self.assertEqual([], self.values(lines, "OUTSIDE_SOLUTION"))

    def test_stops_when_the_only_solution_owns_no_changed_file(self) -> None:
        repo = self.repository
        repo.write("Lib/Lib.csproj", SDK_PROJECT)
        repo.write("Lib/Code.cs")
        repo.write("Only.sln", solution("Other\\Other.csproj"))
        repo.commit("solutions")
        self.assertEqual(
            [["changed files do not belong to any solution found: Only.sln"]], self.values(self.resolve(), "STOP")
        )

    def test_scoring_matches_solution_project_paths_exactly(self) -> None:
        repo = self.repository
        repo.write("Product/Core/Core.csproj", SDK_PROJECT)
        repo.write("Product/Core/Engine.cs")
        repo.write("Product/Core/Parts.cs")
        repo.write("Other/Core/Core.csproj", SDK_PROJECT)
        # Substring and same-basename references that a grep for "Core.csproj" would count.
        repo.write("A/Decoy.sln", solution("..\\Other\\Core\\Core.csproj", "..\\MyCore.csproj", "Core.csproj"))
        repo.write("B/Real.sln", solution("..\\Product\\Core\\Core.csproj"))
        repo.write("bin/Ignored.sln", solution("..\\Product\\Core\\Core.csproj"))
        repo.commit("solutions")
        self.assertEqual([["B/Real.sln", "2"]], self.values(self.resolve(), "SOLUTION"))
        found = targets.find_solutions(repo.root)
        self.assertEqual([repo.root / "A/Decoy.sln", repo.root / "B/Real.sln"], found)

    def test_ties_go_to_the_shallowest_solution(self) -> None:
        repo = self.repository
        repo.write("Lib/Lib.csproj", SDK_PROJECT)
        repo.write("Lib/Code.cs")
        repo.write("Deep/Nested/Nested.sln", solution("..\\..\\Lib\\Lib.csproj"))
        repo.write("Lib/Lib.sln", solution("Lib.csproj"))
        repo.commit("solutions")
        self.assertEqual([["Lib/Lib.sln", "1"]], self.values(self.resolve(), "SOLUTION"))

    def test_multiple_solutions_in_one_directory_are_scored(self) -> None:
        repo = self.repository
        repo.write("Lib/Lib.csproj", SDK_PROJECT)
        repo.write("Lib/Code.cs")
        repo.write("A.sln", solution("Other\\Other.csproj"))
        # A solution names a project in any case only where the file system folds case, as Windows' does.
        repo.write("B.sln", solution("lib\\LIB.csproj" if sys.platform == "win32" else "Lib\\Lib.csproj"))
        repo.commit("solutions")
        self.assertEqual([["B.sln", "1"]], self.values(self.resolve(), "SOLUTION"))

    def test_stops_when_no_solution_owns_the_changed_files(self) -> None:
        repo = self.repository
        repo.write("Lib/Lib.csproj", SDK_PROJECT)
        repo.write("Lib/Code.cs")
        repo.write("A.sln", solution("Other\\Other.csproj"))
        repo.write("B.sln", solution("Third\\Third.csproj"))
        repo.commit("solutions")
        self.assertEqual(
            [["changed files do not belong to any solution found: A.sln, B.sln"]], self.values(self.resolve(), "STOP")
        )

    def test_stops_when_there_is_no_solution(self) -> None:
        self.repository.write("Code.cs")
        self.repository.commit("code")
        lines = self.resolve()
        self.assertEqual([["Code.cs"]], self.values(lines, "FILE"))
        self.assertEqual([[f"no .sln or .slnx found under {self.repository.root}"]], self.values(lines, "STOP"))
        self.assertEqual([], self.values(lines, "FILE_LIST"))

    def test_stops_when_nothing_changed(self) -> None:
        self.assertEqual([["no C# files changed vs origin/main"]], self.values(self.resolve(), "STOP"))

    def test_base_is_the_pull_request_base_then_origin_head_then_main_then_master(self) -> None:
        repo = self.repository
        repo.git("update-ref", "refs/remotes/origin/release", "HEAD")
        repo.git("update-ref", "refs/remotes/origin/develop", "HEAD")
        repo.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop")
        services = Services(pull_base="release")
        self.assertEqual([["origin/release"]], self.values(self.resolve(services=services), "BASE"))
        self.assertIn(["gh", "pr", "view", "--json", "baseRefName"], services.calls)
        self.assertIn(["git", "fetch", "origin", "--quiet"], services.calls)
        self.assertEqual([repo.root.resolve()], services.directories)  # gh reads the repository from its directory

        services = Services(pull_base="missing")
        self.assertEqual([["origin/develop"]], self.values(self.resolve(services=services), "BASE"))
        self.assertIn(["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], services.calls)

        repo.git("update-ref", "-d", "refs/remotes/origin/develop")  # origin/HEAD now names a missing branch
        self.assertEqual([["origin/main"]], self.values(self.resolve(), "BASE"))
        repo.git("symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
        self.assertEqual([["origin/main"]], self.values(self.resolve(), "BASE"))

        repo.git("update-ref", "-d", "refs/remotes/origin/main")
        repo.git("update-ref", "refs/remotes/origin/master", "HEAD")
        self.assertEqual([["origin/master"]], self.values(self.resolve(services=Services("missing")), "BASE"))

    def test_no_base_ref_fails(self) -> None:
        self.repository.git("update-ref", "-d", "refs/remotes/origin/main")
        self.assertEqual(
            "no base ref: none of the pull request base, origin/HEAD, origin/main, or origin/master exists",
            self.failed(),
        )

    def test_a_broken_base_ref_fails_naming_git_instead_of_falling_back(self) -> None:
        # A loose ref file holding no object name: git warns about it rather than answer that it names nothing.
        self.repository.git("update-ref", "-d", "refs/remotes/origin/main")
        broken = self.repository.root / ".git" / "refs" / "remotes" / "origin" / "main"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("nonsense\n", encoding="utf-8")
        self.assertEqual(
            "git could not resolve origin/main: warning: ignoring broken ref refs/remotes/origin/main",
            self.failed(services=Services("missing")),
        )

    def test_an_origin_head_git_cannot_read_fails_naming_git(self) -> None:
        real = Services().git.runner

        def run(command: Sequence[str], timeout: float) -> GitResult:
            if "symbolic-ref" in command:
                return GitResult(128, "", "fatal: bad config line 1 in file .git/config\n")
            return real(command, timeout)

        services = Services("missing")
        services.git = GitClient(run)
        self.assertEqual(
            "git could not read refs/remotes/origin/HEAD: fatal: bad config line 1 in file .git/config",
            self.failed(services=services),
        )

    def test_the_remote_default_branch_of_a_real_clone_is_the_base(self) -> None:
        # A bare remote whose HEAD names develop, with a main branch as a decoy: comparing with origin/main would
        # also list Lib/Shipped.cs, which develop already has.
        with tempfile.TemporaryDirectory() as directory:
            top = Path(directory).resolve()
            seed = Repository(top / "seed")
            seed.root.mkdir()
            seed.git("init", "--quiet", "--initial-branch=main")
            seed.write("README.md", "fixture\n")
            seed.commit("base")
            seed.git("checkout", "--quiet", "-b", "develop")
            seed.write("Lib/Lib.csproj", SDK_PROJECT)
            seed.write("Lib/Shipped.cs")
            seed.write("App.sln", solution("Lib\\Lib.csproj", "App\\App.csproj"))
            seed.commit("develop")
            Repository(top).git("init", "--quiet", "--bare", "--initial-branch=develop", "remote.git")
            seed.git("push", "--quiet", str(top / "remote.git"), "main", "develop")
            Repository(top).git("clone", "--quiet", str(top / "remote.git"), "clone")
            clone = Repository(top / "clone")
            self.assertEqual("refs/remotes/origin/develop\n", clone.git("symbolic-ref", "refs/remotes/origin/HEAD"))
            self.assertEqual(
                "main\n", clone.git("branch", "--remotes", "--list", "origin/main", "--format=%(refname:lstrip=3)")
            )
            clone.git("checkout", "--quiet", "-b", "feature")
            clone.write("App/App.csproj", SDK_PROJECT)
            clone.write("App/Feature.cs")
            clone.commit("feature")

            services = Services()
            lines = self.resolve(cwd=clone.root, services=services)

            self.assertEqual([], [call for call in services.calls if call[0] == "gh"])
            self.assertEqual([["origin/develop"]], self.values(lines, "BASE"))
            self.assertEqual([["App/Feature.cs"]], self.values(lines, "FILE"))
            self.assertEqual([["App.sln", "1"]], self.values(lines, "SOLUTION"))

    def test_a_rate_limited_pull_request_base_falls_back_without_waiting(self) -> None:
        services = Services(pull_base="release")

        def limited(arguments: Sequence[str]) -> CommandResult:
            services.calls.append(list(arguments))
            return CommandResult(1, "", "gh: API rate limit exceeded for user ID 1. (HTTP 403)")

        services.gh = lambda directory: limited
        with mock.patch("time.sleep") as sleep:
            self.assertEqual([["origin/main"]], self.values(self.resolve(services=services), "BASE"))
        # No rate limit is waited out. Off Windows, a timed wait for git to exit polls in pauses of at most a twentieth
        # of a second, which are no backoff.
        self.assertEqual([], [call for call in sleep.call_args_list if call.args[0] > 0.05])
        self.assertEqual(1, sum(1 for call in services.calls if call[0] == "gh"))

    def test_a_malformed_pull_request_base_falls_back_to_origin_main(self) -> None:
        self.repository.git("update-ref", "refs/remotes/origin/develop", "HEAD")
        for output in (
            b"",
            b"develop\n",
            b"not json",
            b"[]",
            b'{"number": 7}',
            b'{"baseRefName": 7}',
            b'{"baseRefName": ""}',
            b'{"baseRefName": null}',
        ):
            with self.subTest(output=output):
                self.assertEqual(
                    [["origin/main"]], self.values(self.resolve(services=Services(gh_output=output)), "BASE")
                )

    def test_outside_a_repository_fails(self) -> None:
        with tempfile.TemporaryDirectory() as outside:
            self.assertEqual(f"{outside} is not inside a Git repository", self.failed(cwd=Path(outside)))

    def test_a_failed_git_command_fails_with_the_reason_git_gave(self) -> None:
        self.repository.write("Code.cs")
        self.repository.commit("code")
        command = "git ls-files -z --others --exclude-standard -- *.cs failed"
        # git's stderr on one line, or the exit code when git printed none: never the command alone.
        for stderr, reason in (
            ("fatal: index file corrupt\nfatal: unable to read\n", "fatal: index file corrupt fatal: unable to read"),
            ("", "exit code 128"),
        ):
            with self.subTest(stderr=stderr):
                services = Services()
                real = services.git.runner

                def run(
                    arguments: Sequence[str],
                    timeout: float,
                    real: Callable[[Sequence[str], float], GitResult] = real,
                    stderr: str = stderr,
                ) -> GitResult:
                    if arguments[3] == "ls-files":
                        return GitResult(128, "", stderr)
                    return real(arguments, timeout)

                services.git = GitClient(run)
                self.assertEqual(f"{command}: {reason}", self.failed(services=services))

    def test_a_git_command_that_never_finishes_fails_but_a_stalled_fetch_does_not(self) -> None:
        self.repository.write("Code.cs")
        self.repository.commit("code")
        services = Services()
        real = services.git.runner
        stalled = {"fetch"}

        def run(command: Sequence[str], timeout: float) -> GitResult:
            if command[3] in stalled:
                raise GitError(f"git {command[3]} did not finish within {timeout:g} seconds", kind="timeout")
            return real(command, timeout)

        services.git = GitClient(run)
        lines = self.resolve(services=services)
        self.assertEqual([["git fetch did not finish within 300 seconds"]], self.values(lines, "FETCH_FAILED"))
        self.assertEqual(["REPO_ROOT", "FETCH_FAILED", "BASE"], [fields[0] for fields in lines[:3]])
        self.assertEqual([["origin/main"]], self.values(lines, "BASE"))
        stalled.add("ls-files")
        self.assertEqual("git ls-files did not finish within 300 seconds", self.failed(services=services))

    def test_a_failed_fetch_is_reported_on_one_line_and_the_last_fetched_refs_serve(self) -> None:
        services = Services()
        real = services.git.runner

        def run(command: Sequence[str], timeout: float) -> GitResult:
            if command[3] == "fetch":
                return GitResult(
                    128, "", "fatal: unable to access 'https://example.invalid/r.git/':\n\tCould not resolve host\n"
                )
            return real(command, timeout)

        services.git = GitClient(run)
        lines = self.resolve(services=services)
        self.assertEqual(
            [["fatal: unable to access 'https://example.invalid/r.git/': Could not resolve host"]],
            self.values(lines, "FETCH_FAILED"),
        )
        self.assertEqual([["origin/main"]], self.values(lines, "BASE"))
        # Without stderr the exit code is the reason; the fixture's fetch exits 128 and says nothing.
        self.assertEqual([["git fetch origin exited with code 128"]], self.values(self.resolve(), "FETCH_FAILED"))

    def test_fetch_errors_use_the_shared_flattener_and_keep_output_field_order(self) -> None:
        message = " \tCannot\r\n fetch  café\u00a0設計\u2003branch.\v "
        services = Services()
        real = services.git.runner
        for failure in (GitResult(128, "", message), GitError(message, kind="timeout")):

            def run(command: Sequence[str], timeout: float, failure: GitResult | GitError = failure) -> GitResult:
                if command[3] == "fetch":
                    if isinstance(failure, GitError):
                        raise failure
                    return failure
                return real(command, timeout)

            services.git = GitClient(run)
            with (
                self.subTest(failure=failure),
                mock.patch.object(targets, "flat_text", wraps=flat_text) as flatten,
            ):
                lines = self.resolve(services=services)
            self.assertEqual(["REPO_ROOT", "FETCH_FAILED", "BASE", "STOP"], [fields[0] for fields in lines])
            self.assertEqual([["Cannot fetch café 設計 branch."]], self.values(lines, "FETCH_FAILED"))
            self.assertEqual([["origin/main"]], self.values(lines, "BASE"))
            flatten.assert_called_once_with(message)

    def test_repository_errors_use_the_shared_flattener_without_splitting_output_fields(self) -> None:
        message = " \tCannot\r\n read  café\u00a0設計\u2003repository.\v "
        services = Services()
        services.git = GitClient(lambda command, timeout: GitResult(128, "", message))
        with mock.patch.object(targets, "flat_text", wraps=flat_text) as flatten:
            self.assertEqual("Cannot read café 設計 repository.", self.failed(services=services))
        # The FAILED line flattens the reason once more, which leaves the flattened text as it is.
        self.assertEqual(
            [mock.call(message.strip()), mock.call("Cannot read café 設計 repository.")], flatten.call_args_list
        )

    def test_a_git_that_could_not_fetch_fails_by_name_never_as_a_failed_fetch(self) -> None:
        # A git that is missing, could not start, or could not end what it started said nothing of origin: reading it
        # as FETCH_FAILED would report a fetch from origin that never ran.
        for failure in (
            GitError(git_client.MISSING_GIT, kind="prerequisite"),
            GitError("Git could not be started: [Errno 13] Permission denied", kind="execution"),
            GitError("git fetch left programs it started that could not be ended", kind="execution"),
        ):
            services = Services()

            def run(
                command: Sequence[str], timeout: float, failure: GitError = failure, real: Any = services.git.runner
            ) -> GitResult:
                if command[3] == "fetch":
                    raise failure
                result: GitResult = real(command, timeout)
                return result

            services.git = GitClient(run)
            with self.subTest(failure=str(failure)):
                self.assertEqual(f"git could not fetch origin: {failure}", self.failed(services=services))
                self.assertEqual([], self.values(self.run_resolve(None, services)[1], "FETCH_FAILED"))

    def test_a_fetch_that_succeeds_reports_nothing(self) -> None:
        services = Services()
        real = services.git.runner

        def run(command: Sequence[str], timeout: float) -> GitResult:
            if command[3] == "fetch":
                return GitResult(0, "", "")
            return real(command, timeout)

        services.git = GitClient(run)
        self.assertEqual([], self.values(self.resolve(services=services), "FETCH_FAILED"))

    def test_missing_git_and_a_stalled_git_are_reported_as_such(self) -> None:
        def missing(command: Sequence[str], timeout: float) -> GitResult:
            raise GitError(git_client.MISSING_GIT, kind="prerequisite")

        def stalled(command: Sequence[str], timeout: float) -> GitResult:
            raise GitError(f"git {command[3]} did not finish within {timeout:g} seconds", kind="timeout")

        for runner, reason in (
            (missing, git_client.MISSING_GIT),
            (stalled, "git rev-parse did not finish within 300 seconds"),
        ):
            with self.subTest(reason=reason):
                services = Services()
                services.git = GitClient(runner)
                self.assertEqual(reason, self.failed(services=services))

    def test_a_repository_git_refuses_is_reported_with_gits_reason(self) -> None:
        services = Services()
        stderr = "fatal: detected dubious ownership in repository at 'C:/repo'\nTo add an exception, run:\n"
        services.git = GitClient(lambda command, timeout: GitResult(128, "", stderr))
        self.assertEqual(
            "fatal: detected dubious ownership in repository at 'C:/repo' To add an exception, run:",
            self.failed(services=services),
        )
        services.git = GitClient(lambda command, timeout: GitResult(129, "", ""))
        self.assertEqual("git rev-parse failed with exit code 129", self.failed(services=services))

    def test_an_unreadable_project_fails(self) -> None:
        self.repository.write("Lib/Lib.csproj", SDK_PROJECT)
        self.repository.write("Lib/Code.cs")
        self.repository.write("App.sln", solution("Lib\\Lib.csproj"))
        self.repository.commit("code")
        denied = PermissionError(13, "Permission denied", "Lib/Lib.csproj")
        opened = Path.open

        def open_project(path: Path, *arguments: Any, **options: Any) -> IO[Any]:
            if path.name == "Lib.csproj":
                raise denied
            handle: IO[Any] = opened(path, *arguments, **options)
            return handle

        with mock.patch.object(Path, "open", open_project):
            failure = self.failed()
        self.assertEqual(f"{self.repository.root / 'Lib' / 'Lib.csproj'} cannot be read: Permission denied", failure)

    def test_an_unreadable_directory_fails_instead_of_hiding_a_solution(self) -> None:
        repo = self.repository
        repo.write("Lib/Lib.csproj", SDK_PROJECT)
        repo.write("Lib/Code.cs")
        repo.write("Locked/Lib.sln", solution("..\\Lib\\Lib.csproj"))
        repo.commit("code")
        locked = repo.root / "Locked"
        real = os.scandir

        def scandir(path: str | os.PathLike[str] = ".") -> object:
            if os.path.normcase(os.fspath(path)) == os.path.normcase(str(locked)):
                raise PermissionError(13, "Permission denied", "Locked")
            return real(path)

        with mock.patch.object(os, "scandir", side_effect=scandir):
            self.assertEqual(f"{locked}: the folder cannot be listed: Permission denied", self.failed())

    def test_dependency_build_and_other_checkouts_folders_are_not_searched(self) -> None:
        # A package restored into packages/ can ship a sample solution, and Visual Studio's .vs/, build output, and
        # a nested clone or worktree are not the repository's; a malformed solution in any of them is never read.
        repo = self.repository
        repo.write("src/App/App.csproj", SDK_PROJECT)
        repo.write("src/App/A.cs")
        repo.write("src/App.slnx", slnx("App/App.csproj"))
        repo.commit("code")
        for folder in ("packages/Some.Pkg/1.0.0/sample", ".vs/App", "src/App/bin/Debug", ".claude/worktrees/task"):
            repo.write(f"{folder}/Broken.slnx", "<Solution")
        repo.write(".claude/worktrees/task/.git", "gitdir: ../../../.git/worktrees/task\n")
        self.assertEqual([["src/App.slnx", "1"]], self.values(self.resolve(), "SOLUTION"))
        self.assertEqual([repo.root / "src/App.slnx"], targets.find_solutions(repo.root))

    def test_a_linked_folder_is_not_entered_even_when_its_target_is_gone_or_holds_the_repository(self) -> None:
        repo = self.repository
        repo.write("Lib/Lib.csproj", SDK_PROJECT)
        repo.write("Lib/Code.cs")
        repo.write("Lib.sln", solution("Lib\\Lib.csproj"))
        repo.commit("code")
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        elsewhere = Path(outside.name, "elsewhere")
        elsewhere.mkdir()
        (elsewhere / "Other.sln").write_bytes(b"not a solution")
        gone = Path(outside.name, "gone")
        gone.mkdir()
        for name, target in {"Loop": repo.root, "Elsewhere": elsewhere, "Stale": gone}.items():
            link_folder(target, repo.root / name)
            self.addCleanup(remove_link, repo.root / name)
        gone.rmdir()
        self.assertEqual([["Lib.sln", "1"]], self.values(self.resolve(), "SOLUTION"))
        self.assertEqual([repo.root / "Lib.sln"], targets.find_solutions(repo.root))

    def test_an_unwritable_temporary_directory_fails(self) -> None:
        self.repository.write("Lib/Lib.csproj", SDK_PROJECT)
        self.repository.write("Lib/Code.cs")
        self.repository.write("Lib.sln", solution("Lib\\Lib.csproj"))
        self.repository.commit("code")
        with mock.patch.object(tempfile, "mkstemp", side_effect=OSError(28, "No space left on device")):
            self.assertEqual("[Errno 28] No space left on device", self.failed())

    def test_an_slnx_solution_is_found_and_scored_like_an_sln(self) -> None:
        repo = self.repository
        repo.write("Sources/Lib/Lib.csproj", SDK_PROJECT)
        repo.write("Sources/Lib/Code.cs")
        repo.write("Sources/App/App.csproj", SDK_PROJECT)
        repo.write("Sources/App/One.cs")
        repo.write("Sources/Local.slnx", slnx("Lib/Lib.csproj"))
        repo.write("Everything.SLNX", slnx("Sources\\Lib\\Lib.csproj", "Sources/App/App.csproj", folder="/src/"))
        repo.commit("solutions")
        self.assertEqual([["Sources/Local.slnx", "1"]], self.values(self.resolve(repo.root / "Sources"), "SOLUTION"))
        lines = self.resolve()
        self.assertEqual([["Everything.SLNX", "2"]], self.values(lines, "SOLUTION"))
        self.assertEqual([], self.values(lines, "OUTSIDE_SOLUTION"))
        self.assertEqual(
            [repo.root / "Everything.SLNX", repo.root / "Sources/Local.slnx"], targets.find_solutions(repo.root)
        )

    def test_an_slnx_without_the_dotnet_sdk_stops_and_an_sln_does_not_need_it(self) -> None:
        repo = self.repository
        repo.write("Lib/Lib.csproj", SDK_PROJECT)
        repo.write("Lib/Code.cs")
        repo.write("App.slnx", slnx("Lib/Lib.csproj"))
        repo.commit("solutions")
        needs_sdk = (
            "is a .slnx solution, or a .sln of SDK-style projects, which the .NET SDK's dotnet format formats; "
            "install the .NET SDK 9.0.200 or newer"
        )
        status, lines, errors = self.run_resolve(services=Services(dotnet=False))
        self.assertEqual((0, ""), (status, errors))
        self.assertEqual(["STOP", f"App.slnx {needs_sdk}"], lines[-1])
        self.assertEqual([], self.values(lines, "FILE_LIST"))
        self.assertEqual([], self.values(lines, "SOLUTION"))

        repo.write("App.sln", solution("Lib\\Lib.csproj"))
        repo.commit("sln")
        lines = self.resolve(services=Services(dotnet=False))
        self.assertEqual(["STOP", f"App.sln {needs_sdk}"], lines[-1])

        repo.write("Old/Old.csproj", LEGACY_PROJECT)
        repo.write("App.sln", solution("Lib\\Lib.csproj", "Old\\Old.csproj"))
        repo.commit("a project in the older format")
        self.assertEqual([["App.sln", "1"]], self.values(self.resolve(services=Services(dotnet=False)), "SOLUTION"))

    def test_an_sln_beside_an_slnx_listing_the_same_projects_wins_the_tie(self) -> None:
        # A repository part way through migrating keeps both; the .sln wins even where the .slnx sorts first by name.
        repo = self.repository
        repo.write("Lib/Lib.csproj", SDK_PROJECT)
        repo.write("Lib/Code.cs")
        repo.write("All.slnx", slnx("Lib/Lib.csproj"))
        repo.write("Legacy.sln", solution("Lib\\Lib.csproj"))
        repo.write("Deep/Deeper.sln", solution("..\\Lib\\Lib.csproj"))
        repo.commit("solutions")
        self.assertEqual([["Legacy.sln", "1"]], self.values(self.resolve(), "SOLUTION"))

    def test_an_slnx_it_cannot_read_fails_with_the_reason_and_a_readable_one_owning_nothing_stops(self) -> None:
        # A solution that is not well-formed XML, or declares a DOCTYPE or an entity, is reported as unreadable:
        # taking it for one that lists no project would tell the user their file belongs to no solution.
        repo = self.repository
        repo.write("Lib/Lib.csproj", SDK_PROJECT)
        repo.write("Lib/Code.cs")
        repo.write("Broken.slnx", '<Solution><Project Path="Lib/Lib.csproj">\n')
        repo.write(
            "Entity.slnx",
            '<!DOCTYPE Solution [<!ENTITY lib "Lib/Lib.csproj">]>\n<Solution><Project Path="&lib;" /></Solution>\n',
        )
        repo.write("Other.slnx", '<Solution><Project Path="Other/Other.csproj" /><Project /></Solution>\n')
        repo.commit("solutions")
        for unreadable, reason in (("Broken.slnx", "not well-formed"), ("Entity.slnx", "DOCTYPE")):
            with self.subTest(solution=unreadable):
                failure = self.failed()
                self.assertTrue(failure.startswith(f"{repo.root / unreadable} cannot be read: "), failure)
                self.assertIn(reason, failure)
                (repo.root / unreadable).unlink()
        repo.commit("readable solutions only")
        self.assertEqual(
            [["changed files do not belong to any solution found: Other.slnx"]],
            self.values(self.resolve(), "STOP"),
        )

    def test_a_solution_above_the_read_bound_or_not_a_regular_file_fails_with_the_reason(self) -> None:
        # Every read opens only a regular file within ejryan89-skill-core's one bound, so a very large solution, or a
        # device a solution's name points at, never holds the run or exhausts its memory.
        repo = self.repository
        repo.write("Lib/Lib.csproj", SDK_PROJECT)
        repo.write("Lib/Code.cs")
        repo.write("App.sln", solution("Lib\\Lib.csproj"))
        repo.commit("solution")
        limit = 64
        with mock.patch.object(dotnet_solution, "READ_LIMIT_BYTES", limit):
            failure = self.failed()
        bound = f"the file is larger than the reader's bound of {limit} bytes, so it is not read"
        self.assertEqual(f"{repo.root / 'App.sln'} cannot be read: {bound}", failure)
        if Path("/dev/zero").exists():
            (repo.root / "App.sln").unlink()
            (repo.root / "App.sln").symlink_to("/dev/zero")
            failure = self.failed()
            # A link out of the repository, to a device here, is judged by its text and never stat or opened.
            linked = repo.root / "App.sln"
            self.assertEqual(
                f"{linked} cannot be examined: {linked_out(linked, Path('/dev/zero'), repo.root)}", failure
            )

    def test_an_slnx_declaring_another_encoding_is_read_as_decoded(self) -> None:
        repo = self.repository
        (repo.root / "Lib").mkdir()
        (repo.root / "Lib/Lib.csproj").write_bytes(SDK_PROJECT.encode("utf-8"))
        repo.write("Lib/Code.cs")
        declared = '<?xml version="1.0" encoding="utf-16"?>\n' + slnx("Lib/Lib.csproj")
        (repo.root / "App.slnx").write_bytes(declared.encode("utf-16"))
        repo.commit("solutions")
        self.assertEqual([["App.slnx", "1"]], self.values(self.resolve(), "SOLUTION"))

    def test_reads_utf16_and_bom_project_files(self) -> None:
        repo = self.repository
        (repo.root / "Web").mkdir()
        (repo.root / "Web/Web.csproj").write_bytes(WEB_PROJECT.encode("utf-16"))
        (repo.root / "Lib").mkdir()
        (repo.root / "Lib/Lib.csproj").write_bytes(b"\xef\xbb\xbf" + SDK_PROJECT.encode("utf-8"))
        repo.write("Web/Page.cs")
        repo.write("Lib/Code.cs")
        (repo.root / "Old").mkdir()
        (repo.root / "Old/Old.csproj").write_bytes(LEGACY_PROJECT.replace("utf-8", "utf-16").encode("utf-16"))
        listed = solution("Lib\\Lib.csproj", "Web\\Web.csproj", "Old\\Old.csproj")
        (repo.root / "Lib.sln").write_bytes(b"\xef\xbb\xbf" + listed.encode("utf-8"))
        repo.commit("encodings")
        lines = self.resolve()
        self.assertEqual([["Web/Page.cs", "Web/Web.csproj"]], self.values(lines, "SKIPPED_ASPNET"))
        self.assertEqual([["Lib.sln", "2"]], self.values(lines, "SOLUTION"))
        self.assertEqual(targets.TOOL_FORMATTER, targets.formatter_for(repo.root, repo.root / "Lib.sln"))
        listed = solution("Lib\\Lib.csproj", "Web\\Web.csproj")
        (repo.root / "Lib.sln").write_bytes(b"\xef\xbb\xbf" + listed.encode("utf-8"))
        self.assertEqual(targets.SDK_FORMATTER, targets.formatter_for(repo.root, repo.root / "Lib.sln"))

    def test_a_listed_project_a_folder_link_leads_outside_is_never_reached_so_the_global_tool_formats(self) -> None:
        # Linked (a junction on Windows) leads outside the repository, where E/E.csproj is SDK-style. Read through
        # the link, every listed project would be SDK-style and, with no dotnet, the run would stop for want of the
        # SDK; judged from Linked's own metadata and text, E is not shown to be SDK-style, so the global tool formats
        # the solution, and nothing below Linked or in the folder it leads to is stat, listed, or opened. A link whose
        # target stays inside the repository is still read through.
        repo = self.repository
        repo.write("Lib/Lib.csproj", SDK_PROJECT)
        repo.write("Lib/Code.cs")
        repo.write("Other/R/R.csproj", SDK_PROJECT)
        repo.write("Lib.sln", solution("Lib\\Lib.csproj", "Linked\\E\\E.csproj"))
        repo.commit("code")
        outside = outside_folder(self)
        link = repo.root / "Linked"
        for name, target in (("Linked", outside), ("In", repo.root / "Other")):
            link_folder(target, repo.root / name)
            self.addCleanup(remove_link, repo.root / name)
        touched: list[tuple[str, bool]] = []
        with recording_reaches(touched):
            lines = self.resolve(services=Services(dotnet=False))
        self.assertEqual([["Lib.sln", "1"]], self.values(lines, "SOLUTION"))
        self.assertIn((str(link), False), touched)
        self.assertEqual([], reached(touched, [link], outside))
        touched.clear()
        with recording_reaches(touched):
            self.assertEqual(targets.TOOL_FORMATTER, targets.formatter_for(repo.root, repo.root / "Lib.sln"))
        self.assertEqual([], reached(touched, [link], outside))
        repo.write("Inside.sln", solution("Lib\\Lib.csproj", "In\\R\\R.csproj"))
        self.assertEqual(targets.SDK_FORMATTER, targets.formatter_for(repo.root, repo.root / "Inside.sln"))

    @unittest.skipIf(sys.platform == "win32", "a symbolic link to a file needs a privilege on Windows")
    def test_a_listed_project_file_that_is_a_link_leading_outside_is_never_reached(self) -> None:
        # Two project files that are links out of the repository, one by an absolute target and one by a relative
        # target climbing out, each to an SDK-style project: neither is stat or opened, so the global tool formats.
        repo = self.repository
        repo.write("Lib/Lib.csproj", SDK_PROJECT)
        repo.write("Lib/Code.cs")
        repo.write("Lib.sln", solution("Lib\\Lib.csproj", "Shared\\Abs.csproj", "Shared\\Rel.csproj"))
        repo.commit("code")
        outside = outside_folder(self)
        (repo.root / "Shared").mkdir()
        absolute, relative = repo.root / "Shared" / "Abs.csproj", repo.root / "Shared" / "Rel.csproj"
        absolute.symlink_to(outside / "E" / "E.csproj")
        relative.symlink_to(f"../../{outside.name}/E/E.csproj")
        touched: list[tuple[str, bool]] = []
        with recording_reaches(touched):
            lines = self.resolve(services=Services(dotnet=False))
        self.assertEqual([["Lib.sln", "1"]], self.values(lines, "SOLUTION"))
        self.assertEqual([], reached(touched, [absolute, relative], outside))
        for link in (absolute, relative):
            (repo.root / "Only.sln").write_text(solution(link.relative_to(repo.root).as_posix()), encoding="utf-8")
            with self.subTest(link=link.name):
                self.assertEqual(targets.TOOL_FORMATTER, targets.formatter_for(repo.root, repo.root / "Only.sln"))

    @unittest.skipIf(sys.platform == "win32", "a symbolic link to a file needs a privilege on Windows")
    def test_a_project_or_solution_found_beside_a_changed_file_that_is_a_link_out_fails_unread(self) -> None:
        # P/P.csproj owns the changed P/Code.cs and leads outside to an ASP.NET project: read, it would make the
        # global tool skip Code.cs as SKIPPED_ASPNET; it is never stat or opened, and the run fails naming it, as for
        # a project it cannot examine. A solution in the invocation folder that leads outside fails the same way.
        repo = self.repository
        repo.write("P/Code.cs")
        repo.write("Lib.sln", solution("P\\P.csproj"))
        repo.commit("code")
        outside = outside_folder(self)
        (outside / "W.csproj").write_text(WEB_PROJECT, encoding="utf-8")
        project = repo.root / "P" / "P.csproj"
        project.symlink_to(outside / "W.csproj")
        touched: list[tuple[str, bool]] = []
        with recording_reaches(touched):
            reason = self.failed()
        self.assertEqual(
            f"{project} cannot be examined: {linked_out(project, outside / 'W.csproj', repo.root)}", reason
        )
        self.assertEqual([], reached(touched, [project], outside))
        project.unlink()
        repo.write("P/P.csproj", SDK_PROJECT)
        linked = repo.root / "Out.sln"
        linked.symlink_to(outside / "Out.sln")
        (outside / "Out.sln").write_text(solution("P\\P.csproj"), encoding="utf-8")
        touched.clear()
        with recording_reaches(touched):
            reason = self.failed()
        self.assertEqual(f"{linked} cannot be examined: {linked_out(linked, outside / 'Out.sln', repo.root)}", reason)
        self.assertEqual([], reached(touched, [linked], outside))

    def test_a_solution_entry_outside_the_repository_is_compared_by_its_text_listing_no_folder_outside(self) -> None:
        # Lib.sln lists Lib\Lib.csproj and a project outside the repository. The choice of the solution compares
        # each entry with the changed file's project; the outside entry is compared by its text, so the case probe of
        # the path key never lists the outside folder or anything below it.
        repo = self.repository
        repo.write("Lib/Lib.csproj", SDK_PROJECT)
        repo.write("Lib/Code.cs")
        outside = outside_folder(self)
        entry = os.path.relpath(outside / "E" / "E.csproj", repo.root).replace("/", "\\")
        repo.write("Lib.sln", solution("Lib\\Lib.csproj", entry))
        repo.commit("code")
        touched: list[tuple[str, bool]] = []
        with recording_reaches(touched):
            lines = self.resolve(services=Services(dotnet=False))
        self.assertEqual([["Lib.sln", "1"]], self.values(lines, "SOLUTION"))
        self.assertEqual([], reached(touched, [], outside))
        self.assertNotIn(str(outside), [name for name, _ in touched])

    @unittest.skipIf(sys.platform == "win32", "Windows creates no file or folder under a name it reserves as a device")
    def test_a_changed_file_or_folder_named_as_a_windows_device_is_formatted_where_the_system_allows_it(self) -> None:
        # Con.cs and the folder Aux are ordinary names on Linux and macOS. A name git lists or the search finds is
        # judged by its links alone, so neither the changed files nor the solution in Tools/Aux ends the run; the
        # device-name rule is for a path a file names, such as a solution's entry.
        repo = self.repository
        repo.write("App/App.csproj", SDK_PROJECT)
        repo.write("App/Con.cs")
        repo.write("Lib/Lib.csproj", SDK_PROJECT)
        repo.write("Lib/Aux/Helper.cs")
        repo.write("Tools/Aux/All.sln", solution("..\\..\\App\\App.csproj", "..\\..\\Lib\\Lib.csproj"))
        repo.commit("code")
        lines = self.resolve()
        self.assertEqual([["App/Con.cs"], ["Lib/Aux/Helper.cs"]], self.values(lines, "FILE"))
        self.assertEqual([["Tools/Aux/All.sln", "2"]], self.values(lines, "SOLUTION"))

    def test_a_folder_the_search_upward_climbs_that_links_outside_is_refused_unlisted(self) -> None:
        # Out is a folder link (a junction on Windows) leading outside the repository. resolve takes the invocation
        # folder resolved and git lists no file through a folder link, so neither search upward meets one there; called
        # with such a folder, each refuses it naming the link, with nothing outside listed.
        repo = self.repository
        outside = outside_folder(self)
        (outside / "Out.sln").write_text(solution("E\\E.csproj"), encoding="utf-8")
        link = repo.root / "Out"
        link_folder(outside, link)
        self.addCleanup(remove_link, link)
        reason = (
            f"{link.as_posix()} is a link to {outside.as_posix()}, outside the examined tree {repo.root.as_posix()}, "
            "which the reader does not follow"
        )
        touched: list[tuple[str, bool]] = []
        searches: tuple[Callable[[], object], ...] = (
            lambda: targets.owning_projects(repo.root, link / "E" / "Code.cs"),
            lambda: targets.nearest_solutions(repo.root, link),
        )
        for search in searches:
            with recording_reaches(touched), self.assertRaises(TreeRefused) as refused:
                search()
            self.assertEqual(reason, refused.exception.reason)
        self.assertEqual([], reached(touched, [link], outside))

    def test_a_solution_the_repository_search_finds_that_is_a_link_out_fails_unread(self) -> None:
        # Run from src/Lib, whose project src/App.sln does not list, so the nearest solution owns no changed file and
        # the search of the whole repository runs. The root's Out.sln leads outside, to a solution listing
        # src\\Lib\\Lib.csproj: read, it would be chosen; it is never stat or opened, and the run fails naming it, as
        # for a solution it cannot examine (not on Windows, where a link to a file needs a privilege). The folder link
        # Elsewhere, a junction on Windows, leads outside to another such solution and is passed over unstat'd.
        repo = self.repository
        repo.write("src/Lib/Lib.csproj", SDK_PROJECT)
        repo.write("src/Lib/Code.cs")
        repo.write("src/App.sln", solution("Nope\\N.csproj"))
        repo.commit("code")
        outside = outside_folder(self)
        (outside / "Out.sln").write_text(solution("src\\Lib\\Lib.csproj"), encoding="utf-8")
        (outside / "Deep").mkdir()
        (outside / "Deep" / "Deep.sln").write_text(solution("src\\Lib\\Lib.csproj"), encoding="utf-8")
        folder = repo.root / "Elsewhere"
        link_folder(outside / "Deep", folder)
        self.addCleanup(remove_link, folder)
        touched: list[tuple[str, bool]] = []
        if sys.platform != "win32":  # a symbolic link to a file needs a privilege on Windows
            linked = repo.root / "Out.sln"
            linked.symlink_to(outside / "Out.sln")
            with recording_reaches(touched):
                reason = self.failed(cwd=repo.root / "src" / "Lib")
            self.assertEqual(
                f"{linked} cannot be examined: {linked_out(linked, outside / 'Out.sln', repo.root)}", reason
            )
            self.assertEqual([], reached(touched, [linked, folder], outside))
            linked.unlink()
            touched.clear()
        with recording_reaches(touched):
            lines = self.resolve(cwd=repo.root / "src" / "Lib")
        self.assertEqual(
            [["changed files do not belong to any solution found: src/App.sln"]], self.values(lines, "STOP")
        )
        self.assertEqual([], reached(touched, [folder], outside))

    @unittest.skipIf(sys.platform == "win32", "a symbolic link to a file needs a privilege on Windows")
    def test_a_changed_file_that_is_a_link_out_fails_unstat_d(self) -> None:
        # Code.cs is tracked as a link to a file outside the repository: it is judged by its own metadata and text
        # before anything stats it, and the run fails naming it, as for a changed file it cannot examine.
        repo = self.repository
        repo.write("Lib/Lib.csproj", SDK_PROJECT)
        repo.write("Lib.sln", solution("Lib\\Lib.csproj"))
        outside = outside_folder(self)
        (outside / "Code.cs").write_text("class C { }\n", encoding="utf-8")
        linked = repo.root / "Lib" / "Code.cs"
        linked.symlink_to(outside / "Code.cs")
        repo.commit("code")
        touched: list[tuple[str, bool]] = []
        with recording_reaches(touched):
            reason = self.failed()
        self.assertEqual(
            f"the changed file Lib/Code.cs cannot be examined: {linked_out(linked, outside / 'Code.cs', repo.root)}",
            reason,
        )
        self.assertEqual([], reached(touched, [linked], outside))


class ListedRefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def test_an_entry_inside_the_root_is_kept_and_a_device_or_share_form_is_refused_unjudged(self) -> None:
        judge = folder_walk.followed_inside
        judged: list[Path] = []

        def recording_judge(path: Path, tree: Path) -> tuple[Path | None, str | None]:
            judged.append(path)
            return judge(path, tree)

        with mock.patch("dotnet_format_targets.followed_inside", recording_judge):
            self.assertIsNone(targets.listed_refusal(self.root, "src/./App/../App/Program.cs"))
            self.assertEqual(
                [self.root / "src" / "App" / "Program.cs", self.root / "src" / "App" / ".." / "App" / "Program.cs"],
                judged,
                "the entry by its text and as written",
            )
            for entry, reason in (
                ("//?/C:/Program.cs", "the path names a device"),
                ("\\\\localhost\\share\\Program.cs", "the path names a network share"),
            ):
                with self.subTest(entry=entry):
                    refused = targets.listed_refusal(self.root, entry)
                    self.assertTrue(refused and refused.startswith(reason), refused)
        self.assertEqual(2, len(judged), "a refused form is never judged by its links")

    def test_an_absolute_entry_or_solution_is_refused_unjudged_inside_the_root_or_outside_it(self) -> None:
        inside = self.root / "inside.cs"
        outside = self.root.parent / "outside.cs"
        absolute = "the entry is an absolute path, and an entry names a file relative to the repository root"
        with mock.patch("dotnet_format_targets.followed_inside") as judge:
            # The form with backslashes is absolute on Linux too once they read as separators.
            for entry in (str(inside), inside.as_posix(), inside.as_posix().replace("/", "\\")):
                with self.subTest(entry=entry):
                    self.assertEqual(absolute, targets.listed_refusal(self.root, entry))
                    self.assertEqual(
                        f"the solution {entry} is refused: {absolute}", targets.solution_refusal(self.root, entry)
                    )
            refused = targets.listed_refusal(self.root, str(outside))
            self.assertTrue(refused and refused.startswith("the path is outside the examined tree"), refused)
        judge.assert_not_called()

    def test_an_entry_whose_name_cannot_be_examined_is_refused_with_the_reason(self) -> None:
        real_lstat = os.lstat
        program = self.root / "Program.cs"

        def lstat(path: Any, *rest: Any, **options: Any) -> os.stat_result:
            if Path(path) == program:
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
            return real_lstat(path, *rest, **options)

        with mock.patch.object(os, "lstat", lstat):
            refused = targets.listed_refusal(self.root, "Program.cs")
        self.assertEqual("its links cannot be followed inside the repository root: Permission denied", refused)

    def test_an_entry_a_link_leads_outside_is_refused_with_nothing_outside_reached(self) -> None:
        # A folder link (a junction on Windows) on the entry's way out of the root: the entry is refused naming the
        # link, from its own metadata and its text, with nothing below it or in the outside folder stat or listed,
        # where resolving the entry would lstat each name of the folder the link leads to.
        outside = outside_folder(self)
        (outside / "Outside.cs").write_text("class C { }\n", encoding="utf-8")
        link = self.root / "Linked"
        link_folder(outside, link)
        self.addCleanup(remove_link, link)
        touched: list[tuple[str, bool]] = []
        with recording_reaches(touched):
            refused = targets.listed_refusal(self.root, "Linked/Outside.cs")
        self.assertEqual(linked_out(link, outside, self.root), refused)
        self.assertIn((str(link), False), touched)
        self.assertEqual([], reached(touched, [link], outside))

    def test_an_entry_holding_dot_dot_is_judged_by_its_text_and_as_linux_and_macos_open_it(self) -> None:
        # `In` leads to a/b inside the root and a/Out leads outside, so `In/../Out/Outside.cs` reads as Out/Outside.cs
        # by its text, which Windows opens and nothing is at, and as a/Out/Outside.cs as written, which Linux and macOS
        # open, taking the `..` after following `In`: it is refused naming a/Out on every platform. The root's Away
        # leads outside too, so `Missing/../Away/Outside.cs`, which Linux and macOS cannot open past the missing name,
        # is refused by its text, as Windows opens it, naming that link.
        outside = outside_folder(self)
        (self.root / "a" / "b").mkdir(parents=True)
        for link, target in (
            (self.root / "In", self.root / "a" / "b"),
            (self.root / "a" / "Out", outside),
            (self.root / "Away", outside),
        ):
            link_folder(target, link)
            self.addCleanup(remove_link, link)
        cases = {
            "In/../Out/Outside.cs": linked_out(self.root / "a" / "Out", outside, self.root),
            "Missing/../Away/Outside.cs": linked_out(self.root / "Away", outside, self.root),
        }
        for entry, reason in cases.items():
            touched: list[tuple[str, bool]] = []
            with self.subTest(entry=entry), recording_reaches(touched):
                self.assertEqual(reason, targets.listed_refusal(self.root, entry))
            self.assertEqual([], reached(touched, [self.root / "a" / "Out", self.root / "Away"], outside))
        # A `..` whose either reading stays inside the root is kept.
        self.assertIsNone(targets.listed_refusal(self.root, "In/../b/Inside.cs"))

    def climbing(self, links: Mapping[str, Path | str], entry: str) -> None:
        """Make each link of `links`, by its path below the root, to its target, and check that `entry`, a FILE_LIST
        entry and a --solution, is refused as climbing above the root after them, with nothing outside reached; a
        `..` after a link to the root leaves the root as Linux and macOS open it, though the text reads inside."""
        outside = outside_folder(self)
        (outside / "Outside.cs").write_text("class C { }\n", encoding="utf-8")
        made = []
        for relative, target in links.items():
            link = self.root / relative
            link.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(target, Path):
                link_folder(target, link)
            else:
                link.symlink_to(target, target_is_directory=True)
            self.addCleanup(remove_link, link)
            made.append(link)
        entry = entry.format(outside=outside.name)
        reason = (
            "its links cannot be followed inside the repository root: a `..` in it climbs above the root once the"
            " links before it are followed"
        )
        touched: list[tuple[str, bool]] = []
        with recording_reaches(touched):
            self.assertEqual(reason, targets.listed_refusal(self.root, entry))
            self.assertEqual(f"the solution {entry} is refused: {reason}", targets.solution_refusal(self.root, entry))
        self.assertEqual([], reached(touched, made, outside))

    def test_a_dot_dot_after_a_link_to_the_root_by_its_absolute_path_is_refused(self) -> None:
        # Abs (a junction on Windows) leads to the root itself, so Abs/.. is the folder above the root.
        self.climbing({"Abs": self.root}, "Abs/../{outside}/Outside.cs")

    @unittest.skipIf(sys.platform == "win32", "a relative symbolic link needs a privilege on Windows")
    def test_a_dot_dot_after_a_relative_link_to_the_root_is_refused(self) -> None:
        # In leads to `.`, the root, and A/Up to `..`, the root from A, so the `..` after either is the folder above.
        for links, entry in (
            ({"In": "."}, "In/../{outside}/Outside.cs"),
            ({"A/Up": ".."}, "A/Up/../{outside}/Outside.cs"),
        ):
            with self.subTest(entry=entry):
                self.climbing(links, entry)


class ConsoleTests(unittest.TestCase):
    def test_output_a_cp1252_console_cannot_encode_is_written_as_utf_8(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository(Path(directory).resolve())
            repository.git("init", "--quiet", "--initial-branch=main")
            repository.write("README.md", "fixture\n")
            repository.commit("base")
            repository.git("update-ref", "refs/remotes/origin/main", "HEAD")
            repository.write("arrow → ✓.cs")
            result = subprocess.run(
                [sys.executable, "-B", targets.__file__, "resolve", "--cwd", str(repository.root)],
                capture_output=True,
                env={**os.environ, "PYTHONIOENCODING": "cp1252"},
                check=False,
            )
        self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", "replace"))
        lines = result.stdout.decode("utf-8").splitlines()
        self.assertIn("FILE\tarrow → ✓.cs", lines)
        self.assertEqual(f"STOP\tno .sln or .slnx found under {repository.root}", lines[-1])


class FileListTests(unittest.TestCase):
    """The one reader of a FILE_LIST, which the formatter and the layout checker both use."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "file list.txt"

    def test_every_name_reads_back_as_resolve_writes_it(self) -> None:
        # A leading space, a byte order mark, and the line separators a line reader splits at belong to the name.
        names = [
            " Leading.cs",
            "file\u2028part.cs",
            "x\u0085y.cs",
            "a\r\nb.cs",
            "\ufeffMarked.cs",
            "src/My Folder/B.cs",
        ]
        written = Path(targets.write_file_list(names))
        self.addCleanup(written.unlink)
        self.assertTrue(written.name.startswith("dotnet-format-files-") and written.name.endswith(".txt"), written)
        self.assertEqual("".join(f"{name}\0" for name in names).encode("utf-8"), written.read_bytes())
        self.assertEqual(names, targets.read_file_list(written))

    def test_the_list_is_written_in_the_temporary_directory_private_folder_judged(self) -> None:
        # The formatter reads the list back as the files it may change, so it lies only where no other account can
        # swap it.
        with recording_temporary_folders() as calls:
            written = Path(targets.write_file_list(["A.cs"]))
        self.addCleanup(written.unlink)
        self.assertEqual([True], judged_folder_calls(calls, "dotnet-format-files-"))

    def test_no_names_are_refused_before_a_list_is_written(self) -> None:
        with mock.patch.object(tempfile, "mkstemp") as mkstemp, self.assertRaises(targets.Failed) as context:
            targets.write_file_list([])
        self.assertEqual("there is no changed file to list", str(context.exception))
        mkstemp.assert_not_called()

    def test_a_name_that_is_not_utf_8_is_refused_before_a_list_is_written(self) -> None:
        with mock.patch.object(tempfile, "mkstemp") as mkstemp, self.assertRaises(targets.Failed) as context:
            targets.write_file_list(["A.cs", "caf\udce9.cs"])
        self.assertEqual(
            "the changed file 'caf\\udce9.cs' has a name that is not UTF-8, which FILE_LIST cannot hold",
            str(context.exception),
        )
        mkstemp.assert_not_called()

    def test_an_empty_undecodable_or_line_separated_list_is_refused(self) -> None:
        for content, message in (
            (b"", "lists no files"),
            (b"caf\xe9.cs\0", "is not UTF-8 text"),
            (b"src/A.cs\nsrc/B.cs\n", "does not end each name with a NUL, as resolve writes a FILE_LIST"),
            (b"src/A.cs\0src/B.cs", "does not end each name with a NUL, as resolve writes a FILE_LIST"),
            (b"src/A.cs\0\0", "name 2 is empty, which no path is"),
            (b"\0", "name 1 is empty, which no path is"),
        ):
            self.path.write_bytes(content)
            with self.subTest(content=content), self.assertRaises(ValueError) as context:
                targets.read_file_list(self.path)
            self.assertEqual(f"{self.path} {message}", str(context.exception))

    def test_a_missing_list_is_an_os_error(self) -> None:
        with self.assertRaises(FileNotFoundError):
            targets.read_file_list(self.path)


def install(folder: Path, name: str) -> Path:
    """A program as its installer leaves it: `<name>.exe` on Windows, and an executable `<name>` elsewhere."""
    program = folder / (f"{name}.exe" if sys.platform == "win32" else name)
    program.write_bytes(b"fixture")
    program.chmod(0o755)
    return program


class LocatedToolsTests(unittest.TestCase):
    """gh and the .NET SDK are found by ejryan89-skill-core's locator, as `deploy.py check` finds them, and gh runs at
    that absolute path. The environment is sanitized: PATH is one empty folder and the local application data folder a
    fresh one, so the only programs found are the ones a test puts there."""

    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="targets locator ")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.path_folder = root / "on path"
        if sys.platform == "win32":
            self.links = root / "local app data" / "Microsoft" / "WinGet" / "Links"
            environment = {
                "PATH": str(self.path_folder),
                "PATHEXT": ".com;.exe",
                "LOCALAPPDATA": str(root / "local app data"),
            }
        else:
            # No package manager links commands into a folder of its own off Windows, so the folder PATH names
            # after the first stands in for one: the place the locator looks once the first has no such program.
            self.links = root / "later on path"
            environment = {"PATH": os.pathsep.join((str(self.path_folder), str(self.links)))}
        self.path_folder.mkdir()
        self.links.mkdir(parents=True)
        patch = mock.patch.dict(os.environ, environment)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_tool_only_in_wingets_links_folder_is_found_and_gh_is_run_from_there(self) -> None:
        services = targets.Services()
        for name in ("gh", "dotnet"):
            with self.subTest(name=name):
                self.assertIsNone(services.which(name))
                program = install(self.links, name)
                self.assertEqual(str(program), services.which(name))
        finished = bounded_process.Finished(0, b"{}", b"")
        with mock.patch.object(github_client, "run_bounded", return_value=finished) as run:
            services.gh(self.path_folder)(["gh", "pr", "view", "--json", "baseRefName"])
        linked = str(self.links / ("gh.exe" if sys.platform == "win32" else "gh"))
        self.assertEqual([linked, "pr", "view", "--json", "baseRefName"], run.call_args.args[0])

    def test_a_tool_on_path_comes_before_the_links_folder(self) -> None:
        install(self.links, "gh")
        on_path = install(self.path_folder, "gh")
        self.assertEqual(str(on_path), targets.Services().which("gh"))


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param([], id="no-command"),
        pytest.param(["resolve", "--unknown"], id="unknown-option"),
        pytest.param(["other"], id="unknown-command"),
    ],
)
def test_usage_errors_exit_2_on_stderr(arguments: list[str]) -> None:
    output, errors = io.StringIO(), io.StringIO()
    with (
        contextlib.redirect_stdout(output),
        contextlib.redirect_stderr(errors),
        pytest.raises(SystemExit) as raised,
    ):
        targets.main(arguments, Services())
    assert raised.value.code == 2
    assert output.getvalue() == ""
    assert "usage:" in errors.getvalue()


# Every character `str.splitlines()` breaks a line at, read from Python rather than typed, and the tab a fact line
# splits its fields at.
LINE_BREAKS = [chr(code) for code in range(0x3000) if len(f"a{chr(code)}b".splitlines()) == 2]
FIELD_BREAKS = [*LINE_BREAKS, "\t"]


def holdable_breaks(directory: Path) -> list[str]:
    """The FIELD_BREAKS this file system lets a file name hold; Windows refuses control characters in a name."""
    held: list[str] = []
    for character in FIELD_BREAKS:
        probe = directory / f"probe{character}.cs"
        try:
            probe.write_bytes(b"")
        except OSError:
            continue
        probe.unlink()
        held.append(character)
    return held


class OneLineFactTests(unittest.TestCase):
    """A name or a reason holding a line break or a tab stays one fact on one line, through `fact_line`."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name).resolve()

    def test_line_breaks_are_read_from_python(self) -> None:
        self.assertTrue({"\n", "\r", "\x85", "\u2028", "\u2029"} <= set(LINE_BREAKS), LINE_BREAKS)

    def test_a_refused_entry_holding_a_line_or_field_break_prints_one_refused_line(self) -> None:
        for character in FIELD_BREAKS:
            entry = str(self.directory / f"x{character}STOP\tthing.cs")
            printed: list[str] = []
            with self.subTest(character=character):
                self.assertEqual([], targets.inside_root(self.directory, [entry], printed.append))
                (line,) = "\n".join(printed).splitlines()
                self.assertEqual(["REFUSED", flat_text(entry)], line.split("\t")[:2])
                self.assertEqual(3, len(line.split("\t")))

    def test_changed_files_and_a_root_holding_a_line_break_print_one_line_each(self) -> None:
        held = holdable_breaks(self.directory)
        self.assertIn("\u2028", held, "every supported file system holds a line separator in a name")
        repository = Repository(self.directory / "repo\u2028STOP")
        repository.root.mkdir()
        repository.git("init", "--quiet", "--initial-branch=main")
        repository.write("README.md", "fixture\n")
        repository.commit("base")
        repository.git("update-ref", "refs/remotes/origin/main", "HEAD")
        names = [f"x{character}STOP thing.cs" for character in held]
        for name in names:
            repository.write(name)
        repository.commit("names")
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = targets.main(["resolve", "--cwd", str(repository.root)], Services())
        self.assertEqual((0, ""), (status, errors.getvalue()))
        lines = [line.split("\t") for line in output.getvalue().splitlines()]
        # The fixture's fetch fails, so a FETCH_FAILED line comes before BASE.
        self.assertEqual(
            ["REPO_ROOT", "FETCH_FAILED", "BASE", *("FILE" for _ in names), "STOP"], [fields[0] for fields in lines]
        )
        self.assertEqual([2] * len(lines), [len(fields) for fields in lines])
        self.assertEqual(flat_text(str(repository.root)), lines[0][1])
        self.assertEqual(sorted(flat_text(name) for name in names), sorted(fields[1] for fields in lines[3:-1]))
        self.assertEqual(f"no .sln or .slnx found under {flat_text(str(repository.root))}", lines[-1][1])


def test_origin_s_default_branch_keeps_a_name_ending_in_a_character_strip_drops() -> None:
    def run(command: Sequence[str], timeout: float) -> git_client.GitResult:
        return git_client.GitResult(0, "refs/remotes/origin/main\u00a0\n", "")

    services = targets.Services(git=git_client.GitClient(run))
    assert targets.remote_default_branch(services, Path("r")) == "origin/main\u00a0"


@pytest.mark.parametrize(
    ("expected", "sdk"),
    [
        (True, "Microsoft.NET.Sdk.Web"),
        (True, "MICROSOFT.NET.SDK.WEB"),
        # The long s and the Kelvin sign are no s or k to MSBuild, which compares ordinally ignoring ASCII case.
        (False, "Microsoft.NET.\u017fdk.Web"),
        (False, "Microsoft.NET.Sd\u212a.Web"),
    ],
)
def test_a_web_project_is_named_as_msbuild_compares_its_sdk(expected: bool, sdk: str, tmp_path: Path) -> None:
    project = tmp_path / "App.csproj"
    project.write_text(f'<Project Sdk="{sdk}"></Project>\n', encoding="utf-8")
    assert expected is targets.is_web_project(TreeReader(tmp_path), project)


LONG_S = "\u017f"


def test_a_solution_or_project_suffix_spelled_with_a_long_s_is_not_one(tmp_path: Path) -> None:
    # The long s is no s to .NET's ordinal comparison ignoring case: a file whose suffix spells .slnx with it is no
    # .slnx for the SDK's formatter, and a listed project whose suffix spells .csproj with it is no C# project.
    (tmp_path / "Lib").mkdir()
    (tmp_path / "Lib" / f"Lib.c{LONG_S}proj").write_text(SDK_PROJECT, encoding="utf-8")
    listed = tmp_path / "App.sln"
    listed.write_text(solution(f"Lib\\Lib.c{LONG_S}proj"), encoding="utf-8")
    assert targets.formatter_for(tmp_path, listed) == targets.TOOL_FORMATTER
    imitation = tmp_path / f"App.{LONG_S}lnx"
    imitation.write_text(solution(), encoding="utf-8")
    assert targets.formatter_for(tmp_path, imitation) == targets.TOOL_FORMATTER


def test_a_tie_goes_to_a_sln_over_a_slnx_by_its_suffix_as_dotnet_compares_it(tmp_path: Path) -> None:
    # A long-s spelling of .slnx is no .slnx, so the tie between it and a .sln goes by name.
    named, plain = tmp_path / f"A.{LONG_S}lnx", tmp_path / "B.sln"
    assert targets.best_solution(tmp_path, [plain, named], []) == (named, 0)
    assert targets.best_solution(tmp_path, [tmp_path / "A.slnx", plain], []) == (plain, 0)
