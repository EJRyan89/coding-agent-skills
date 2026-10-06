from __future__ import annotations

import codecs
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIRECTORY))

from review_analyzers import MAX_SETTINGS, UNREAD, inventory, tool_names  # noqa: E402

SDK_PROJECT = """<?xml version="1.0" encoding="utf-8"?>
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <AnalysisMode>Recommended</AnalysisMode>
    <AnalysisLevelSecurity>latest-all</AnalysisLevelSecurity>
    <TreatWarningsAsErrors>true</TreatWarningsAsErrors>
    <Nullable>enable</Nullable>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="StyleCop.Analyzers" Version="1.1.118" PrivateAssets="all" />
    <PackageReference Include="Newtonsoft.Json" Version="13.0.3" />
    <PackageReference Include="AsyncFixer" Version="1.6.0" />
    <Analyzer Include="..\\tools\\TeamRules.dll" />
  </ItemGroup>
</Project>
"""
LEGACY_PROJECT = """<?xml version="1.0" encoding="utf-16"?>
<Project ToolsVersion="15.0" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemGroup><PackageReference Include="Roslynator.Analyzers" Version="4.12.0" /></ItemGroup>
</Project>
"""
CENTRAL_PACKAGES = """<Project>
  <ItemGroup>
    <PackageVersion Include="SonarAnalyzer.CSharp" Version="9.0.0" />
    <GlobalPackageReference Include="Meziantou.Analyzer" Version="2.0.0" />
  </ItemGroup>
</Project>
"""
EDITORCONFIG = """root = true

[*.cs]
indent_style = space
dotnet_diagnostic.CA2000.severity = suggestion
dotnet_diagnostic.SA1101.severity = none
dotnet_analyzer_diagnostic.category-Security.severity = error
dotnet_analyzer_diagnostic.severity = warning
"""
PYPROJECT = """[project]
name = "fixture"

[tool.ruff]
line-length = 120

[tool.ruff.lint]
select = ["E", "F", "B"]
ignore = ["B008"]

[tool.mypy]
strict = true
"""


class InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, files: dict[str, str | bytes]) -> list[str]:
        for relative, content in files.items():
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content, encoding="utf-8", newline="\n")
        return list(files)

    def settings(self, result: dict, path: str) -> list[str]:
        return [entry["setting"] for entry in result["settings"] if entry["file"] == path]

    def test_dotnet_projects_packages_and_severities(self) -> None:
        paths = self.write({
            "src/App/App.csproj": SDK_PROJECT,
            "src/Old/Old.csproj": codecs.BOM_UTF16_LE + LEGACY_PROJECT.encode("utf-16-le"),
            "Directory.Packages.props": CENTRAL_PACKAGES,
            ".editorconfig": EDITORCONFIG,
            "src/App/Program.cs": "class Program {}\n",
        })
        result = inventory(self.root, paths)
        tools = {entry["tool"]: entry["files"] for entry in result["tools"]}
        self.assertEqual({
            "AsyncFixer": ["src/App/App.csproj"],
            "Meziantou.Analyzer": ["Directory.Packages.props"],
            "Microsoft.CodeAnalysis.CSharp.CodeStyle": ["src/App/App.csproj"],
            "Microsoft.CodeAnalysis.NetAnalyzers": ["src/App/App.csproj"],
            "Roslynator.Analyzers": ["src/Old/Old.csproj"],
            "StyleCop.Analyzers": ["src/App/App.csproj"],
            "TeamRules": ["src/App/App.csproj"],
        }, tools, "a central PackageVersion pins a version but adds no analyzer; a legacy project has no SDK analyzers")
        self.assertEqual([
            "<TargetFramework>net8.0</TargetFramework>",
            "<AnalysisMode>Recommended</AnalysisMode>",
            "<AnalysisLevelSecurity>latest-all</AnalysisLevelSecurity>",
            "<TreatWarningsAsErrors>true</TreatWarningsAsErrors>",
        ], self.settings(result, "src/App/App.csproj"))
        self.assertEqual([
            "[*.cs] dotnet_diagnostic.CA2000.severity = suggestion",
            "[*.cs] dotnet_diagnostic.SA1101.severity = none",
            "[*.cs] dotnet_analyzer_diagnostic.category-Security.severity = error",
            "[*.cs] dotnet_analyzer_diagnostic.severity = warning",
        ], self.settings(result, ".editorconfig"))
        self.assertFalse(result["settings_truncated"])
        self.assertEqual(sorted(tools, key=str.casefold), tool_names(result))

    def test_python_shell_and_javascript_configuration(self) -> None:
        paths = self.write({
            "pyproject.toml": PYPROJECT,
            "tools/ruff.toml": 'extend-select = ["UP"]\n',
            "setup.cfg": "[metadata]\nname = fixture\n\n[flake8]\nmax-line-length = 120\nextend-ignore = E203\n",
            "tox.ini": "[tox]\nenvlist = py311\n",
            "lib/.flake8": "[flake8]\nselect = E,W\n",
            ".shellcheckrc": "# project policy\nenable=require-variable-braces\ndisable=SC1091\n",
            "web/package.json": '{"devDependencies": {"eslint": "^9", "eslint-plugin-react": "^7",'
                                ' "@typescript-eslint/eslint-plugin": "^8", "typescript": "^5"}}',
            "PSScriptAnalyzerSettings.psd1": "@{}\n",
            ".semgrep/rules.yml": "rules: []\n",
        })
        result = inventory(self.root, paths)
        self.assertEqual(
            ["@typescript-eslint/eslint-plugin", "eslint", "eslint-plugin-react", "flake8", "mypy",
             "PSScriptAnalyzer", "ruff", "Semgrep", "ShellCheck"],
            tool_names(result),
            "a setup.cfg or tox.ini without an analyzer section names no analyzer",
        )
        self.assertEqual(['tool.ruff.lint.select = ["E", "F", "B"]', 'tool.ruff.lint.ignore = ["B008"]'],
                         self.settings(result, "pyproject.toml"))
        self.assertEqual(['extend-select = ["UP"]'], self.settings(result, "tools/ruff.toml"))
        self.assertEqual(["[flake8] extend-ignore = E203"], self.settings(result, "setup.cfg"))
        self.assertEqual(["[flake8] select = E,W"], self.settings(result, "lib/.flake8"))
        self.assertEqual(["enable=require-variable-braces", "disable=SC1091"], self.settings(result, ".shellcheckrc"))

    def test_unparseable_files_are_listed_as_unread_and_never_expanded(self) -> None:
        paths = self.write({
            "Broken.csproj": "<Project Sdk='Microsoft.NET.Sdk'><PropertyGroup>",
            "Entities.props": '<?xml version="1.0"?><!DOCTYPE p [<!ENTITY a "aaaa">]><Project>&a;</Project>',
            ".editorconfig": "[*.cs\nbroken",
            "ruff.toml": "select = [\n",
            "pyproject.toml": "not toml = = =\n",
            "package.json": "{not json",
        })
        result = inventory(self.root, paths)
        self.assertEqual(["ruff"], tool_names(result), "a broken pyproject.toml or package.json names no analyzer")
        self.assertEqual(
            [(".editorconfig", UNREAD), ("Broken.csproj", UNREAD), ("Entities.props", UNREAD), ("ruff.toml", UNREAD)],
            [(entry["file"], entry["setting"]) for entry in result["settings"]],
        )

    def test_settings_are_bounded(self) -> None:
        lines = "".join(f"dotnet_diagnostic.CA{index:04d}.severity = none\n" for index in range(MAX_SETTINGS + 5))
        result = inventory(self.root, self.write({".globalconfig": "is_global = true\n" + lines}))
        self.assertEqual(MAX_SETTINGS, len(result["settings"]))
        self.assertEqual("is_global = true", result["settings"][0]["setting"])
        self.assertTrue(result["settings_truncated"])

    def test_a_tool_lists_a_bounded_number_of_files(self) -> None:
        paths = self.write({f"src/P{index:02d}/P.csproj": SDK_PROJECT for index in range(12)})
        entry = next(item for item in inventory(self.root, paths)["tools"] if item["tool"] == "StyleCop.Analyzers")
        self.assertEqual(10, len(entry["files"]))
        self.assertEqual(2, entry["more_files"])

    def tools(self, result: dict) -> dict[str, list[str]]:
        return {entry["tool"]: entry["files"] for entry in result["tools"]}

    def test_an_empty_snapshot_has_the_schema_and_nothing_else(self) -> None:
        result = inventory(self.root, [])
        self.assertEqual({"schema_version": 1, "tools": [], "settings": [], "settings_truncated": False}, result)
        self.assertEqual([], tool_names(result))

    def test_sdk_analyzers_follow_the_project_kind(self) -> None:
        sdk = '<Project Sdk="Microsoft.NET.Sdk"></Project>\n'
        paths = self.write({
            "vb/App.vbproj": sdk,
            "fs/App.fsproj": sdk,
            "element/App.csproj": '<Project><Sdk Name="Microsoft.NET.Sdk" /></Project>\n',
            "upper/App.CSPROJ": sdk,
            "empty/App.csproj": '<Project Sdk=""></Project>\n',
            "Directory.Build.props": sdk,
            "build/Common.targets": sdk,
        })
        self.assertEqual({
            "Microsoft.CodeAnalysis.CSharp.CodeStyle": ["element/App.csproj", "upper/App.CSPROJ"],
            "Microsoft.CodeAnalysis.NetAnalyzers": ["element/App.csproj", "fs/App.fsproj", "upper/App.CSPROJ",
                                                    "vb/App.vbproj"],
            "Microsoft.CodeAnalysis.VisualBasic.CodeStyle": ["vb/App.vbproj"],
        }, self.tools(inventory(self.root, paths)),
            "an F# project has no code-style analyzer, an empty Sdk is not SDK-style, "
            "and props or targets are not projects")

    def test_analyzer_items_and_analysis_properties(self) -> None:
        paths = self.write({"build/Analyzers.targets": """<Project>
  <PropertyGroup>
    <NoWarn>CS1591;
      CA2007</NoWarn>
    <RunAnalyzers>  </RunAnalyzers>
    <WarningsAsErrors><Nested>CA1000</Nested></WarningsAsErrors>
    <AnalysisLevelStyle> latest </AnalysisLevelStyle>
    <LangVersion>latest</LangVersion>
    <CodeAnalysisRuleSet>team.ruleset</CodeAnalysisRuleSet>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Update="Ignored.Analyzers" Version="1.0.0" />
    <PackageReference Include="   " Version="1.0.0" />
    <PackageReference Include=" Padded.Analyzers " Version="1.0.0" />
    <PackageReference Include="British.Analysers" Version="1.0.0" />
    <PackageReference Include="asyncfixer" Version="1.0.0" />
    <PackageReference Include="Serilog" Version="1.0.0" />
    <PackageVersion Include="Pinned.Analyzers" Version="1.0.0" />
    <Reference Include="Referenced.Analyzers" />
    <Analyzer Include="tools\\Rules.Custom.dll" />
    <Analyzer Include="" />
  </ItemGroup>
</Project>
"""})
        result = inventory(self.root, paths)
        self.assertEqual(["asyncfixer", "British.Analysers", "Padded.Analyzers", "Rules.Custom"], tool_names(result))
        self.assertEqual([
            "<NoWarn>CS1591; CA2007</NoWarn>",
            "<AnalysisLevelStyle>latest</AnalysisLevelStyle>",
            "<CodeAnalysisRuleSet>team.ruleset</CodeAnalysisRuleSet>",
        ], self.settings(result, "build/Analyzers.targets"),
            "blank properties, properties with child elements, and unrelated properties are not settings")

    def test_namespaced_legacy_properties_are_read(self) -> None:
        paths = self.write({"Legacy.csproj": """<?xml version="1.0" encoding="utf-8"?>
<Project ToolsVersion="15.0" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <PropertyGroup><TreatWarningsAsErrors>true</TreatWarningsAsErrors></PropertyGroup>
</Project>
"""})
        result = inventory(self.root, paths)
        self.assertEqual([], tool_names(result))
        self.assertEqual(["<TreatWarningsAsErrors>true</TreatWarningsAsErrors>"],
                         self.settings(result, "Legacy.csproj"))

    def test_a_tool_merges_case_insensitively_under_its_first_name_in_path_order(self) -> None:
        paths = self.write({
            "B/B.csproj": '<Project><ItemGroup><PackageReference Include="stylecop.analyzers" /></ItemGroup></Project>',
            "a/A.csproj": '<Project><ItemGroup><PackageReference Include="StyleCop.Analyzers" /></ItemGroup></Project>',
        })
        self.assertEqual([{"tool": "StyleCop.Analyzers", "files": ["a/A.csproj", "B/B.csproj"]}],
                         inventory(self.root, paths)["tools"])

    def test_settings_follow_path_order_whatever_the_input_order(self) -> None:
        paths = self.write({
            "z/.shellcheckrc": "disable=SC2034\n",
            "A/.shellcheckrc": "disable=SC1091\n",
            "m/.shellcheckrc": "disable=SC2086\n",
        })
        self.assertEqual(["A/.shellcheckrc", "m/.shellcheckrc", "z/.shellcheckrc"],
                         [entry["file"] for entry in inventory(self.root, paths)["settings"]])

    def test_editorconfig_and_globalconfig_severities(self) -> None:
        paths = self.write({
            "config/team.globalconfig": "is_global = true\nglobal_level = 100\n"
                                        "dotnet_diagnostic.CA1000.severity = warning\n"
                                        "build_property.RootNamespace = App\n",
            ".editorconfig": "root = true\n"
                             "IS_GLOBAL = false\n"
                             "# dotnet_diagnostic.CA0001.severity = none\n"
                             "; dotnet_diagnostic.CA0002.severity = none\n"
                             "[*.cs]\n"
                             "DOTNET_DIAGNOSTIC.IDE0005.SEVERITY = error\n"
                             "dotnet_diagnostic.severity = none\n"
                             "dotnet_style_qualification_for_field = false:warning\n"
                             "dotnet_diagnostic.CA1001.severityx = none\n"
                             "dotnet_diagnostic.CA1002.severity = none\n"
                             "dotnet_diagnostic.CA1002.severity = error\n"
                             "[DEFAULT]\n"
                             "dotnet_diagnostic.CA1003.severity = 100%\n",
        })
        result = inventory(self.root, paths)
        self.assertEqual([], tool_names(result), "severities configure analyzers but do not name one")
        self.assertEqual([
            "IS_GLOBAL = false",
            "[*.cs] DOTNET_DIAGNOSTIC.IDE0005.SEVERITY = error",
            "[*.cs] dotnet_diagnostic.severity = none",
            "[*.cs] dotnet_diagnostic.CA1002.severity = error",
            "[DEFAULT] dotnet_diagnostic.CA1003.severity = 100%",
        ], self.settings(result, ".editorconfig"),
            "a repeated key keeps its last value, and DEFAULT is a plain section")
        self.assertEqual(["is_global = true", "global_level = 100", "dotnet_diagnostic.CA1000.severity = warning"],
                         self.settings(result, "config/team.globalconfig"))

    def test_an_indented_editorconfig_line_is_its_own_setting(self) -> None:
        # EditorConfig has no continuation lines, so configparser folded CA2000 into CA1000's value.
        paths = self.write({
            ".editorconfig": "[*.cs]\n"
                             "dotnet_diagnostic.CA1000.severity = warning\n"
                             "    dotnet_diagnostic.CA2000.severity = error\n",
            "team.globalconfig": "  is_global = true\n"
                                 "dotnet_diagnostic.CA1000.severity = warning\n"
                                 "\tdotnet_diagnostic.CA2000.severity = error\n",
        })
        result = inventory(self.root, paths)
        self.assertEqual(["[*.cs] dotnet_diagnostic.CA1000.severity = warning",
                          "[*.cs] dotnet_diagnostic.CA2000.severity = error"], self.settings(result, ".editorconfig"))
        self.assertEqual(["is_global = true", "dotnet_diagnostic.CA1000.severity = warning",
                          "dotnet_diagnostic.CA2000.severity = error"], self.settings(result, "team.globalconfig"))

    def test_editorconfig_lines_that_are_neither_section_nor_setting_leave_it_unread(self) -> None:
        for text in ("[*.cs]\nbroken\n", "[*.cs]\n  = error\n", "[]\nroot = true\n"):
            with self.subTest(text=text):
                result = inventory(self.root, self.write({".editorconfig": text}))
                self.assertEqual([UNREAD], self.settings(result, ".editorconfig"))

    def test_ruff_and_pyproject_tool_tables(self) -> None:
        paths = self.write({
            "a/pyproject.toml": '[tool.ruff]\nselect = ["E"]\n[tool.ruff.lint]\nextend-select = ["I"]\n'
                                '[tool.pylint]\n[tool.pyright]\n[tool.bandit]\n'
                                '[tool.flake8]\nextend-ignore = ["E203"]\nmax-line-length = 120\n',
            "b/pyproject.toml": "tool = 1\n",
            "c/pyproject.toml": '[project]\nname = "fixture"\n',
            "d/pyproject.toml": "[tool]\nruff = 1\nflake8 = 2\n",
            "e/ruff.toml": "lint = 1\nline-length = 100\n",
            "f/.ruff.toml": '[lint]\nignore = ["É501"]\nextend-ignore = []\n',
        })
        result = inventory(self.root, paths)
        self.assertEqual({
            "bandit": ["a/pyproject.toml"],
            "flake8": ["a/pyproject.toml", "d/pyproject.toml"],
            "pylint": ["a/pyproject.toml"],
            "pyright": ["a/pyproject.toml"],
            "ruff": ["a/pyproject.toml", "e/ruff.toml", "f/.ruff.toml"],
        }, self.tools(result), "a pyproject names a tool by its section, but ruff only when its section is a table")
        self.assertEqual([
            'tool.ruff.select = ["E"]',
            'tool.ruff.lint.extend-select = ["I"]',
            'tool.flake8.extend-ignore = ["E203"]',
        ], self.settings(result, "a/pyproject.toml"))
        self.assertEqual([], self.settings(result, "e/ruff.toml"))
        self.assertEqual(['lint.ignore = ["É501"]', "lint.extend-ignore = []"], self.settings(result, "f/.ruff.toml"))

    def test_flake8_setup_cfg_and_tox_ini(self) -> None:
        paths = self.write({
            "setup.cfg": "[mypy]\nstrict = True\n[pylint.MESSAGES CONTROL]\ndisable = C0114\n"
                         "[FLAKE8]\nSelect = E,W\nextend_ignore =\n    E203,\n    W503\nmax-line-length = 120\n",
            "tox.ini": "[flake8]\nextend-select = B\n",
            "pkg/setup.cfg": "[mypy-pkg.*]\nignore_missing_imports = True\n",
            "lib/.flake8": "[other]\nselect = E\n",
            "bad/.flake8": "select = E\n",
            "bad/setup.cfg": "select = E\n",
            "bad/tox.ini": "[flake8\n",
        })
        result = inventory(self.root, paths)
        self.assertEqual({
            "flake8": ["bad/.flake8", "lib/.flake8", "setup.cfg", "tox.ini"],
            "mypy": ["setup.cfg"],
            "pylint": ["setup.cfg"],
        }, self.tools(result), "a per-module mypy section alone, or a broken setup.cfg or tox.ini, names no analyzer")
        self.assertEqual(["[flake8] select = E,W", "[flake8] extend_ignore = E203, W503"],
                         self.settings(result, "setup.cfg"))
        self.assertEqual(["[flake8] extend-select = B"], self.settings(result, "tox.ini"))
        self.assertEqual([], self.settings(result, "lib/.flake8"), "a .flake8 names flake8 even without the section")
        self.assertEqual([("bad/.flake8", UNREAD)],
                         [(entry["file"], entry["setting"]) for entry in result["settings"]
                          if entry["file"].startswith("bad/")])

    def test_shellcheck_directives(self) -> None:
        paths = self.write({
            "shellcheckrc": "\n   \n  # indented comment\n  source-path=SCRIPTDIR  \nexternal-sources=true\n",
            "empty/.shellcheckrc": "",
        })
        result = inventory(self.root, paths)
        self.assertEqual([{"tool": "ShellCheck", "files": ["empty/.shellcheckrc", "shellcheckrc"]}], result["tools"])
        self.assertEqual(["source-path=SCRIPTDIR", "external-sources=true"], self.settings(result, "shellcheckrc"))
        self.assertEqual([], self.settings(result, "empty/.shellcheckrc"))

    def test_package_json_names_eslint_and_its_plugins(self) -> None:
        paths = self.write({
            "a/package.json": '{"eslintConfig": {}, "dependencies": {"eslint-plugin": "1", "@acme/eslint-plugin": "1",'
                              ' "@acme/eslint-plugin-rules": "1", "eslint-config-airbnb": "1", "@eslint/js": "1",'
                              ' "typescript-eslint": "1"}, "peerDependencies": {"eslint-plugin-peer": "1"}}',
            "b/package.json": '["eslint"]',
            "c/package.json": '{"dependencies": ["eslint"], "devDependencies": "eslint"}',
        })
        self.assertEqual({
            "@acme/eslint-plugin": ["a/package.json"],
            "@acme/eslint-plugin-rules": ["a/package.json"],
            "eslint": ["a/package.json"],
            "eslint-plugin": ["a/package.json"],
        }, self.tools(inventory(self.root, paths)), "configs, peers, and non-object documents or groups name nothing")

    def test_named_configuration_files_are_listed_without_being_read(self) -> None:
        paths = [".pylintrc", "mypy.ini", "web/biome.jsonc", "go/.golangci.toml", "rs/clippy.toml",
                 "java/checkstyle.xml", ".rubocop.yml", "ui/eslint.config.mjs", ".semgrep.yml", ".semgrep/a/b.yaml",
                 ".semgrep/rules.json", "sub/.semgrep/rules.yml", "README.md", "pyproject.toml.bak"]
        result = inventory(self.root, paths)  # none of these files exists
        self.assertEqual({
            "Biome": ["web/biome.jsonc"],
            "Checkstyle": ["java/checkstyle.xml"],
            "Clippy": ["rs/clippy.toml"],
            "eslint": ["ui/eslint.config.mjs"],
            "golangci-lint": ["go/.golangci.toml"],
            "mypy": ["mypy.ini"],
            "pylint": [".pylintrc"],
            "RuboCop": [".rubocop.yml"],
            "Semgrep": [".semgrep.yml", ".semgrep/a/b.yaml"],
        }, self.tools(result), "Semgrep rules count only as YAML under the top-level .semgrep folder")
        self.assertEqual([], result["settings"])

    def test_undecodable_or_missing_files(self) -> None:
        invalid = b"\x80 not utf-8\n"
        paths = self.write({name: invalid for name in (
            ".editorconfig", "a/App.csproj", "b/team.globalconfig", "c/.shellcheckrc", "d/ruff.toml", "e/.ruff.toml",
            "f/.flake8", "g/setup.cfg", "h/tox.ini", "i/pyproject.toml", "j/package.json",
        )})
        result = inventory(self.root, [*paths, "missing/.editorconfig", "missing/setup.cfg"])
        self.assertEqual(
            [(path, UNREAD) for path in (".editorconfig", "a/App.csproj", "b/team.globalconfig", "c/.shellcheckrc",
                                         "d/ruff.toml", "e/.ruff.toml", "f/.flake8", "missing/.editorconfig")],
            [(entry["file"], entry["setting"]) for entry in result["settings"]],
            "files that may configure no analyzer at all are not listed when they cannot be read",
        )
        # A file whose name alone names its tool still names it, as when it cannot be parsed, so a reviewer can
        # report that tool's coverage. A Windows-1252 comment once hid ShellCheck entirely.
        self.assertEqual({"ShellCheck": ["c/.shellcheckrc"], "ruff": ["d/ruff.toml", "e/.ruff.toml"],
                          "flake8": ["f/.flake8"]}, self.tools(result))

    def test_encodings_and_declarations(self) -> None:
        sdk = '<?xml version="1.0" encoding="windows-1252"?>\n<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>' \
              "<NoWarn>CS1591;é</NoWarn></PropertyGroup></Project>\n"
        paths = self.write({
            "be/App.csproj": codecs.BOM_UTF16_BE + sdk.encode("utf-16-be"),
            "bom/App.csproj": codecs.BOM_UTF8 + sdk.encode("utf-8"),
            ".editorconfig": codecs.BOM_UTF8 + b"[*.cs]\ndotnet_diagnostic.CA1000.severity = none\n",
            "Comment.props": "<Project><!-- see <!DOCTYPE in the docs --></Project>",
        })
        result = inventory(self.root, paths)
        self.assertEqual(["be/App.csproj", "bom/App.csproj"],
                         self.tools(result)["Microsoft.CodeAnalysis.NetAnalyzers"])
        self.assertEqual(["<NoWarn>CS1591;é</NoWarn>"], self.settings(result, "be/App.csproj"),
                         "a declaration naming another encoding never overrides the decoded text")
        self.assertEqual(["<NoWarn>CS1591;é</NoWarn>"], self.settings(result, "bom/App.csproj"))
        self.assertEqual(["[*.cs] dotnet_diagnostic.CA1000.severity = none"], self.settings(result, ".editorconfig"))
        self.assertEqual([UNREAD], self.settings(result, "Comment.props"), "any declaration text refuses the file")

    def test_settings_truncate_only_past_four_hundred(self) -> None:
        for count, truncated in ((400, False), (401, True)):
            with self.subTest(count=count):
                lines = "".join(f"disable=SC{index:04d}\n" for index in range(count))
                result = inventory(self.root, self.write({f"n{count}/.shellcheckrc": lines}))
                self.assertEqual(400, len(result["settings"]))
                self.assertEqual("disable=SC0399", result["settings"][-1]["setting"])
                self.assertEqual(truncated, result["settings_truncated"])

    def test_more_files_appears_only_past_ten(self) -> None:
        for count, more in ((10, None), (11, 1)):
            with self.subTest(count=count):
                paths = self.write({f"n{count}/P{index:02d}/.shellcheckrc": "" for index in range(count)})
                entry = inventory(self.root, paths)["tools"][0]
                self.assertEqual(paths[:10], entry["files"])
                self.assertEqual(more, entry.get("more_files"))
                self.assertEqual({"tool", "files"} | ({"more_files"} if more else set()), set(entry))


if __name__ == "__main__":
    unittest.main()
