"""Inventory the diagnostic analyzers a repository already has, from its hash-verified source snapshot.

A reviewer may mark a finding's analyzer coverage `available` only for a tool this inventory lists, so "the
repository already has this analyzer" is checked against the repository's own files, not a reviewer's memory.

Analyzers are recognized by their configuration files and, for .NET, by analyzer package references and SDK-style
projects (which carry the SDK's own analyzers). Where the format is one the standard library reads, the inventory
also lists the settings that decide which rules run and how severely: MSBuild analysis properties, .editorconfig and
.globalconfig diagnostic severities, ruff and flake8 rule selections, and ShellCheck directives. Nothing here runs an
analyzer or executes repository code, and a file that cannot be parsed is listed as present but unread. EditorConfig
files are read one `key = value` per line, since the format has no continuation lines; setup.cfg and tox.ini keep
configparser's, which are real there.
"""

from __future__ import annotations

import codecs
import configparser
import json
import re
import tomllib
import xml.etree.ElementTree as ElementTree
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

INVENTORY_SCHEMA_VERSION = 1
MAX_SETTINGS = 400
MAX_FILES_PER_TOOL = 10
UNREAD = "(present, but could not be parsed; read the file for its settings)"

SDK_ANALYZERS = "Microsoft.CodeAnalysis.NetAnalyzers"
CODE_STYLE_ANALYZERS = {
    ".csproj": "Microsoft.CodeAnalysis.CSharp.CodeStyle",
    ".vbproj": "Microsoft.CodeAnalysis.VisualBasic.CodeStyle",
}
MSBUILD_SUFFIXES = frozenset({".csproj", ".vbproj", ".fsproj", ".props", ".targets"})
# Items that put an analyzer into every build they reach; a PackageVersion only pins a version centrally.
ANALYZER_ITEMS = frozenset({"PackageReference", "GlobalPackageReference", "Analyzer"})
ANALYZER_PACKAGE = re.compile(r"analy[sz]er", re.IGNORECASE)
OTHER_ANALYZER_PACKAGES = frozenset({"asyncfixer"})
MSBUILD_PROPERTIES = frozenset(
    {
        "EnableNETAnalyzers",
        "EnforceCodeStyleInBuild",
        "TreatWarningsAsErrors",
        "WarningsAsErrors",
        "WarningsNotAsErrors",
        "NoWarn",
        "CodeAnalysisTreatWarningsAsErrors",
        "CodeAnalysisRuleSet",
        "RunAnalyzers",
        "RunAnalyzersDuringBuild",
        "TargetFramework",
        "TargetFrameworks",
    }
)
MSBUILD_PROPERTY_PREFIXES = ("AnalysisLevel", "AnalysisMode")
DIAGNOSTIC_KEY = re.compile(r"dotnet_(?:analyzer_)?diagnostic(?:\.[^\s=]+)?\.severity", re.IGNORECASE)
GLOBAL_KEYS = frozenset({"is_global", "global_level"})
XML_DECLARATION = re.compile(r"\A\s*<\?xml[^>]*\?>")
TOP_SECTION = "\x00top"
SECTION_HEADER = re.compile(r"\[(.+)\]")
RULE_SELECTIONS = ("select", "extend-select", "ignore", "extend-ignore")
FLAKE8_SELECTIONS = ("select", "extend-select", "ignore", "extend-ignore", "extend_select", "extend_ignore")
PYPROJECT_TOOLS = {"pylint": "pylint", "mypy": "mypy", "pyright": "pyright", "flake8": "flake8", "bandit": "bandit"}
# Configuration files whose name alone says which tool reads them.
NAMED_BY_FILE = {
    "ruff.toml": "ruff",
    ".ruff.toml": "ruff",
    ".flake8": "flake8",
    ".shellcheckrc": "ShellCheck",
    "shellcheckrc": "ShellCheck",
}
SETUP_CFG_TOOLS = {"flake8": "flake8", "mypy": "mypy", "pylint": "pylint", "pylint.messages control": "pylint"}
ESLINT_PLUGIN = re.compile(r"(?:@[^/]+/)?eslint-plugin(?:-.+)?")
# Configuration files that name their analyzer and whose settings this inventory does not read.
CONFIG_FILES = {
    ".pylintrc": "pylint",
    "pylintrc": "pylint",
    "mypy.ini": "mypy",
    ".mypy.ini": "mypy",
    "pyrightconfig.json": "pyright",
    "PSScriptAnalyzerSettings.psd1": "PSScriptAnalyzer",
    ".eslintrc": "eslint",
    ".eslintrc.json": "eslint",
    ".eslintrc.js": "eslint",
    ".eslintrc.cjs": "eslint",
    ".eslintrc.yaml": "eslint",
    ".eslintrc.yml": "eslint",
    "eslint.config.js": "eslint",
    "eslint.config.mjs": "eslint",
    "eslint.config.cjs": "eslint",
    "eslint.config.ts": "eslint",
    "eslint.config.mts": "eslint",
    "eslint.config.cts": "eslint",
    "biome.json": "Biome",
    "biome.jsonc": "Biome",
    ".stylelintrc": "stylelint",
    ".stylelintrc.json": "stylelint",
    "stylelint.config.js": "stylelint",
    ".golangci.yml": "golangci-lint",
    ".golangci.yaml": "golangci-lint",
    ".golangci.toml": "golangci-lint",
    ".golangci.json": "golangci-lint",
    ".rubocop.yml": "RuboCop",
    "clippy.toml": "Clippy",
    ".clippy.toml": "Clippy",
    ".swiftlint.yml": "SwiftLint",
    ".semgrep.yml": "Semgrep",
    ".semgrep.yaml": "Semgrep",
    ".hadolint.yaml": "hadolint",
    ".hadolint.yml": "hadolint",
    ".markdownlint.json": "markdownlint",
    ".markdownlint.yaml": "markdownlint",
    ".markdownlint.yml": "markdownlint",
    ".yamllint": "yamllint",
    ".yamllint.yaml": "yamllint",
    ".yamllint.yml": "yamllint",
    "checkstyle.xml": "Checkstyle",
}


