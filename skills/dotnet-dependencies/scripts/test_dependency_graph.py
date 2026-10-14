"""Regression tests for the dependency graph's queries on a mixed solution of invented projects."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import dependency_graph
from dependency_graph import Services, main
from dotnet_solution import same_path
from sdk_why import Completed

SCRIPT = Path(__file__).resolve().parent / "dependency_graph.py"
CSHARP = "{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}"
WEB_PROJECT = """<?xml version="1.0" encoding="utf-8"?>
<Project ToolsVersion="15.0" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <PropertyGroup>
    <TargetFrameworkVersion>v4.7.2</TargetFrameworkVersion>
  </PropertyGroup>
  <ItemGroup>
    <Reference Include="Lantern.Json, Version=13.0.0.0, Culture=neutral">
      <HintPath>..\\..\\packages\\Lantern.Json.13.0.1\\lib\\net45\\Lantern.Json.dll</HintPath>
    </Reference>
    <Reference Include="Lantern.Core, Version=2.0.0.0, Culture=neutral">
      <HintPath>..\\..\\packages\\Lantern.Core.2.0.0\\lib\\net45\\Lantern.Core.dll</HintPath>
    </Reference>
    <Reference Include="Quill.Http, Version=1.2.0.0, Culture=neutral">
      <HintPath>..\\..\\packages\\Quill.Http.1.2.0\\lib\\net45\\Quill.Http.dll</HintPath>
    </Reference>
    <Reference Include="System.Web" />
  </ItemGroup>
  <ItemGroup>
    <ProjectReference Include="..\\Harbor.Core\\Harbor.Core.csproj" />
  </ItemGroup>
</Project>
"""
WEB_PACKAGES = """<?xml version="1.0" encoding="utf-8"?>
<packages>
  <package id="Lantern.Core" version="2.0.0" targetFramework="net472" />
  <package id="Lantern.Json" version="13.0.1" targetFramework="net472" />
  <package id="Quill.Http" version="1.2.0" targetFramework="net472" />
</packages>
"""
WEB_CONFIG = """<?xml version="1.0"?>
<configuration>
  <runtime>
    <assemblyBinding xmlns="urn:schemas-microsoft-com:asm.v1">
      <dependentAssembly>
        <assemblyIdentity name="Lantern.Json" culture="neutral" />
        <bindingRedirect oldVersion="0.0.0.0-13.0.0.0" newVersion="13.0.0.0" />
      </dependentAssembly>
      <dependentAssembly>
        <assemblyIdentity name="Quill.Http" culture="neutral" />
        <bindingRedirect oldVersion="0.0.0.0-2.0.0.0" newVersion="2.0.0.0" />
      </dependentAssembly>
      <dependentAssembly>
        <assemblyIdentity name="Ghost.Assembly" culture="neutral" />
        <bindingRedirect oldVersion="0.0.0.0-1.0.0.0" newVersion="1.0.0.0" />
      </dependentAssembly>
    </assemblyBinding>
  </runtime>
</configuration>
"""
CORE_NUSPEC = """<?xml version="1.0"?>
<package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd">
  <metadata>
    <id>Lantern.Core</id>
    <version>2.0.0</version>
    <dependencies>
      <dependency id="Lantern.Json" version="[12.0.0, )" />
    </dependencies>
  </metadata>
</package>
"""
JSON_NUSPEC = """<package><metadata><id>Lantern.Json</id><version>13.0.1</version></metadata></package>
"""
CORE_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Quill.Http" Version="1.2.0" />
  </ItemGroup>
</Project>
"""
CORE_ASSETS = {
    "version": 3,
    "targets": {
        "net8.0": {
            "Lantern.Json/12.0.3": {"type": "package", "compile": {"lib/netstandard2.0/Lantern.Json.dll": {}}},
            "Quill.Http/1.2.0": {
                "type": "package",
                "dependencies": {"Lantern.Json": "12.0.3"},
                "compile": {"lib/net8.0/Quill.Http.dll": {}},
            },
        }
    },
    "libraries": {},
    "projectFileDependencyGroups": {"net8.0": ["Quill.Http >= 1.2.0"]},
    "packageFolders": {},
    "project": {
        "frameworks": {"net8.0": {"dependencies": {"Quill.Http": {"target": "Package", "version": "[1.2.0, )"}}}}
    },
}
TESTS_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Lantern.Json" />
    <ProjectReference Include="../Harbor.Core/Harbor.Core.csproj" />
  </ItemGroup>
</Project>
"""
CENTRAL = """<Project>
  <ItemGroup><PackageVersion Include="Lantern.Json" Version="13.0.1" /></ItemGroup>
