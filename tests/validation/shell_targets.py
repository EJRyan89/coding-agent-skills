"""Shell and PowerShell targets: tokens in shipped Bash and PowerShell, ShellCheck on skill scripts and Markdown
Bash fences, and PSScriptAnalyzer on .ps1 files and PowerShell fences.
"""

from __future__ import annotations

import ast
import json
import re
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from job_pool import run_process
from toolchain import VALIDATION_FLOORS, find_git_bash, find_powershell, find_psscriptanalyzer, find_shellcheck
from validation_support import (
    REPOSITORY_ROOT,
    TEMPLATE_TOKEN,
    is_shell_script,
    relative,
    repository_files,
    repository_skill_directories,
)

from deployer import platform_support, render, tools

SHELL_LABELS = {"shell": "Bash", "powershell": "PowerShell"}
SHELL_ESCAPES = {"shell": "\\", "powershell": "`"}
# The finder whose result runs each context's rendered script, called as a function or a method.
SHELL_FINDERS = {"find_bash": "shell", "find_pwsh": "powershell", "find_powershell": "powershell"}
SHELL_WORD_BREAKS = " \t\n;|&()"


def _shell_units(path: Path) -> list[tuple[str, int, str]]:
    """A shipped file's Bash and PowerShell as (context, first line, text), classified as the deployer renders it."""
    context = render.SUFFIX_CONTEXT.get(path.suffix.casefold())
    text = path.read_text(encoding="utf-8", errors="replace")
    if context in SHELL_LABELS:
        return [(context, 1, text)]
    if context != "markdown":
        return []
    lines = text.split("\n")
    return [
        (fence.context, fence.opener + 2, "\n".join(lines[index] for index in fence.body(len(lines))))
        for fence in render.find_fences(lines)
        if fence.closer is not None and fence.context in SHELL_LABELS
    ]


def _shell_tokens(text: str, context: str) -> list[tuple[int, str, bool]]:
    """Each token outside a comment, as (line offset, name, whether it sits inside quotes).

    Heredocs are not parsed, so a token in a heredoc body counts as unquoted.
    """
    escape = SHELL_ESCAPES[context]
    found: list[tuple[int, str, bool]] = []
    quote: str | None = None
    line = 0
    word_start = True
    index = 0
    while index < len(text):
        token = TEMPLATE_TOKEN.match(text, index)
        if token:
            found.append((line, token.group(1), quote is not None))
            index = token.end()
            word_start = False
            continue
        char = text[index]
        if char == escape and quote != "'":
            following = text[index + 1 : index + 2]
            # An escape before a token does not quote the value the token renders to.
            if following and not TEMPLATE_TOKEN.match(text, index + 1):
                line += following == "\n"
                index += 2
                word_start = False
                continue
        elif quote is None and char == "#" and word_start:
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
            continue
        elif quote is None and char in "'\"":
            quote = char
        elif char == quote:
            quote = None
        line += char == "\n"
        word_start = quote is None and char in SHELL_WORD_BREAKS
        index += 1
    return found


def _called(node: ast.AST) -> str:
    """The name a call calls, as a function or a method, or "" for anything else."""
    if not isinstance(node, ast.Call):
        return ""
    function = node.func
    return (
        function.attr if isinstance(function, ast.Attribute) else function.id if isinstance(function, ast.Name) else ""
    )


def _single_assignments(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[str, ast.expr]]:
    return [
        (node.targets[0].id, node.value)
        for node in ast.walk(function)
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
    ]


def _spaced_value(value: ast.expr) -> bool:
    """Whether an expression is built from a literal holding a space, other than a template that carries a token."""
    return any(
        isinstance(part, ast.Constant)
        and isinstance(part.value, str)
        and " " in part.value
        and not TEMPLATE_TOKEN.search(part.value)
        for part in ast.walk(value)
    )


def _runs(value: ast.expr, finders: dict[str, str]) -> str | None:
    """The context whose finder starts a run_tool call, as its first argument's first item, or None."""
    if _called(value) != "run_tool" or not isinstance(value, ast.Call) or not value.args:
        return None
    command = value.args[0]
    first = command.elts[0] if isinstance(command, (ast.List, ast.Tuple)) and command.elts else None
    if isinstance(first, ast.Name):
        return finders.get(first.id)
    return SHELL_FINDERS.get(_called(first)) if first is not None else None


