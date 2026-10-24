"""Inventory the diagnostic analyzers a repository already has, from its hash-verified source snapshot.

A reviewer may mark a finding's analyzer coverage `available` only for a tool this inventory lists, so "the
repository already has this analyzer" is checked against the repository's own files, not a reviewer's memory.

Analyzers are recognized by their configuration files and, for .NET, by analyzer package references and SDK-style
projects (which carry the SDK's own analyzers). Where the format is one the standard library reads, the inventory
also lists the settings that decide which rules run and how severely: MSBuild analysis properties, .editorconfig and
.globalconfig diagnostic severities, ruff and flake8 rule selections, and ShellCheck directives. Nothing here runs an
analyzer or executes repository code. Each settings file is read whole, within the reader's size bound, and parsed in
memory, and one that cannot be read or parsed, for any reason its parser gives on its content, is listed as present but
unread and names a tool only when its file name does. EditorConfig files are read one `key = value` per line, since the
format has no continuation lines; setup.cfg and tox.ini keep configparser's, which are real there.
"""

from __future__ import annotations

import codecs
import configparser
import json
import re
import sys
import tomllib
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import json_reader
from dotnet_solution import read_bounded, read_text
from flat_text import flat_text
from msbuild_xml import is_sdk_style, local_name
from review_io import CONTENT_ERRORS
from xml_reader import read_xml

INVENTORY_SCHEMA_VERSION = 1
MAX_SETTINGS = 400
MAX_FILES_PER_TOOL = 10
UNREAD = "(present, but could not be parsed; read the file for its settings)"
# What a parser here raises on content it cannot read: what json and tomllib raise, `XmlRefused` among the ValueErrors,
# and configparser's own errors.
PARSE_ERRORS = (*CONTENT_ERRORS, configparser.Error)

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
        self.settings.append({"file": path, "setting": flat_text(text)})

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
    """A file's text, or None when it cannot be read. An MSBuild file is read as the .NET skills read one, through
    ejryan89-skill-core's `read_text`, which decodes a UTF-16 file by its byte-order mark, as older Visual Studio
    projects are written, and replaces a byte that is not UTF-8. Every other file is decoded strictly, by the same
    byte-order marks, so one whose text is not valid is listed unread rather than read with its settings garbled."""
    file = root.joinpath(*PurePosixPath(path).parts)
    try:
        if PurePosixPath(path).suffix.lower() in MSBUILD_SUFFIXES:
            return read_text(file)
        data = read_bounded(file)
        if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            return data.decode("utf-16")
        return data.decode("utf-8-sig")
    except (OSError, UnicodeError):
        return None


class _Unparsed(ValueError):
    """Content a reader here refuses although no parser raised on it."""


def _msbuild(found: _Inventory, path: str, text: str) -> None:
    project = read_xml(text)
    suffix = PurePosixPath(path).suffix.lower()
    if is_sdk_style(project) and suffix in {".csproj", ".vbproj", ".fsproj"}:
        found.tool(SDK_ANALYZERS, path)
        if suffix in CODE_STYLE_ANALYZERS:
            found.tool(CODE_STYLE_ANALYZERS[suffix], path)
    for element in project.iter():
        name = local_name(element)
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
            raise _Unparsed("a line that is neither a section nor a setting")
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
    document = tomllib.loads(text)
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
    parser.read_string(text)
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
    document = json_reader.loads(text)
    # JSON's \u escapes can spell a lone surrogate, which no UTF-8 file, analyzers.json included, can hold.
    json.dumps(document, ensure_ascii=False).encode("utf-8")
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
        handler = _settings_handler(path)
        if handler is None:
            continue
        text = _text(root, path)
        if text is None or not _parsed(handler, found, path, text):
            # A file whose name names its tool still names it, so a reviewer can report that tool's coverage; an
            # MSBuild file, setup.cfg, tox.ini, pyproject.toml, or package.json may configure none, so it names none.
            if name in NAMED_BY_FILE:
                found.tool(NAMED_BY_FILE[name], path)
            found.setting(path, UNREAD)
    return found.result()


def _parsed(handler: Callable[[_Inventory, str, str], None], found: _Inventory, path: str, text: str) -> bool:
    """Whether `handler` read the file, listing what it found; False when its parser refused the content."""
    try:
        handler(found, path, text)
    except PARSE_ERRORS:
        return False
    return True


# The files whose text the inventory parses for settings, by name; MSBuild files are read by their suffix and
# *.globalconfig files by their ending. The threat model's analyzer-inventory rows name each of them.
PARSED_BY_NAME: dict[str, Callable[[_Inventory, str, str], None]] = {
    ".editorconfig": _editorconfig,
    "ruff.toml": _toml,
    ".ruff.toml": _toml,
    "pyproject.toml": _toml,
    ".flake8": _ini,
    "setup.cfg": _ini,
    "tox.ini": _ini,
    ".shellcheckrc": _shellcheckrc,
    "shellcheckrc": _shellcheckrc,
    "package.json": _package_json,
}


def _settings_handler(path: str) -> Callable[[_Inventory, str, str], None] | None:
    """The reader of a file whose text the inventory parses for settings, by its name, or None."""
    posix = PurePosixPath(path)
    name, suffix = posix.name, posix.suffix.lower()
    if name in CONFIG_FILES or (posix.parts[0] == ".semgrep" and suffix in {".yml", ".yaml"}):
        return None  # named by the file alone
    if suffix in MSBUILD_SUFFIXES:
        return _msbuild
    if name.endswith(".globalconfig"):
        return _editorconfig
    return PARSED_BY_NAME.get(name)


def reads_settings(path: str) -> bool:
    """Whether the inventory reads this snapshot file's text, so a lazy snapshot writes it before the inventory runs."""
    return _settings_handler(path) is not None


def tool_names(value: dict[str, Any]) -> list[str]:
    """The tool names an inventory lists."""
    return [entry["tool"] for entry in value["tools"]]
