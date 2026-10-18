"""Regression tests for the evaluation of a project file and the files it imports, as MSBuild evaluates them."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import msbuild_evaluation
from msbuild_evaluation import ProjectFile, Sources, Unsettled, read_project
from msbuild_reader import PackageRef, Unreadable

LEGACY_PROJECT = """<?xml version="1.0" encoding="utf-8"?>
<Project ToolsVersion="15.0" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <PropertyGroup>
    <AssemblyName>Harbor.Web</AssemblyName>
    <TargetFrameworkVersion>v4.7.2</TargetFrameworkVersion>
  </PropertyGroup>
  <ItemGroup>
    <Reference Include="Lantern.Json, Version=13.0.0.0, Culture=neutral, processorArchitecture=MSIL">
      <HintPath>..\\..\\packages\\Lantern.Json.13.0.1\\lib\\net45\\Lantern.Json.dll</HintPath>
    </Reference>
    <Reference Include="System.Web" />
    <Reference Include="" />
  </ItemGroup>
  <ItemGroup>
    <ProjectReference Include="..\\Harbor.Core\\Harbor.Core.csproj" />
  </ItemGroup>
</Project>
"""
SDK_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFrameworks>net8.0;net472</TargetFrameworks>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Quill.Http" Version="1.2.0" />
    <PackageReference Include="Lantern.Json"><Version>12.0.3</Version></PackageReference>
    <PackageReference Include="Lantern.Core" />
    <PackageReference Include="Lantern.Text" VersionOverride="3.0.0" />
    <PackageReference Include="Beacon.Unknown" />
    <PackageReference Update="Ignored.Update" Version="9.9.9" />
    <PackageReference Include="Beacon.Removed" Version="1.0.0" />
    <PackageReference Update="quill.http" Version="1.3.0" />
    <PackageReference Remove="Beacon.Removed" />
  </ItemGroup>
</Project>
"""
CONDITIONAL_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFrameworks>net8.0;net472</TargetFrameworks></PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Quill.Http" Version="1.2.0" />
    <PackageReference Include="Lantern.Tuple" Version="4.5.0" Condition="'$(TargetFramework)'=='net472'" />
    <PackageReference Include="Lantern.Span" Version="4.5.5" Condition=" '$(TargetFramework)' != 'net8.0' " />
  </ItemGroup>
  <ItemGroup Condition="'$(TargetFramework)' == 'net8.0' Or '$(TargetFramework)' == 'net9.0'">
    <PackageReference Include="Lantern.Modern" Version="2.0.0" />
    <PackageReference Update="Quill.Http" Version="1.3.0" />
  </ItemGroup>
  <ItemGroup Condition="$(TargetFramework.StartsWith('net4'))">
    <PackageReference Include="Lantern.Classic" Version="1.0.0" />
  </ItemGroup>
  <ItemGroup Condition="'$(Configuration)' == 'Debug'">
    <PackageReference Include="Beacon.Debug" Version="1.0.0" />
  </ItemGroup>
  <Choose>
    <When Condition="'$(TargetFramework)' == 'net472'">
      <ItemGroup><PackageReference Include="Lantern.Chosen" Version="1.0.0" /></ItemGroup>
    </When>
    <Otherwise>
      <ItemGroup><PackageReference Include="Lantern.Other" Version="1.0.0" /></ItemGroup>
    </Otherwise>
  </Choose>
</Project>
"""
# Two Choose elements: the first's Whens overlap and end in an Otherwise; the second's first When cannot be evaluated.
CHOOSE_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <Choose>
    <When Condition="$(TargetFramework.StartsWith('net4'))">
      <ItemGroup><PackageReference Include="Lantern.First" Version="1.0.0" /></ItemGroup>
    </When>
    <When Condition="$(TargetFramework.StartsWith('net'))">
      <ItemGroup><PackageReference Include="Lantern.Second" Version="1.0.0" /></ItemGroup>
    </When>
    <Otherwise>
      <ItemGroup><PackageReference Include="Lantern.Rest" Version="1.0.0" /></ItemGroup>
    </Otherwise>
  </Choose>
  <Choose>
    <When Condition="'$(Configuration)' == 'Debug'">
      <ItemGroup><PackageReference Include="Beacon.Unsure" Version="1.0.0" /></ItemGroup>
    </When>
    <When Condition="'$(TargetFramework)' == 'net472'">
      <ItemGroup><PackageReference Include="Beacon.Later" Version="1.0.0" /></ItemGroup>
    </When>
    <Otherwise>
      <ItemGroup><PackageReference Include="Beacon.Rest" Version="1.0.0" /></ItemGroup>
    </Otherwise>
  </Choose>
</Project>
"""
# A Choose entered with net8.0, whose When holds then, before a group that sets net472.
ENTERED_CHOOSE_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>
  <Choose>
    <When Condition="'$(TargetFramework)'=='net8.0'">
      <ItemGroup><PackageReference Include="Lantern.Entered" Version="1.0.0" /></ItemGroup>
    </When>
    <Otherwise>
      <ItemGroup><PackageReference Include="Lantern.Other" Version="1.0.0" /></ItemGroup>
    </Otherwise>
  </Choose>
  <PropertyGroup><TargetFramework>net472</TargetFramework></PropertyGroup>
  <ItemGroup Condition="'$(TargetFramework)'=='net472'">
    <PackageReference Include="Lantern.Final" Version="1.0.0" />
  </ItemGroup>
