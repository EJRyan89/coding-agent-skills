"""Regression tests for the readers of the files a dependency graph is built from."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import msbuild_reader
from msbuild_reader import (
    PackageItem,
    PackageRef,
    TargetItem,
    Unreadable,
    assemblies_for,
    central_versions_file,
    condition_holds,
    framework_key,
    matching_framework,
    package_assemblies,
    packages_for,
    project_refs_for,
    read_assets,
    read_central_versions,
    read_nuspec,
    read_packages_config,
    read_project,
    read_redirects,
    same_framework,
)
from xml_reader import read_xml

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
PACKAGES_CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<packages>
  <package id="Lantern.Core" version="2.0.0" targetFramework="net472" />
  <package id="Lantern.Json" version="13.0.1" targetFramework="net472" />
  <package version="0.1" />
</packages>
"""
WEB_CONFIG = """<?xml version="1.0"?>
<configuration>
  <runtime>
    <assemblyBinding xmlns="urn:schemas-microsoft-com:asm.v1">
      <dependentAssembly>
        <assemblyIdentity name="Lantern.Json" publicKeyToken="abc" culture="neutral" />
        <bindingRedirect oldVersion="0.0.0.0-13.0.0.0" newVersion="13.0.0.0" />
      </dependentAssembly>
      <dependentAssembly>
        <assemblyIdentity name="Quill.Http" />
        <bindingRedirect oldVersion="0.0.0.0-2.0.0.0" newVersion="2.0.0.0" />
      </dependentAssembly>
      <dependentAssembly>
        <bindingRedirect oldVersion="0.0.0.0-1.0.0.0" newVersion="1.0.0.0" />
      </dependentAssembly>
    </assemblyBinding>
  </runtime>
</configuration>
"""
NUSPEC = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd">
  <metadata>
    <id>Lantern.Core</id>
    <version>2.0.0</version>
    <dependencies>
      <group targetFramework=".NETFramework4.7.2">
        <dependency id="Lantern.Json" version="[12.0.0, )" />
      </group>
      <group targetFramework=".NETStandard2.0">
        <dependency id="Lantern.Json" version="[12.0.0, )" />
        <dependency id="Quill.Http" version="1.2.0" />
      </group>
    </dependencies>
  </metadata>