</Project>
"""
CYAN = "\x1b[38;5;14m"
RESET = "\x1b[0m"
SDK_CORE_TEXT = (
    "Project 'Harbor.Core' has the following dependency graph(s) for \r\n"
    "'Lantern.Json':\r\n"
    "\r\n"
    "  [net8.0]                                                                      \r\n"
    "  \u2514\u2500\u2500 Quill.Http (v1.2.0)                                                       \r\n"
    f"      \u2514\u2500\u2500 {CYAN}Lantern.Json (v12.0.3){RESET}\r\n"
    "\r\n"
)
SDK_ABSENT_TEXT = "Project 'Harbor.Core' does not have a dependency on '{name}'.\r\n"


def remove_file_and_directory(path: Path) -> None:
    path.unlink()
    path.parent.rmdir()


def solution(*projects: str) -> str:
    lines = ["Microsoft Visual Studio Solution File, Format Version 12.00"]
    for number, project in enumerate(projects):
        lines += [f'Project("{CSHARP}") = "{Path(project).stem}", "{project}", "{{{number}}}"', "EndProject"]
    lines += [
        'Project("{2150E333-8FDC-42A3-9474-1A3956D46DE8}") = "Solution Items", "Solution Items", "{X}"',
        "EndProject",
    ]
    return "\r\n".join(lines) + "\r\n"


class FakeSdk:
    """A `dotnet nuget why` that answers Harbor.Core from the captured text and refuses every other project."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, arguments: Sequence[str], timeout: float) -> Completed:
        self.commands.append(list(arguments))
        project, package = Path(arguments[-2]), arguments[-1]
        if project.stem != "Harbor.Core":
            return Completed(
                0, "No assets file was found for the project. Please run restore before running this command."
            )
        if package.casefold() == "lantern.json":
            return Completed(0, SDK_CORE_TEXT)
        return Completed(0, SDK_ABSENT_TEXT.format(name=package))


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dotnet-dependencies-graph-")).resolve()
        self.addCleanup(self.remove_root)
        self.sdk = FakeSdk()
        self.with_dotnet = Services(
            run=self.sdk, which=lambda name: "C:/tools/dotnet.exe" if name == "dotnet" else None
        )
        self.without_dotnet = Services(run=self.sdk, which=lambda name: None)

    def remove_root(self) -> None:
        for path in sorted(self.root.rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        self.root.rmdir()

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def mixed_solution(self, assets: bool = True) -> Path:
        self.write("src/Harbor.Web/Harbor.Web.csproj", WEB_PROJECT)
        self.write("src/Harbor.Web/packages.config", WEB_PACKAGES)
        self.write("src/Harbor.Web/Web.config", WEB_CONFIG)
        self.write("packages/Lantern.Core.2.0.0/Lantern.Core.nuspec", CORE_NUSPEC)
        self.write("packages/Lantern.Core.2.0.0/lib/net45/Lantern.Core.dll", "")
        self.write("packages/Lantern.Json.13.0.1/Lantern.Json.nuspec", JSON_NUSPEC)
        self.write("packages/Lantern.Json.13.0.1/lib/net45/Lantern.Json.dll", "")
        self.write("src/Harbor.Core/Harbor.Core.csproj", CORE_PROJECT)
        if assets:
            self.write("src/Harbor.Core/obj/project.assets.json", json.dumps(CORE_ASSETS))
        self.write("src/Harbor.Tests/Harbor.Tests.csproj", TESTS_PROJECT)
        self.write("Directory.Packages.props", CENTRAL)
        return self.write(
            "Harbor.sln",
            solution(
                "src\\Harbor.Web\\Harbor.Web.csproj",
                "src\\Harbor.Core\\Harbor.Core.csproj",
                "src\\Harbor.Tests\\Harbor.Tests.csproj",
            ),
        )

    def run_main(self, *arguments: str, services: Services | None = None) -> tuple[int, list[list[str]]]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(list(arguments), services or self.without_dotnet)
        return code, [line.split("\t") for line in output.getvalue().splitlines()]

    def lines(self, rows: list[list[str]], key: str) -> list[list[str]]:
        return [row[1:] for row in rows if row[0] == key]


class GraphTests(Fixture):
    def test_each_project_states_its_style_and_where_its_package_edges_came_from(self) -> None:
        root = self.mixed_solution()
        _, rows = self.run_main("conflicts", "--root", str(root))
        self.assertEqual([root.as_posix()], self.lines(rows, "ROOT")[0])
        self.assertEqual(
            [
                ["src/Harbor.Core/Harbor.Core.csproj", "sdk", "assets", "listed"],
                ["src/Harbor.Tests/Harbor.Tests.csproj", "sdk", "direct", "listed"],
                ["src/Harbor.Web/Harbor.Web.csproj", "legacy", "nuspec", "listed"],
            ],
            self.lines(rows, "PROJECT"),
        )
        self.assertEqual([], self.lines(rows, "UNREADABLE"))

    def test_why_walks_the_graph_for_every_project_when_dotnet_is_absent(self) -> None:
        root = self.mixed_solution()
        code, rows = self.run_main("why", "Lantern.Json", "--root", str(root))
        self.assertEqual(0, code)
        self.assertEqual([], self.sdk.commands)
        self.assertEqual(
            [
                [
                    "src/Harbor.Core/Harbor.Core.csproj",
                    "net8.0",
                    "Lantern.Json",
                    "graph",
                    "Quill.Http (v1.2.0) > Lantern.Json (v12.0.3)",
                ],
                [
                    "src/Harbor.Tests/Harbor.Tests.csproj",
                    "-",
                    "Lantern.Json",
                    "graph",
                    "Harbor.Core (project) > Quill.Http (v1.2.0) > Lantern.Json (v12.0.3)",
                ],
                ["src/Harbor.Tests/Harbor.Tests.csproj", "-", "Lantern.Json", "graph", "Lantern.Json (v13.0.1)"],
                [
                    "src/Harbor.Web/Harbor.Web.csproj",
                    "-",
                    "Lantern.Json",
                    "graph",
                    "Harbor.Core (project) > Quill.Http (v1.2.0) > Lantern.Json (v12.0.3)",
                ],
                [
                    "src/Harbor.Web/Harbor.Web.csproj",
                    "-",
                    "Lantern.Json",
                    "graph",
                    "Lantern.Core (v2.0.0) > Lantern.Json (v13.0.1)",
                ],
                ["src/Harbor.Web/Harbor.Web.csproj", "-", "Lantern.Json", "graph", "Lantern.Json (v13.0.1)"],
                [
                    "src/Harbor.Web/Harbor.Web.csproj",
                    "-",
                    "Lantern.Json",
                    "graph",
                    "Lantern.Json (v13.0.0.0, assembly)",
                ],
            ],
            self.lines(rows, "PATH"),
        )
        self.assertEqual([["7", "1", "0"]], self.lines(rows, "SUMMARY"))

    def test_why_asks_the_sdk_only_for_a_restored_sdk_project_and_marks_each_line(self) -> None:
        root = self.mixed_solution()
        code, rows = self.run_main(
            "why", "Lantern.Json", "Nothing.Here", "--root", str(root), services=self.with_dotnet
        )
        self.assertEqual(0, code)
        self.assertEqual(
            [
                [str(self.root / "src" / "Harbor.Core" / "Harbor.Core.csproj"), "Lantern.Json"],
                [str(self.root / "src" / "Harbor.Core" / "Harbor.Core.csproj"), "Nothing.Here"],
            ],
            [command[3:] for command in self.sdk.commands],
        )
        paths = self.lines(rows, "PATH")
        self.assertIn(
            [
                "src/Harbor.Core/Harbor.Core.csproj",
                "net8.0",
                "Lantern.Json",
                "sdk",
                "Quill.Http (v1.2.0) > Lantern.Json (v12.0.3)",
            ],
            paths,
        )
        self.assertEqual({"graph"}, {row[3] for row in paths if not row[0].startswith("src/Harbor.Core")})
        self.assertEqual([], self.lines(rows, "SDK_FAILED"))
        self.assertEqual(7, len(paths))
        self.assertEqual([["Nothing.Here"]], self.lines(rows, "MISSING"))
        self.assertEqual([["7", "1", "1"]], self.lines(rows, "SUMMARY"))

    def test_a_stale_or_absent_restore_and_an_sdk_run_that_answers_nothing_fall_back_to_the_graph(self) -> None:
        root = self.mixed_solution()
        assets = self.root / "src" / "Harbor.Core" / "obj" / "project.assets.json"
        old = time.time() - 600
        os.utime(assets, (old, old))
        _, rows = self.run_main("why", "Lantern.Json", "--root", str(root), services=self.with_dotnet)
        self.assertEqual([], self.sdk.commands)
        self.assertEqual({"graph"}, {row[3] for row in self.lines(rows, "PATH")})
        assets.unlink()
        _, rows = self.run_main("why", "Lantern.Json", "--root", str(root), services=self.with_dotnet)
        self.assertEqual([], self.sdk.commands)
        self.assertEqual(
            [["src/Harbor.Core/Harbor.Core.csproj", "sdk", "direct", "listed"]],
            [row for row in self.lines(rows, "PROJECT") if "Core" in row[0]],
        )
        # Without a restore the SDK project's direct references are all the graph knows, stated as `direct`.
        self.assertEqual([], [row for row in self.lines(rows, "PATH") if row[0].startswith("src/Harbor.Core")])

    def test_a_failed_sdk_run_is_reported_and_leaves_the_project_to_the_graph(self) -> None:
        root = self.mixed_solution()
        for run, reason in (
            (lambda arguments, timeout: Completed(1, "boom"), "dotnet nuget why answered nothing (exit code 1)"),
            # A run that fails after printing a heading and part of a tree is no answer either.
            (
                lambda arguments, timeout: Completed(1, SDK_CORE_TEXT),
                "dotnet nuget why exited with code 1 after printing part of an answer",
            ),
            (
                lambda arguments, timeout: Completed(-1, "", timed_out=True),
                f"dotnet nuget why did not finish within {dependency_graph.DEFAULT_TIMEOUT_SECONDS} seconds",
            ),
        ):
            with self.subTest(reason=reason):
                services = Services(run=run, which=self.with_dotnet.which)
                code, rows = self.run_main("why", "Lantern.Json", "--root", str(root), services=services)
                # The failure is not a finding in the solution: the graph walk answered the project.
                self.assertEqual(0, code)
                self.assertEqual(
                    [["src/Harbor.Core/Harbor.Core.csproj", "Lantern.Json", reason]], self.lines(rows, "SDK_FAILED")
                )
                self.assertIn(
                    [
                        "src/Harbor.Core/Harbor.Core.csproj",
                        "net8.0",
                        "Lantern.Json",
                        "graph",
                        "Quill.Http (v1.2.0) > Lantern.Json (v12.0.3)",
                    ],
                    self.lines(rows, "PATH"),
                )

    def test_why_all_lists_everything_one_project_reaches_with_its_first_hop(self) -> None:
        root = self.mixed_solution()
        code, rows = self.run_main(
            "why",
            "--all",
            "--project",
            str(self.root / "src" / "Harbor.Web" / "Harbor.Web.csproj"),
            "--root",
            str(root),
        )
        self.assertEqual(0, code)
        self.assertEqual(
            [
                [
                    "src/Harbor.Web/Harbor.Web.csproj",
                    "-",
                    "Lantern.Core",
                    "2.0.0.0",
                    "assembly",
                    "Lantern.Core (v2.0.0.0, assembly)",
                ],
                [
                    "src/Harbor.Web/Harbor.Web.csproj",
                    "-",
                    "Lantern.Json",
                    "13.0.0.0",
                    "assembly",
                    "Lantern.Json (v13.0.0.0, assembly)",
                ],
                [
                    "src/Harbor.Web/Harbor.Web.csproj",
                    "-",
                    "Quill.Http",
                    "1.2.0.0",
                    "assembly",
                    "Quill.Http (v1.2.0.0, assembly)",
                ],
                ["src/Harbor.Web/Harbor.Web.csproj", "-", "System.Web", "-", "framework", "System.Web (framework)"],
                ["src/Harbor.Web/Harbor.Web.csproj", "-", "Lantern.Core", "2.0.0", "package", "Lantern.Core (v2.0.0)"],
                ["src/Harbor.Web/Harbor.Web.csproj", "-", "Lantern.Json", "12.0.3", "package", "Harbor.Core (project)"],
                [
                    "src/Harbor.Web/Harbor.Web.csproj",
                    "-",
                    "Lantern.Json",
                    "13.0.1",
                    "package",
                    "Lantern.Json (v13.0.1)",
                ],
                ["src/Harbor.Web/Harbor.Web.csproj", "-", "Quill.Http", "1.2.0", "package", "Quill.Http (v1.2.0)"],
                ["src/Harbor.Web/Harbor.Web.csproj", "-", "Harbor.Core", "-", "project", "Harbor.Core (project)"],
            ],
            self.lines(rows, "REACHES"),
        )
        self.assertEqual([["9"]], self.lines(rows, "SUMMARY"))

    def test_where_names_each_project_and_how_its_references_resolve_to_the_assembly(self) -> None:
        root = self.mixed_solution()
        code, rows = self.run_main("where", "lantern.json", "--root", str(root))
        self.assertEqual(0, code)
        self.assertEqual(
            [
                [
                    "src/Harbor.Core/Harbor.Core.csproj",
                    "net8.0",
                    "Lantern.Json",
                    "package",
                    "12.0.3",
                    "Quill.Http (v1.2.0)",
                ],
                [
                    "src/Harbor.Tests/Harbor.Tests.csproj",
                    "-",
                    "Lantern.Json",
                    "package",
                    "13.0.1",
                    "Lantern.Json (v13.0.1)",
                ],
                [
                    "src/Harbor.Web/Harbor.Web.csproj",
                    "-",
                    "Lantern.Json",
                    "reference",
                    "13.0.0.0",
                    "..\\..\\packages\\Lantern.Json.13.0.1\\lib\\net45\\Lantern.Json.dll",
                ],
                [
                    "src/Harbor.Web/Harbor.Web.csproj",
                    "-",
                    "Lantern.Json",
                    "package",
                    "13.0.1",
                    "Lantern.Json (v13.0.1)",
                ],
            ],
            self.lines(rows, "WHERE"),
        )
        self.assertEqual([["3"]], self.lines(rows, "SUMMARY"))
        _, rows = self.run_main("where", "System.Web", "--root", str(root))
        self.assertEqual(
            [["src/Harbor.Web/Harbor.Web.csproj", "-", "System.Web", "framework", "-", "-"]], self.lines(rows, "WHERE")
        )
        _, rows = self.run_main("where", "Harbor.Core", "--root", str(root))
        self.assertEqual(
            [
                [
                    "src/Harbor.Tests/Harbor.Tests.csproj",
                    "-",
                    "Harbor.Core",
                    "project",
                    "-",
                    "src/Harbor.Core/Harbor.Core.csproj",
                ],
                [
                    "src/Harbor.Web/Harbor.Web.csproj",
                    "-",
                    "Harbor.Core",
                    "project",
                    "-",
                    "src/Harbor.Core/Harbor.Core.csproj",
                ],
            ],
            self.lines(rows, "WHERE"),
        )

    def test_conflicts_name_each_side_of_a_version_split_and_the_redirects_to_check(self) -> None:
        root = self.mixed_solution()
        code, rows = self.run_main("conflicts", "--root", str(root))
        self.assertEqual(1, code)
        self.assertEqual(
            [
                [
                    "Lantern.Json",
                    "package",
                    "12.0.3",
                    "src/Harbor.Core/Harbor.Core.csproj",
                    "net8.0",
                    "Quill.Http (v1.2.0)",
                ],
                [
                    "Lantern.Json",
                    "package",
                    "12.0.3",
                    "src/Harbor.Tests/Harbor.Tests.csproj",
                    "-",
                    "Harbor.Core (project)",
                ],
                ["Lantern.Json", "package", "12.0.3", "src/Harbor.Web/Harbor.Web.csproj", "-", "Harbor.Core (project)"],
                [
                    "Lantern.Json",
                    "package",
                    "13.0.1",
                    "src/Harbor.Tests/Harbor.Tests.csproj",
                    "-",
                    "Lantern.Json (v13.0.1)",
                ],
                [
                    "Lantern.Json",
                    "package",
                    "13.0.1",
                    "src/Harbor.Web/Harbor.Web.csproj",
                    "-",
                    "Lantern.Json (v13.0.1)",
                ],
            ],
            self.lines(rows, "CONFLICT"),
        )
        self.assertEqual(
            [["src/Harbor.Web/Harbor.Web.csproj", "src/Harbor.Web/Web.config", "Quill.Http", "2.0.0.0", "1.2.0.0"]],
            self.lines(rows, "REDIRECT_STALE"),
        )
        self.assertEqual(
            [["src/Harbor.Web/Harbor.Web.csproj", "src/Harbor.Web/Web.config", "Ghost.Assembly", "1.0.0.0"]],
            self.lines(rows, "REDIRECT_UNREFERENCED"),
        )
        self.assertEqual([["1", "1"]], self.lines(rows, "SUMMARY"))

    def test_project_narrows_every_query_and_an_unknown_project_fails(self) -> None:
        root = self.mixed_solution()
        core = str(self.root / "src" / "Harbor.Core" / "Harbor.Core.csproj")
        _, rows = self.run_main("why", "Lantern.Json", "--root", str(root), "--project", core)
        self.assertEqual(1, len(self.lines(rows, "PATH")))
        code, rows = self.run_main("conflicts", "--root", str(root), "--project", core)
        self.assertEqual((0, [["0", "0"]]), (code, self.lines(rows, "SUMMARY")))
        code, rows = self.run_main(
            "where", "Lantern.Json", "--root", str(root), "--project", str(self.root / "nowhere.csproj")
        )
        self.assertEqual(1, code)
        self.assertTrue(rows[-1][0].startswith("FAILED "), rows[-1])
        self.assertIn("is not a project of", rows[-1][0])

    def test_unreadable_files_cost_their_own_facts_and_exit_1(self) -> None:
        root = self.mixed_solution()
        self.write("src/Harbor.Tests/Harbor.Tests.csproj", "<Project Sdk='Microsoft.NET.Sdk'><ItemGroup></Project>")
        self.write("src/Harbor.Web/packages.config", "<!DOCTYPE packages><packages />")
        code, rows = self.run_main("why", "Lantern.Json", "--root", str(root))
        self.assertEqual(1, code)
        unreadable = self.lines(rows, "UNREADABLE")
        self.assertEqual(
            ["src/Harbor.Tests/Harbor.Tests.csproj", "src/Harbor.Web/packages.config"],
            sorted(row[0] for row in unreadable),
        )
        self.assertTrue(any("DOCTYPE" in row[1] for row in unreadable))
        self.assertTrue(any("not well-formed" in row[1] for row in unreadable))
        self.assertIn(
            ["src/Harbor.Tests/Harbor.Tests.csproj", "legacy", "direct", "listed"], self.lines(rows, "PROJECT")
        )
        # The web project keeps its assembly references though its packages.config was refused.
        self.assertIn(
            ["src/Harbor.Web/Harbor.Web.csproj", "-", "Lantern.Json", "graph", "Lantern.Json (v13.0.0.0, assembly)"],
            self.lines(rows, "PATH"),
        )
        self.assertEqual("SUMMARY", rows[-1][0])

    def test_a_solution_entry_that_is_missing_and_a_referenced_project_outside_the_list_are_reported(self) -> None:
        root = self.mixed_solution()
        self.write("Harbor.sln", solution("src\\Harbor.Web\\Harbor.Web.csproj", "src\\Gone\\Gone.csproj"))
        _, rows = self.run_main("why", "Quill.Http", "--root", str(root))
        self.assertEqual(
            [["src/Gone/Gone.csproj", "listed by the solution but not found"]], self.lines(rows, "UNREADABLE")
        )
        self.assertEqual(
            [
                ["src/Harbor.Core/Harbor.Core.csproj", "sdk", "assets", "referenced"],
                ["src/Harbor.Web/Harbor.Web.csproj", "legacy", "nuspec", "listed"],
            ],
            self.lines(rows, "PROJECT"),
        )

    def test_root_takes_a_slnx_a_directory_with_one_solution_or_a_directory_to_search(self) -> None:
        root = self.mixed_solution()
        root.unlink()
        self.write("Harbor.slnx", '<Solution><Project Path="src/Harbor.Web/Harbor.Web.csproj" /></Solution>')
        _, rows = self.run_main("where", "Lantern.Json", "--root", str(self.root))
        self.assertEqual([(self.root / "Harbor.slnx").as_posix()], self.lines(rows, "ROOT")[0])
        self.assertEqual(2, len(self.lines(rows, "PROJECT")))
        self.write("Other.sln", solution())
        code, rows = self.run_main("where", "Lantern.Json", "--root", str(self.root))
        self.assertEqual(1, code)
        self.assertIn("more than one solution (Harbor.slnx, Other.sln)", rows[-1][0])
        (self.root / "Harbor.slnx").unlink()
        (self.root / "Other.sln").unlink()
        _, rows = self.run_main("where", "Lantern.Json", "--root", str(self.root))
        self.assertEqual([self.root.as_posix()], self.lines(rows, "ROOT")[0])
        self.assertEqual(3, len(self.lines(rows, "PROJECT")))
        code, rows = self.run_main("where", "Lantern.Json", "--root", str(self.root / "packages"))
        self.assertEqual(1, code)
        self.assertIn("no solution or .csproj found", rows[-1][0])
        _, rows = self.run_main("where", "Lantern.Json", "--root", str(self.root / "Directory.Packages.props"))
        self.assertIn("is not a .sln or .slnx solution", rows[-1][0])

    def test_graph_writes_a_mermaid_or_dot_file_and_refuses_a_path_in_a_skills_directory(self) -> None:
        root = self.mixed_solution()
        code, rows = self.run_main("graph", "--root", str(root))
        self.assertEqual(0, code)
        (wrote,) = self.lines(rows, "WROTE")
        written = Path(wrote[0])
        self.addCleanup(remove_file_and_directory, written)
        self.assertTrue(written.name == "graph.mmd" and written.parent.name.startswith("dotnet-dependencies-"))
        text = written.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("graph LR\n"))
        self.assertIn('["src/Harbor.Web/Harbor.Web.csproj"]:::project', text)
        self.assertIn('["Lantern.Json 13.0.1"]:::package', text)
        self.assertIn('["System.Web"]:::framework', text)
        self.assertIn(" --> ", text)
        self.assertEqual([wrote[1:]], self.lines(rows, "SUMMARY"))
        target = self.root / "out" / "graph.dot"
        code, rows = self.run_main("graph", "--format", "dot", "--output", str(target), "--root", str(root))
        self.assertEqual(0, code)
        dot = target.read_text(encoding="utf-8")
        self.assertTrue(dot.startswith("digraph dependencies {\n"))
        self.assertIn('[label="Lantern.Json 12.0.3" shape=ellipse]', dot)
        self.assertIn(" -> ", dot)
        inside = SCRIPT.parent / "graph.mmd"
        code, rows = self.run_main("graph", "--output", str(inside), "--root", str(root))
        self.assertEqual(1, code)
        self.assertIn("inside the skills directory", rows[-1][0])
        self.assertFalse(inside.exists())

    def test_more_chains_than_the_limit_are_truncated(self) -> None:
        root = self.mixed_solution()
        original = dependency_graph.PATH_LIMIT
        dependency_graph.PATH_LIMIT = 1
        self.addCleanup(setattr, dependency_graph, "PATH_LIMIT", original)
        _, rows = self.run_main("why", "Lantern.Json", "--root", str(root))
        self.assertEqual(
            [["src/Harbor.Web/Harbor.Web.csproj", "-", "Lantern.Json", "1"]],
            [row for row in self.lines(rows, "TRUNCATED") if "Web" in row[0]],
        )
        self.assertEqual(1, len([row for row in self.lines(rows, "PATH") if "Web" in row[0]]))

    def test_with_the_sdk_answering_every_chain_within_the_limit_is_printed(self) -> None:
        # The walk's package chain is the one the SDK printed, so it is printed as `sdk` and the assembly chain past
        # the limit is the one cut.
        root = self.mixed_solution()
        reference = '<Reference Include="Lantern.Json"><HintPath>lib\\Lantern.Json.dll</HintPath></Reference>'
        self.write(
            "src/Harbor.Core/Harbor.Core.csproj", CORE_PROJECT.replace("</ItemGroup>", f"{reference}</ItemGroup>")
        )
        self.write("src/Harbor.Core/obj/project.assets.json", json.dumps(CORE_ASSETS))
        original = dependency_graph.PATH_LIMIT
        dependency_graph.PATH_LIMIT = 1
        self.addCleanup(setattr, dependency_graph, "PATH_LIMIT", original)
        _, rows = self.run_main("why", "Lantern.Json", "--root", str(root), services=self.with_dotnet)
        core = "src/Harbor.Core/Harbor.Core.csproj"
        self.assertEqual(
            [[core, "net8.0", "Lantern.Json", "sdk", "Quill.Http (v1.2.0) > Lantern.Json (v12.0.3)"]],
            [row for row in self.lines(rows, "PATH") if row[0] == core],
        )
        self.assertEqual(
            [[core, "net8.0", "Lantern.Json", "1"]], [row for row in self.lines(rows, "TRUNCATED") if row[0] == core]
        )
        dependency_graph.PATH_LIMIT = 2
        _, rows = self.run_main("why", "Lantern.Json", "--root", str(root), services=self.with_dotnet)
        self.assertEqual(
            [
                [core, "net8.0", "Lantern.Json", "sdk", "Quill.Http (v1.2.0) > Lantern.Json (v12.0.3)"],
                [core, "net8.0", "Lantern.Json", "graph", "Lantern.Json (assembly)"],
            ],
            [row for row in self.lines(rows, "PATH") if row[0] == core],
        )
        self.assertEqual([], [row for row in self.lines(rows, "TRUNCATED") if row[0] == core])


