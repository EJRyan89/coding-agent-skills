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


if __name__ == "__main__":
    unittest.main()
