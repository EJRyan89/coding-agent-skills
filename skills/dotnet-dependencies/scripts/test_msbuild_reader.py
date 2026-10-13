"""Regression tests for the readers of the files a dependency graph is built from."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import msbuild_reader
from msbuild_reader import (
    PackageRef,
    Unreadable,
    central_versions_file,
    package_assemblies,
    read_assets,
    read_central_versions,
    read_nuspec,
    read_packages_config,
    read_project,
    read_redirects,
)

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
CENTRAL = """<Project>
  <PropertyGroup><ManagePackageVersionsCentrally>true</ManagePackageVersionsCentrally></PropertyGroup>
  <ItemGroup>
    <PackageVersion Include="Lantern.Core" Version="2.0.0" />
    <PackageVersion Include="Lantern.Text" Version="2.5.0" />
  </ItemGroup>
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

    def central(self, directory: Path) -> dict[str, str]:
        found = central_versions_file(directory, self.root)
        return read_central_versions(found) if found else {}

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

    def test_central_versions_stop_at_the_solution_directory(self) -> None:
        props = self.write("Directory.Packages.props", CENTRAL)
        self.assertIsNone(central_versions_file(self.root / "src", self.root / "src"))
        self.assertEqual(props, central_versions_file(self.root / "src", self.root))
        self.assertEqual({"lantern.core": "2.0.0", "lantern.text": "2.5.0"}, read_central_versions(props))
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

    def test_module_names_the_files_it_reads(self) -> None:
        self.assertEqual(("web.config", "app.config"), msbuild_reader.CONFIG_FILES)
        self.assertEqual("project.assets.json", msbuild_reader.ASSETS_FILE)


if __name__ == "__main__":
    unittest.main()