API_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFrameworks>net8.0;net9.0</TargetFrameworks></PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Lantern.Bundle" Version="1.0.0" />
    <Reference Include="Beacon.Legacy"><HintPath>lib\\Beacon.Legacy.dll</HintPath></Reference>
  </ItemGroup>
</Project>
"""
# A package that ships an assembly named unlike itself, and a framework that reaches a package the other does not.
API_ASSETS = {
    "version": 3,
    "targets": {
        "net8.0": {
            "Lantern.Bundle/1.0.0": {
                "type": "package",
                "dependencies": {"Lantern.Json": "12.0.3"},
                "compile": {"lib/net8.0/Beacon.Shared.dll": {}},
            },
            "Lantern.Json/12.0.3": {"type": "package", "compile": {"lib/net8.0/Lantern.Json.dll": {}}},
        },
        "net9.0": {
            "Lantern.Bundle/1.0.0": {
                "type": "package",
                "dependencies": {"Lantern.Json": "13.0.1", "Only.Second": "2.0.0"},
                "compile": {"lib/net9.0/Beacon.Shared.dll": {}},
            },
            "Lantern.Json/13.0.1": {"type": "package", "compile": {"lib/net9.0/Lantern.Json.dll": {}}},
            "Only.Second/2.0.0": {"type": "package"},
        },
    },
    "project": {
        "frameworks": {
            framework: {"dependencies": {"Lantern.Bundle": {"target": "Package", "version": "[1.0.0, )"}}}
            for framework in ("net8.0", "net9.0")
        }
    },
}
BUNDLE_SDK_TEXT = (
    "Project 'Harbor.Api' has the following dependency graph(s) for 'Lantern.Bundle':\r\n"
    "\r\n"
    "  [net8.0]\r\n"
    "  └── Lantern.Bundle (v1.0.0)\r\n"
    "\r\n"
    "  [net9.0]\r\n"
    "  └── Lantern.Bundle (v1.0.0)\r\n"
    "\r\n"
)
# A conditional reference that only one framework of the restore holds; the other framework restored no package.
TUPLE_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFrameworks>net8.0;net472</TargetFrameworks></PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Lantern.Tuple" Version="4.5.0" Condition="'$(TargetFramework)'=='net472'" />
  </ItemGroup>
</Project>
"""
TUPLE_ASSETS = {
    "version": 3,
    "targets": {
        "net8.0": {},
        "net472": {"Lantern.Tuple/4.5.0": {"type": "package", "compile": {"lib/net47/Lantern.Tuple.dll": {}}}},
    },
    "project": {
        "frameworks": {
            "net8.0": {},
            "net472": {"dependencies": {"Lantern.Tuple": {"target": "Package", "version": "[4.5.0, )"}}},
        }
    },
}
# A restored project that reaches a packages.config project and an SDK-style project through ProjectReference.
APP_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net472</TargetFramework></PropertyGroup>
  <ItemGroup>
    <ProjectReference Include="..\\Harbor.Legacy\\Harbor.Legacy.csproj" />
    <ProjectReference Include="..\\Harbor.Shared\\Harbor.Shared.csproj" />
  </ItemGroup>
</Project>
"""
APP_ASSETS = {
    "version": 3,
    "targets": {
        "net472": {
            "Lantern.Json/12.0.3": {"type": "package", "compile": {"lib/net45/Lantern.Json.dll": {}}},
            "Harbor.Legacy/1.0.0": {"type": "project"},
            "Harbor.Shared/1.0.0": {"type": "project", "dependencies": {"Lantern.Json": "12.0.3"}},
        }
    },
    "project": {"frameworks": {"net472": {}}},
}
LEGACY_PROJECT = """<?xml version="1.0" encoding="utf-8"?>
<Project ToolsVersion="15.0" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <PropertyGroup><TargetFrameworkVersion>v4.7.2</TargetFrameworkVersion></PropertyGroup>
</Project>
"""
SHARED_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net472</TargetFramework></PropertyGroup>
  <ItemGroup><PackageReference Include="Lantern.Json" Version="12.0.3" /></ItemGroup>
</Project>
"""
# The SDK prints a project reference at the project's own version.
APP_SDK_TEXT = (
    "Project 'Harbor.App' has the following dependency graph(s) for 'Lantern.Json':\r\n\r\n  [net472]\r\n"
    "  └── Harbor.Shared (v1.0.0)\r\n      └── Lantern.Json (v12.0.3)\r\n\r\n"
)
# A host that pins a package above the version the project it references asks for, each restored on its own.
HOST_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>
  <ItemGroup>
    <ProjectReference Include="..\\Harbor.Data\\Harbor.Data.csproj" />
    <PackageReference Include="Lantern.Json" Version="13.0.1" />
  </ItemGroup>
