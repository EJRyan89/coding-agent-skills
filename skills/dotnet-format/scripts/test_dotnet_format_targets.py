from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import dotnet_format_targets as targets
import git_client
from git_client import GitClient, GitError, GitResult
from github_client import CommandResult
from github_client import Runner as GhRunner

SDK_PROJECT = '<Project Sdk="Microsoft.NET.Sdk"></Project>\n'
WEB_PROJECT = '<Project Sdk="Microsoft.NET.Sdk.Web"></Project>\n'
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
        (repo.root / "App/Deleted.cs").unlink()
        repo.git("mv", "App/Renamed.cs", "App/RenamedAgain.cs")
        repo.write("App/Modified.cs", "class Modified { }\n")
        repo.write("Loose/Untracked.cs")
        repo.write("App.sln", solution("App\\App.csproj", "Forms\\Forms.csproj"))

        lines = self.resolve()

        self.assertEqual(["REPO_ROOT", str(repo.root)], lines[0])
        self.assertEqual([["origin/main"]], self.values(lines, "BASE"))
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
            "Loose/Untracked.cs",
        ]
        self.assertEqual([[name] for name in expected], self.values(lines, "FILE"))
        (file_list,) = self.values(lines, "FILE_LIST")
        self.assertEqual("".join(f"{name}\n" for name in expected), Path(file_list[0]).read_text(encoding="utf-8"))
        self.assertEqual([["App.sln", "4"]], self.values(lines, "SOLUTION"))
        self.assertEqual([["Loose/Untracked.cs"]], self.values(lines, "OUTSIDE_SOLUTION"))
        self.assertEqual("SOLUTION", lines[-1][0])

    def test_web_project_at_repository_root_owns_files_without_a_nearer_project(self) -> None:
        self.repository.write("Root.csproj", WEB_PROJECT)
        self.repository.write("Pages/Index.cs")
        self.repository.commit("web")
        lines = self.resolve()
        self.assertEqual([["Pages/Index.cs", "Root.csproj"]], self.values(lines, "SKIPPED_ASPNET"))
        self.assertEqual(
            [["no C# files changed vs origin/main (excluding ASP.NET projects)"]], self.values(lines, "STOP")
        )

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
        repo.write("B.sln", solution("lib\\LIB.csproj"))
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
        self.assertEqual(
            [["no C# files changed vs origin/main (excluding ASP.NET projects)"]], self.values(self.resolve(), "STOP")
        )

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
        sleep.assert_not_called()
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

    def test_a_failed_git_command_fails(self) -> None:
        self.repository.write("Code.cs")
        self.repository.commit("code")
        services = Services()
        real = services.git.runner

        def run(command: Sequence[str], timeout: float) -> GitResult:
            if command[3] == "ls-files":
                return GitResult(128, "", "")
            return real(command, timeout)

        services.git = GitClient(run)
        self.assertEqual("git ls-files --others --exclude-standard -- *.cs failed", self.failed(services=services))

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
        self.assertEqual("git rev-parse --show-toplevel exited with code 129", self.failed(services=services))

    def test_an_unreadable_project_fails(self) -> None:
        self.repository.write("Lib/Lib.csproj", SDK_PROJECT)
        self.repository.write("Lib/Code.cs")
        self.repository.commit("code")
        denied = PermissionError(13, "Permission denied", "Lib/Lib.csproj")
        with mock.patch.object(Path, "read_bytes", side_effect=denied):
            self.assertEqual("[Errno 13] Permission denied: 'Lib/Lib.csproj'", self.failed())

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
            self.assertEqual("[Errno 13] Permission denied: 'Locked'", self.failed())

    def test_an_unwritable_temporary_directory_fails(self) -> None:
        self.repository.write("Lib/Lib.csproj", SDK_PROJECT)
        self.repository.write("Lib/Code.cs")
        self.repository.write("Lib.sln", solution("Lib\\Lib.csproj"))
        self.repository.commit("code")
        with mock.patch.object(tempfile, "mkstemp", side_effect=OSError(28, "No space left on device")):
            self.assertEqual("[Errno 28] No space left on device", self.failed())

    def test_usage_errors_exit_2_on_stderr(self) -> None:
        for arguments in ([], ["resolve", "--unknown"], ["other"]):
            with self.subTest(arguments=arguments):
                output, errors = io.StringIO(), io.StringIO()
                with (
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(errors),
                    self.assertRaises(SystemExit) as raised,
                ):
                    targets.main(arguments, Services())
                self.assertEqual(2, raised.exception.code)
                self.assertEqual("", output.getvalue())
                self.assertIn("usage:", errors.getvalue())

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
        status, lines, errors = self.run_resolve(services=Services(dotnet=False))
        self.assertEqual((0, ""), (status, errors))
        self.assertEqual(
            [
                "STOP",
                "App.slnx is a .slnx solution, which only the .NET SDK's dotnet format opens; "
                "install the .NET SDK 9.0.200 or newer",
            ],
            lines[-1],
        )
        self.assertEqual([], self.values(lines, "FILE_LIST"))
        self.assertEqual([], self.values(lines, "SOLUTION"))

        repo.write("App.sln", solution("Lib\\Lib.csproj"))
        repo.commit("sln")
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

    def test_an_slnx_that_is_not_xml_owns_nothing(self) -> None:
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
        self.assertEqual(
            [["changed files do not belong to any solution found: Broken.slnx, Entity.slnx, Other.slnx"]],
            self.values(self.resolve(), "STOP"),
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
        (repo.root / "Lib.sln").write_bytes(b"\xef\xbb\xbf" + solution("Lib\\Lib.csproj").encode("utf-8"))
        repo.commit("encodings")
        lines = self.resolve()
        self.assertEqual([["Web/Page.cs", "Web/Web.csproj"]], self.values(lines, "SKIPPED_ASPNET"))
        self.assertEqual([["Lib.sln", "1"]], self.values(lines, "SOLUTION"))


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


if __name__ == "__main__":
    unittest.main()