</package>
"""
ASSETS = {
    "version": 3,
    "targets": {
        "net8.0": {
            "Lantern.Json/12.0.3": {
                "type": "package",
                "compile": {"lib/netstandard2.0/Lantern.Json.dll": {}},
            },
            "Quill.Http/1.2.0": {
                "type": "package",
                "dependencies": {"Lantern.Json": "12.0.3"},
                "compile": {"lib/net8.0/Quill.Http.dll": {}, "lib/net8.0/Quill.Http.Abstractions.dll": {}},
            },
            "Harbor.Core/1.0.0": {"type": "project", "dependencies": {"Quill.Http": "1.2.0"}},
        },
        "net8.0/win-x64": {"Lantern.Json/12.0.3": {"type": "package"}},
    },
    "libraries": {},
    "project": {
        "frameworks": {
            "net8.0": {
                "dependencies": {
                    "Quill.Http": {"target": "Package", "version": "[1.2.0, )"},
                    "Harbor.Core": {"target": "Project"},
                }
            }
        }
    },
    "packageFolders": {"C:\\\\cache\\\\packages\\\\": {}},
}


class ReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dotnet-dependencies-reader-"))
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

    def central(self, directory: Path) -> list[PackageItem]:
        found = central_versions_file(directory, self.root)
        return read_central_versions(found).items if found else []

    def test_legacy_project_gives_assembly_references_with_version_and_hint_path(self) -> None:
        project = read_project(self.write("src/Harbor.Web/Harbor.Web.csproj", LEGACY_PROJECT), self.central)
        self.assertEqual("legacy", project.style)
        self.assertEqual("Harbor.Web", project.assembly_name)
        self.assertEqual(["v4.7.2"], project.frameworks)
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
        project = read_project(self.write("src/Harbor.Core/Harbor.Core.csproj", SDK_PROJECT), self.central)
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
        project = read_project(self.write("src/Harbor.Api/Harbor.Api.csproj", CONDITIONAL_PROJECT), self.central)
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
                found, unevaluated = packages_for(project.package_items, framework)
                self.assertEqual(references, [(item.id, item.version) for item in found])
                # A condition on another property is not guessed: the item is left out and named.
                self.assertEqual([("Beacon.Debug", debug)], [(item.id, text) for item, text, _ in unevaluated])
        # With no framework, as for a project with no restore, every item counts whatever its condition.
        self.assertEqual(
            [
                "Quill.Http",
                "Lantern.Tuple",
                "Lantern.Span",
                "Lantern.Modern",
                "Lantern.Classic",
                "Beacon.Debug",
                "Lantern.Chosen",
                "Lantern.Other",
            ],
            [item.id for item in project.packages],
        )
        self.assertEqual(project.packages, packages_for(project.package_items, None)[0])

    def test_conditions_are_evaluated_only_in_the_forms_the_reader_knows(self) -> None:
        holds = [
            ("'$(TargetFramework)'=='net472'", "net472"),
            ("'$(TargetFramework)' == 'NET472'", "net472"),
            ("'net8.0' == '$(TargetFramework)'", "net8.0"),
            ("$(TargetFramework) == 'net8.0'", "net8.0"),
            ("('$(TargetFramework)' == 'a' or '$(TargetFramework)' == 'b') AND '$(TargetFramework)' != 'c'", "b"),
            ("$(TargetFramework.StartsWith('net4'))", "net472"),
            ("'' == ''", "net8.0"),
        ]
        fails = [
            ("'$(TargetFramework)'=='net472'", "net8.0"),
            ("'$(TargetFramework)' != 'net8.0'", "net8.0"),
            ("$(TargetFramework.StartsWith('net4'))", "net8.0"),
            ("'$(TargetFramework)' == 'a' and '$(TargetFramework)' == 'b'", "a"),
        ]
        unknown = [
            ("'$(Configuration)' == 'Debug'", "net8.0"),
            ("'$(TargetFrameworkIdentifier)' == '.NETFramework'", "net8.0"),
            ("$([MSBuild]::IsTargetFrameworkCompatible('$(TargetFramework)', 'net6.0'))", "net8.0"),
            ("Exists('packages.lock.json')", "net8.0"),
            ("'$(TargetFramework)' ==", "net8.0"),
            ("('$(TargetFramework)' == 'net8.0'", "net8.0"),
            ("", "net8.0"),
            # A restore that names the framework by its long name cannot be compared with a short one, in either form
            # a condition reads the property.
            ("'$(TargetFramework)' == 'net472'", ".NETFramework,Version=v4.7.2"),
            ("$(TargetFramework.StartsWith('net4'))", ".NETFramework,Version=v4.7.2"),
        ]
        for expected, cases in ((True, holds), (False, fails), (None, unknown)):
            for text, framework in cases:
                with self.subTest(text=text, framework=framework):
                    self.assertIs(expected, condition_holds(text, framework))

    def test_a_choose_applies_its_first_when_that_holds_else_its_otherwise(self) -> None:
        project = read_project(self.write("src/Harbor.Choice/Harbor.Choice.csproj", CHOOSE_PROJECT), self.central)
        debug = "'$(Configuration)' == 'Debug'"
        cases = {
            # Both Whens of the first Choose hold for net472, and only the first applies. In the second, a When after
            # one that cannot be evaluated is not evaluated either, and the Otherwise is ruled out because a When
            # holds, whatever the first one is.
            "net472": (["Lantern.First"], [("Beacon.Unsure", debug), ("Beacon.Later", debug)]),
            # The first When fails and the second holds; in the second Choose the Otherwise is not evaluated.
            "netstandard2.0": (["Lantern.Second"], [("Beacon.Unsure", debug), ("Beacon.Rest", debug)]),
            # No When of the first Choose holds, so its Otherwise applies.
            "uap10.0": (["Lantern.Rest"], [("Beacon.Unsure", debug), ("Beacon.Rest", debug)]),
        }
        for framework, (references, unevaluated) in cases.items():
            with self.subTest(framework=framework):
                found, skipped = packages_for(project.package_items, framework)
                self.assertEqual(references, [item.id for item in found])
                self.assertEqual(unevaluated, [(item.id, text) for item, text, _ in skipped])

    def test_an_include_update_or_remove_lists_each_item_it_names(self) -> None:
        project = read_project(self.write("src/Harbor.List/Harbor.List.csproj", LIST_PROJECT), self.central)
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

    def test_a_framework_is_one_framework_whichever_form_names_it(self) -> None:
        same = [
            ("net8.0", ".NETCoreApp,Version=v8.0"),
            ("netcoreapp3.1", ".NETCoreApp,Version=v3.1"),
            ("net472", ".NETFramework,Version=v4.7.2"),
            ("NET48", ".netframework,version=v4.8"),
            ("net40", ".NETFramework,Version=v4.0"),
            ("net40-client", ".NETFramework,Version=v4.0,Profile=Client"),
            ("netstandard2.0", ".NETStandard,Version=v2.0"),
            # A restore writes out the platform version a project leaves to the default.
            ("net8.0-windows", "net8.0-windows7.0"),
            ("net8.0-windows7.0", "NET8.0-Windows7.0"),
            # A restore drops a platform version's trailing zero parts down to two parts.
            ("net8.0-windows10.0.19041.0", "net8.0-windows10.0.19041"),
            ("net8.0-windows10", "net8.0-windows10.0"),
            ("net8.0-windows7.0.0", "net8.0-windows"),
            # A workload sets the default of a platform other than Windows, so any version may be it.
            ("net8.0-android", "net8.0-android34.0"),
        ]
        different = [
            ("net8.0", "netcoreapp8.0-x"),
            ("net472", ".NETFramework,Version=v4.7.1"),
            ("netcoreapp3.1", ".NETStandard,Version=v3.1"),
            ("net8.0", "net8.0-windows"),
            ("net8.0-windows10.0", "net8.0-windows7.0"),
            # A name that leaves the version to the default is never one with another version.
            ("net8.0-windows", "net8.0-windows10.0.19041"),
            ("net8.0-windows", "net8.0-windows10.0.19041.0"),
            ("net8.0-windows10.0.19041", "net8.0-windows10.0.22621.0"),
            ("net8.0-android34.0", "net8.0-android35.0"),
            ("net8.0-android", "net8.0-ios"),
            ("net9.0-windows", "net8.0-windows7.0"),
            (".NETFoo,Version=v1.0", "foo1.0"),
        ]
        for expected, cases in ((True, same), (False, different)):
            for first, second in cases:
                with self.subTest(first=first, second=second):
                    self.assertIs(expected, same_framework(first, second))
                    self.assertIs(expected, same_framework(second, first))
        self.assertEqual(".netfoo,version=v1.0", framework_key(".NETFoo,Version=v1.0"))

    def test_a_restore_keyed_by_long_names_is_keyed_by_the_projects_own_names(self) -> None:
        tuple_reference = {"Lantern.Tuple": {"target": "Package", "version": "[4.5.0, )"}}
        targets = {
            ".NETCoreApp,Version=v8.0": {},
            ".NETFramework,Version=v4.7.2": {"Lantern.Tuple/4.5.0": {"type": "package"}},
            ".NETFramework,Version=v4.7.2/win-x86": {"Lantern.Tuple/4.5.0": {"type": "package"}},
            "net8.0-windows7.0": {},
        }
        document = {
            "version": 3,
            "targets": targets,
            "project": {
                "frameworks": {
                    "net8.0": {},
                    "net472": {"dependencies": tuple_reference},
                    "net8.0-windows": {"targetAlias": "net8.0-windows"},
                }
            },
        }
        path = self.write("obj/project.assets.json", json.dumps(document))
        names = ["net8.0", "net472", "net8.0-windows"]
        assets = read_assets(path)
        self.assertEqual(names, list(assets.nodes))
        self.assertEqual(["Lantern.Tuple"], list(assets.nodes["net472"]))
        self.assertEqual(
            {
                "net8.0": [],
                "net472": [PackageRef("Lantern.Tuple", "[4.5.0, )", "PackageReference")],
                "net8.0-windows": [],
            },
            assets.direct,
        )
        # Without a project section the project file's own names key the restore, so its conditions read as written.
        path = self.write("obj/project.assets.json", json.dumps({**document, "project": {}}))
        self.assertEqual(
            ["NET8.0", "net472", "net8.0-windows"],
            list(read_assets(path, ["NET8.0", "net472", "net8.0-windows"]).nodes),
        )
        # A target no name matches keeps the name the restore wrote.
        self.assertEqual(
            [".NETCoreApp,Version=v8.0", ".NETFramework,Version=v4.7.2", "net8.0-windows7.0"],
            list(read_assets(path).nodes),
        )

    def test_a_project_naming_one_platform_with_and_without_a_version_keeps_both_targets(self) -> None:
        # Each pair is what the project declares and what its restore writes for each, in order.
        cases = [
            (["net8.0-windows", "net8.0-windows10.0.19041.0"], ["net8.0-windows7.0", "net8.0-windows10.0.19041"]),
            (["net8.0-windows", "net8.0-windows10.0.19041.0"], ["net8.0-windows7.0", "net8.0-windows10.0.19041.0"]),
            (["net8.0-windows10.0.19041.0", "net8.0-windows"], ["net8.0-windows10.0.19041", "net8.0-windows7.0"]),
            # A workload's default is unknown, so only the name spelled alike tells the two apart.
            (["net8.0-android", "net8.0-android34.0"], ["net8.0-android35.0", "net8.0-android34.0"]),
            (["net8.0-android34.0", "net8.0-android"], ["net8.0-android34.0", "net8.0-android35.0"]),
        ]
        packages = ["Lantern.Wpf", "Lantern.WinRT"]
        for declared, written in cases:
            with self.subTest(declared=declared, written=written):
                targets = {
                    name: {f"{package}/1.0.0": {"type": "package"}}
                    for name, package in zip(written, packages, strict=True)
                }
                sections = {
                    name: {"dependencies": {package: {"target": "Package", "version": "[1.0.0, )"}}}
                    for name, package in zip(declared, packages, strict=True)
                }
                for project in ({"frameworks": sections}, {}):
                    document = {"version": 3, "targets": targets, "project": project}
                    path = self.write("obj/project.assets.json", json.dumps(document))
                    assets = read_assets(path, declared)
                    self.assertEqual(
                        {name: [package] for name, package in zip(declared, packages, strict=True)},
                        {name: list(nodes) for name, nodes in assets.nodes.items()},
                    )
                    direct = {
                        name: [PackageRef(package, "[1.0.0, )", "PackageReference")]
                        for name, package in zip(declared, packages, strict=True)
                    }
                    self.assertEqual(direct if project else {}, assets.direct)

    def test_a_name_is_found_among_names_spelled_alike_first(self) -> None:
        names = ["net8.0-android", "net8.0-android34.0", "net8.0-windows", "NET8.0-Windows10.0.19041.0"]
        self.assertEqual("net8.0-android34.0", matching_framework("net8.0-android34.0", names))
        self.assertEqual("net8.0-android", matching_framework("net8.0-android35.0", names))
        self.assertEqual("net8.0-windows", matching_framework("net8.0-windows7.0", names))
        self.assertEqual("NET8.0-Windows10.0.19041.0", matching_framework("net8.0-windows10.0.19041", names))
        self.assertIsNone(matching_framework("net8.0-windows10.0.22621.0", names))
        self.assertIsNone(matching_framework("net8.0", names))

    def test_central_versions_stop_at_the_solution_directory(self) -> None:
        props = self.write("Directory.Packages.props", CENTRAL)
        self.assertIsNone(central_versions_file(self.root / "src", self.root / "src"))
        self.assertEqual(props, central_versions_file(self.root / "src", self.root))
        self.assertEqual(
            [("Lantern.Core", "2.0.0", "central"), ("Lantern.Text", "2.5.0", "central")],
            [(item.id, item.version, item.origin) for item in read_central_versions(props).items],
        )
        with self.assertRaises(Unreadable) as refused:
            read_central_versions(self.write("Directory.Packages.props", "<!DOCTYPE x><Project />"))
        self.assertIn("DOCTYPE", refused.exception.reason)

    def test_sdk_element_and_sdk_import_count_as_sdk_style(self) -> None:
        for text in (
            '<Project><Sdk Name="Microsoft.NET.Sdk" /><PropertyGroup><TargetFramework>net8.0</TargetFramework>'
            "</PropertyGroup></Project>",
            '<Project><Import Project="Sdk.props" Sdk="Microsoft.NET.Sdk" /></Project>',
        ):
            self.assertEqual("sdk", read_project(self.write("a/a.csproj", text), self.central).style)
        self.assertEqual("legacy", read_project(self.write("b/b.csproj", "<Project></Project>"), self.central).style)

    def test_packages_config_lists_each_package_with_its_version_and_framework(self) -> None:
        self.assertEqual(
            [
                PackageRef("Lantern.Core", "2.0.0", "packages.config", "net472"),
                PackageRef("Lantern.Json", "13.0.1", "packages.config", "net472"),
            ],
            read_packages_config(self.write("packages.config", PACKAGES_CONFIG)),
        )

    def test_redirects_name_the_assembly_and_both_versions_and_skip_one_without_an_identity(self) -> None:
        config = self.write("web.config", WEB_CONFIG)
        self.assertEqual(
            [("Lantern.Json", "0.0.0.0-13.0.0.0", "13.0.0.0"), ("Quill.Http", "0.0.0.0-2.0.0.0", "2.0.0.0")],
            [(item.name, item.old_version, item.new_version) for item in read_redirects(config)],
        )
        self.assertEqual(config, read_redirects(config)[0].config)

    def test_nuspec_gives_the_package_and_every_dependency_once_across_groups(self) -> None:
        node = read_nuspec(self.write("Lantern.Core.2.0.0/Lantern.Core.nuspec", NUSPEC), ("Lantern.Core",))
        self.assertEqual(("Lantern.Core", "2.0.0"), (node.id, node.version))
        self.assertEqual({"Lantern.Json": "[12.0.0, )", "Quill.Http": "1.2.0"}, node.dependencies)
        self.assertEqual(("Lantern.Core",), node.assemblies)
        with self.assertRaises(Unreadable) as refused:
            read_nuspec(self.write("bad/bad.nuspec", "<package><metadata><id>x</id></metadata></package>"))
        self.assertIn("names no package id and version", refused.exception.reason)

    def test_package_assemblies_are_the_dll_stems_under_lib(self) -> None:
        self.write("pkg/lib/net45/Lantern.Json.dll", "")
        self.write("pkg/lib/netstandard2.0/Lantern.Json.dll", "")
        self.write("pkg/lib/net45/Lantern.Json.Extras.dll", "")
        self.write("pkg/tools/Other.dll", "")
        self.assertEqual(("Lantern.Json", "Lantern.Json.Extras"), package_assemblies(self.root / "pkg"))
        self.assertEqual((), package_assemblies(self.root / "nowhere"))

    def test_assets_give_each_frameworks_packages_and_direct_dependencies(self) -> None:
        assets = read_assets(self.write("obj/project.assets.json", json.dumps(ASSETS)))
        self.assertEqual(["net8.0"], list(assets.nodes))
        self.assertEqual(["Lantern.Json", "Quill.Http"], sorted(assets.nodes["net8.0"]))
        quill = assets.nodes["net8.0"]["Quill.Http"]
        self.assertEqual(
            ("Quill.Http", "1.2.0", {"Lantern.Json": "12.0.3"}), (quill.id, quill.version, quill.dependencies)
        )
        self.assertEqual(("Quill.Http", "Quill.Http.Abstractions"), quill.assemblies)
        self.assertEqual({"net8.0": [PackageRef("Quill.Http", "[1.2.0, )", "PackageReference")]}, assets.direct)

    def test_unreadable_files_name_the_file_and_the_reason(self) -> None:
        cases = {
            "<Project><ItemGroup></Project>": "not well-formed",
            "<!DOCTYPE project><Project />": "DOCTYPE",
            '<?xml version="1.0"?><!ENTITY x "y"><Project />': "ENTITY",
        }
        for text, reason in cases.items():
            with self.subTest(reason=reason), self.assertRaises(Unreadable) as refused:
                read_project(self.write("bad/bad.csproj", text), self.central)
            self.assertEqual(self.root / "bad" / "bad.csproj", refused.exception.file)
            self.assertIn(reason, refused.exception.reason)
        with self.assertRaises(Unreadable) as refused:
            read_project(self.root / "missing.csproj", self.central)
        self.assertEqual(self.root / "missing.csproj", refused.exception.file)
        with self.assertRaises(Unreadable):
            read_assets(self.write("obj/project.assets.json", "{not json"))
        with self.assertRaises(Unreadable) as refused:
            read_assets(self.write("obj/project.assets.json", "{}"))
        self.assertIn("no targets", refused.exception.reason)

    def test_one_operand_decides_whatever_its_place_and_a_malformed_condition_is_never_evaluated(self) -> None:
        debug = "'$(Configuration)'=='Debug'"
        mirrored = "'Debug'=='$(Configuration)'"
        framework = "'$(TargetFramework)'=='net472'"
        holds = [
            (f"{framework} or {debug}", "net472"),
            (f"{framework} or {mirrored}", "net472"),
            (f"{debug} or {framework}", "net472"),
            (f"({debug}) or ({framework})", "net472"),
            ("$(TargetFramework.StartsWith('net'))", "net8.0"),
        ]
        fails = [
            (f"{framework} and {debug}", "net8.0"),
            (f"{framework} and {mirrored}", "net8.0"),
            (f"{debug} and {framework}", "net8.0"),
            # MSBuild's StartsWith compares with regard to case.
            ("$(TargetFramework.StartsWith('NET'))", "net8.0"),
        ]
        unknown = [
            # Kept as today: an operand the reader cannot evaluate leaves undecided what no other operand decides.
            (f"{framework} or {debug}", "net8.0"),
            (f"{framework} and {debug}", "net472"),
            # A dangling, leading, or doubled keyword, an unclosed or unopened parenthesis, a lone operand, and a
            # missing operand make a condition MSBuild refuses, whatever its other operands.
            (f"{framework} or or", "net472"),
            (f"or {framework}", "net472"),
            (f"{framework} or", "net472"),
            (f"{framework} and", "net472"),
            (f"{framework} or and {framework}", "net472"),
            (f"{framework})", "net472"),
            ("'net472'", "net472"),
            ("== 'net472'", "net472"),
        ]
        for expected, cases in ((True, holds), (False, fails), (None, unknown)):
            for text, name in cases:
                with self.subTest(text=text, framework=name):
                    self.assertIs(expected, condition_holds(text, name))

    def test_each_item_kind_holds_for_the_frameworks_its_conditions_hold_for(self) -> None:
        self.write("Directory.Packages.props", CONDITIONAL_CENTRAL)
        path = self.write("src/Harbor.Api/Harbor.Api.csproj", CONDITIONAL_ITEMS_PROJECT)
        project = read_project(path, self.central)
        legacy = (self.root / "src" / "Harbor.Legacy" / "Harbor.Legacy.csproj").resolve()
        shared = (self.root / "src" / "Harbor.Shared" / "Harbor.Shared.csproj").resolve()
        debug = "'$(Configuration)' == 'Debug'"
        cases = {
            "net8.0": (
                [shared],
                [("Beacon.Modern", None)],
                [("Lantern.Json", "13.0.1", "central")],
            ),
            "net472": (
                [shared, legacy],
                [("System.Web", None), ("Beacon.Hinted", "lib\\Beacon.Hinted.dll")],
                [("Lantern.Json", "12.0.3", "central")],
            ),
        }
        for framework, (projects, assemblies, packages) in cases.items():
            with self.subTest(framework=framework):
                found, skipped = project_refs_for(project.project_items, framework)
                self.assertEqual(projects, found)
                self.assertEqual([("Remove", debug)], [(item.operation, text) for item, text, _ in skipped])
                references, unevaluated = assemblies_for(project.reference_items, framework)
                self.assertEqual(assemblies, [(item.name, item.hint_path) for item in references])
                self.assertEqual(
                    [("Include", "Beacon.Debug", debug)], [(i.operation, i.spec, t) for i, t, _ in unevaluated]
                )
                refs, left = packages_for(project.package_items, framework, project.central_items)
                self.assertEqual(packages, [(ref.id, ref.version, ref.origin) for ref in refs])
                # A central item whose condition cannot be evaluated is named for a package that takes its version.
                self.assertEqual(
                    [("Update", "Lantern.Json", "central")], [(i.operation, i.id, i.origin) for i, _, _ in left]
                )
        # With no framework, as for a project with no restore, every Include and Update applies whatever its
        # condition, as before; a Remove whose condition cannot be shown to hold for every framework is not made.
        self.assertEqual([shared, legacy], project.project_refs)
        self.assertEqual(
            ["Beacon.Modern", "System.Web", "Beacon.Hinted", "Beacon.Debug"], [item.name for item in project.assemblies]
        )
        self.assertEqual([PackageRef("Lantern.Json", "14.0.0", "central")], project.packages)

    def test_a_conditional_remove_removes_only_for_the_frameworks_its_conditions_hold_for(self) -> None:
        # One Remove of each kind under a condition, and with no framework, as for a project with no restore, whose
        # one target stands for every framework it builds.
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
            # Removed for net472 alone: net8.0 keeps each item, and so does the project's one target.
            ("net8.0;net472", net472, {"net8.0": True, "net472": False}, True),
            # A condition that holds for every framework the project names removes everywhere.
            ("net8.0;net472", " Condition=\"$(TargetFramework.StartsWith('net'))\"", {"net8.0": False}, False),
            ("net472", net472, {"net472": False}, False),
            # An unconditional Remove removes everywhere, as before.
            ("net8.0;net472", "", {"net8.0": False, "net472": False}, False),
            # A condition the reader cannot evaluate removes nowhere, and per framework it is named.
            ("net8.0;net472", " Condition=\"'$(Configuration)'=='Debug'\"", {"net8.0": True}, True),
        ]
        for frameworks, condition, per_framework, kept in cases:
            with self.subTest(frameworks=frameworks, condition=condition):
                body = f"<PropertyGroup><TargetFrameworks>{frameworks}</TargetFrameworks></PropertyGroup>"
                text = f'<Project Sdk="Microsoft.NET.Sdk">{body}{items.format(condition)}</Project>'
                project = read_project(self.write("src/App/App.csproj", text), self.central)
                self.assertEqual(
                    ([lib], ["System.Web"], ["Lib.Pkg"]) if kept else ([], [], []),
                    (
                        project.project_refs,
                        [item.name for item in project.assemblies],
                        [item.id for item in project.packages],
                    ),
                )
                for framework, held in per_framework.items():
                    found, unevaluated = project_refs_for(project.project_items, framework)
                    self.assertEqual([lib] if held else [], found)
                    self.assertEqual(
                        ["'$(Configuration)'=='Debug'"] if "Configuration" in condition else [],
                        [text for _, text, _ in unevaluated],
                    )
                    self.assertEqual(
                        ["System.Web"] if held else [],
                        [item.name for item in assemblies_for(project.reference_items, framework)[0]],
                    )
                    self.assertEqual(
                        ["Lib.Pkg"] if held else [],
                        [item.id for item in packages_for(project.package_items, framework)[0]],
                    )

    def test_with_no_framework_a_remove_it_cannot_evaluate_for_a_framework_is_named_with_it(self) -> None:
        # With no restore, a Remove applies only where it holds for every framework the project names, so one that
        # holds for one framework alone, the first or the second, or fails for one, is kept with nothing to name; one
        # that fails for none but cannot be evaluated for one is kept and named with the first such framework.
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
            (f"'$(TargetFramework)'=='net472' or {debug}", ["net8.0"]),
            (f"'$(TargetFramework)'=='net8.0' or {debug}", ["net472"]),
        ]
        body = "<PropertyGroup><TargetFrameworks>net8.0;net472</TargetFrameworks></PropertyGroup>"
        for condition, named in cases:
            with self.subTest(condition=condition):
                text = f'<Project Sdk="Microsoft.NET.Sdk">{body}{items.format(condition)}</Project>'
                project = read_project(self.write("src/App/App.csproj", text), self.central)
                self.assertEqual(
                    ([lib], ["System.Web"], ["Lib.Pkg"]),
                    (
                        project.project_refs,
                        [item.name for item in project.assemblies],
                        [i.id for i in project.packages],
                    ),
                )
                frameworks = project.frameworks
                expected = [("Remove", condition, framework) for framework in named]
                for unevaluated in (
                    project_refs_for(project.project_items, None, frameworks)[1],
                    assemblies_for(project.reference_items, None, frameworks)[1],
                    packages_for(project.package_items, None, (), frameworks)[1],
                ):
                    self.assertEqual(expected, [(item.operation, text, name) for item, text, name in unevaluated])
        # A central PackageVersion's Remove is decided the same way, for the frameworks the project names.
        props = self.write(
            "Directory.Packages.props",
            '<Project><ItemGroup><PackageVersion Include="Lib.Pkg" Version="1.0.0" />'
            f'<PackageVersion Remove="Lib.Pkg" Condition="{debug}" /></ItemGroup></Project>',
        )
        central = read_central_versions(props).items
        text = f'<Project Sdk="Microsoft.NET.Sdk">{body}<ItemGroup><PackageReference Include="Lib.Pkg" /></ItemGroup>'
        project = read_project(self.write("src/App/App.csproj", f"{text}</Project>"), self.central)
        references, unevaluated = packages_for(project.package_items, None, central, project.frameworks)
        self.assertEqual([PackageRef("Lib.Pkg", "1.0.0", "central")], references)
        self.assertEqual(
            [("central", "Remove", debug, "net8.0")], [(i.origin, i.operation, t, f) for i, t, f in unevaluated]
        )

    def test_a_choose_chooses_its_branch_for_its_items_with_the_framework_it_was_entered_with(self) -> None:
        # MSBuild tests a Choose's Whens once, when it enters the Choose, so a When's items hold for the framework a
        # later group sets when the When held for the one before it; an item's own condition reads the final value.
        project = read_project(self.write("src/App/App.csproj", ENTERED_CHOOSE_PROJECT), self.central)
        self.assertEqual(["net472"], project.frameworks)
        found, unevaluated = packages_for(project.package_items, "net472")
        self.assertEqual(["Lantern.Entered", "Lantern.Final"], [item.id for item in found])
        self.assertEqual([], unevaluated)
        # A framework TargetFrameworks names is fixed for its build from the start, whatever the file sets after.
        project = read_project(self.write("src/App/App.csproj", APART_CHOOSE_PROJECT), self.central)
        self.assertEqual(["net8.0", "net472"], project.frameworks)
        for framework, expected in (("net8.0", ["Lantern.Other"]), ("net472", ["Lantern.Apart"])):
            with self.subTest(framework=framework):
                self.assertEqual(expected, [item.id for item in packages_for(project.package_items, framework)[0]])
        # A Choose entered while the framework may hold one of several values cannot be chosen for a framework the
        # file sets after it, and its items are named, not applied.
        text = ENTERED_CHOOSE_PROJECT.replace("<PropertyGroup>", "<PropertyGroup Condition=\"'$(OS)'=='Unix'\">", 1)
        project = read_project(self.write("src/App/App.csproj", text), self.central)
        found, unevaluated = packages_for(project.package_items, "net472")
        self.assertEqual(["Lantern.Final"], [item.id for item in found])
        entered = "'$(TargetFramework)'=='net8.0'"
        self.assertEqual(
            [("Lantern.Entered", entered), ("Lantern.Other", entered)], [(i.id, t) for i, t, _ in unevaluated]
        )

    def test_a_directory_build_props_makes_the_files_unset_framework_one_of_the_values_it_may_hold(self) -> None:
        when = "'$(TargetFramework)'==''"
        text = (
            '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup><ProjectReference Include="..\\Lib\\Lib.csproj" /></ItemGroup>'
            f'<Choose><When Condition="{when}"><PropertyGroup><TargetFramework>net472</TargetFramework>'
            '</PropertyGroup><ItemGroup><ProjectReference Remove="..\\Lib\\Lib.csproj" /></ItemGroup></When>'
            "<Otherwise><PropertyGroup><Keep>1</Keep></PropertyGroup></Otherwise></Choose></Project>"
        )
        lib = (self.root / "src" / "Lib" / "Lib.csproj").resolve()
        # With no import, the file's unset value is the one MSBuild enters the Choose with, so the When applies.
        project = read_project(self.write("src/App/App.csproj", text), self.central)
        self.assertEqual((["net472"], []), (project.frameworks, project.project_refs))
        self.assertEqual([], project_refs_for(project.project_items, None, project.frameworks)[1])
        # Under a Directory.Build.props, at any depth above the project, the When cannot be chosen: its Remove is kept
        # and named, and the import's value never stands as a framework the project names.
        for relative in ("src/App/Directory.Build.props", "Directory.Build.props"):
            with self.subTest(props=relative):
                props = self.write(relative, "<Project />")
                project = read_project(self.root / "src" / "App" / "App.csproj", self.central)
                self.assertEqual((["net472"], [lib]), (project.frameworks, project.project_refs))
                unevaluated = project_refs_for(project.project_items, None, project.frameworks)[1]
                self.assertEqual([("Remove", when, "net472")], [(i.operation, t, f) for i, t, f in unevaluated])
                self.assertEqual(
                    [("Remove", when, "net8.0")],
                    [(i.operation, t, f) for i, t, f in project_refs_for(project.project_items, "net8.0")[1]],
                )
                props.unlink()
        # A framework the file sets before the Choose replaces whatever the import set, so the Choose is chosen with
        # it as before.
        self.write("Directory.Build.props", "<Project />")
        settled = text.replace(
            "<ItemGroup>", "<PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup><ItemGroup>", 1
        )
        project = read_project(self.write("src/App/App.csproj", settled), self.central)
        self.assertEqual((["net8.0"], [lib]), (project.frameworks, project.project_refs))
        self.assertEqual([], project_refs_for(project.project_items, "net8.0")[1])

    def test_items_inside_a_target_are_left_out_and_named(self) -> None:
        path = self.write("src/Harbor.Late/Harbor.Late.csproj", TARGET_PROJECT)
        project = read_project(path, self.central)
        self.assertEqual([PackageRef("Quill.Http", "1.2.0", "PackageReference")], project.packages)
        self.assertEqual([], project.project_refs)
        self.assertEqual([], project.assemblies)
        self.assertEqual(["net8.0"], project.frameworks)
        self.assertEqual(
            [
                TargetItem("PackageReference", "Beacon.Late", "AddLate"),
                TargetItem("ProjectReference", "..\\Harbor.Late.Core\\Harbor.Late.Core.csproj", "AddLate"),
                TargetItem("Reference", "System.Late", "AddLate"),
                TargetItem("PackageReference", "Quill.Http", "DropQuill"),
            ],
            project.in_targets,
        )
        props = self.write("Directory.Packages.props", TARGET_CENTRAL)
        central = read_central_versions(props)
        self.assertEqual(["Lantern.Json"], [item.id for item in central.items])
        self.assertEqual([TargetItem("PackageVersion", "Beacon.Late", "Pin")], central.in_targets)

    def test_a_framework_property_counts_where_its_conditions_hold(self) -> None:
        cases = [
            # A property under a condition that fails names nothing, and one that holds replaces the value before it.
            (
                "<PropertyGroup><TargetFrameworks>net8.0;net472</TargetFrameworks></PropertyGroup>"
                "<PropertyGroup Condition=\"'$(TargetFramework)' == 'net8.0'\">"
                "<TargetFrameworks>net6.0</TargetFrameworks></PropertyGroup>",
                ["net8.0", "net472"],
            ),
            (
                "<PropertyGroup><TargetFrameworks>net8.0;net472</TargetFrameworks></PropertyGroup>"
                "<PropertyGroup><TargetFrameworks>net9.0</TargetFrameworks></PropertyGroup>",
                ["net9.0"],
            ),
            # `$(TargetFramework)` is empty until the file sets it.
            (
                "<PropertyGroup Condition=\"'$(TargetFramework)' == ''\"><TargetFramework>net8.0</TargetFramework>"
                "</PropertyGroup><PropertyGroup Condition=\"'$(TargetFramework)' == 'net8.0'\">"
                "<TargetFrameworkVersion>v4.8</TargetFrameworkVersion></PropertyGroup>",
                ["net8.0", "v4.8"],
            ),
            # A condition the reader cannot evaluate adds its names to those it may replace, never choosing.
            (
                "<PropertyGroup><TargetFrameworks>net8.0</TargetFrameworks></PropertyGroup>"
                "<PropertyGroup Condition=\"'$(OS)' == 'Windows_NT'\">"
                "<TargetFrameworks>net8.0;net472</TargetFrameworks></PropertyGroup>",
                ["net8.0", "net472"],
            ),
            (
                "<Choose><When Condition=\"'$(Configuration)' == 'Debug'\"><PropertyGroup>"
                "<TargetFramework>net8.0</TargetFramework></PropertyGroup></When><Otherwise><PropertyGroup>"
                "<TargetFramework>net472</TargetFramework></PropertyGroup></Otherwise></Choose>"
                "<PropertyGroup Condition=\"'$(TargetFramework)' == 'net8.0'\"><TargetFrameworkVersion>v4.8"
                "</TargetFrameworkVersion></PropertyGroup>",
                ["net8.0", "net472", "v4.8"],
            ),
            # A Choose selects its branch once, when it is reached, so a When that sets the property it tests never
            # hands over to the Otherwise.
            (
                "<Choose><When Condition=\"'$(TargetFramework)'==''\"><PropertyGroup><TargetFramework>net8.0"
                "</TargetFramework></PropertyGroup></When><Otherwise><PropertyGroup><TargetFramework>net472"
                "</TargetFramework></PropertyGroup></Otherwise></Choose>",
                ["net8.0"],
            ),
            # Every When of a Choose is tested before any branch applies: a later When is not tested against what an
            # earlier one the reader cannot evaluate may set.
            (
                "<Choose><When Condition=\"'$(Configuration)' == 'Debug'\"><PropertyGroup>"
                "<TargetFramework>net8.0</TargetFramework></PropertyGroup></When>"
                "<When Condition=\"'$(TargetFramework)'==''\"><PropertyGroup><TargetFramework>net472"
                "</TargetFramework></PropertyGroup></When></Choose>",
                ["net8.0", "net472"],
            ),
            # Each branch that may apply is read from the state the Choose was entered with: the Otherwise sees the
            # framework still unset, whatever the When it may follow sets, and both names join.
            (
                "<Choose><When Condition=\"'$(Configuration)'=='Debug'\"><PropertyGroup>"
                "<TargetFramework>net8.0</TargetFramework></PropertyGroup></When><Otherwise>"
                "<PropertyGroup Condition=\"'$(TargetFramework)'==''\"><TargetFramework>net472</TargetFramework>"
                "</PropertyGroup></Otherwise></Choose>",
                ["net8.0", "net472"],
            ),
            # After a property the reader cannot tell is set, or a Choose none of whose branches may apply, the
            # framework may still be unset, so a later condition on it cannot be evaluated and its names join.
            (
                "<PropertyGroup Condition=\"'$(Configuration)'=='Debug'\"><TargetFramework>net8.0</TargetFramework>"
                "</PropertyGroup><PropertyGroup Condition=\"'$(TargetFramework)'==''\">"
                "<TargetFramework>net472</TargetFramework></PropertyGroup>",
                ["net8.0", "net472"],
            ),
            (
                "<Choose><When Condition=\"'$(Configuration)'=='Debug'\"><PropertyGroup>"
                "<TargetFramework>net8.0</TargetFramework></PropertyGroup></When></Choose>"
                "<PropertyGroup Condition=\"'$(TargetFramework)'==''\"><TargetFramework>net472</TargetFramework>"
                "</PropertyGroup>",
                ["net8.0", "net472"],
            ),
            # A Choose inside a When chooses its own branch when it is reached, and a PropertyGroup inside a When
            # applies only where its own condition holds.
            (
                "<Choose><When Condition=\"'$(TargetFramework)'==''\"><Choose>"
                "<When Condition=\"'$(TargetFramework)'==''\"><PropertyGroup><TargetFramework>net8.0"
                "</TargetFramework></PropertyGroup></When><Otherwise><PropertyGroup><TargetFramework>net472"
                "</TargetFramework></PropertyGroup></Otherwise></Choose>"
                "<PropertyGroup Condition=\"'$(TargetFramework)'=='net472'\"><TargetFrameworkVersion>v4.7.2"
                "</TargetFrameworkVersion></PropertyGroup>"
                "<PropertyGroup Condition=\"'$(TargetFramework)'=='net8.0'\"><TargetFrameworkVersion>v4.8"
                "</TargetFrameworkVersion></PropertyGroup></When></Choose>",
                ["net8.0", "v4.8"],
            ),
            # A PropertyGroup's condition is evaluated once, when it is reached, not again for each property in it.
            (
                "<PropertyGroup Condition=\"'$(TargetFramework)'==''\"><TargetFramework>net8.0</TargetFramework>"
                "<TargetFrameworkVersion>v4.8</TargetFrameworkVersion></PropertyGroup>",
                ["net8.0", "v4.8"],
            ),
            # A property's own condition is evaluated when the property is reached.
            (
                "<PropertyGroup><TargetFramework>net8.0</TargetFramework>"
                "<TargetFrameworkVersion Condition=\"'$(TargetFramework)'=='net8.0'\">v4.8</TargetFrameworkVersion>"
                "</PropertyGroup>",
                ["net8.0", "v4.8"],
            ),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                path = self.write("src/a/a.csproj", f'<Project Sdk="Microsoft.NET.Sdk">{body}</Project>')
                self.assertEqual(expected, read_project(path, self.central).frameworks)
                self.assertEqual(expected, msbuild_reader.frameworks_of(msbuild_reader.evaluation_root(path)))

    def test_a_when_without_a_condition_makes_the_file_unreadable(self) -> None:
        choose = (
            '<Choose><When><ItemGroup><PackageReference Include="Alpha.Pkg" Version="1.0.0" /></ItemGroup></When>'
            "<When Condition=\"'$(TargetFramework)'=='net8.0'\"><ItemGroup>"
            '<PackageReference Include="Beta.Pkg" Version="1.0.0" /></ItemGroup></When>'
            '<Otherwise><ItemGroup><PackageReference Include="Gamma.Pkg" Version="1.0.0" /></ItemGroup></Otherwise>'
            "</Choose>"
        )
        for relative in ("src/a/a.csproj", "Directory.Packages.props"):
            path = self.write(relative, f"<Project>{choose.replace('PackageReference', 'PackageVersion')}</Project>")
            reader = read_central_versions if relative.endswith(".props") else lambda p: read_project(p, self.central)
            with self.subTest(file=relative), self.assertRaises(Unreadable) as refused:
                reader(path)
            self.assertEqual(path, refused.exception.file)
            self.assertIn("a When has no Condition", refused.exception.reason)
        # Read directly, such a When cannot be evaluated, and neither can a When after it; the Otherwise is ruled out
        # by a later When that holds, and not evaluated when none does.
        root = read_xml(f'<Project Sdk="Microsoft.NET.Sdk">{choose}</Project>')
        for framework, unevaluated_ids in (
            ("net8.0", ["Alpha.Pkg", "Beta.Pkg"]),
            ("net472", ["Alpha.Pkg", "Gamma.Pkg"]),
        ):
            found, unevaluated = packages_for(msbuild_reader.package_items(root), framework)
            self.assertEqual([], found)
            self.assertEqual(unevaluated_ids, [item.id for item, _, _ in unevaluated])
        self.assertEqual(3, len(packages_for(msbuild_reader.package_items(root), None)[0]))

    def test_a_framework_version_of_any_length_is_read(self) -> None:
        digits = "9" * 5000
        self.assertEqual(f"net{digits}.0", framework_key(f".NETCoreApp,Version=v{digits}"))
        self.assertEqual("net10.0", framework_key(".NETCoreApp,Version=v10.0"))
        self.assertEqual("net5.0", framework_key(".NETCoreApp,Version=v5.0"))
        self.assertEqual(f"net8.0-windows1{digits}.0", framework_key(f"net8.0-windows1{digits}.0.0"))
        self.assertEqual("net8.0-windows10.0", framework_key(f"net8.0-windows10.0.{'0' * 5000}"))
        document = {"version": 3, "targets": {f".NETCoreApp,Version=v{digits}": {}}, "project": {}}
        assets = read_assets(self.write("obj/project.assets.json", json.dumps(document)))
        self.assertEqual([f".NETCoreApp,Version=v{digits}"], list(assets.nodes))

    def test_a_restore_records_each_frameworks_project_references_where_the_project_now_is(self) -> None:
        # Restored at another path, as in a checkout moved since; a path sharing no root is taken as written.
        restored = "D:\\old\\src\\Harbor.Api\\Harbor.Api.csproj"
        elsewhere = (self.root / "Elsewhere" / "Other.csproj").resolve()
        recorded: dict[str, dict[str, str]] = {
            "D:\\old\\src\\Harbor.Legacy\\Harbor.Legacy.csproj": {},
            "D:\\old\\src\\Harbor.Api\\Inner\\Inner.csproj": {},
            str(elsewhere): {},
        }
        restore: dict[str, Any] = {
            "projectPath": restored,
            "frameworks": {
                "net8.0": {"projectReferences": {}},
                "net472": {"projectReferences": recorded},
                "net9.0": {},
            },
        }
        document = {
            "version": 3,
            "targets": {".NETCoreApp,Version=v8.0": {}, ".NETFramework,Version=v4.7.2": {}},
            "project": {"restore": restore, "frameworks": {"net8.0": {}, "net472": {}}},
        }
        path = self.write("src/Harbor.Api/obj/project.assets.json", json.dumps(document))
        assets = read_assets(path)
        api = self.root / "src" / "Harbor.Api"
        self.assertEqual(
            {
                "net8.0": [],
                "net472": [
                    (api.parent / "Harbor.Legacy" / "Harbor.Legacy.csproj").resolve(),
                    (api / "Inner" / "Inner.csproj").resolve(),
                    elsewhere,
                ],
            },
            assets.project_refs,
        )
        # A restore that does not name the project it ran for records none.
        del restore["projectPath"]
        path = self.write("src/Harbor.Api/obj/project.assets.json", json.dumps(document))
        self.assertEqual({}, read_assets(path).project_refs)

    def test_module_names_the_files_it_reads(self) -> None:
        self.assertEqual(("web.config", "app.config"), msbuild_reader.CONFIG_FILES)
        self.assertEqual("project.assets.json", msbuild_reader.ASSETS_FILE)


if __name__ == "__main__":
    unittest.main()