</Project>
"""
HOST_ASSETS = {
    "version": 3,
    "targets": {
        "net8.0": {
            "Lantern.Json/13.0.1": {"type": "package"},
            "Harbor.Data/1.0.0": {"type": "project", "dependencies": {"Lantern.Json": "12.0.3"}},
        }
    },
    "project": {
        "frameworks": {"net8.0": {"dependencies": {"Lantern.Json": {"target": "Package", "version": "[13.0.1, )"}}}}
    },
}
DATA_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>
  <ItemGroup><PackageReference Include="Lantern.Json" Version="12.0.3" /></ItemGroup>
</Project>
"""
DATA_ASSETS = {
    "version": 3,
    "targets": {"net8.0": {"Lantern.Json/12.0.3": {"type": "package"}}},
    "project": {
        "frameworks": {"net8.0": {"dependencies": {"Lantern.Json": {"target": "Package", "version": "[12.0.3, )"}}}}
    },
}
HOST_SDK_TEXT = (
    "Project 'Harbor.Host' has the following dependency graph(s) for 'Lantern.Json':\r\n\r\n  [net8.0]\r\n"
    "  ├── Harbor.Data (v1.0.0)\r\n  │   └── Lantern.Json (v13.0.1)\r\n  └── Lantern.Json (v13.0.1)\r\n\r\n"
)
DATA_SDK_TEXT = (
    "Project 'Harbor.Data' has the following dependency graph(s) for 'Lantern.Json':\r\n\r\n  [net8.0]\r\n"
    "  └── Lantern.Json (v12.0.3)\r\n\r\n"
)


class TableSdk:
    """A `dotnet nuget why` that answers from a table keyed by the project path and the package, case-folded, and
    says every other package is not a dependency."""

    def __init__(self, answers: dict[tuple[str, str], str]) -> None:
        self.answers = answers
        self.commands: list[list[str]] = []

    def __call__(self, arguments: Sequence[str], timeout: float) -> Completed:
        self.commands.append(list(arguments))
        project, package = Path(arguments[-2]), arguments[-1]
        text = self.answers.get((same_path(project), package.casefold()))
        return Completed(0, text or f"Project '{project.stem}' does not have a dependency on '{package}'.\r\n")


class FrameworkAndAssemblyTests(Fixture):
    def api_solution(self) -> Path:
        project = self.write("src/Harbor.Api/Harbor.Api.csproj", API_PROJECT)
        self.write("src/Harbor.Api/obj/project.assets.json", json.dumps(API_ASSETS))
        self.write("Harbor.sln", solution("src\\Harbor.Api\\Harbor.Api.csproj"))
        return project

    def sdk_services(self, answers: dict[tuple[str, str], str]) -> tuple[TableSdk, Services]:
        sdk = TableSdk(answers)
        return sdk, Services(run=sdk, which=self.with_dotnet.which)

    def test_an_assembly_named_unlike_its_package_or_named_by_a_reference_is_found_with_or_without_dotnet(
        self,
    ) -> None:
        project = self.api_solution()
        sdk, with_sdk = self.sdk_services({})
        expected = [
            [
                "src/Harbor.Api/Harbor.Api.csproj",
                "net8.0;net9.0",
                "Beacon.Shared",
                "graph",
                "Lantern.Bundle (v1.0.0) > Beacon.Shared (assembly)",
            ],
            ["src/Harbor.Api/Harbor.Api.csproj", "net8.0;net9.0", "Beacon.Legacy", "graph", "Beacon.Legacy (assembly)"],
        ]
        for services in (self.without_dotnet, with_sdk):
            with self.subTest(dotnet=services is with_sdk):
                code, rows = self.run_main(
                    "why", "Beacon.Shared", "Beacon.Legacy", "--root", str(self.root), services=services
                )
                self.assertEqual(0, code)
                self.assertEqual(expected, self.lines(rows, "PATH"))
                self.assertEqual([["2", "2", "0"]], self.lines(rows, "SUMMARY"))
        # The SDK was asked for both names and found no package by either, so the graph walk answered them.
        self.assertEqual(
            [[str(project), "Beacon.Shared"], [str(project), "Beacon.Legacy"]], [c[3:] for c in sdk.commands]
        )
        _, rows = self.run_main("where", "beacon.shared", "--root", str(self.root))
        self.assertEqual(
            [
                [
                    "src/Harbor.Api/Harbor.Api.csproj",
                    "net8.0;net9.0",
                    "Beacon.Shared",
                    "package",
                    "1.0.0",
                    "Lantern.Bundle (v1.0.0)",
                ]
            ],
            self.lines(rows, "WHERE"),
        )

    def test_dotnet_changes_only_the_source_of_a_package_chain(self) -> None:
        project = self.api_solution()
        _, with_sdk = self.sdk_services({(same_path(project), "lantern.bundle"): BUNDLE_SDK_TEXT})
        answers: dict[str, list[list[str]]] = {}
        for source, services in (("sdk", with_sdk), ("graph", self.without_dotnet)):
            _, rows = self.run_main("why", "Lantern.Bundle", "--root", str(self.root), services=services)
            answers[source] = self.lines(rows, "PATH")
        self.assertEqual(
            [["src/Harbor.Api/Harbor.Api.csproj", "net8.0;net9.0", "Lantern.Bundle", "sdk", "Lantern.Bundle (v1.0.0)"]],
            answers["sdk"],
        )
        self.assertEqual([[*row[:3], "graph", row[4]] for row in answers["sdk"]], answers["graph"])

    def test_a_framework_whose_restore_holds_no_package_is_labelled_with_none(self) -> None:
        self.write("src/Harbor.Tuple/Harbor.Tuple.csproj", TUPLE_PROJECT)
        self.write("src/Harbor.Tuple/obj/project.assets.json", json.dumps(TUPLE_ASSETS))
        self.write("Harbor.sln", solution("src\\Harbor.Tuple\\Harbor.Tuple.csproj"))
        label = "src/Harbor.Tuple/Harbor.Tuple.csproj"
        why = [[label, "net472", "Lantern.Tuple", "graph", "Lantern.Tuple (v4.5.0)"]]
        where = [[label, "net472", "Lantern.Tuple", "package", "4.5.0", "Lantern.Tuple (v4.5.0)"]]
        _, rows = self.run_main("why", "Lantern.Tuple", "--root", str(self.root))
        self.assertEqual(why, self.lines(rows, "PATH"))
        _, rows = self.run_main("where", "Lantern.Tuple", "--root", str(self.root))
        self.assertEqual(where, self.lines(rows, "WHERE"))
        # A restore that records no project section leaves each framework the references whose conditions hold for
        # it; one whose condition the reader cannot evaluate is reported for each framework, never guessed.
        debug = "'$(Configuration)'=='Debug'"
        self.write(
            "src/Harbor.Tuple/Harbor.Tuple.csproj",
            TUPLE_PROJECT.replace(
                "</ItemGroup>",
                f'  <PackageReference Include="Beacon.Debug" Version="1.0.0" Condition="{debug}" />\n  </ItemGroup>',
            ),
        )
        self.write("src/Harbor.Tuple/obj/project.assets.json", json.dumps({**TUPLE_ASSETS, "project": {}}))
        code, rows = self.run_main("why", "Lantern.Tuple", "--root", str(self.root))
        self.assertEqual(why, self.lines(rows, "PATH"))
        self.assertEqual(
            [
                [
                    label,
                    f"the condition {debug} on PackageReference Beacon.Debug cannot be evaluated for {framework}, "
                    "so it is left out",
                ]
                for framework in ("net8.0", "net472")
            ],
            self.lines(rows, "UNREADABLE"),
        )
        self.assertEqual(1, code)

    def test_a_package_chain_the_sdk_cannot_see_is_kept_and_dotnet_changes_only_the_source(self) -> None:
        app = self.write("src/Harbor.App/Harbor.App.csproj", APP_PROJECT)
        self.write("src/Harbor.App/obj/project.assets.json", json.dumps(APP_ASSETS))
        self.write("src/Harbor.Legacy/Harbor.Legacy.csproj", LEGACY_PROJECT)
        self.write(
            "src/Harbor.Legacy/packages.config",
            '<packages><package id="Legacy.Pkg" version="1.0.0" targetFramework="net472" /></packages>',
        )
        self.write(
            "packages/Legacy.Pkg.1.0.0/Legacy.Pkg.nuspec",
            "<package><metadata><id>Legacy.Pkg</id><version>1.0.0</version></metadata></package>",
        )
        self.write("src/Harbor.Shared/Harbor.Shared.csproj", SHARED_PROJECT)
        self.write("Harbor.sln", solution("src\\Harbor.App\\Harbor.App.csproj"))
        # packages.config dependencies do not flow into a PackageReference restore, so the SDK finds no Legacy.Pkg;
        # it finds Lantern.Json through Harbor.Shared, printing the project at its own version.
        sdk, with_sdk = self.sdk_services({(same_path(app), "lantern.json"): APP_SDK_TEXT})
        expected = {
            "Legacy.Pkg": [
                [
                    "src/Harbor.App/Harbor.App.csproj",
                    "net472",
                    "graph",
                    "Harbor.Legacy (project) > Legacy.Pkg (v1.0.0)",
                ],
                ["src/Harbor.Legacy/Harbor.Legacy.csproj", "-", "graph", "Legacy.Pkg (v1.0.0)"],
            ],
            "Lantern.Json": [
                [
                    "src/Harbor.App/Harbor.App.csproj",
                    "net472",
                    "sdk",
                    "Harbor.Shared (project) > Lantern.Json (v12.0.3)",
                ],
                ["src/Harbor.Shared/Harbor.Shared.csproj", "-", "graph", "Lantern.Json (v12.0.3)"],
            ],
        }
        for name, lines in expected.items():
            with self.subTest(name=name):
                _, rows = self.run_main("why", name, "--root", str(self.root), services=with_sdk)
                self.assertEqual([[*line[:2], name, *line[2:]] for line in lines], self.lines(rows, "PATH"))
                self.assertEqual([["2", "1", "0"]], self.lines(rows, "SUMMARY"))
                _, rows = self.run_main("why", name, "--root", str(self.root))
                self.assertEqual([[*line[:2], name, "graph", line[3]] for line in lines], self.lines(rows, "PATH"))
                self.assertEqual([["2", "1", "0"]], self.lines(rows, "SUMMARY"))
        self.assertEqual({str(app)}, {command[3] for command in sdk.commands})

    def test_every_framework_of_a_restore_answers_and_labels_its_lines(self) -> None:
        self.api_solution()
        _, rows = self.run_main("why", "Only.Second", "Lantern.Json", "--root", str(self.root))
        self.assertEqual(
            [
                [
                    "src/Harbor.Api/Harbor.Api.csproj",
                    "net9.0",
                    "Only.Second",
                    "graph",
                    "Lantern.Bundle (v1.0.0) > Only.Second (v2.0.0)",
                ],
                [
                    "src/Harbor.Api/Harbor.Api.csproj",
                    "net8.0",
                    "Lantern.Json",
                    "graph",
                    "Lantern.Bundle (v1.0.0) > Lantern.Json (v12.0.3)",
                ],
                [
                    "src/Harbor.Api/Harbor.Api.csproj",
                    "net9.0",
                    "Lantern.Json",
                    "graph",
                    "Lantern.Bundle (v1.0.0) > Lantern.Json (v13.0.1)",
                ],
            ],
            self.lines(rows, "PATH"),
        )
        _, rows = self.run_main("where", "Only.Second", "--root", str(self.root))
        self.assertEqual(
            [
                [
                    "src/Harbor.Api/Harbor.Api.csproj",
                    "net9.0",
                    "Only.Second",
                    "package",
                    "2.0.0",
                    "Lantern.Bundle (v1.0.0)",
                ]
            ],
            self.lines(rows, "WHERE"),
        )
        project = str(self.root / "src" / "Harbor.Api" / "Harbor.Api.csproj")
        _, rows = self.run_main("why", "--all", "--project", project, "--root", str(self.root))
        reaches = self.lines(rows, "REACHES")
        self.assertIn(
            [
                "src/Harbor.Api/Harbor.Api.csproj",
                "net9.0",
                "Only.Second",
                "2.0.0",
                "package",
                "Lantern.Bundle (v1.0.0)",
            ],
            reaches,
        )
        self.assertIn(
            [
                "src/Harbor.Api/Harbor.Api.csproj",
                "net8.0;net9.0",
                "Lantern.Bundle",
                "1.0.0",
                "package",
                "Lantern.Bundle (v1.0.0)",
            ],
            reaches,
        )

    def test_two_frameworks_of_one_project_at_different_versions_are_not_a_conflict(self) -> None:
        self.api_solution()
        code, rows = self.run_main("conflicts", "--root", str(self.root))
        self.assertEqual((0, [], [["0", "0"]]), (code, self.lines(rows, "CONFLICT"), self.lines(rows, "SUMMARY")))
        # Another project that wants a third version is at odds with each framework.
        self.write(
            "src/Harbor.Cli/Harbor.Cli.csproj",
            '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>'
            '<PackageReference Include="Lantern.Json" Version="11.0.0" /></ItemGroup></Project>',
        )
        self.write("Harbor.sln", solution("src\\Harbor.Api\\Harbor.Api.csproj", "src\\Harbor.Cli\\Harbor.Cli.csproj"))
        code, rows = self.run_main("conflicts", "--root", str(self.root))
        self.assertEqual(
            [
                [
                    "Lantern.Json",
                    "package",
                    "11.0.0",
                    "src/Harbor.Cli/Harbor.Cli.csproj",
                    "-",
                    "Lantern.Json (v11.0.0)",
                ],
                [
                    "Lantern.Json",
                    "package",
                    "12.0.3",
                    "src/Harbor.Api/Harbor.Api.csproj",
                    "net8.0",
                    "Lantern.Bundle (v1.0.0)",
                ],
                [
                    "Lantern.Json",
                    "package",
                    "13.0.1",
                    "src/Harbor.Api/Harbor.Api.csproj",
                    "net9.0",
                    "Lantern.Bundle (v1.0.0)",
                ],
            ],
            self.lines(rows, "CONFLICT"),
        )
        self.assertEqual(1, code)

    def test_a_project_naming_one_platform_with_and_without_a_version_answers_each_framework_apart(self) -> None:
        # What the project declares, and what its restore and the SDK write for each, in order.
        cases = [
            (("net8.0-windows", "net8.0-windows10.0.19041.0"), ("net8.0-windows7.0", "net8.0-windows10.0.19041")),
            (("net8.0-android", "net8.0-android34.0"), ("net8.0-android35.0", "net8.0-android34.0")),
        ]
        label = "src/Harbor.Desk/Harbor.Desk.csproj"
        for declared, written in cases:
            with self.subTest(declared=declared):
                project = self.write(
                    "src/Harbor.Desk/Harbor.Desk.csproj",
                    f'<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFrameworks>{";".join(declared)}'
                    "</TargetFrameworks></PropertyGroup></Project>",
                )
                packages = (("Lantern.Wpf", "12.0.3"), ("Lantern.WinRT", "13.0.1"))
                targets = {
                    name: {
                        f"{package}/1.0.0": {"type": "package", "dependencies": {"Lantern.Json": json_version}},
                        f"Lantern.Json/{json_version}": {"type": "package"},
                    }
                    for name, (package, json_version) in zip(written, packages, strict=True)
                }
                sections = {
                    name: {"dependencies": {package: {"target": "Package", "version": "[1.0.0, )"}}}
                    for name, (package, _) in zip(declared, packages, strict=True)
                }
                assets = {"version": 3, "targets": targets, "project": {"frameworks": sections}}
                self.write("src/Harbor.Desk/obj/project.assets.json", json.dumps(assets))
                self.write("Harbor.sln", solution("src\\Harbor.Desk\\Harbor.Desk.csproj"))
                why = [
                    [label, declared[0], "Lantern.Wpf", "graph", "Lantern.Wpf (v1.0.0)"],
                    [label, declared[1], "Lantern.WinRT", "graph", "Lantern.WinRT (v1.0.0)"],
                ]
                code, rows = self.run_main("why", "Lantern.Wpf", "Lantern.WinRT", "--root", str(self.root))
                self.assertEqual((0, why), (code, self.lines(rows, "PATH")))
                # Each framework resolves the shared package at its own version, which is no conflict.
                code, rows = self.run_main("conflicts", "--root", str(self.root))
                self.assertEqual(
                    (0, [], [["0", "0"]]), (code, self.lines(rows, "CONFLICT"), self.lines(rows, "SUMMARY"))
                )
                # The SDK names the second framework as the restore writes it, and answers the project's name for it.
                sdk_text = (
                    "Project 'Harbor.Desk' has the following dependency graph(s) for 'Lantern.WinRT':\r\n\r\n"
                    f"  [{written[1]}]\r\n  └── Lantern.WinRT (v1.0.0)\r\n\r\n"
                )
                _, with_sdk = self.sdk_services({(same_path(project), "lantern.winrt"): sdk_text})
                _, rows = self.run_main("why", "Lantern.WinRT", "--root", str(self.root), services=with_sdk)
                self.assertEqual(
                    [[label, declared[1], "Lantern.WinRT", "sdk", "Lantern.WinRT (v1.0.0)"]], self.lines(rows, "PATH")
                )

    def test_two_restored_projects_with_one_file_name_keep_their_own_sdk_answers(self) -> None:
        shop = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>
  <ItemGroup><PackageReference Include="{package}" Version="1.0.0" /></ItemGroup>