class _Inventory:
    def __init__(self) -> None:
        self.tools: dict[str, tuple[str, set[str]]] = {}  # casefolded name -> (name as first seen, files)
        self.settings: list[dict[str, str]] = []
        self.truncated = False

    def tool(self, name: str, path: str) -> None:
        self.tools.setdefault(name.casefold(), (name, set()))[1].add(path)

    def setting(self, path: str, text: str) -> None:
        if len(self.settings) >= MAX_SETTINGS:
            self.truncated = True
            return
        self.settings.append({"file": path, "setting": " ".join(text.split())})

    def result(self) -> dict[str, Any]:
        tools = []
        for name, files in sorted(self.tools.values(), key=lambda item: item[0].casefold()):
            ordered = sorted(files, key=str.casefold)
            entry: dict[str, Any] = {"tool": name, "files": ordered[:MAX_FILES_PER_TOOL]}
            if len(ordered) > MAX_FILES_PER_TOOL:
                entry["more_files"] = len(ordered) - MAX_FILES_PER_TOOL
            tools.append(entry)
        return {
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "tools": tools,
            "settings": self.settings,
            "settings_truncated": self.truncated,
        }


def _text(root: Path, path: str) -> str | None:
    """A file's text. Older Visual Studio projects are UTF-16 with a byte-order mark."""
    try:
        data = root.joinpath(*PurePosixPath(path).parts).read_bytes()
        if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            return data.decode("utf-16")
        return data.decode("utf-8-sig")
    except (OSError, UnicodeError):
        return None


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _msbuild(found: _Inventory, path: str, text: str) -> None:
    # Declarations can expand entities without bound; no project or props file needs them.
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        found.setting(path, UNREAD)
        return
    try:
        # The text is already decoded, so a declaration naming another encoding would only contradict it.
        project = ElementTree.fromstring(XML_DECLARATION.sub("", text, count=1))
    except (ElementTree.ParseError, ValueError):
        found.setting(path, UNREAD)
        return
    suffix = PurePosixPath(path).suffix.lower()
    sdk_style = bool(project.get("Sdk")) or any(_local(element.tag) == "Sdk" for element in project)
    if sdk_style and suffix in {".csproj", ".vbproj", ".fsproj"}:
        found.tool(SDK_ANALYZERS, path)
        if suffix in CODE_STYLE_ANALYZERS:
            found.tool(CODE_STYLE_ANALYZERS[suffix], path)
    for element in project.iter():
        name = _local(element.tag)
        if name in ANALYZER_ITEMS:
            package = (element.get("Include") or "").strip()
            if name == "Analyzer" and package:
                found.tool(PurePosixPath(package.replace("\\", "/")).stem, path)
            elif package and (ANALYZER_PACKAGE.search(package) or package.casefold() in OTHER_ANALYZER_PACKAGES):
                found.tool(package, path)
        elif name in MSBUILD_PROPERTIES or name.startswith(MSBUILD_PROPERTY_PREFIXES):
            if element.text and element.text.strip() and not len(element):
                found.setting(path, f"<{name}>{element.text.strip()}</{name}>")


def _editorconfig(found: _Inventory, path: str, text: str) -> None:
    # One `key = value` per line: EditorConfig has no continuation lines, so indentation means nothing. A repeated
    # section or key merges into its first position and the last value wins.
    sections: dict[str, dict[str, str]] = {TOP_SECTION: {}}
    current = sections[TOP_SECTION]
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        header = SECTION_HEADER.fullmatch(stripped)
        key, equals, value = stripped.partition("=")
        if header:
            current = sections.setdefault(header.group(1), {})
        elif equals and key.strip():
            current[key.strip()] = value.strip()
        else:
            found.setting(path, UNREAD)
            return
    for section, values in sections.items():
        for key, value in values.items():
            if DIAGNOSTIC_KEY.fullmatch(key) or key.casefold() in GLOBAL_KEYS:
                prefix = "" if section == TOP_SECTION else f"[{section}] "
                found.setting(path, f"{prefix}{key} = {value}")