</Project>
"""
# A Choose in a project whose frameworks TargetFrameworks names, though the file sets TargetFramework after it.
APART_CHOOSE_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFrameworks>net8.0;net472</TargetFrameworks></PropertyGroup>
  <Choose>
    <When Condition="'$(TargetFramework)'=='net472'">
      <ItemGroup><PackageReference Include="Lantern.Apart" Version="1.0.0" /></ItemGroup>
    </When>
    <Otherwise>
      <ItemGroup><PackageReference Include="Lantern.Other" Version="1.0.0" /></ItemGroup>
    </Otherwise>
  </Choose>
  <PropertyGroup Condition="'$(TargetFramework)'==''"><TargetFramework>net8.0</TargetFramework></PropertyGroup>
</Project>
"""
LIST_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <ItemGroup>
    <PackageReference Include="Lantern.Json; Quill.Http;Beacon.Gone;Beacon.Spent" Version="1.0.0" />
    <PackageReference Update="Lantern.Json;Lantern.Absent" Version="13.0.1" />
    <PackageReference Update="Quill.Http" Version="2.0.0" />
    <PackageReference Remove=";Beacon.Gone; Beacon.Spent;" />
    <ProjectReference Include="..\\Harbor.Core\\Harbor.Core.csproj;..\\Harbor.Data\\Harbor.Data.csproj" />
    <Reference Include="Beacon.One, Version=1.0.0.0;Beacon.Two" />
  </ItemGroup>
</Project>
"""
CENTRAL = """<Project>
  <PropertyGroup><ManagePackageVersionsCentrally>true</ManagePackageVersionsCentrally></PropertyGroup>
  <ItemGroup>
    <PackageVersion Include="Lantern.Core" Version="2.0.0" />
    <PackageVersion Include="Lantern.Text" Version="2.5.0" />
  </ItemGroup>
</Project>
"""
# Reference, ProjectReference, and central PackageVersion items under conditions on the framework and on another
# property, with an Update and a Remove.
CONDITIONAL_ITEMS_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFrameworks>net8.0;net472</TargetFrameworks></PropertyGroup>
  <ItemGroup>
    <ProjectReference Include="..\\Harbor.Shared\\Harbor.Shared.csproj" />
    <PackageReference Include="Lantern.Json" />
    <Reference Include="Beacon.Modern" Condition="'$(TargetFramework)' == 'net8.0'" />
  </ItemGroup>
  <ItemGroup Condition="'$(TargetFramework)' == 'net472'">
    <Reference Include="System.Web" />
    <Reference Include="Beacon.Hinted" />
    <Reference Update="Beacon.Hinted"><HintPath>lib\\Beacon.Hinted.dll</HintPath></Reference>
    <ProjectReference Include="..\\Harbor.Legacy\\Harbor.Legacy.csproj" />
  </ItemGroup>
  <ItemGroup Condition="'$(Configuration)' == 'Debug'">
    <Reference Include="Beacon.Debug" />
    <ProjectReference Remove="..\\Harbor.Shared\\Harbor.Shared.csproj" />
  </ItemGroup>
</Project>
"""
CONDITIONAL_CENTRAL = """<Project>
  <ItemGroup Condition="'$(TargetFramework)' == 'net8.0'">
    <PackageVersion Include="Lantern.Json" Version="13.0.1" />
  </ItemGroup>
  <ItemGroup Condition="'$(TargetFramework)' != 'net8.0'">
    <PackageVersion Include="Lantern.Json" Version="12.0.3" />
  </ItemGroup>
  <ItemGroup Condition="'$(Configuration)' == 'Debug'">
    <PackageVersion Update="Lantern.Json" Version="14.0.0" />
    <PackageVersion Include="Beacon.Unused" Version="1.0.0" />
  </ItemGroup>
</Project>
"""
# Items and a property inside Targets, which MSBuild defines only when the target runs.
TARGET_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>
  <ItemGroup><PackageReference Include="Quill.Http" Version="1.2.0" /></ItemGroup>
  <Target Name="AddLate" BeforeTargets="CollectPackageReferences">
    <PropertyGroup><TargetFramework>net472</TargetFramework></PropertyGroup>
    <ItemGroup>
      <PackageReference Include="Beacon.Late" Version="1.0.0" />
      <ProjectReference Include="..\\Harbor.Late.Core\\Harbor.Late.Core.csproj" />
      <Reference Include="System.Late" />
    </ItemGroup>
  </Target>
  <Target Name="DropQuill">
    <ItemGroup><PackageReference Remove="Quill.Http" /></ItemGroup>
  </Target>
</Project>
"""
TARGET_CENTRAL = """<Project>
  <ItemGroup><PackageVersion Include="Lantern.Json" Version="13.0.1" /></ItemGroup>
  <Target Name="Pin"><ItemGroup><PackageVersion Include="Beacon.Late" Version="1.0.0" /></ItemGroup></Target>