</Project>
"""
        projects = []
        for folder, package in (("src/Api", "Lantern.Bundle"), ("tools/Api", "Quill.Http")):
            projects.append(self.write(f"{folder}/Shop.csproj", shop.format(package=package)))
            # Each restore holds the chain its SDK answer prints, so the walk finds it too and it is printed once.
            assets = {
                "version": 3,
                "targets": {
                    "net8.0": {
                        f"{package}/1.0.0": {"type": "package", "dependencies": {"Lantern.Json": "12.0.3"}},
                        "Lantern.Json/12.0.3": {"type": "package"},
                    }
                },
                "project": {
                    "frameworks": {"net8.0": {"dependencies": {package: {"target": "Package", "version": "[1.0.0, )"}}}}
                },
            }
            self.write(f"{folder}/obj/project.assets.json", json.dumps(assets))
        self.write("Shop.sln", solution("src\\Api\\Shop.csproj", "tools\\Api\\Shop.csproj"))
        chain = (
            "Project 'Shop' has the following dependency graph(s) for 'Lantern.Json':\r\n\r\n  [net8.0]\r\n"
            "  └── {first} (v1.0.0)\r\n      └── Lantern.Json (v12.0.3)\r\n\r\n"
        )
        _, services = self.sdk_services(
            {
                (same_path(projects[0]), "lantern.json"): chain.format(first="Lantern.Bundle"),
                (same_path(projects[1]), "lantern.json"): chain.format(first="Quill.Http"),
            }
        )
        _, rows = self.run_main("why", "Lantern.Json", "--root", str(self.root), services=services)
        self.assertEqual(
            [
                [
                    "src/Api/Shop.csproj",
                    "net8.0",
                    "Lantern.Json",
                    "sdk",
                    "Lantern.Bundle (v1.0.0) > Lantern.Json (v12.0.3)",
                ],
                [
                    "tools/Api/Shop.csproj",
                    "net8.0",
                    "Lantern.Json",
                    "sdk",
                    "Quill.Http (v1.0.0) > Lantern.Json (v12.0.3)",
                ],
            ],
            self.lines(rows, "PATH"),
        )

    def host_and_data_solution(self) -> tuple[Path, Path]:
        host = self.write("src/Harbor.Host/Harbor.Host.csproj", HOST_PROJECT)
        self.write("src/Harbor.Host/obj/project.assets.json", json.dumps(HOST_ASSETS))
        data = self.write("src/Harbor.Data/Harbor.Data.csproj", DATA_PROJECT)
        self.write("src/Harbor.Data/obj/project.assets.json", json.dumps(DATA_ASSETS))
        self.write(
            "Harbor.sln", solution("src\\Harbor.Host\\Harbor.Host.csproj", "src\\Harbor.Data\\Harbor.Data.csproj")
        )
        return host, data

    def test_a_route_the_sdk_and_the_walk_both_find_is_printed_once_at_the_sdks_version(self) -> None:
        # The host pins Lantern.Json above the version its referenced project asks for. The SDK prints the route
        # through the project at the version the host resolves; the walk takes it from the project's own restore.
        host, data = self.host_and_data_solution()
        _, with_sdk = self.sdk_services(
            {
                (same_path(host), "lantern.json"): HOST_SDK_TEXT,
                (same_path(data), "lantern.json"): DATA_SDK_TEXT,
            }
        )
        host_label, data_label = "src/Harbor.Host/Harbor.Host.csproj", "src/Harbor.Data/Harbor.Data.csproj"
        expected = {
            "sdk": [
                [data_label, "net8.0", "Lantern.Json", "sdk", "Lantern.Json (v12.0.3)"],
                [
                    host_label,
                    "net8.0",
                    "Lantern.Json",
                    "sdk",
                    "Harbor.Data (project) > Lantern.Json (v13.0.1, graph v12.0.3)",
                ],
                [host_label, "net8.0", "Lantern.Json", "sdk", "Lantern.Json (v13.0.1)"],
            ],
            "graph": [
                [data_label, "net8.0", "Lantern.Json", "graph", "Lantern.Json (v12.0.3)"],
                [host_label, "net8.0", "Lantern.Json", "graph", "Harbor.Data (project) > Lantern.Json (v12.0.3)"],
                [host_label, "net8.0", "Lantern.Json", "graph", "Lantern.Json (v13.0.1)"],
            ],
        }
        for source, services in (("sdk", with_sdk), ("graph", self.without_dotnet)):
            with self.subTest(source=source):
                _, rows = self.run_main("why", "Lantern.Json", "--root", str(self.root), services=services)
                self.assertEqual(expected[source], self.lines(rows, "PATH"))
                self.assertEqual([["3", "1", "0"]], self.lines(rows, "SUMMARY"))
        # The versions the two restores resolve apart are a conflict, which `conflicts` reports.
        code, rows = self.run_main("conflicts", "--root", str(self.root))
        self.assertEqual(
            [
                ["Lantern.Json", "package", "12.0.3", data_label, "net8.0", "Lantern.Json (v12.0.3)"],
                ["Lantern.Json", "package", "12.0.3", host_label, "net8.0", "Harbor.Data (project)"],
                ["Lantern.Json", "package", "13.0.1", host_label, "net8.0", "Lantern.Json (v13.0.1)"],
            ],
            self.lines(rows, "CONFLICT"),
        )
        self.assertEqual(1, code)

    def test_a_route_the_walk_finds_at_two_other_versions_names_both_after_graph(self) -> None:
        # Two walked chains along the SDK's route reach the package at versions apart from the SDK's and each other.
        printed = dependency_graph.SdkPath("Harbor.Host", "net8.0", (("Harbor.Data", ""), ("Lantern.Json", "13.0.1")))
        walked: list[tuple[dependency_graph.Hop, ...]] = [
            (("project", "Harbor.Data", ""), ("package", "Lantern.Json", version))
            for version in ("12.0.3", "13.0.1", "11.0.0", "12.0.3")
        ]
        self.assertEqual(
            "Harbor.Data (project) > Lantern.Json (v13.0.1, graph v12.0.3;v11.0.0)",
            dependency_graph.merged_chain(printed, walked),
        )

    def test_a_restore_keyed_by_long_framework_names_keeps_its_recorded_direct_packages(self) -> None:
        # Older SDKs key a restore's targets by long names beside the project section's short ones.
        project = self.write("src/Harbor.Tuple/Harbor.Tuple.csproj", TUPLE_PROJECT)
        targets = {
            ".NETCoreApp,Version=v8.0": {},
            ".NETFramework,Version=v4.7.2": {"Lantern.Tuple/4.5.0": {"type": "package"}},
        }
        assets = {**TUPLE_ASSETS, "targets": targets}
        self.write("src/Harbor.Tuple/obj/project.assets.json", json.dumps(assets))
        self.write("Harbor.sln", solution("src\\Harbor.Tuple\\Harbor.Tuple.csproj"))
        label = "src/Harbor.Tuple/Harbor.Tuple.csproj"
        why = [[label, "net472", "Lantern.Tuple", "graph", "Lantern.Tuple (v4.5.0)"]]
        code, rows = self.run_main("why", "Lantern.Tuple", "--root", str(self.root))
        self.assertEqual((0, why, []), (code, self.lines(rows, "PATH"), self.lines(rows, "UNREADABLE")))
        # Without a project section, the project file's own names key the restore, so its conditions are evaluated,
        # in either form they read the property.
        starts = TUPLE_PROJECT.replace("'$(TargetFramework)'=='net472'", "$(TargetFramework.StartsWith('net4'))")
        for text in (TUPLE_PROJECT, starts):
            with self.subTest(condition=text):
                self.write("src/Harbor.Tuple/Harbor.Tuple.csproj", text)
                self.write("src/Harbor.Tuple/obj/project.assets.json", json.dumps({**assets, "project": {}}))
                code, rows = self.run_main("why", "Lantern.Tuple", "--root", str(self.root))
                self.assertEqual((0, why, []), (code, self.lines(rows, "PATH"), self.lines(rows, "UNREADABLE")))
        # An SDK that prints the long name answers the framework the project names.
        sdk_text = (
            "Project 'Harbor.Tuple' has the following dependency graph(s) for 'Lantern.Tuple':\r\n\r\n"
            "  [.NETFramework,Version=v4.7.2]\r\n  └── Lantern.Tuple (v4.5.0)\r\n\r\n"
        )
        _, with_sdk = self.sdk_services({(same_path(project), "lantern.tuple"): sdk_text})
        _, rows = self.run_main("why", "Lantern.Tuple", "--root", str(self.root), services=with_sdk)
        self.assertEqual([[*why[0][:3], "sdk", why[0][4]]], self.lines(rows, "PATH"))

    def test_two_referenced_projects_with_one_assembly_name_are_each_walked(self) -> None:
        shop = '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup><PackageReference Include="{0}" Version="1.0.0" />' + (
            "</ItemGroup></Project>"
        )
        self.write("src/Api/Shop.csproj", shop.format("Lantern.Bundle"))
        self.write("tools/Api/Shop.csproj", shop.format("Quill.Http"))
        host = self.write(
            "src/Host/Harbor.Host.csproj",
            '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>'
            '<ProjectReference Include="..\\Api\\Shop.csproj" />'
            '<ProjectReference Include="..\\..\\tools\\Api\\Shop.csproj" />'
            "</ItemGroup></Project>",
        )
        self.write("Harbor.sln", solution("src\\Host\\Harbor.Host.csproj"))
        _, rows = self.run_main("why", "--all", "--project", str(host), "--root", str(self.root))
        self.assertEqual(
            [("Lantern.Bundle", "1.0.0"), ("Quill.Http", "1.0.0")],
            [(row[2], row[3]) for row in self.lines(rows, "REACHES") if row[4] == "package"],
        )


class UnreadableInputTests(Fixture):
    def test_an_unreadable_central_versions_file_is_reported_once_and_each_project_keeps_its_own_facts(self) -> None:
        root = self.mixed_solution()
        self.write("Directory.Packages.props", "<!DOCTYPE x><Project />")
        code, rows = self.run_main("why", "Quill.Http", "--root", str(root))
        self.assertEqual(1, code)
        self.assertEqual(["Directory.Packages.props"], [row[0] for row in self.lines(rows, "UNREADABLE")])
        self.assertEqual(
            [
                ["src/Harbor.Core/Harbor.Core.csproj", "sdk", "assets", "listed"],
                ["src/Harbor.Tests/Harbor.Tests.csproj", "sdk", "direct", "listed"],
                ["src/Harbor.Web/Harbor.Web.csproj", "legacy", "nuspec", "listed"],
            ],
            self.lines(rows, "PROJECT"),
        )
        # The test project still reaches its project reference, and asks for its package at no known version.
        self.assertIn(
            [
                "src/Harbor.Tests/Harbor.Tests.csproj",
                "-",
                "Quill.Http",
                "graph",
                "Harbor.Core (project) > Quill.Http (v1.2.0)",
            ],
            self.lines(rows, "PATH"),
        )
        _, rows = self.run_main("why", "Lantern.Json", "--root", str(root))
        self.assertIn(
            ["src/Harbor.Tests/Harbor.Tests.csproj", "-", "Lantern.Json", "graph", "Lantern.Json (v?)"],
            self.lines(rows, "PATH"),
        )

    def test_a_referenced_project_whose_folder_is_missing_costs_only_its_own_facts(self) -> None:
        root = self.mixed_solution()
        self.write(
            "src/Harbor.Tests/Harbor.Tests.csproj",
            TESTS_PROJECT.replace("</ItemGroup>", '<ProjectReference Include="../Missing/Gone.csproj" /></ItemGroup>'),
        )
        code, rows = self.run_main("why", "Lantern.Json", "--root", str(root))
        self.assertEqual(1, code)
        self.assertEqual(["src/Missing/Gone.csproj"], [row[0] for row in self.lines(rows, "UNREADABLE")])
        self.assertIn(["src/Missing/Gone.csproj", "legacy", "direct", "referenced"], self.lines(rows, "PROJECT"))
        self.assertEqual(7, len(self.lines(rows, "PATH")))
        self.assertEqual([["7", "1", "0"]], self.lines(rows, "SUMMARY"))
        self.assertFalse(any(row[0].startswith("FAILED") for row in rows))

    def test_a_malformed_slnx_is_reported_unreadable_never_answered_as_an_empty_solution(self) -> None:
        self.mixed_solution().unlink()
        solution_file = self.write(
            "Harbor.slnx", '<Solution><Project Path="src/Harbor.Web/Harbor.Web.csproj"></Solution>'
        )
        code, rows = self.run_main("conflicts", "--root", str(solution_file))
        self.assertEqual(1, code)
        ((file, reason),) = self.lines(rows, "UNREADABLE")
        self.assertEqual("Harbor.slnx", file)
        self.assertIn("not well-formed", reason)
        self.assertEqual([], self.lines(rows, "PROJECT"))
        self.write("Harbor.slnx", "<Solution />")
        code, rows = self.run_main("conflicts", "--root", str(solution_file))
        self.assertEqual((0, []), (code, self.lines(rows, "UNREADABLE")))


# A restored project of two frameworks whose assembly and project references hold for one; the referenced project
# has no restore.
FRAMEWORK_ITEMS_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFrameworks>net8.0;net472</TargetFrameworks></PropertyGroup>
  <ItemGroup><PackageReference Include="Quill.Http" Version="1.2.0" /></ItemGroup>
  <ItemGroup Condition="'$(TargetFramework)' == 'net472'">
    <Reference Include="System.Web" />
    <ProjectReference Include="..\\Harbor.Legacy\\Harbor.Legacy.csproj" />
  </ItemGroup>
</Project>
"""
FRAMEWORK_ITEMS_ASSETS = {
    "version": 3,
    "targets": {framework: {"Quill.Http/1.2.0": {"type": "package"}} for framework in ("net8.0", "net472")},
    "project": {
        "frameworks": {
            framework: {"dependencies": {"Quill.Http": {"target": "Package", "version": "[1.2.0, )"}}}
            for framework in ("net8.0", "net472")
        }
    },
}
LEGACY_PACKAGE_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net472</TargetFramework></PropertyGroup>
  <ItemGroup><PackageReference Include="Legacy.Pkg" Version="1.0.0" /></ItemGroup>
