"""Regression tests for the readers of the files a dependency graph is built from."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import dotnet_solution
import msbuild_reader
from msbuild_reader import (
    PackageRef,
    Unreadable,
    examined_tree,
    framework_key,
    matching_framework,
    nearest_file,
    package_assemblies,
    read_assets,
    read_bounded,
    read_nuspec,
    read_packages_config,
    read_redirects,
    read_solution,
    same_framework,
)

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
        self.root = Path(tempfile.mkdtemp(prefix="dotnet-dependencies-reader-")).resolve()
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

    def test_the_nearest_file_is_searched_for_up_to_the_filesystems_root(self) -> None:
        # As MSBuild searches for Directory.Build.props and Directory.Packages.props: past the folder a run starts in.
        props = self.write("Directory.Packages.props", "<Project />")
        (self.root / "src" / "App").mkdir(parents=True)
        self.assertEqual(props, nearest_file(self.root / "src" / "App", "Directory.Packages.props"))
        nearer = self.write("src/Directory.Packages.props", "<Project />")
        self.assertEqual(nearer, nearest_file(self.root / "src" / "App", "Directory.Packages.props"))
        self.assertIsNone(nearest_file(self.root / "src" / "App", "Nothing.Above.props"))

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

    def test_an_unreadable_assets_file_names_the_file_and_the_reason(self) -> None:
        with self.assertRaises(Unreadable):
            read_assets(self.write("obj/project.assets.json", "{not json"))
        with self.assertRaises(Unreadable) as refused:
            read_assets(self.write("obj/project.assets.json", "{}"))
        self.assertIn("no targets", refused.exception.reason)

    def test_a_section_of_another_kind_makes_the_assets_file_unreadable_by_name(self) -> None:
        def changed(where: tuple[str, ...], value: object) -> dict[str, object]:
            document: dict[str, object] = json.loads(json.dumps(ASSETS))
            parent: Any = document
            for step in where[:-1]:
                parent = parent.setdefault(step, {})
            parent[where[-1]] = value
            return document

        restore = {"projectPath": "/repo/a.csproj", "frameworks": "net8.0"}
        cases = {
            "the assets file's target net8.0 is not an object": changed(("targets", "net8.0"), "text"),
            "the assets file's target net8.0/win-x64 is not an object": changed(("targets", "net8.0/win-x64"), []),
            "the assets file's project is not an object": changed(("project",), "text"),
            "the assets file's project.frameworks is not an object": changed(("project", "frameworks"), ["net8.0"]),
            "the assets file's project.frameworks.net8.0 is not an object": changed(
                ("project", "frameworks", "net8.0"), "text"
            ),
            "the assets file's project.restore is not an object": changed(("project", "restore"), 1),
            "the assets file's project.restore.frameworks is not an object": changed(("project", "restore"), restore),
        }
        for reason, document in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaises(Unreadable) as refused:
                    read_assets(self.write("obj/project.assets.json", json.dumps(document)))
                self.assertEqual(reason, refused.exception.reason)

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
        # Restored at another path, as in a checkout moved since; a path on the restore's drive outside its tree is
        # taken from the project too, and a path sharing no root is taken as written. The restore's drive is one the
        # temporary root is not on, so the last path shares no root with it on every machine.
        elsewhere = (self.root / "Elsewhere" / "Other.csproj").resolve()
        drive = "E:" if elsewhere.drive.casefold() == "d:" else "D:"
        restored = f"{drive}\\old\\src\\Harbor.Api\\Harbor.Api.csproj"
        recorded: dict[str, dict[str, str]] = {
            f"{drive}\\old\\src\\Harbor.Legacy\\Harbor.Legacy.csproj": {},
            f"{drive}\\old\\src\\Harbor.Api\\Inner\\Inner.csproj": {},
            f"{drive}\\shared\\Shared.csproj": {},
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
                    (self.root.parent / "shared" / "Shared.csproj").resolve(),
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


class PathBoundTests(unittest.TestCase):
    """The lexical rule for the paths files name, the examined tree, and the bounded read."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="dotnet-dependencies-bounds-")).resolve()
        self.addCleanup(self.remove_root)

    def remove_root(self) -> None:
        for path in sorted(self.root.rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        self.root.rmdir()

    def test_the_checkout_marker_is_the_documented_one(self) -> None:
        self.assertEqual(".git", msbuild_reader.CHECKOUT_MARKER)

    def test_the_examined_tree_is_the_nearest_folder_holding_the_marker_else_the_base(self) -> None:
        base = self.root / "repo" / "src"
        base.mkdir(parents=True)
        marker = ".dotnet-dependencies-test-checkout"
        with mock.patch.object(msbuild_reader, "CHECKOUT_MARKER", marker):
            self.assertEqual(base, examined_tree(base))
            (self.root / marker).mkdir()
            self.assertEqual(self.root, examined_tree(base))
            (self.root / "repo" / marker).write_text("gitdir: elsewhere\n", encoding="utf-8")
            self.assertEqual(self.root / "repo", examined_tree(base))
            (base / marker).mkdir()
            self.assertEqual(base, examined_tree(base))

    def test_a_read_takes_a_regular_file_within_the_bound_and_refuses_any_other(self) -> None:
        path = self.root / "Lib.csproj"
        path.write_bytes(b"x" * 16)
        with mock.patch.object(dotnet_solution, "READ_LIMIT_BYTES", 16):
            self.assertEqual(b"x" * 16, read_bounded(path))
            path.write_bytes(b"x" * 17)
            with self.assertRaises(Unreadable) as refused:
                read_bounded(path)
            self.assertEqual(
                "the file is larger than the reader's bound of 16 bytes, so it is not read", refused.exception.reason
            )
            # A file whose size the system understates, as a growing file's or a special file's, is cut at the bound.
            small = os.stat_result((stat.S_IFREG | 0o644, 0, 0, 1, 0, 0, 1, 0, 0, 0))
            with mock.patch.object(Path, "stat", return_value=small), self.assertRaises(Unreadable) as refused:
                read_bounded(path)
            self.assertIn("larger than the reader's bound", refused.exception.reason)
        with self.assertRaises(Unreadable) as refused:
            read_bounded(self.root)
        self.assertEqual("not a regular file, so the reader does not open it", refused.exception.reason)
        with self.assertRaises(Unreadable) as refused:
            read_bounded(self.root / "Missing.csproj")
        self.assertEqual(self.root / "Missing.csproj", refused.exception.file)

    def test_an_assets_file_or_solution_above_the_bound_is_unreadable(self) -> None:
        assets = self.root / "project.assets.json"
        assets.write_text(json.dumps({"targets": {"net8.0": {}}}), encoding="utf-8")
        listed = self.root / "Harbor.slnx"
        listed.write_text('<Solution><Project Path="a.csproj" /></Solution>', encoding="utf-8")
        self.assertEqual(["a.csproj"], read_solution(listed))
        with mock.patch.object(dotnet_solution, "READ_LIMIT_BYTES", 8):
            with self.assertRaises(Unreadable) as refused:
                read_assets(assets)
            self.assertIn("larger than the reader's bound of 8 bytes", refused.exception.reason)
            with self.assertRaises(Unreadable) as refused:
                read_solution(listed)
            self.assertIn("larger than the reader's bound of 8 bytes", refused.exception.reason)


@pytest.mark.parametrize(
    ("expected", "first", "second"),
    [
        (True, "net8.0", ".NETCoreApp,Version=v8.0"),
        (True, "netcoreapp3.1", ".NETCoreApp,Version=v3.1"),
        (True, "net472", ".NETFramework,Version=v4.7.2"),
        (True, "NET48", ".netframework,version=v4.8"),
        (True, "net40", ".NETFramework,Version=v4.0"),
        (True, "net40-client", ".NETFramework,Version=v4.0,Profile=Client"),
        (True, "netstandard2.0", ".NETStandard,Version=v2.0"),
        # A restore writes out the platform version a project leaves to the default.
        (True, "net8.0-windows", "net8.0-windows7.0"),
        (True, "net8.0-windows7.0", "NET8.0-Windows7.0"),
        # A restore drops a platform version's trailing zero parts down to two parts.
        (True, "net8.0-windows10.0.19041.0", "net8.0-windows10.0.19041"),
        (True, "net8.0-windows10", "net8.0-windows10.0"),
        (True, "net8.0-windows7.0.0", "net8.0-windows"),
        # A workload sets the default of a platform other than Windows, so any version may be it.
        (True, "net8.0-android", "net8.0-android34.0"),
        (False, "net8.0", "netcoreapp8.0-x"),
        (False, "net472", ".NETFramework,Version=v4.7.1"),
        (False, "netcoreapp3.1", ".NETStandard,Version=v3.1"),
        (False, "net8.0", "net8.0-windows"),
        (False, "net8.0-windows10.0", "net8.0-windows7.0"),
        # A name that leaves the version to the default is never one with another version.
        (False, "net8.0-windows", "net8.0-windows10.0.19041"),
        (False, "net8.0-windows", "net8.0-windows10.0.19041.0"),
        (False, "net8.0-windows10.0.19041", "net8.0-windows10.0.22621.0"),
        (False, "net8.0-android34.0", "net8.0-android35.0"),
        (False, "net8.0-android", "net8.0-ios"),
        (False, "net9.0-windows", "net8.0-windows7.0"),
        (False, ".NETFoo,Version=v1.0", "foo1.0"),
    ],
)
def test_a_framework_is_one_framework_whichever_form_names_it(expected: bool, first: str, second: str) -> None:
    assert expected is same_framework(first, second)
    assert expected is same_framework(second, first)


def test_a_framework_key_is_the_name_case_folded() -> None:
    assert framework_key(".NETFoo,Version=v1.0") == ".netfoo,version=v1.0"