def _selections(found: _Inventory, path: str, table: Any, prefix: str, keys: Iterable[str]) -> None:
    if not isinstance(table, dict):
        return
    for key in keys:
        if key in table:
            found.setting(path, f"{prefix}{key} = {json.dumps(table[key], ensure_ascii=False)}")


def _ruff(found: _Inventory, path: str, table: dict[str, Any], prefix: str) -> None:
    found.tool("ruff", path)
    _selections(found, path, table, prefix, RULE_SELECTIONS)
    _selections(found, path, table.get("lint"), f"{prefix}lint.", RULE_SELECTIONS)


def _toml(found: _Inventory, path: str, text: str) -> None:
    name = PurePosixPath(path).name
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        if name != "pyproject.toml":
            found.tool("ruff", path)
            found.setting(path, UNREAD)
        return
    if name != "pyproject.toml":
        _ruff(found, path, document, "")
        return
    tools = document.get("tool")
    if not isinstance(tools, dict):
        return
    if isinstance(tools.get("ruff"), dict):
        _ruff(found, path, tools["ruff"], "tool.ruff.")
    for section, tool in PYPROJECT_TOOLS.items():
        if section in tools:
            found.tool(tool, path)
    _selections(found, path, tools.get("flake8"), "tool.flake8.", FLAKE8_SELECTIONS)


def _ini(found: _Inventory, path: str, text: str) -> None:
    name = PurePosixPath(path).name
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        if name == ".flake8":
            found.tool("flake8", path)
            found.setting(path, UNREAD)
        return
    sections = {section.casefold(): section for section in parser.sections()}
    if name == ".flake8":
        found.tool("flake8", path)
    else:
        for section, tool in SETUP_CFG_TOOLS.items():
            if section in sections:
                found.tool(tool, path)
    if "flake8" in sections:
        for key in FLAKE8_SELECTIONS:
            if parser.has_option(sections["flake8"], key):
                found.setting(path, f"[flake8] {key} = {parser.get(sections['flake8'], key, raw=True)}")


def _shellcheckrc(found: _Inventory, path: str, text: str) -> None:
    found.tool("ShellCheck", path)
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            found.setting(path, line.strip())


def _package_json(found: _Inventory, path: str, text: str) -> None:
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return
    if not isinstance(document, dict):
        return
    if "eslintConfig" in document:
        found.tool("eslint", path)
    for group in ("dependencies", "devDependencies"):
        packages = document.get(group)
        for package in packages if isinstance(packages, dict) else ():
            if package == "eslint" or ESLINT_PLUGIN.fullmatch(package):
                found.tool(package, path)


def inventory(root: Path, paths: Iterable[str]) -> dict[str, Any]:
    """The analyzers and analyzer settings in a snapshot, given its repository-relative file paths."""
    found = _Inventory()
    for path in sorted(paths, key=str.casefold):
        posix = PurePosixPath(path)
        name, suffix = posix.name, posix.suffix.lower()
        if name in CONFIG_FILES:
            found.tool(CONFIG_FILES[name], path)
            continue
        if posix.parts[0] == ".semgrep" and suffix in {".yml", ".yaml"}:
            found.tool("Semgrep", path)
            continue
        if suffix in MSBUILD_SUFFIXES:
            handler = _msbuild
        elif name == ".editorconfig" or name.endswith(".globalconfig"):
            handler = _editorconfig
        elif name in {"ruff.toml", ".ruff.toml", "pyproject.toml"}:
            handler = _toml
        elif name in {".flake8", "setup.cfg", "tox.ini"}:
            handler = _ini
        elif name in {".shellcheckrc", "shellcheckrc"}:
            handler = _shellcheckrc
        elif name == "package.json":
            handler = _package_json
        else:
            continue
        text = _text(root, path)
        if text is None:
            # A file whose name names its tool still names it, as when it cannot be parsed. A setup.cfg, tox.ini,
            # pyproject.toml, or package.json may configure no analyzer at all, so it is not listed.
            if name in NAMED_BY_FILE:
                found.tool(NAMED_BY_FILE[name], path)
            if handler in (_msbuild, _editorconfig) or name in NAMED_BY_FILE:
                found.setting(path, UNREAD)
            continue
        handler(found, path, text)
    return found.result()


def tool_names(value: dict[str, Any]) -> list[str]:
    """The tool names an inventory lists."""
    return [entry["tool"] for entry in value["tools"]]