def _spaced_runs(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """The contexts in which a test runs a rendered result and asserts its output against a value with a space."""
    assignments = _single_assignments(function)
    finders = {name: SHELL_FINDERS[_called(value)] for name, value in assignments if _called(value) in SHELL_FINDERS}
    spaced = {name for name, value in assignments if _spaced_value(value)}
    runs = {name: context for name, value in assignments if (context := _runs(value, finders))}
    contexts: set[str] = set()
    for node in ast.walk(function):
        if _called(node) in {"assertEqual", "assertIn"} and isinstance(node, ast.Call) and len(node.args) >= 2:
            first, second = ({part.id for part in ast.walk(arg) if isinstance(part, ast.Name)} for arg in node.args[:2])
            for ran, against in ((first, second), (second, first)):
                if against & spaced:
                    contexts |= {runs[name] for name in ran & set(runs)}
    return contexts


def _literal_tokens(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """The tokens a function's string literals carry, its docstring and comments aside."""
    docstring = ast.get_docstring(function, clean=False)
    return {
        token
        for node in ast.walk(function)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value != docstring
        for token in TEMPLATE_TOKEN.findall(node.value)
    }


def _executed_shell_tokens(root: Path) -> set[tuple[str, str]]:
    """The (context, token) pairs a tests/deployer test named *with_spaces* renders and executes with a spaced value.

    The test function carries the token in a string literal, binds a value built from a literal with a space, runs
    the rendered result with that context's finder through run_tool, and asserts the run's output against that value.
    """
    executed: set[tuple[str, str]] = set()
    for path in sorted((root / "tests" / "deployer").glob("test_*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_") or "with_spaces" not in node.name:
                continue
            executed |= {(context, token) for context in _spaced_runs(node) for token in _literal_tokens(node)}
    return executed


def shell_token_problems(root: Path) -> list[str]:
    """Report a token in shipped Bash or PowerShell outside quotes, or with no execution fixture in that language.

    "Template values" in docs/adding-a-skill.md keeps tokens quoted in executable content, and CLAUDE.md asks for a
    rendered execution fixture with representative values containing spaces.
    """
    unquoted: list[str] = []
    first: dict[tuple[str, str], tuple[str, int]] = {}
    files = sorted(
        (path for path in (root / "skills").rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    for path in files:
        name = path.relative_to(root).as_posix()
        for context, start, text in _shell_units(path):
            for offset, token, quoted in _shell_tokens(text, context):
                line = start + offset
                if not quoted:
                    unquoted.append(f"{name}:{line} has {{{{{token}}}}} outside quotes in {SHELL_LABELS[context]}")
                first.setdefault((context, token), (name, line))
    executed = _executed_shell_tokens(root)
    missing = sorted(
        (name, line, token, context)
        for (context, token), (name, line) in first.items()
        if (context, token) not in executed
    )
    return unquoted + [
        f"{name}:{line} carries {{{{{token}}}}} in {SHELL_LABELS[context]}, but no tests/deployer test named "
        "*with_spaces* renders and executes it there"
        for name, line, token, context in missing
    ]


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def run_git_bash(command: str) -> None:
    environment: dict[str, str] = {}
    prefix = ""
    shellcheck = find_shellcheck()
    if shellcheck is not None:
        # A login shell rebuilds PATH, which can drop the directory ShellCheck was found in.
        environment["SHELLCHECK_BIN_DIR"] = platform_support.to_shell_path(str(Path(shellcheck).parent))
        prefix = 'export PATH="$SHELLCHECK_BIN_DIR:$PATH"; '
    root = shell_quote(platform_support.to_shell_path(str(REPOSITORY_ROOT)))
    run_process(
        [find_git_bash(), "-l", "-c", f"{prefix}set -euo pipefail; cd {root}; {command}"],
        environment,
    )


def shell_script_targets(root: Path) -> list[Path]:
    """Every Bash script under skills/, and under each repository skill's scripts/."""
    scripts = [path for path in (root / "skills").rglob("*") if path.is_file() and is_shell_script(path)]
    for skill in repository_skill_directories(root):
        scripts += [path for path in (skill / "scripts").rglob("*") if path.is_file() and is_shell_script(path)]
    return sorted(scripts, key=lambda path: path.relative_to(root).as_posix().casefold())


def static_shell_check() -> None:
    scripts = shell_script_targets(REPOSITORY_ROOT)
    if not scripts:
        raise AssertionError("No shell scripts were found to validate.")
    checks = " && ".join(f"bash -n {shell_quote(relative(path))}" for path in scripts)
    arguments = " ".join(shell_quote(relative(path)) for path in scripts)
    run_git_bash(f"{checks} && shellcheck --severity=warning {arguments}")


# PSScriptAnalyzer reads every .ps1 under these roots, and every PowerShell fence in Markdown outside tests/.
SCRIPT_ANALYZER_ROOTS = ("tools", "tests", "skills", ".claude/skills")
# Reads the targets from the JSON file PSSA_TARGETS names and prints one JSON array of Warning and Error findings.
SCRIPT_ANALYZER_RUN = (
    "$ErrorActionPreference = 'Stop'; "
    "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
    "Import-Module PSScriptAnalyzer -MinimumVersion $env:PSSA_FLOOR; "
    "$targets = @(Get-Content -LiteralPath $env:PSSA_TARGETS -Raw -Encoding utf8 | ConvertFrom-Json); "
    "$found = [Collections.Generic.List[object]]::new(); "
    "for ($index = 0; $index -lt $targets.Count; $index++) { "
    "$target = $targets[$index]; "
    "$source = if ($null -ne $target.path) { @{ Path = $target.path } } else { @{ ScriptDefinition = $target.text } }; "
    "foreach ($record in Invoke-ScriptAnalyzer @source -Severity Warning, Error) { "
    "$found.Add([ordered]@{ target = $index; line = $record.Line; rule = $record.RuleName; "
    'severity = "$($record.Severity)"; message = $record.Message }) } }; '
    "[Console]::Out.WriteLine((ConvertTo-Json -InputObject $found.ToArray() -Compress -Depth 3))"
)


@dataclass(frozen=True)
class PowerShellTarget:
    """A .ps1 file (path) or a PowerShell fence (text) for PSScriptAnalyzer, named by where its first line sits."""

    name: str
    first_line: int
    path: Path | None
    text: str | None


def powershell_targets(root: Path, files: list[Path]) -> list[PowerShellTarget]:
    """Every .ps1 under SCRIPT_ANALYZER_ROOTS, and every non-empty PowerShell fence in Markdown outside tests/."""
    targets: list[PowerShellTarget] = []
    for path in sorted(files, key=lambda path: path.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        suffix = path.suffix.casefold()
        if suffix == ".ps1" and name.startswith(tuple(f"{top}/" for top in SCRIPT_ANALYZER_ROOTS)):
            targets.append(PowerShellTarget(name, 1, path, None))
        elif suffix == ".md" and not name.startswith("tests/"):
            targets += [
                PowerShellTarget(name, start, None, text)
                for context, start, text in _shell_units(path)
                if context == "powershell" and text.strip()
            ]
    return targets


def script_analyzer_check(root: Path, files: list[Path]) -> None:
    """Fail, naming each finding by file, line, and rule, when PSScriptAnalyzer warns on any PowerShell target."""
    if find_psscriptanalyzer() is None:
        raise AssertionError(f"PSScriptAnalyzer was not found: {platform_support.install_hint('PSScriptAnalyzer')}")
    targets = powershell_targets(root, files)
    if not targets:
        return
    with tempfile.TemporaryDirectory(prefix="psscriptanalyzer-") as directory:
        request = Path(directory) / "targets.json"
        request.write_text(
            json.dumps(
                [{"path": str(target.path) if target.path else None, "text": target.text} for target in targets]
            ),
            encoding="utf-8",
        )
        result = platform_support.run_tool(
            [find_powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", SCRIPT_ANALYZER_RUN],
            {
                "PSSA_TARGETS": str(request),
                "PSSA_FLOOR": tools.format_version(VALIDATION_FLOORS["PSScriptAnalyzer"]),
            },
        )
    lines = [line for line in result.output.splitlines() if line.strip()]
    try:
        findings = json.loads(lines[-1]) if result.returncode == 0 and lines else None
    except json.JSONDecodeError:
        findings = None
    if not isinstance(findings, list):
        raise AssertionError(f"PSScriptAnalyzer could not run (exit code {result.returncode}):\n{result.output}")
    if findings:
        reported = [
            f"  {targets[finding['target']].name}:{targets[finding['target']].first_line + finding['line'] - 1}: "
            f"{finding['rule']} ({finding['severity']}): {finding['message']}"
            for finding in findings
        ]
        raise AssertionError(
            "PSScriptAnalyzer found these Warning and Error diagnostics. Fix each at its cause; never suppress one "
            'with SuppressMessageAttribute or a settings file (CLAUDE.md, "Required validation"):\n'
            + "\n".join(reported)
        )


def static_powershell_check(root: Path = REPOSITORY_ROOT) -> None:
    script_analyzer_check(root, repository_files(root))


# ShellCheck reads every Bash fence in Markdown outside these roots: Markdown under skills/ is rendered and checked at
# deployment, with its tokens substituted, and tests/ holds fixtures.
MARKDOWN_SHELL_EXCLUDED_ROOTS = ("skills", "tests")


@dataclass(frozen=True)
class BashFence:
    """A Bash fence in Markdown, named by its file and the line its body starts on."""

    name: str
    first_line: int
    text: str


def markdown_shell_targets(root: Path, files: list[Path]) -> list[BashFence]:
    """Every closed, non-empty Bash fence in Markdown outside MARKDOWN_SHELL_EXCLUDED_ROOTS."""
    targets: list[BashFence] = []
    for path in sorted(files, key=lambda path: path.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        if path.suffix.casefold() != ".md" or name.split("/")[0] in MARKDOWN_SHELL_EXCLUDED_ROOTS:
            continue
        targets += [
            BashFence(name, start, text)
            for context, start, text in _shell_units(path)
            if context == "shell" and text.strip()
        ]
    return targets


def markdown_shell_check(root: Path, files: list[Path]) -> None:
    """Fail, naming each finding by file, line, and code, when ShellCheck warns on a Bash fence in Markdown.

    Each fence is checked as a fragment under the header deployer/render.py gives the command examples it extracts,
    the one sanctioned suppression (CLAUDE.md, "Required validation").
    """
    shellcheck = find_shellcheck()
    if shellcheck is None:
        raise AssertionError(f"ShellCheck was not found: {platform_support.install_hint('ShellCheck')}")
    targets = markdown_shell_targets(root, files)
    if not targets:
        return
    header_lines = render.SHELLCHECK_HEADER.count("\n")
    with tempfile.TemporaryDirectory(prefix="markdown-shellcheck-") as directory:
        fragments: dict[str, BashFence] = {}
        for index, target in enumerate(targets):
            path = Path(directory) / f"{index}.sh"
            path.write_text(f"{render.SHELLCHECK_HEADER}{target.text}\n", encoding="utf-8", newline="\n")
            fragments[platform_support.normalize(path)] = target
        result = platform_support.run_tool([shellcheck, "--format=gcc", "--severity=warning", *fragments])
    if result.returncode == 0:
        return
    reported: list[str] = []
    for line in result.output.splitlines():
        found = next((path for path in fragments if line.startswith(f"{path}:")), None)
        position = re.match(r":(\d+)(:.*)$", line[len(found) :]) if found else None
        if found is None or position is None:
            reported.append(f"  {line}")
            continue
        target = fragments[found]
        reported.append(
            f"  {target.name}:{target.first_line + int(position.group(1)) - header_lines - 1}{position.group(2)}"
        )
    raise AssertionError(
        "ShellCheck found these warnings in Bash fences in Markdown. Fix each at its cause; never suppress one "
        '(CLAUDE.md, "Required validation"):\n' + "\n".join(reported)
    )


def static_markdown_shell_check(root: Path = REPOSITORY_ROOT) -> None:
    markdown_shell_check(root, repository_files(root))


# A ShellCheck directive that disables a check. The header deployer/render.py adds to the command examples it
# extracts is the one sanctioned suppression, and it lives in that Python module, never in a Bash file or fence.
SHELLCHECK_DISABLE = re.compile(r"^[ \t]*#[ \t]*shellcheck[ \t][^\n]*\bdisable[ \t]*=", re.IGNORECASE | re.MULTILINE)
# PSScriptAnalyzer's suppression attribute, under any of the names PowerShell resolves to it, wherever it stands in an
# attribute list: first after the bracket, or after another attribute and a comma, on the same line or the next.
SUPPRESS_MESSAGE = re.compile(r"\bSuppressMessage(?:Attribute)?\s*\(", re.IGNORECASE)
# Configuration files ShellCheck and PSScriptAnalyzer find on their own, which could disable a rule for every file.
SHELL_CONFIGURATION_FILES = frozenset({".shellcheckrc", "shellcheckrc", "psscriptanalyzersettings.psd1"})


def shell_suppression_problems(root: Path, files: list[Path]) -> list[str]:
    """Each ShellCheck disable directive in a Bash file or a Markdown Bash fence, each SuppressMessageAttribute in a
    .ps1 file or a Markdown PowerShell fence, and each ShellCheck or PSScriptAnalyzer configuration file."""
    found: list[str] = []
    for path in sorted(files, key=lambda path: path.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        if path.name.casefold() in SHELL_CONFIGURATION_FILES:
            found.append(f"{name} configures {'ShellCheck' if 'shellcheck' in path.name else 'PSScriptAnalyzer'}")
            continue
        if path.suffix.casefold() not in {".sh", ".bash", ".ps1", ".md"}:
            continue
        for context, start, text in _shell_units(path):
            pattern, what = (
                (SHELLCHECK_DISABLE, "a ShellCheck disable directive")
                if context == "shell"
                else (SUPPRESS_MESSAGE, "SuppressMessageAttribute")
            )
            found += [
                f"{name}:{start + text.count(chr(10), 0, match.start())} suppresses a rule with {what}"
                for match in pattern.finditer(text)
            ]
    return [f"{problem}; fix the cause instead" for problem in found]


class ShellTargetsPolicies(unittest.TestCase):
    def test_shell_tokens_are_quoted_and_executed_by_a_fixture(self) -> None:
        self.assertEqual([], shell_token_problems(REPOSITORY_ROOT))

    def test_repository_suppresses_no_shellcheck_or_psscriptanalyzer_rule(self) -> None:
        self.assertEqual([], shell_suppression_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))