</Project>
"""
FRAMEWORK_CENTRAL = """<Project>
  <ItemGroup Condition="'$(TargetFramework)' == 'net8.0'">
    <PackageVersion Include="Lantern.Json" Version="13.0.1" />
  </ItemGroup>
  <ItemGroup Condition="'$(TargetFramework)' != 'net8.0'">
    <PackageVersion Include="Lantern.Json" Version="12.0.3" />
  </ItemGroup>
  <ItemGroup Condition="'$(Configuration)'=='Debug'">
    <PackageVersion Update="Lantern.Json" Version="14.0.0" />
  </ItemGroup>
</Project>
"""
# An Update and a Remove under a condition the reader cannot evaluate, and a package added inside a Target.
UNEVALUATED_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Quill.Http" Version="1.2.0" />
    <PackageReference Include="Lantern.Json" Version="12.0.3" />
  </ItemGroup>
  <ItemGroup Condition="'$(Configuration)'=='Release'">
    <PackageReference Update="Quill.Http" Version="2.0.0" />
    <PackageReference Remove="Lantern.Json" />
  </ItemGroup>
  <Target Name="AddLate" BeforeTargets="CollectPackageReferences">
    <ItemGroup><PackageReference Include="Beacon.Late" Version="1.0.0" /></ItemGroup>
  </Target>
</Project>
"""
# A project of two frameworks that references Lib and removes it under a condition, and Lib, which asks for a package.
REMOVE_CONDITION = "'$(TargetFramework)'=='net472'"
REMOVING_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFrameworks>net8.0;net472</TargetFrameworks></PropertyGroup>
  <ItemGroup>
    <ProjectReference Include="..\\Lib\\Lib.csproj" />
    <ProjectReference Remove="..\\Lib\\Lib.csproj" Condition="{0}" />
  </ItemGroup>
</Project>
"""
LIB_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>
  <ItemGroup><PackageReference Include="Lib.Pkg" Version="1.0.0" /></ItemGroup>
</Project>
"""
# A framework a Choose's first When sets while testing it, and a reference that holds for that framework.
CHOOSING_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <Choose>
    <When Condition="'$(TargetFramework)'==''">
      <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>
    </When>
    <Otherwise><PropertyGroup><TargetFramework>net472</TargetFramework></PropertyGroup></Otherwise>
  </Choose>
  <ItemGroup><Reference Include="Beacon.Modern" Condition="'$(TargetFramework)'=='net8.0'" /></ItemGroup>
</Project>
"""
# A project that includes Lib and then holds a Choose whose When sets the framework it tests, and whose Otherwise,
# which MSBuild never enters, removes Lib.
CHOSEN_REMOVE_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <ItemGroup><ProjectReference Include="..\\Lib\\Lib.csproj" /></ItemGroup>
  <Choose>
    <When Condition="'$(TargetFramework)'==''">
      <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>
    </When>
    <Otherwise>
      <PropertyGroup><TargetFramework>net472</TargetFramework></PropertyGroup>
      <ItemGroup><ProjectReference Remove="..\\Lib\\Lib.csproj" /></ItemGroup>
    </Otherwise>
  </Choose>
</Project>
"""
# The same Otherwise after a Choose entered with net8.0, before a group that sets net472.
RESET_REMOVE_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>
  <ItemGroup><ProjectReference Include="..\\Lib\\Lib.csproj" /></ItemGroup>
  <Choose>
    <When Condition="'$(TargetFramework)'=='net8.0'"><PropertyGroup><Chosen>true</Chosen></PropertyGroup></When>
    <Otherwise><ItemGroup><ProjectReference Remove="..\\Lib\\Lib.csproj" /></ItemGroup></Otherwise>
  </Choose>
  <PropertyGroup><TargetFramework>net472</TargetFramework></PropertyGroup>
</Project>
"""
# A legacy project with a packages.config whose Removes the reader cannot evaluate: its Reference's is reported, and
# its PackageReference's is not, since the packages.config gives its packages.
CONFIG_REMOVE_PROJECT = """<Project ToolsVersion="15.0" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <PropertyGroup><TargetFrameworkVersion>v4.7.2</TargetFrameworkVersion></PropertyGroup>
  <ItemGroup>
    <Reference Include="Beacon.Classic" />
    <Reference Remove="Beacon.Classic" Condition="'$(Configuration)'=='Debug'" />
    <PackageReference Remove="Lantern.Json" Condition="'$(Configuration)'=='Debug'" />
  </ItemGroup>
