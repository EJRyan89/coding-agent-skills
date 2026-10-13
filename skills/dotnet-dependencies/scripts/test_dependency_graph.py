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
                [label, f"the condition {debug} on PackageReference Beacon.Debug cannot be evaluated for {framework}"]
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


if __name__ == "__main__":
    unittest.main()