</Project>
"""


def unsettled_rows(entries: list[Unsettled]) -> list[tuple[str, str, str, str]]:
    """Each unsettled entry as its operation, name, condition, and framework."""
    return [(entry.operation, entry.name, entry.condition, entry.framework) for entry in entries]


class EvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dotnet-dependencies-evaluation-")).resolve()
        self.addCleanup(self.remove_root)

    def remove_root(self) -> None:
        for path in sorted(self.root.rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        self.root.rmdir()

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def read(self, relative: str, text: str | None = None) -> ProjectFile:
        path = self.write(relative, text) if text is not None else self.root / relative
        return read_project(path, Sources(self.root))

    def test_legacy_project_gives_assembly_references_with_version_and_hint_path(self) -> None:
        project = self.read("src/Harbor.Web/Harbor.Web.csproj", LEGACY_PROJECT)
        self.assertEqual("legacy", project.style)
        self.assertEqual("Harbor.Web", project.assembly_name)
        # A project that is not SDK-style builds the .NET Framework its TargetFrameworkVersion names.
        self.assertEqual(["net472"], project.frameworks)
        self.assertEqual([(self.root / "src" / "Harbor.Core" / "Harbor.Core.csproj").resolve()], project.project_refs)
        self.assertEqual([], project.packages)
        self.assertEqual(
            [
                ("Lantern.Json", "13.0.0.0", "..\\..\\packages\\Lantern.Json.13.0.1\\lib\\net45\\Lantern.Json.dll"),
                ("System.Web", None, None),
            ],
            [(item.name, item.version, item.hint_path) for item in project.assemblies],
        )

    def test_sdk_project_takes_versions_from_attribute_child_override_or_central_file(self) -> None:
        # An Update item sets the version of a reference included before it and never adds one; a Remove drops one.
        self.write("Directory.Packages.props", CENTRAL)
        project = self.read("src/Harbor.Core/Harbor.Core.csproj", SDK_PROJECT)
        self.assertEqual("sdk", project.style)
        self.assertEqual("Harbor.Core", project.assembly_name)
        self.assertEqual(["net8.0", "net472"], project.frameworks)
        self.assertEqual(
            [
                PackageRef("Quill.Http", "1.3.0", "PackageReference"),
                PackageRef("Lantern.Json", "12.0.3", "PackageReference"),
                PackageRef("Lantern.Core", "2.0.0", "central"),
                PackageRef("Lantern.Text", "3.0.0", "PackageReference"),
                PackageRef("Beacon.Unknown", None, "PackageReference"),
            ],
            project.packages,
        )

    def test_a_frameworks_references_are_those_whose_conditions_hold_for_it(self) -> None:
        project = self.read("src/Harbor.Api/Harbor.Api.csproj", CONDITIONAL_PROJECT)
        debug = "'$(Configuration)' == 'Debug'"
        expected = {
            "net8.0": [("Quill.Http", "1.3.0"), ("Lantern.Modern", "2.0.0"), ("Lantern.Other", "1.0.0")],
            "net472": [
                ("Quill.Http", "1.2.0"),
                ("Lantern.Tuple", "4.5.0"),
                ("Lantern.Span", "4.5.5"),
                ("Lantern.Classic", "1.0.0"),
                ("Lantern.Chosen", "1.0.0"),
            ],
        }
        for framework, references in expected.items():
            with self.subTest(framework=framework):
                build = project.build(framework)
                self.assertEqual(references, [(item.id, item.version) for item in build.packages])
                # A condition on another property is not guessed: the item is left out and named.
                self.assertEqual([("Beacon.Debug", debug)], [(item.name, item.condition) for item in build.unsettled])
        # With no restore, every Include and Update that may apply in a build counts.
        self.assertEqual(
            [
                ("Quill.Http", "1.3.0"),
                ("Lantern.Tuple", "4.5.0"),
                ("Lantern.Span", "4.5.5"),
                ("Lantern.Modern", "2.0.0"),
                ("Lantern.Classic", "1.0.0"),
                ("Beacon.Debug", "1.0.0"),
                ("Lantern.Chosen", "1.0.0"),
                ("Lantern.Other", "1.0.0"),
            ],
            [(item.id, item.version) for item in project.packages],
        )
        self.assertEqual([], project.unsettled)

    def test_a_choose_applies_its_first_when_that_holds_else_its_otherwise(self) -> None:
        project = self.read("src/Harbor.Choice/Harbor.Choice.csproj", CHOOSE_PROJECT)
        debug = "'$(Configuration)' == 'Debug'"
        cases = {
            # Both Whens of the first Choose hold for net472, and only the first applies. In the second, a When after
            # one that cannot be evaluated cannot be either, and the Otherwise is ruled out because a When holds,
            # whatever the first one is.
            "net472": (["Lantern.First"], [("Beacon.Unsure", debug), ("Beacon.Later", debug)]),
            # The first When fails and the second holds; in the second Choose the Otherwise is not settled.
            "netstandard2.0": (["Lantern.Second"], [("Beacon.Unsure", debug), ("Beacon.Rest", debug)]),
            # No When of the first Choose holds, so its Otherwise applies.
            "uap10.0": (["Lantern.Rest"], [("Beacon.Unsure", debug), ("Beacon.Rest", debug)]),
        }
        for framework, (references, unsettled) in cases.items():
            with self.subTest(framework=framework):
                build = project.build(framework)
                self.assertEqual(references, [item.id for item in build.packages])
                self.assertEqual(unsettled, [(item.name, item.condition) for item in build.unsettled])

    def test_an_include_update_or_remove_lists_each_item_it_names(self) -> None:
        project = self.read("src/Harbor.List/Harbor.List.csproj", LIST_PROJECT)
        self.assertEqual(
            [
                PackageRef("Lantern.Json", "13.0.1", "PackageReference"),
                PackageRef("Quill.Http", "2.0.0", "PackageReference"),
            ],
            project.packages,
        )
        self.assertEqual(
            [(self.root / "src" / name / f"{name}.csproj").resolve() for name in ("Harbor.Core", "Harbor.Data")],
            project.project_refs,
        )
        self.assertEqual(
            [("Beacon.One", "1.0.0.0"), ("Beacon.Two", None)], [(a.name, a.version) for a in project.assemblies]
        )

    def test_central_versions_come_from_the_nearest_props_file_at_or_above_the_project_inside_the_tree(self) -> None:
        # MSBuild searches for Directory.Packages.props up to the filesystem's root, past the solution's folder; one
        # it finds outside the examined tree is reported as an import not read, naming its path, and never opened.
        props = self.write("Directory.Packages.props", CENTRAL)
        text = '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup><PackageReference Include="Lantern.Core" /></ItemGroup>'
        path = self.write("src/App/App.csproj", f"{text}</Project>")
        project = self.read("src/App/App.csproj")
        self.assertEqual(([PackageRef("Lantern.Core", "2.0.0", "central")], []), (project.packages, project.reports))
        tree = self.root / "src"
        with mock.patch.object(msbuild_evaluation, "evaluation_root", wraps=msbuild_evaluation.evaluation_root) as read:
            found = read_project(path, Sources(tree))
        self.assertEqual([PackageRef("Lantern.Core", None, "PackageReference")], found.packages)
        self.assertNotIn(props, [call.args[0] for call in read.call_args_list])
        reason = (
            f"the Import of {props.as_posix()} is not read: the path is outside the examined tree {tree.as_posix()},"
            " which the reader does not leave, so every property is unsettled after it"
        )
        self.assertEqual([(path, reason)], [(report.file, report.reason) for report in found.reports])

    def test_sdk_element_and_sdk_import_count_as_sdk_style(self) -> None:
        for text in (
            '<Project><Sdk Name="Microsoft.NET.Sdk" /><PropertyGroup><TargetFramework>net8.0</TargetFramework>'
            "</PropertyGroup></Project>",
            '<Project><Import Project="Sdk.props" Sdk="Microsoft.NET.Sdk" /></Project>',
        ):
            self.assertEqual("sdk", self.read("a/a.csproj", text).style)
        self.assertEqual("legacy", self.read("b/b.csproj", "<Project></Project>").style)

    def test_an_unreadable_project_file_names_the_file_and_the_reason(self) -> None:
        cases = {
            "<Project><ItemGroup></Project>": "not well-formed",
            "<!DOCTYPE project><Project />": "DOCTYPE",
            '<?xml version="1.0"?><!ENTITY x "y"><Project />': "ENTITY",
        }
        for text, reason in cases.items():
            with self.subTest(reason=reason), self.assertRaises(Unreadable) as refused:
                self.read("bad/bad.csproj", text)
            self.assertEqual(self.root / "bad" / "bad.csproj", refused.exception.file)
            self.assertIn(reason, refused.exception.reason)
        with self.assertRaises(Unreadable) as refused:
            self.read("missing.csproj")
        self.assertEqual(self.root / "missing.csproj", refused.exception.file)

    def test_each_item_kind_holds_for_the_frameworks_its_conditions_hold_for(self) -> None:
        self.write("Directory.Packages.props", CONDITIONAL_CENTRAL)
        project = self.read("src/Harbor.Api/Harbor.Api.csproj", CONDITIONAL_ITEMS_PROJECT)
        legacy = (self.root / "src" / "Harbor.Legacy" / "Harbor.Legacy.csproj").resolve()
        shared = (self.root / "src" / "Harbor.Shared" / "Harbor.Shared.csproj").resolve()
        debug = "'$(Configuration)' == 'Debug'"
        cases = {
            "net8.0": ([shared], [("Beacon.Modern", None)], [("Lantern.Json", "13.0.1", "central")]),
            "net472": (
                [shared, legacy],
                [("System.Web", None), ("Beacon.Hinted", "lib\\Beacon.Hinted.dll")],
                [("Lantern.Json", "12.0.3", "central")],
            ),
        }
        for framework, (projects, assemblies, packages) in cases.items():
            with self.subTest(framework=framework):
                build = project.build(framework)
                self.assertEqual(projects, build.project_refs)
                self.assertEqual([(item.name, item.hint_path) for item in build.assemblies], assemblies)
                self.assertEqual(packages, [(ref.id, ref.version, ref.origin) for ref in build.packages])
                # A central item whose condition cannot be evaluated is named for a package that takes its version.
                self.assertEqual(
                    [
                        ("ProjectReference", "Remove", debug),
                        ("Reference", "Include", debug),
                        ("PackageVersion", "Update", debug),
                    ],
                    sorted(
                        ((item.kind, item.operation, item.condition) for item in build.unsettled),
                        key=lambda row: ("ProjectReference", "Reference", "PackageVersion").index(row[0]),
                    ),
                )
        # With no restore, every Include and Update that may apply in a build counts; a Remove that cannot be shown to
        # hold in every build is not made.
        self.assertEqual([shared, legacy], project.project_refs)
        self.assertEqual(
            ["Beacon.Modern", "System.Web", "Beacon.Hinted", "Beacon.Debug"], [item.name for item in project.assemblies]
        )
        self.assertEqual([PackageRef("Lantern.Json", "14.0.0", "central")], project.packages)

    def test_a_conditional_remove_removes_only_for_the_frameworks_its_conditions_hold_for(self) -> None:
        # One Remove of each kind under a condition, and the view with no restore, whose one target stands for every
        # build of the project.
        lib = (self.root / "src" / "Lib" / "Lib.csproj").resolve()
        items = (
            '<ItemGroup><ProjectReference Include="..\\Lib\\Lib.csproj" /><Reference Include="System.Web" />'
            '<PackageReference Include="Lib.Pkg" Version="1.0.0" /></ItemGroup>'
            "<ItemGroup{0}>"
            '<ProjectReference Remove="..\\Lib\\Lib.csproj" /><Reference Remove="System.Web" />'
            '<PackageReference Remove="Lib.Pkg" /></ItemGroup>'
        )
        net472 = " Condition=\"'$(TargetFramework)'=='net472'\""
        cases = [
            # Removed for net472 alone: net8.0 keeps each item, and so does the view with no restore.
            ("net8.0;net472", net472, {"net8.0": True, "net472": False}, True),
            # Removed for the second framework alone, likewise.
            ("net472;net8.0", net472, {"net472": False, "net8.0": True}, True),
            # A condition that holds for every framework the project names removes everywhere.
            ("net8.0;net472", " Condition=\"$(TargetFramework.StartsWith('net'))\"", {"net8.0": False}, False),
            ("net472", net472, {"net472": False}, False),
            # An unconditional Remove removes everywhere.
            ("net8.0;net472", "", {"net8.0": False, "net472": False}, False),
            # A condition the reader cannot evaluate removes nowhere, and per framework it is named.
            ("net8.0;net472", " Condition=\"'$(Configuration)'=='Debug'\"", {"net8.0": True}, True),
        ]
        for frameworks, condition, per_framework, kept in cases:
            with self.subTest(frameworks=frameworks, condition=condition):
                body = f"<PropertyGroup><TargetFrameworks>{frameworks}</TargetFrameworks></PropertyGroup>"
                project = self.read(
                    "src/App/App.csproj", f'<Project Sdk="Microsoft.NET.Sdk">{body}{items.format(condition)}</Project>'
                )
                self.assertEqual(
                    ([lib], ["System.Web"], ["Lib.Pkg"]) if kept else ([], [], []),
                    (
                        project.project_refs,
                        [item.name for item in project.assemblies],
                        [item.id for item in project.packages],
                    ),
                )
                for framework, held in per_framework.items():
                    build = project.build(framework)
                    self.assertEqual([lib] if held else [], build.project_refs)
                    self.assertEqual(
                        ["'$(Configuration)'=='Debug'"] * 3 if "Configuration" in condition else [],
                        [item.condition for item in build.unsettled],
                    )
                    self.assertEqual(["System.Web"] if held else [], [item.name for item in build.assemblies])
                    self.assertEqual(["Lib.Pkg"] if held else [], [item.id for item in build.packages])

    def test_with_no_restore_a_remove_it_cannot_settle_in_a_build_is_named_with_that_build(self) -> None:
        # With no restore, a Remove applies only where it holds in every build, so one that holds for one framework
        # alone, the first or the second, or fails for one, is kept with nothing to name; one that fails for none but
        # cannot be settled in one is kept and named with the first such build.
        lib = (self.root / "src" / "Lib" / "Lib.csproj").resolve()
        debug = "'$(Configuration)'=='Debug'"
        items = (
            '<ItemGroup><ProjectReference Include="..\\Lib\\Lib.csproj" /><Reference Include="System.Web" />'
            '<PackageReference Include="Lib.Pkg" Version="1.0.0" /></ItemGroup>'
            '<ItemGroup Condition="{0}">'
            '<ProjectReference Remove="..\\Lib\\Lib.csproj" /><Reference Remove="System.Web" />'
            '<PackageReference Remove="Lib.Pkg" /></ItemGroup>'
        )
        cases = [
            ("'$(TargetFramework)'=='net8.0'", []),
            ("'$(TargetFramework)'=='net472'", []),
            (f"'$(TargetFramework)'=='net472' and {debug}", []),
            (f"'$(TargetFramework)'=='net8.0' and {debug}", []),
            (f"'$(TargetFramework)'=='net472' or {debug}", ["net8.0"]),
            (f"'$(TargetFramework)'=='net8.0' or {debug}", ["net472"]),
        ]
        body = "<PropertyGroup><TargetFrameworks>net8.0;net472</TargetFrameworks></PropertyGroup>"
        for condition, named in cases:
            with self.subTest(condition=condition):
                project = self.read(
                    "src/App/App.csproj", f'<Project Sdk="Microsoft.NET.Sdk">{body}{items.format(condition)}</Project>'
                )
                self.assertEqual(
                    ([lib], ["System.Web"], ["Lib.Pkg"]),
                    (
                        project.project_refs,
                        [item.name for item in project.assemblies],
                        [i.id for i in project.packages],
                    ),
                )
                self.assertEqual(
                    [
                        (kind, "Remove", condition, framework)
                        for framework in named
                        for kind in ("ProjectReference", "Reference", "PackageReference")
                    ],
                    [(item.kind, item.operation, item.condition, item.framework) for item in project.unsettled],
                )
        # A central PackageVersion's Remove is decided the same way, for the builds of the project.
        self.write(
            "Directory.Packages.props",
            '<Project><ItemGroup><PackageVersion Include="Lib.Pkg" Version="1.0.0" />'
            f'<PackageVersion Remove="Lib.Pkg" Condition="{debug}" /></ItemGroup></Project>',
        )
        text = f'<Project Sdk="Microsoft.NET.Sdk">{body}<ItemGroup><PackageReference Include="Lib.Pkg" /></ItemGroup>'
        project = self.read("src/App/App.csproj", f"{text}</Project>")
        self.assertEqual([PackageRef("Lib.Pkg", "1.0.0", "central")], project.packages)
        self.assertEqual(
            [("PackageVersion", "Remove", debug, "net8.0")],
            [(item.kind, item.operation, item.condition, item.framework) for item in project.unsettled],
        )
        # A central Remove under a framework condition removes the version only where every build removes it.
        self.write(
            "Directory.Packages.props",
            '<Project><ItemGroup><PackageVersion Include="Lib.Pkg" Version="1.0.0" />'
            "<PackageVersion Remove=\"Lib.Pkg\" Condition=\"'$(TargetFramework)'=='net472'\" /></ItemGroup></Project>",
        )
        project = self.read("src/App/App.csproj")
        self.assertEqual(([PackageRef("Lib.Pkg", "1.0.0", "central")], []), (project.packages, project.unsettled))
        self.assertEqual([PackageRef("Lib.Pkg", None, "PackageReference")], project.build("net472").packages)

    def test_a_choose_chooses_its_branch_with_the_values_it_is_entered_with(self) -> None:
        # MSBuild tests a Choose's Whens once, when it enters the Choose, so a When's items hold for the framework a
        # later group sets when the When held for the one before it; an item's own condition reads the final value.
        project = self.read("src/App/App.csproj", ENTERED_CHOOSE_PROJECT)
        self.assertEqual(["net472"], project.frameworks)
        build = project.build("net472")
        self.assertEqual(
            (["Lantern.Entered", "Lantern.Final"], []), ([item.id for item in build.packages], build.unsettled)
        )
        # A framework TargetFrameworks names is fixed for its build from the start, whatever the file sets after.
        project = self.read("src/App/App.csproj", APART_CHOOSE_PROJECT)
        self.assertEqual(["net8.0", "net472"], project.frameworks)
        for framework, expected in (("net8.0", ["Lantern.Other"]), ("net472", ["Lantern.Apart"])):
            with self.subTest(framework=framework):
                self.assertEqual(expected, [item.id for item in project.build(framework).packages])
        # A Choose entered while the framework may hold one of several values cannot be chosen, and its items are
        # named, not applied.
        text = ENTERED_CHOOSE_PROJECT.replace("<PropertyGroup>", "<PropertyGroup Condition=\"'$(OS)'=='Unix'\">", 1)
        build = self.read("src/App/App.csproj", text).build("net472")
        self.assertEqual(["Lantern.Final"], [item.id for item in build.packages])
        entered = "'$(TargetFramework)'=='net8.0'"
        self.assertEqual(
            [("Lantern.Entered", entered), ("Lantern.Other", entered)], [(i.name, i.condition) for i in build.unsettled]
        )

    def test_items_inside_a_target_are_left_out_and_named(self) -> None:
        project = self.read("src/Harbor.Late/Harbor.Late.csproj", TARGET_PROJECT)
        self.assertEqual([PackageRef("Quill.Http", "1.2.0", "PackageReference")], project.packages)
        self.assertEqual(([], [], ["net8.0"]), (project.project_refs, project.assemblies, project.frameworks))
        late = "inside Target {} is left out: MSBuild defines it only when that target runs"
        self.assertEqual(
            [
                f"the PackageReference Beacon.Late {late.format('AddLate')}",
                f"the ProjectReference ..\\Harbor.Late.Core\\Harbor.Late.Core.csproj {late.format('AddLate')}",
                f"the Reference System.Late {late.format('AddLate')}",
                f"the PackageReference Quill.Http {late.format('DropQuill')}",
            ],
            [report.reason for report in project.reports],
        )
        # A central file's Target is reported against that file, and its other items still count.
        props = self.write("Directory.Packages.props", TARGET_CENTRAL)
        text = '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup><PackageReference Include="Lantern.Json" /></ItemGroup>'
        project = self.read("src/App/App.csproj", f"{text}</Project>")
        self.assertEqual([PackageRef("Lantern.Json", "13.0.1", "central")], project.packages)
        self.assertEqual(
            [(props, f"the PackageVersion Beacon.Late {late.format('Pin')}")],
            [(report.file, report.reason) for report in project.reports],
        )

    def test_a_framework_property_counts_where_its_conditions_hold(self) -> None:
        # Each case is a project body, the frameworks it names, and its assembly name, which the later groups set
        # where their conditions hold at that point, so the name shows what each condition read there.
        cases = [
            # A property under a condition that fails names nothing, and one that holds replaces the value before it.
            (
                "<PropertyGroup><TargetFrameworks>net8.0;net472</TargetFrameworks></PropertyGroup>"
                "<PropertyGroup Condition=\"'$(TargetFramework)' == 'net8.0'\">"
                "<TargetFrameworks>net6.0</TargetFrameworks></PropertyGroup>",
                ["net8.0", "net472"],
                "a",
            ),
            (
                "<PropertyGroup><TargetFrameworks>net8.0;net472</TargetFrameworks></PropertyGroup>"
                "<PropertyGroup><TargetFrameworks>net9.0</TargetFrameworks></PropertyGroup>",
                ["net9.0"],
                "a",
            ),
            # `$(TargetFramework)` is empty until the file sets it.
            (
                "<PropertyGroup Condition=\"'$(TargetFramework)' == ''\"><TargetFramework>net8.0</TargetFramework>"
                "</PropertyGroup><PropertyGroup Condition=\"'$(TargetFramework)' == 'net8.0'\">"
                "<AssemblyName>Seen</AssemblyName></PropertyGroup>",
                ["net8.0"],
                "Seen",
            ),
            # A condition the reader cannot evaluate adds its names to those it may replace, never choosing.
            (
                "<PropertyGroup><TargetFrameworks>net8.0</TargetFrameworks></PropertyGroup>"
                "<PropertyGroup Condition=\"'$(OS)' == 'Windows_NT'\">"
                "<TargetFrameworks>net8.0;net472</TargetFrameworks></PropertyGroup>",
                ["net8.0", "net472"],
                "a",
            ),
            # A Choose selects its branch once, when it is reached, so a When that sets the property it tests never
            # hands over to the Otherwise.
            (
                "<Choose><When Condition=\"'$(TargetFramework)'==''\"><PropertyGroup><TargetFramework>net8.0"
                "</TargetFramework></PropertyGroup></When><Otherwise><PropertyGroup><TargetFramework>net472"
                "</TargetFramework></PropertyGroup></Otherwise></Choose>",
                ["net8.0"],
                "a",
            ),
            # Every When of a Choose is tested before any branch applies: a later When is not tested against what an
            # earlier one the reader cannot evaluate may set.
            (
                "<Choose><When Condition=\"'$(Configuration)' == 'Debug'\"><PropertyGroup>"
                "<TargetFramework>net8.0</TargetFramework></PropertyGroup></When>"
                "<When Condition=\"'$(TargetFramework)'==''\"><PropertyGroup><TargetFramework>net472"
                "</TargetFramework></PropertyGroup></When></Choose>",
                ["net8.0", "net472"],
                "a",
            ),
            # Each branch that may apply is read from the state the Choose was entered with: the Otherwise sees the
            # framework still unset, whatever the When it may follow sets, and both names join.
            (
                "<Choose><When Condition=\"'$(Configuration)'=='Debug'\"><PropertyGroup>"
                "<TargetFramework>net8.0</TargetFramework></PropertyGroup></When><Otherwise>"
                "<PropertyGroup Condition=\"'$(TargetFramework)'==''\"><TargetFramework>net472</TargetFramework>"
                "</PropertyGroup></Otherwise></Choose>",
                ["net8.0", "net472"],
                "a",
            ),
            # After a property the reader cannot tell is set, or a Choose none of whose branches may apply, the
            # framework may still be unset, so a later condition on it cannot be evaluated and its names join.
            (
                "<PropertyGroup Condition=\"'$(Configuration)'=='Debug'\"><TargetFramework>net8.0</TargetFramework>"
                "</PropertyGroup><PropertyGroup Condition=\"'$(TargetFramework)'==''\">"
                "<TargetFramework>net472</TargetFramework></PropertyGroup>",
                ["net8.0", "net472"],
                "a",
            ),
            (
                "<Choose><When Condition=\"'$(Configuration)'=='Debug'\"><PropertyGroup>"
                "<TargetFramework>net8.0</TargetFramework></PropertyGroup></When></Choose>"
                "<PropertyGroup Condition=\"'$(TargetFramework)'==''\"><TargetFramework>net472</TargetFramework>"
                "</PropertyGroup>",
                ["net8.0", "net472"],
                "a",
            ),
            # A Choose inside a When chooses its own branch when it is reached, and a PropertyGroup inside a When
            # applies only where its own condition holds.
            (
                "<Choose><When Condition=\"'$(TargetFramework)'==''\"><Choose>"
                "<When Condition=\"'$(TargetFramework)'==''\"><PropertyGroup><TargetFramework>net8.0"
                "</TargetFramework></PropertyGroup></When><Otherwise><PropertyGroup><TargetFramework>net472"
                "</TargetFramework></PropertyGroup></Otherwise></Choose>"
                "<PropertyGroup Condition=\"'$(TargetFramework)'=='net472'\"><AssemblyName>Wrong"
                "</AssemblyName></PropertyGroup>"
                "<PropertyGroup Condition=\"'$(TargetFramework)'=='net8.0'\"><AssemblyName>Inner"
                "</AssemblyName></PropertyGroup></When></Choose>",
                ["net8.0"],
                "Inner",
            ),
            # A PropertyGroup's condition is evaluated once, when it is reached, not again for each property in it.
            (
                "<PropertyGroup Condition=\"'$(TargetFramework)'==''\"><TargetFramework>net8.0</TargetFramework>"
                "<AssemblyName>Once</AssemblyName></PropertyGroup>",
                ["net8.0"],
                "Once",
            ),
            # An SDK-style project names its framework by TargetFramework, whatever TargetFrameworkVersion says.
            (
                "<PropertyGroup><TargetFramework>net8.0</TargetFramework>"
                "<TargetFrameworkVersion>v4.7.2</TargetFrameworkVersion></PropertyGroup>",
                ["net8.0"],
                "a",
            ),
            # A property's own condition is evaluated when the property is reached.
            (
                "<PropertyGroup><TargetFramework>net8.0</TargetFramework>"
                "<AssemblyName Condition=\"'$(TargetFramework)'=='net8.0'\">Own</AssemblyName>"
                "</PropertyGroup>",
                ["net8.0"],
                "Own",
            ),
        ]
        for body, frameworks, name in cases:
            with self.subTest(body=body):
                project = self.read("src/a/a.csproj", f'<Project Sdk="Microsoft.NET.Sdk">{body}</Project>')
                self.assertEqual((frameworks, name), (project.frameworks, project.assembly_name))

    def test_a_when_without_a_condition_makes_the_file_unreadable(self) -> None:
        choose = (
            '<Choose><When><ItemGroup><PackageReference Include="Alpha.Pkg" Version="1.0.0" /></ItemGroup></When>'
            '<Otherwise><ItemGroup><PackageReference Include="Gamma.Pkg" Version="1.0.0" /></ItemGroup></Otherwise>'
            "</Choose>"
        )
        reason = "a When has no Condition, which MSBuild refuses, so the file does not evaluate"
        with self.assertRaises(Unreadable) as refused:
            self.read("src/a/a.csproj", f"<Project>{choose}</Project>")
        self.assertEqual(
            (self.root / "src" / "a" / "a.csproj", reason), (refused.exception.file, refused.exception.reason)
        )
        # A central file MSBuild refuses is reported against itself, and the project keeps its own facts.
        props = self.write(
            "Directory.Packages.props", f"<Project>{choose.replace('PackageReference', 'PackageVersion')}</Project>"
        )
        text = (
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>'
        )
        project = self.read(
            "src/b/b.csproj", f'{text}<ItemGroup><PackageReference Include="Alpha.Pkg" /></ItemGroup></Project>'
        )
        self.assertEqual([(props, reason)], [(report.file, report.reason) for report in project.reports])
        self.assertEqual(
            ([PackageRef("Alpha.Pkg", None, "PackageReference")], ["net8.0"]), (project.packages, project.frameworks)
        )

    def test_each_file_is_parsed_once_and_each_build_evaluated_once(self) -> None:
        # A project of two frameworks is evaluated once for its files' own values and once per framework, its central
        # file parsed once for every project that imports it, and asking for a build again evaluates nothing.
        self.write("Directory.Packages.props", CENTRAL)
        body = "<PropertyGroup><TargetFrameworks>net8.0;net472</TargetFrameworks></PropertyGroup>"
        sources = Sources(self.root)
        parsed: list[Path] = []
        evaluated: list[object] = []
        parse = msbuild_evaluation.parse_xml
        evaluation = msbuild_evaluation.Evaluation.__init__

        def parse_counting(path: Path) -> object:
            parsed.append(path)
            return parse(path)

        def evaluate_counting(self: object, *arguments: object) -> None:
            evaluated.append(arguments)
            evaluation(self, *arguments)  # type: ignore[arg-type]  # the arguments the evaluator passes through

        with (
            mock.patch.object(msbuild_evaluation, "parse_xml", parse_counting),
            mock.patch.object(msbuild_evaluation.Evaluation, "__init__", evaluate_counting),
        ):
            for name in ("A", "B"):
                path = self.write(f"src/{name}/{name}.csproj", f'<Project Sdk="Microsoft.NET.Sdk">{body}</Project>')
                project = read_project(path, sources)
                project.build("net8.0")
                project.build("net472")
        self.assertEqual(6, len(evaluated))
        self.assertEqual(
            sorted(["Directory.Packages.props", "A.csproj", "B.csproj"]), sorted(path.name for path in parsed)
        )
