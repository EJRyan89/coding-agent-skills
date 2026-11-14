"""Regression tests for the readers of the files a dependency graph is built from."""

from __future__ import annotations

import contextlib
import errno
import json
import os
import stat
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator
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

    def test_the_nearest_file_passes_over_a_folder_of_its_name_and_refuses_one_it_cannot_stat(self) -> None:
        props = self.write("Directory.Packages.props", "<Project />")
        (self.root / "src" / "App" / "Directory.Packages.props").mkdir(parents=True)
        self.assertEqual(props, nearest_file(self.root / "src" / "App", "Directory.Packages.props"))
        nearer = self.root / "src" / "Directory.Packages.props"
        with stat_failing_at(nearer), self.assertRaises(msbuild_reader.Unreadable) as raised:
            nearest_file(self.root / "src" / "App", "Directory.Packages.props")
        self.assertEqual((nearer, "Access is denied"), (raised.exception.file, raised.exception.reason))

    def test_a_stat_error_naming_no_file_is_refused_against_the_candidate_the_search_checked(self) -> None:
        self.write("Directory.Packages.props", "<Project />")
        (self.root / "src" / "App").mkdir(parents=True)
        nearer = self.root / "src" / "Directory.Packages.props"
        with (
            failing_without_a_file(msbuild_reader, "is_present", nearer),
            self.assertRaises(msbuild_reader.Unreadable) as raised,
        ):
            nearest_file(self.root / "src" / "App", "Directory.Packages.props")
        self.assertEqual((nearer, "Access is denied"), (raised.exception.file, raised.exception.reason))

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

    def test_package_assemblies_are_in_the_order_of_their_ordinal_fold_whatever_their_case(self) -> None:
        for stem in ("beta", "Alpha", "Gamma"):
            self.write(f"pkg/lib/net45/{stem}.dll", "")
        self.assertEqual(("Alpha", "beta", "Gamma"), package_assemblies(self.root / "pkg"))

    def test_a_lib_that_is_no_folder_or_cannot_be_looked_at_is_unreadable_rather_than_empty(self) -> None:
        lib = self.write("file/lib", "")
        with self.assertRaises(Unreadable) as refused:
            package_assemblies(self.root / "file")
        self.assertEqual((lib, "not a directory"), (refused.exception.file, refused.exception.reason))
        locked = self.root / "locked" / "lib"
        self.write("locked/lib/net45/Lantern.Json.dll", "")
        real_stat = Path.stat
        # An error with its reason as strerror, and one with no strerror, whose message is the reason.
        for failure, reason in (
            (PermissionError(13, "Permission denied", str(locked)), "Permission denied"),
            (OSError("the stat gave up"), "the stat gave up"),
        ):

            def denied(path: Path, *, follow_symlinks: bool = True, failure: OSError = failure) -> os.stat_result:
                if path == locked:
                    raise failure
                return real_stat(path, follow_symlinks=follow_symlinks)

            with (
                self.subTest(reason=reason),
                mock.patch.object(Path, "stat", autospec=True, side_effect=denied),
                self.assertRaises(Unreadable) as refused,
            ):
                package_assemblies(self.root / "locked")
            self.assertEqual((locked, reason), (refused.exception.file, refused.exception.reason))

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

    def test_a_direct_dependency_with_no_target_or_an_empty_one_is_a_package(self) -> None:
        document = json.loads(json.dumps(ASSETS))
        document["project"]["frameworks"]["net8.0"]["dependencies"] = {
            "Quill.Http": {"version": "[1.2.0, )"},
            "Lantern.Json": {"target": "", "version": "12.0.3"},
            "Harbor.Core": {"target": "Project"},
        }
        assets = read_assets(self.write("obj/project.assets.json", json.dumps(document)))
        self.assertEqual(
            [
                PackageRef("Quill.Http", "[1.2.0, )", "PackageReference"),
                PackageRef("Lantern.Json", "12.0.3", "PackageReference"),
            ],
            assets.direct["net8.0"],
        )

    def test_an_unreadable_assets_file_names_the_file_and_the_reason(self) -> None:
        with self.assertRaises(Unreadable):
            read_assets(self.write("obj/project.assets.json", "{not json"))
        path = self.write("obj/project.assets.json", "{}")
        with self.assertRaises(Unreadable) as refused:
            read_assets(path)
        self.assertEqual((path, "the assets file has no targets"), (refused.exception.file, refused.exception.reason))

    def test_a_section_of_another_kind_makes_the_assets_file_unreadable_by_name(self) -> None:
        def changed(where: tuple[str, ...], value: object) -> dict[str, object]:
            document: dict[str, object] = json.loads(json.dumps(ASSETS))
            parent: Any = document
            for step in where[:-1]:
                parent = parent.setdefault(step, {})
            parent[where[-1]] = value
            return document

        restore = {"projectPath": "/repo/a.csproj", "frameworks": "net8.0"}
        quill = ("targets", "net8.0", "Quill.Http/1.2.0")
        harbor = ("project", "frameworks", "net8.0", "dependencies", "Harbor.Core")
        restored = ("project", "restore")
        quill_at = "the assets file's target net8.0's Quill.Http/1.2.0"
        harbor_at = "the assets file's project.frameworks.net8.0.dependencies.Harbor.Core"
        cases = {
            f"{quill_at} is not an object": changed(quill, "package"),
            f"{quill_at}.type is not text": changed((*quill, "type"), ["package"]),
            f"{quill_at}.dependencies is not an object": changed((*quill, "dependencies"), ["Lantern.Json"]),
            f"{quill_at}.dependencies.Lantern.Json is not text": changed((*quill, "dependencies", "Lantern.Json"), 12),
            f"{quill_at}.compile is not an object": changed((*quill, "compile"), "lib/net8.0/Quill.Http.dll"),
            "the assets file's target net8.0's Quill.Http is a package that names no version": changed(
                ("targets", "net8.0", "Quill.Http"), {"type": "package"}
            ),
            "the assets file's project.frameworks.net8.0.dependencies is not an object": changed(
                ("project", "frameworks", "net8.0", "dependencies"), ["Quill.Http"]
            ),
            f"{harbor_at} is not an object": changed(harbor, "Project"),
            f"{harbor_at}.target is not text": changed((*harbor, "target"), {"kind": "Project"}),
            "the assets file's project.frameworks.net8.0.dependencies.Quill.Http.version is not text": changed(
                ("project", "frameworks", "net8.0", "dependencies", "Quill.Http", "version"), 1.2
            ),
            "the assets file's project.restore.projectPath is not text": changed((*restored, "projectPath"), 1),
            "the assets file's project.restore.frameworks.net8.0 is not an object": changed(
                restored, {"projectPath": "/repo/a.csproj", "frameworks": {"net8.0": []}}
            ),
            "the assets file's project.restore.frameworks.net8.0.projectReferences is not an object": changed(
                restored, {"projectPath": "/repo/a.csproj", "frameworks": {"net8.0": {"projectReferences": "a"}}}
            ),
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
                path = self.write("obj/project.assets.json", json.dumps(document))
                with self.assertRaises(Unreadable) as refused:
                    read_assets(path)
                # The UNREADABLE line names the file a user fixes, and why.
                self.assertEqual((path, reason), (refused.exception.file, refused.exception.reason))

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

    @unittest.skipIf(sys.platform == "win32", "a symbolic link to a folder needs a privilege on Windows")
    def test_a_checkout_marker_that_is_a_link_to_nothing_marks_no_checkout(self) -> None:
        # Git reads nothing through a link to nothing, so the tree is the checkout above it, never the folder it is in.
        base = self.root / "repo" / "src"
        base.mkdir(parents=True)
        marker = ".dotnet-dependencies-test-checkout"
        (self.root / marker).mkdir()
        (self.root / "repo" / marker).symlink_to(self.root / "nowhere", target_is_directory=True)
        with mock.patch.object(msbuild_reader, "CHECKOUT_MARKER", marker):
            self.assertEqual(self.root, examined_tree(base))

    def test_a_checkout_marker_it_cannot_stat_is_refused_never_read_as_no_checkout(self) -> None:
        base = self.root / "repo" / "src"
        base.mkdir(parents=True)
        marker = ".dotnet-dependencies-test-checkout"
        (self.root / marker).mkdir()
        locked = self.root / "repo" / marker
        for name, failing in (
            ("naming the marker", stat_failing_at(locked)),
            ("naming no file", failing_without_a_file(msbuild_reader, "is_there", locked)),
        ):
            with (
                self.subTest(name),
                mock.patch.object(msbuild_reader, "CHECKOUT_MARKER", marker),
                failing,
                self.assertRaises(msbuild_reader.Unreadable) as raised,
            ):
                examined_tree(base)
            self.assertEqual((locked, "Access is denied"), (raised.exception.file, raised.exception.reason))

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


@pytest.mark.parametrize(
    ("expected", "first", "second"),
    [
        # Arabic-Indic and full-width digits are no version digits to NuGet's framework parser, so a long name holding
        # them is no short one, and a platform version of them is not trimmed as a version.
        (False, ".NETFramework,Version=v\u0664.\u0668", "net\u0664\u0668"),
        (False, ".NETCoreApp,Version=v\uff18.0", "net\uff18.0"),
        (False, "net8.0-windows\u0661\u0660.0", "net8.0-windows\u0661\u0660"),
        # The long s and the Kelvin sign are no s or k to .NET's ordinal comparison ignoring case.
        (False, "net48-klient", ".NETFramework,Version=v4.8,Profile=\u212alient"),
        (False, "netstandard2.0", "net\u017ftandard2.0"),
        (False, "net8.0-ios", "NET8.0-IO\u017f"),
        # A letter beyond ASCII matches its other case, as the ordinal comparison ignoring case finds it.
        (True, "net8.0-\u00e9x", "NET8.0-\u00c9X"),
        (True, "net48-client", ".NETFramework,Version=v4.8,Profile=CLIENT"),
    ],
)
def test_a_framework_name_is_folded_and_its_digits_read_as_nuget_reads_them(
    expected: bool, first: str, second: str
) -> None:
    assert expected is same_framework(first, second)
    assert expected is same_framework(second, first)


SHARP_S = "Lantern.Stra\u00dfe"
CAFE = "Lantern.Caf\u00e9"
CAFE_UPPER = "LANTERN.CAF\u00c9"
KELVIN = "Lantern.\u212alient"
# Every spelling, in an order no fold sorts them in: the sharp s and ss, the Kelvin sign and K, two cases of one
# accented letter.
SPELLINGS = ("Lantern.Strasz", SHARP_S, "Lantern.Strasse", KELVIN, "Lantern.Klient", CAFE)


def nuspec_dependencies(root: Path) -> list[str]:
    listed = "".join(
        f'<dependency id="{name}" version="1.0.0" />' for name in (*SPELLINGS, CAFE_UPPER, "LANTERN.KLIENT")
    )
    path = root / "Lantern.Core.nuspec"
    path.write_text(
        f"<package><metadata><id>Lantern.Core</id><version>1.0.0</version><dependencies>{listed}"
        "</dependencies></metadata></package>",
        encoding="utf-8",
    )
    return list(read_nuspec(path).dependencies)


def shipped_assemblies(root: Path) -> tuple[str, ...]:
    # The ss and K spellings ship in a folder of their own: macOS's default APFS folds the sharp s to ss and the
    # Kelvin sign to k, so one folder there would hold one of each pair, while two folders hold every spelling on
    # every file system.
    for framework, names in (
        ("net45", (SHARP_S, "Lantern.Strasz", KELVIN, CAFE)),
        ("net48", ("Lantern.Strasse", "Lantern.Klient")),
    ):
        lib = root / "Lantern.Core.1.0.0" / "lib" / framework
        lib.mkdir(parents=True)
        for name in names:
            (lib / f"{name}.dll").write_text("", encoding="utf-8")
    return package_assemblies(root / "Lantern.Core.1.0.0")


def restored_assemblies(root: Path) -> tuple[str, ...]:
    compile_entries: dict[str, object] = {f"lib/net8.0/{name}.dll": {} for name in SPELLINGS}
    return msbuild_reader.assets_assemblies({"compile": compile_entries}, root / "project.assets.json", "Lantern.Core")


def restored_folder(root: Path, recorded: str, restored: str) -> str:
    """Where a project a restore recorded in `recorded`, beside the restored project in `restored`, is after the
    checkout moved to `moved`."""
    found = msbuild_reader.restored_path(
        (root / "w" / recorded / "Lib" / "Lib.csproj").as_posix(),
        (root / "w" / restored / "App" / "App.csproj").as_posix(),
        root / "moved" / restored / "App",
    )
    return found.relative_to(root).as_posix()


READER_ROWS: list[tuple[str, Callable[[Path], object], object]] = [
    # read_nuspec keeps the first of each dependency spelled alike, ordinally ignoring case.
    (
        "read_nuspec",
        nuspec_dependencies,
        ["Lantern.Strasz", SHARP_S, "Lantern.Strasse", KELVIN, "Lantern.Klient", CAFE],
    ),
    # package_assemblies and assets_assemblies list each assembly in the order of its ordinal fold.
    (
        "package_assemblies",
        shipped_assemblies,
        (CAFE, "Lantern.Klient", "Lantern.Strasse", "Lantern.Strasz", SHARP_S, KELVIN),
    ),
    (
        "assets_assemblies",
        restored_assemblies,
        (CAFE, "Lantern.Klient", "Lantern.Strasse", "Lantern.Strasz", SHARP_S, KELVIN),
    ),
    # restored_path shares a folder only where the two spellings are one name.
    (
        "restored_path: sharp s is not ss",
        lambda root: restored_folder(root, "Stra\u00dfe", "Strasse"),
        "moved/Stra\u00dfe/Lib/Lib.csproj",
    ),
    (
        "restored_path: Kelvin sign is not K",
        lambda root: restored_folder(root, "\u212alient", "Klient"),
        "moved/\u212alient/Lib/Lib.csproj",
    ),
    (
        "restored_path: other case matches",
        lambda root: restored_folder(root, "Caf\u00e9", "CAF\u00c9"),
        "moved/CAF\u00c9/Lib/Lib.csproj",
    ),
]


@pytest.mark.parametrize(("reader", "read", "expected"), READER_ROWS, ids=[row[0] for row in READER_ROWS])
def test_each_reader_compares_names_by_the_ordinal_fold(
    reader: str, read: Callable[[Path], object], expected: object, tmp_path: Path
) -> None:
    # The sharp s and ss, and the Kelvin sign and K, are two names; an accented letter and its other case are one.
    assert read(tmp_path.resolve()) == expected


@contextlib.contextmanager
def stat_failing_at(target: Path) -> Iterator[None]:
    """A stat of `target` alone fails, as on a path the script may not examine: Path.stat, os.stat, and os.lstat raise
    PermissionError for it, and Path's and os.path's presence predicates answer False for it, as Python 3.14 and later
    do there, so a predicate left on the path reads it as absent. Every other path is read as it is."""

    def refused(name: object) -> bool:
        return isinstance(name, (str, os.PathLike)) and Path(name) == target

    def raising(real: Callable[..., Any]) -> Callable[..., Any]:
        def call(name: Any, *rest: Any, **options: Any) -> Any:
            if refused(name):
                raise PermissionError(errno.EACCES, "Access is denied", str(name))
            return real(name, *rest, **options)

        return call

    def answering_false(real: Callable[..., Any]) -> Callable[..., Any]:
        def call(name: Any, *rest: Any, **options: Any) -> Any:
            return False if refused(name) else real(name, *rest, **options)

        return call

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(Path, "stat", raising(Path.stat)))
        for name in ("stat", "lstat"):
            stack.enter_context(mock.patch.object(os, name, raising(getattr(os, name))))
        for name in ("exists", "is_file", "is_dir"):
            stack.enter_context(mock.patch.object(Path, name, answering_false(getattr(Path, name))))
        for name in ("exists", "lexists", "isfile", "isdir"):
            stack.enter_context(mock.patch.object(os.path, name, answering_false(getattr(os.path, name))))
        yield


@contextlib.contextmanager
def failing_without_a_file(owner: object, name: str, target: Path) -> Iterator[None]:
    """`owner.<name>` called on `target` alone raises PermissionError naming no file, so a report of it names the path
    its caller checked, never one the error carries; every other call is answered as it is."""
    real = getattr(owner, name)

    def call(path: Any, *rest: Any, **options: Any) -> Any:
        if path == target:
            raise PermissionError(errno.EACCES, "Access is denied")
        return real(path, *rest, **options)

    with mock.patch.object(owner, name, call):
        yield