</Project>
"""
OTHER_ROUTE_SDK_TEXT = (
    "Project 'Harbor.Core' has the following dependency graph(s) for 'Lantern.Json':\r\n"
    "\r\n"
    "  [net8.0]\r\n"
    "  \u2514\u2500\u2500 Other.Pkg (v1.0.0)\r\n"
    "      \u2514\u2500\u2500 Lantern.Json (v12.0.3)\r\n"
    "\r\n"
)


class ConditionalItemTests(Fixture):
    def framework_items_solution(self, project: str = FRAMEWORK_ITEMS_PROJECT) -> None:
        self.write("src/Harbor.Api/Harbor.Api.csproj", project)
        self.write("src/Harbor.Legacy/Harbor.Legacy.csproj", LEGACY_PACKAGE_PROJECT)
        self.write("Harbor.sln", solution("src\\Harbor.Api\\Harbor.Api.csproj"))

    def test_a_conditional_reference_and_project_reference_hold_for_their_framework_only(self) -> None:
        self.framework_items_solution()
        self.write("src/Harbor.Api/obj/project.assets.json", json.dumps(FRAMEWORK_ITEMS_ASSETS))
        api, legacy = "src/Harbor.Api/Harbor.Api.csproj", "src/Harbor.Legacy/Harbor.Legacy.csproj"
        code, rows = self.run_main("where", "System.Web", "--root", str(self.root))
        self.assertEqual([[api, "net472", "System.Web", "framework", "-", "-"]], self.lines(rows, "WHERE"))
        self.assertEqual((0, []), (code, self.lines(rows, "UNREADABLE")))
        _, rows = self.run_main("where", "Harbor.Legacy", "--root", str(self.root))
        self.assertEqual([[api, "net472", "Harbor.Legacy", "project", "-", legacy]], self.lines(rows, "WHERE"))
        _, rows = self.run_main("why", "Legacy.Pkg", "--root", str(self.root))
        chain = "Harbor.Legacy (project) > Legacy.Pkg (v1.0.0)"
        self.assertEqual(
            [
                [api, "net472", "Legacy.Pkg", "graph", chain],
                [legacy, "-", "Legacy.Pkg", "graph", "Legacy.Pkg (v1.0.0)"],
            ],
            self.lines(rows, "PATH"),
        )
        # Kept as today: an unconditional reference holds for every framework.
        _, rows = self.run_main("why", "Quill.Http", "--root", str(self.root))
        self.assertEqual(
            [[api, "net8.0;net472", "Quill.Http", "graph", "Quill.Http (v1.2.0)"]], self.lines(rows, "PATH")
        )

    def test_a_restores_recorded_project_references_decide_and_an_unevaluated_reference_is_reported(self) -> None:
        # The restore ran at another path and recorded the project reference for net472 alone, so its condition,
        # which the reader cannot evaluate, is not needed; the Reference's is reported for each framework.
        debug = "'$(Configuration)'=='Debug'"
        project = FRAMEWORK_ITEMS_PROJECT.replace(
            "<ItemGroup Condition=\"'$(TargetFramework)' == 'net472'\">", f'<ItemGroup Condition="{debug}">'
        ).replace('<Reference Include="System.Web" />', '<Reference Include="Beacon.Debug" />')
        self.framework_items_solution(project)
        restore = {
            "projectPath": "D:\\old\\src\\Harbor.Api\\Harbor.Api.csproj",
            "frameworks": {
                "net8.0": {"projectReferences": {}},
                "net472": {"projectReferences": {"D:\\old\\src\\Harbor.Legacy\\Harbor.Legacy.csproj": {}}},
            },
        }
        assets = json.loads(json.dumps(FRAMEWORK_ITEMS_ASSETS))
        assets["project"]["restore"] = restore
        self.write("src/Harbor.Api/obj/project.assets.json", json.dumps(assets))
        api = "src/Harbor.Api/Harbor.Api.csproj"
        code, rows = self.run_main("why", "Legacy.Pkg", "--project", str(self.root / api), "--root", str(self.root))
        chain = "Harbor.Legacy (project) > Legacy.Pkg (v1.0.0)"
        self.assertEqual([[api, "net472", "Legacy.Pkg", "graph", chain]], self.lines(rows, "PATH"))
        self.assertEqual(
            [
                [
                    api,
                    f"the condition {debug} on Reference Beacon.Debug cannot be evaluated for {framework}, so it is "
                    "left out",
                ]
                for framework in ("net8.0", "net472")
            ],
            self.lines(rows, "UNREADABLE"),
        )
        self.assertEqual(1, code)
        # With no restore, every item counts whatever its condition, as before.
        (self.root / "src" / "Harbor.Api" / "obj" / "project.assets.json").unlink()
        code, rows = self.run_main("where", "Beacon.Debug", "--root", str(self.root))
        self.assertEqual([[api, "-", "Beacon.Debug", "framework", "-", "-"]], self.lines(rows, "WHERE"))
        self.assertEqual((0, []), (code, self.lines(rows, "UNREADABLE")))

    def test_a_central_version_holds_for_the_frameworks_its_condition_holds_for(self) -> None:
        self.write("Directory.Packages.props", FRAMEWORK_CENTRAL)
        self.write(
            "src/Harbor.Json/Harbor.Json.csproj",
            FRAMEWORK_ITEMS_PROJECT.replace('Include="Quill.Http" Version="1.2.0"', 'Include="Lantern.Json"'),
        )
        document = {"version": 3, "targets": {"net8.0": {}, "net472": {}}, "project": {}}
        self.write("src/Harbor.Json/obj/project.assets.json", json.dumps(document))
        self.write("src/Harbor.Legacy/Harbor.Legacy.csproj", LEGACY_PACKAGE_PROJECT)
        self.write("Harbor.sln", solution("src\\Harbor.Json\\Harbor.Json.csproj"))
        code, rows = self.run_main("why", "Lantern.Json", "--root", str(self.root))
        label = "src/Harbor.Json/Harbor.Json.csproj"
        self.assertEqual(
            [
                [label, "net8.0", "Lantern.Json", "graph", "Lantern.Json (v13.0.1)"],
                [label, "net472", "Lantern.Json", "graph", "Lantern.Json (v12.0.3)"],
            ],
            self.lines(rows, "PATH"),
        )
        self.assertEqual(
            [
                [
                    label,
                    "the condition '$(Configuration)'=='Debug' on the Update of PackageVersion Lantern.Json cannot be "
                    f"evaluated for {framework}, so the Update is not applied",
                ]
                for framework in ("net8.0", "net472")
            ],
            self.lines(rows, "UNREADABLE"),
        )
        self.assertEqual(1, code)

    def test_an_unevaluated_update_or_remove_says_what_it_left_and_a_targets_item_is_left_out(self) -> None:
        self.write("src/Harbor.Late/Harbor.Late.csproj", UNEVALUATED_PROJECT)
        document = {"version": 3, "targets": {"net8.0": {}}, "project": {}}
        self.write("src/Harbor.Late/obj/project.assets.json", json.dumps(document))
        self.write("Harbor.sln", solution("src\\Harbor.Late\\Harbor.Late.csproj"))
        label = "src/Harbor.Late/Harbor.Late.csproj"
        release = "'$(Configuration)'=='Release'"
        code, rows = self.run_main("why", "Quill.Http", "Lantern.Json", "Beacon.Late", "--root", str(self.root))
        self.assertEqual(
            [
                [
                    label,
                    "the PackageReference Beacon.Late inside Target AddLate is left out: MSBuild defines it only when "
                    "that target runs",
                ],
                [
                    label,
                    f"the condition {release} on the Update of PackageReference Quill.Http cannot be evaluated for "
                    "net8.0, so the Update is not applied",
                ],
                [
                    label,
                    f"the condition {release} on the Remove of PackageReference Lantern.Json cannot be evaluated "
                    "for net8.0, so it is not removed",
                ],
            ],
            self.lines(rows, "UNREADABLE"),
        )
        self.assertEqual(
            [
                [label, "net8.0", "Quill.Http", "graph", "Quill.Http (v1.2.0)"],
                [label, "net8.0", "Lantern.Json", "graph", "Lantern.Json (v12.0.3)"],
            ],
            self.lines(rows, "PATH"),
        )
        self.assertEqual(([["Beacon.Late"]], 1), (self.lines(rows, "MISSING"), code))

    def test_a_conditional_project_reference_remove_keeps_the_project_for_the_other_frameworks(self) -> None:
        # The issue's two projects, App removing Lib for net472 alone, read from the directory holding both.
        app, lib = "App/App.csproj", "Lib/Lib.csproj"
        self.write(app, REMOVING_PROJECT.format(REMOVE_CONDITION))
        self.write(lib, LIB_PROJECT)
        chain = [app, "-", "Lib.Pkg", "graph", "Lib (project) > Lib.Pkg (v1.0.0)"]
        own = [lib, "-", "Lib.Pkg", "graph", "Lib.Pkg (v1.0.0)"]
        code, rows = self.run_main("why", "Lib.Pkg", "--root", str(self.root))
        self.assertEqual((0, [chain, own], []), (code, self.lines(rows, "PATH"), self.lines(rows, "UNREADABLE")))
        # Listed alone, App still loads Lib as its reference, with no restore and with one: recorded for net8.0, or
        # not recorded, so each framework's conditions decide.
        self.write("Harbor.sln", solution("App\\App.csproj"))
        _, rows = self.run_main("why", "Lib.Pkg", "--root", str(self.root))
        self.assertEqual([chain, own], self.lines(rows, "PATH"))
        restore = {
            "projectPath": "D:\\old\\App\\App.csproj",
            "frameworks": {
                "net8.0": {"projectReferences": {"D:\\old\\Lib\\Lib.csproj": {}}},
                "net472": {"projectReferences": {}},
            },
        }
        for project in ({"restore": restore}, {}):
            with self.subTest(recorded=bool(project)):
                document = {"version": 3, "targets": {"net8.0": {}, "net472": {}}, "project": project}
                self.write("App/obj/project.assets.json", json.dumps(document))
                code, rows = self.run_main("why", "Lib.Pkg", "--root", str(self.root))
                self.assertEqual(
                    (0, [[app, "net8.0", *chain[2:]], own], []),
                    (code, self.lines(rows, "PATH"), self.lines(rows, "UNREADABLE")),
                )
        # A Remove whose condition the reader cannot evaluate is reported as not removed, and is not: the walk keeps
        # Lib for each framework.
        debug = "'$(Configuration)'=='Debug'"
        self.write(app, REMOVING_PROJECT.format(debug))
        code, rows = self.run_main("why", "Lib.Pkg", "--root", str(self.root))
        self.assertEqual([[app, "net8.0;net472", *chain[2:]], own], self.lines(rows, "PATH"))
        self.assertEqual(
            [
                [
                    app,
                    f"the condition {debug} on the Remove of ProjectReference Lib/Lib.csproj cannot be evaluated for "
                    f"{framework}, so it is not removed",
                ]
                for framework in ("net8.0", "net472")
            ],
            self.lines(rows, "UNREADABLE"),
        )
        self.assertEqual(1, code)
        # An unconditional Remove still removes the project everywhere, so App reaches nothing and Lib is not read.
        (self.root / "App" / "obj" / "project.assets.json").unlink()
        self.write(app, REMOVING_PROJECT.replace(' Condition="{0}"', ""))
        code, rows = self.run_main("why", "Lib.Pkg", "--root", str(self.root))
        self.assertEqual((0, [["Lib.Pkg"]]), (code, self.lines(rows, "MISSING")))
        self.assertEqual([[app, "sdk", "direct", "listed"]], self.lines(rows, "PROJECT"))

    def test_a_choose_that_sets_the_framework_it_tests_keeps_the_framework_its_when_chose(self) -> None:
        # The restore keys its target by the long name, which reads the project's conditions only through the short
        # name the project's Choose gives it.
        label = "App/App.csproj"
        self.write(label, CHOOSING_PROJECT)
        document = {"version": 3, "targets": {".NETCoreApp,Version=v8.0": {}}, "project": {}}
        self.write("App/obj/project.assets.json", json.dumps(document))
        code, rows = self.run_main("where", "Beacon.Modern", "--root", str(self.root))
        self.assertEqual([[label, "net8.0", "Beacon.Modern", "framework", "-", "-"]], self.lines(rows, "WHERE"))
        self.assertEqual((0, []), (code, self.lines(rows, "UNREADABLE")))

    def test_a_remove_in_a_branch_the_choose_did_not_choose_is_never_applied(self) -> None:
        # The issue's two projects: MSBuild enters App's Choose with the framework unset, so the When applies and the
        # Otherwise's Remove never does, with no restore and with one; nor when a later group sets the framework the
        # Choose's When did not test.
        app, lib = "App/App.csproj", "Lib/Lib.csproj"
        self.write(lib, LIB_PROJECT)
        own = [lib, "-", "Lib.Pkg", "graph", "Lib.Pkg (v1.0.0)"]
        for project, framework in ((CHOSEN_REMOVE_PROJECT, "net8.0"), (RESET_REMOVE_PROJECT, "net472")):
            with self.subTest(framework=framework):
                assets = self.root / "App" / "obj" / "project.assets.json"
                assets.unlink(missing_ok=True)
                self.write(app, project)
                chain = [app, "-", "Lib.Pkg", "graph", "Lib (project) > Lib.Pkg (v1.0.0)"]
                code, rows = self.run_main("why", "Lib.Pkg", "--root", str(self.root))
                self.assertEqual(
                    (0, [chain, own], []), (code, self.lines(rows, "PATH"), self.lines(rows, "UNREADABLE"))
                )
                document = {"version": 3, "targets": {framework: {}}, "project": {}}
                self.write("App/obj/project.assets.json", json.dumps(document))
                code, rows = self.run_main("why", "Lib.Pkg", "--root", str(self.root))
                self.assertEqual(
                    (0, [[app, framework, *chain[2:]], own], []),
                    (code, self.lines(rows, "PATH"), self.lines(rows, "UNREADABLE")),
                )

    def test_with_no_restore_a_remove_it_cannot_evaluate_is_reported_and_not_applied(self) -> None:
        # The issue's projects: App removes Lib under a condition the reader cannot evaluate, and a central
        # PackageVersion's Remove under one gives Lib.Pkg's version. With no restore, both are kept and reported for
        # the first framework each cannot be evaluated for, as with a restore.
        app, lib = "App/App.csproj", "Lib/Lib.csproj"
        debug = "'$(Configuration)'=='Debug'"
        self.write(app, REMOVING_PROJECT.format(debug))
        self.write(lib, LIB_PROJECT.replace(' Version="1.0.0"', ""))
        self.write(
            "Directory.Packages.props",
            '<Project><ItemGroup><PackageVersion Include="Lib.Pkg" Version="1.0.0" />'
            f'<PackageVersion Remove="Lib.Pkg" Condition="{debug}" /></ItemGroup></Project>',
        )
        code, rows = self.run_main("why", "Lib.Pkg", "--root", str(self.root))
        self.assertEqual(
            [
                [app, "-", "Lib.Pkg", "graph", "Lib (project) > Lib.Pkg (v1.0.0)"],
                [lib, "-", "Lib.Pkg", "graph", "Lib.Pkg (v1.0.0)"],
            ],
            self.lines(rows, "PATH"),
        )
        self.assertEqual(
            [
                [
                    app,
                    f"the condition {debug} on the Remove of ProjectReference Lib/Lib.csproj cannot be evaluated for "
                    "net8.0, so it is not removed",
                ],
                [
                    lib,
                    f"the condition {debug} on the Remove of PackageVersion Lib.Pkg cannot be evaluated for net8.0, "
                    "so it is not removed",
                ],
            ],
            sorted(self.lines(rows, "UNREADABLE")),
        )
        self.assertEqual(1, code)
        # A project whose packages.config gives its packages reports its Reference's Remove and not its
        # PackageReference's, which it does not read.
        label = "Classic/Classic.csproj"
        self.write(label, CONFIG_REMOVE_PROJECT)
        self.write("Classic/packages.config", '<?xml version="1.0"?><packages />')
        self.write("Harbor.sln", solution("Classic\\Classic.csproj"))
        code, rows = self.run_main("where", "Beacon.Classic", "--root", str(self.root))
        self.assertEqual([[label, "-", "Beacon.Classic", "framework", "-", "-"]], self.lines(rows, "WHERE"))
        self.assertEqual(
            [
                [
                    label,
                    f"the condition {debug} on the Remove of Reference Beacon.Classic cannot be evaluated for v4.7.2, "
                    "so it is not removed",
                ]
            ],
            self.lines(rows, "UNREADABLE"),
        )
        self.assertEqual(1, code)

    def test_the_chain_limit_counts_the_sdks_chains(self) -> None:
        root = self.mixed_solution()
        core = self.root / "src" / "Harbor.Core" / "Harbor.Core.csproj"
        sdk = TableSdk({(same_path(core), "lantern.json"): OTHER_ROUTE_SDK_TEXT})
        services = Services(run=sdk, which=self.with_dotnet.which)
        original = dependency_graph.PATH_LIMIT
        dependency_graph.PATH_LIMIT = 1
        self.addCleanup(setattr, dependency_graph, "PATH_LIMIT", original)
        _, rows = self.run_main("why", "Lantern.Json", "--project", str(core), "--root", str(root), services=services)
        label = "src/Harbor.Core/Harbor.Core.csproj"
        self.assertEqual(
            [[label, "net8.0", "Lantern.Json", "sdk", "Other.Pkg (v1.0.0) > Lantern.Json (v12.0.3)"]],
            self.lines(rows, "PATH"),
        )
        self.assertEqual([[label, "net8.0", "Lantern.Json", "1"]], self.lines(rows, "TRUNCATED"))
        # Within the limit, the SDK's chain and the walk's are both printed and nothing is cut.
        dependency_graph.PATH_LIMIT = 2
        _, rows = self.run_main("why", "Lantern.Json", "--project", str(core), "--root", str(root), services=services)
        self.assertEqual(2, len(self.lines(rows, "PATH")))
        self.assertEqual([], self.lines(rows, "TRUNCATED"))

    def test_a_when_without_a_condition_and_a_framework_of_thousands_of_digits_cost_only_their_own_facts(
        self,
    ) -> None:
        root = self.mixed_solution()
        self.write(
            "src/Harbor.Tests/Harbor.Tests.csproj",
            '<Project Sdk="Microsoft.NET.Sdk"><Choose><When><ItemGroup><PackageReference Include="Alpha.Pkg" '
            'Version="1.0.0" /></ItemGroup></When></Choose></Project>',
        )
        long_name = ".NETCoreApp,Version=v" + "9" * 5000
        assets = json.loads(json.dumps(CORE_ASSETS))
        assets["targets"][long_name] = {}
        self.write("src/Harbor.Core/obj/project.assets.json", json.dumps(assets))
        code, rows = self.run_main("why", "Quill.Http", "--root", str(root))
        self.assertEqual(
            [
                [
                    "src/Harbor.Tests/Harbor.Tests.csproj",
                    "a When has no Condition, which MSBuild refuses, so the file does not evaluate",
                ]
            ],
            self.lines(rows, "UNREADABLE"),
        )
        # The restore's target of a name no project names keeps the name it was written with.
        core = [row for row in self.lines(rows, "PATH") if row[0] == "src/Harbor.Core/Harbor.Core.csproj"]
        self.assertEqual([["net8.0", long_name]], [row[1].split(";") for row in core])
        self.assertEqual((1, "SUMMARY"), (code, rows[-1][0]))


class GraphNarrowingTests(Fixture):
    def test_graph_narrowed_to_a_project_holds_it_and_the_projects_it_reaches(self) -> None:
        root = self.mixed_solution()
        target = self.root / "out" / "graph.mmd"
        tests = str(self.root / "src" / "Harbor.Tests" / "Harbor.Tests.csproj")
        code, rows = self.run_main("graph", "--project", tests, "--output", str(target), "--root", str(root))
        self.assertEqual(0, code)
        text = target.read_text(encoding="utf-8")
        self.assertIn('["src/Harbor.Tests/Harbor.Tests.csproj"]:::project', text)
        self.assertIn('["src/Harbor.Core/Harbor.Core.csproj"]:::project', text)
        self.assertIn('["Quill.Http 1.2.0"]:::package', text)
        self.assertNotIn("Harbor.Web", text)
        self.assertNotIn("System.Web", text)
        code, rows = self.run_main("graph", "--project", str(self.root / "nowhere.csproj"), "--root", str(root))
        self.assertEqual(1, code)
        self.assertIn("is not a project of", rows[-1][0])


class EntryPointTests(Fixture):
    def run_script(self, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *arguments],
            capture_output=True,
            cwd=self.root,
            env={**os.environ, **(env or {})},
            check=False,
        )

    def test_the_program_defaults_to_the_current_directory_and_prints_utf8(self) -> None:
        self.mixed_solution()
        self.write("src/Harbor.Web/Harbor.Web.csproj", WEB_PROJECT.replace("System.Web", "Caf\u00e9.Web"))
        result = self.run_script("where", "Caf\u00e9.Web", env={"PYTHONIOENCODING": "cp1252"})
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("WHERE\tsrc/Harbor.Web/Harbor.Web.csproj\t-\tCaf\u00e9.Web\tframework".encode(), result.stdout)

    def test_usage_errors_exit_2_and_a_bad_root_prints_failed_and_exits_1(self) -> None:
        result = self.run_script("why")
        self.assertEqual(2, result.returncode)
        self.assertIn(b"at least one name", result.stderr)
        result = self.run_script("why", "--all", "Lantern.Json", "--project", "x.csproj")
        self.assertEqual(2, result.returncode)
        result = self.run_script("why", "Lantern.Json", "--root", str(self.root / "missing"))
        self.assertEqual(1, result.returncode)
        self.assertTrue(result.stdout.rstrip().endswith(b"is neither a solution file nor a directory"), result.stdout)


class UnreadableSolutionTests(Fixture):
    def test_a_solution_the_system_refuses_to_read_is_reported_unreadable_and_the_run_goes_on(self) -> None:
        root = self.mixed_solution()
        refused = PermissionError(13, "Permission denied", str(root))
        with mock.patch.object(dependency_graph, "read_listed_projects", side_effect=refused):
            code, rows = self.run_main("why", "Lantern.Json", "--root", str(root))
        self.assertEqual(1, code)
        self.assertEqual([["Harbor.sln", "Permission denied"]], self.lines(rows, "UNREADABLE"))
        self.assertEqual([], self.lines(rows, "PROJECT"))
        self.assertFalse(any(row[0].startswith("FAILED") for row in rows), rows)
        self.assertEqual("SUMMARY", rows[-1][0])


class FailedSdkHeadingTests(Fixture):
    def test_a_no_dependency_heading_then_a_nonzero_exit_is_a_failed_run_through_why(self) -> None:
        # The heading parses as a whole answer, that the project does not depend on the package, so a reader that
        # failed only a partial tree would take it and print no SDK_FAILED line.
        root = self.mixed_solution()
        services = Services(
            run=lambda arguments, timeout: Completed(1, SDK_ABSENT_TEXT.format(name="Lantern.Json")),
            which=self.with_dotnet.which,
        )
        code, rows = self.run_main("why", "Lantern.Json", "--root", str(root), services=services)
        self.assertEqual(0, code)
        self.assertEqual(
            [
                [
                    "src/Harbor.Core/Harbor.Core.csproj",
                    "Lantern.Json",
                    "dotnet nuget why exited with code 1 after printing part of an answer",
                ]
            ],
            self.lines(rows, "SDK_FAILED"),
        )
        self.assertIn(
            [
                "src/Harbor.Core/Harbor.Core.csproj",
                "net8.0",
                "Lantern.Json",
                "graph",
                "Quill.Http (v1.2.0) > Lantern.Json (v12.0.3)",
            ],
            self.lines(rows, "PATH"),
        )


if __name__ == "__main__":
    unittest.main()
