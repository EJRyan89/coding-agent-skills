"""Shell and PowerShell targets: tokens in shipped Bash and PowerShell, ShellCheck on skill scripts and Markdown
Bash fences, and PSScriptAnalyzer on .ps1 files and PowerShell fences.
"""

from __future__ import annotations

import ast
import itertools
import re
import tempfile
import unittest
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import file_presence
import frontmatter
from bash_syntax import BashSyntaxError, Word, every_command, expanded_words, here_documents, parse
from job_pool import refuse_termination, run_process
from path_containment import posix_within
from text_lines import LINE_BREAK, program_lines
from toolchain import (
    VALIDATION_FLOORS,
    bash_syntax_job,
    find_git_bash,
    find_psscriptanalyzer,
    find_shellcheck,
    powershell_json,
    shellcheck_job,
)
from validation_support import (
    REPOSITORY_ROOT,
    TEMPLATE_TOKEN,
    find_fences,
    is_shell_script,
    is_test_script,
    prose_code_spans,
    python_trees,
    relative,
    repository_files,
    repository_skill_directories,
)

from deployer import config, platform_support, render, tools

SHELL_LABELS = {"shell": "Bash", "powershell": "PowerShell"}
POWERSHELL_ESCAPE = "`"
# The finder whose result runs each context's rendered script, called as a function or a method.
SHELL_FINDERS = {"find_bash": "shell", "find_pwsh": "powershell", "find_powershell": "powershell"}
SHELL_WORD_BREAKS = " \t\n;|&()"
# What _bash_tokens writes in each token's place before the grammar reads the text: plain word characters, as a value
# that needs no quoting renders, numbered by the token's place among the text's tokens, so each one the grammar gives
# back is placed where the value would stand.
TOKEN_MARKER = re.compile(r"TEMPLATE_TOKEN_([0-9]+)_")
# Python named by hand, as a word of its own, where a shipped skill writes {{PYTHON}} so that the command renders as the
# platform names it: python on Windows and python3 elsewhere.
HAND_WRITTEN_INTERPRETER = re.compile(r"(?<![\w./{}-])python3?(?![\w./}-])")
# An inline code span in a shipped skill's Markdown prose that runs Python named by hand: the interpreter, its options,
# then what it runs. A span naming the interpreter or an option alone, such as `python -c`, names no command.
HAND_WRITTEN_SPAN_COMMAND = re.compile(r"\s*python3?(?:\s+-\S*)*\s+[^-\s]")
INTERPRETER_DOC = '"Template values" in docs/adding-a-skill.md'
# A token config.OPTIONAL_VARIABLES lets render empty, quoted as a word of its own. Windows PowerShell 5.1 drops an
# empty argument, so the option before it takes the next one; joined to its option, as --name="{{TOKEN}}", the
# word is never empty.
LONE_OPTIONAL_TOKEN = re.compile(
    r"(?<![^ \t\n;|&()])([\"'])\{\{(" + "|".join(config.OPTIONAL_VARIABLES) + r")\}\}\1(?![^ \t\n;|&()])"
)


# Commands whose program the commands reader cannot read, by the scope that starts them, and why.
COMMANDS_ALLOWED = {
    "run_bash_suite": "starts the Git Bash toolchain.find_git_bash located",
}


def _shell_units(path: Path) -> list[tuple[str, int, str]]:
    """A shipped file's Bash and PowerShell as (context, first line, text), classified as the deployer renders it. A
    fence left open runs to the end of what holds it, so its commands reach every check; unclosed_fence_problems
    reports it. Each line of a fence is read as CommonMark gives it to the fence's content, without the indentation
    of the fence and of the list items that hold it, so an indented fence's here-document closes at its delimiter,
    and a column a check reports counts from the content's start."""
    context = render.SUFFIX_CONTEXT.get(path.suffix.casefold())
    text = path.read_text(encoding="utf-8", errors="replace")
    if context in SHELL_LABELS:
        return [(context, 1, text)]
    if context != "markdown":
        return []
    lines = LINE_BREAK.split(text)
    return [
        (
            render.fence_context(fence),
            fence.opener + 2,
            "\n".join(fence.content(lines[index]) for index in fence.body()),
        )
        for fence in find_fences(lines)
        if render.fence_context(fence) in SHELL_LABELS
    ]


def unclosed_fence_problems(root: Path, files: list[Path]) -> list[str]:
    """Report each fence a Markdown file opens and never closes, outside tests/fixtures, which are other trees.

    One left open runs to the end of what holds it, so GitHub shows the rest of the file as code and every reader of
    fences reads it as the fence's body, which is rarely what its author meant.
    """
    problems: list[str] = []
    for path in sorted(files, key=lambda found: found.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        if path.suffix.casefold() != ".md" or name.startswith("tests/fixtures/"):
            continue
        lines = LINE_BREAK.split(path.read_text(encoding="utf-8", errors="replace"))
        problems += [
            f"{name}:{fence.opener + 1} opens a {fence.language or 'plain'} fence it never closes; close it"
            for fence in find_fences(lines)
            if fence.closer is None
        ]
    return problems


def _shell_tokens(text: str, context: str) -> list[tuple[int, str, bool | None]]:
    """Each token outside a comment, as (line offset, name, whether it sits inside quotes), in the order the text holds
    them: Bash's as _bash_tokens reads them, and PowerShell's as _powershell_tokens does. Raises BashSyntaxError for
    Bash the grammar refuses."""
    return _bash_tokens(text) if context == "shell" else _powershell_tokens(text)


def _bash_tokens(text: str) -> list[tuple[int, str, bool | None]]:
    """Each token of a Bash text outside a comment, read through ejryan89-skill-core's bash_syntax, the repository's
    one Bash grammar, as (line offset, name, whether it sits inside quotes). A token is quoted when every character of
    it stands inside single or double quotes in its word, wherever Bash reads that word: a command's, an assignment's,
    a redirection's, a case's or a loop's, in a substitution's command too, and inside a `${ }` expansion when the
    expansion is quoted; an escape before it quotes only its first character, so it is not. A here-document's body is
    data that no word splitting reaches, so a token in its text is quoted; one in a substitution the body runs is read
    in that substitution's command. A token that stands where the grammar keeps no text of its own, in a `$'...'`
    string that holds an escape, an array assignment's value, arithmetic, or a function's name, or in an unquoted
    `${ }` expansion, whose own quotes the grammar does not keep, is None, which validation cannot judge. Each token
    is read where a value that needs no quoting would stand, TOKEN_MARKER, so a token joined to a `$NAME` before it
    is read as part of the name, as Bash reads the rendered value, and is None too. Raises BashSyntaxError for Bash
    the grammar refuses, and for text that already holds a TOKEN_MARKER."""
    if TOKEN_MARKER.search(text):
        raise BashSyntaxError("it holds text of the form validation marks each token with")
    places = itertools.count()
    marked = TEMPLATE_TOKEN.sub(lambda _: f"TEMPLATE_TOKEN_{next(places)}_", text)
    placed: dict[int, tuple[int, bool | None]] = {}
    for command in every_command(marked):
        for word in expanded_words(command):
            for index, rank, quoted in _word_markers(word):
                if placed.get(index, (3, False))[0] > rank:
                    placed[index] = (rank, quoted)
        for document in here_documents(command):
            for marker in TOKEN_MARKER.finditer(document.body):
                placed.setdefault(int(marker.group(1)), (2, True))
    tree = parse(marked)
    found: list[tuple[int, str, bool | None]] = []
    for index, token in enumerate(TEMPLATE_TOKEN.finditer(text)):
        if index in placed:
            quoted: bool | None = placed[index][1]
        elif parse(marked.replace(f"TEMPLATE_TOKEN_{index}_", "", 1)) == tree:
            continue  # the grammar reads nothing where the token stands: it is in a comment
        else:
            quoted = None
        found.append((text.count("\n", 0, token.start()), token.group(1), quoted))
    return found


def _word_markers(word: Word) -> Iterator[tuple[int, int, bool | None]]:
    """Each token marker a Bash word holds, as (its index, how near the word's own text it stands, whether every
    character of it is quoted): 0 in a run of the word's literal text, which breaks at each expansion, and 1 in a
    `${ }` expansion's text, whose own substitutions the grammar reads as commands: quoted when the expansion is, and
    None, which validation cannot judge, when it is not, since the grammar keeps no quotes of the expansion's own."""
    runs: list[list[tuple[str, bool]]] = [[]]
    for part in word.parts:
        if part.kind == "text":
            runs[-1] += [(character, part.quoted) for character in part.text]
            continue
        runs.append([])
        quoted = True if part.quoted else None
        yield from ((int(marker.group(1)), 1, quoted) for marker in TOKEN_MARKER.finditer(part.text))
    for run in runs:
        for marker in TOKEN_MARKER.finditer("".join(character for character, _ in run)):
            yield int(marker.group(1)), 0, all(quoted for _, quoted in run[marker.start() : marker.end()])


def _powershell_tokens(text: str) -> list[tuple[int, str, bool | None]]:
    """Each token of a PowerShell text outside a comment, as (line offset, name, whether it sits inside quotes), read
    one character at a time: a backtick escapes the character after it outside single quotes, and a `#` that starts a
    word outside quotes starts a comment."""
    found: list[tuple[int, str, bool | None]] = []
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
        if char == POWERSHELL_ESCAPE and quote != "'":
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


def _lone_optional_tokens(text: str) -> list[tuple[int, str]]:
    """Each token that may render empty quoted as a word of its own, as (line offset, name)."""
    return [(text.count("\n", 0, lone.start()), lone.group(2)) for lone in LONE_OPTIONAL_TOKEN.finditer(text)]


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


def _executed_shell_tokens(root: Path, unreadable: list[str]) -> set[tuple[str, str]]:
    """The (context, token) pairs a tests/deployer test named *with_spaces* renders and executes with a spaced value.

    The test function carries the token in a string literal, binds a value built from a literal with a space, runs
    the rendered result with that context's finder through run_tool, and asserts the run's output against that value.
    """
    executed: set[tuple[str, str]] = set()
    suites = sorted(
        path for path in file_presence.matching(root / "tests" / "deployer", "*.py") if is_test_script(path)
    )
    for _, _, tree in python_trees(suites, root, unreadable):
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_") or "with_spaces" not in node.name:
                continue
            executed |= {(context, token) for context in _spaced_runs(node) for token in _literal_tokens(node)}
    return executed


def shell_token_problems(root: Path) -> list[str]:
    """Report a token in shipped Bash or PowerShell outside quotes, one that may render empty as an argument of its
    own, or one with no execution fixture in that language.

    "Template values" in docs/adding-a-skill.md keeps tokens quoted in executable content, and CLAUDE.md asks for a
    rendered execution fixture with representative values containing spaces.
    """
    unquoted: list[str] = []
    first: dict[tuple[str, str], tuple[str, int]] = {}
    files = sorted(
        (
            path
            for path in file_presence.matching(root / "skills", "**/*")
            if file_presence.is_present(path, other_is_absent=True)
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    for path in files:
        name = path.relative_to(root).as_posix()
        for context, start, text in _shell_units(path):
            unquoted += [
                f"{name}:{start + offset} passes {{{{{token}}}}} as an argument of its own in {SHELL_LABELS[context]}, "
                "which Windows PowerShell drops when the value is empty; join it to its option, as "
                f'--name="{{{{{token}}}}}"'
                for offset, token in _lone_optional_tokens(text)
            ]
            try:
                tokens = _shell_tokens(text, context)
            except BashSyntaxError as error:
                unquoted.append(
                    f"{name}:{start} holds Bash validation cannot read ({error}), so its tokens cannot be judged; "
                    "write it as Bash reads it"
                )
                continue
            for offset, token, quoted in tokens:
                line = start + offset
                if quoted is None:
                    unquoted.append(
                        f"{name}:{line} has {{{{{token}}}}} where validation cannot read whether Bash quotes it; "
                        "write it in a quoted word of a command"
                    )
                # An interpreter is a command word from a fixed set, so it renders unquoted, as a command must.
                elif not quoted and token not in config.INTERPRETER_VARIABLES:
                    unquoted.append(f"{name}:{line} has {{{{{token}}}}} outside quotes in {SHELL_LABELS[context]}")
                first.setdefault((context, token), (name, line))
    executed = _executed_shell_tokens(root, unquoted)
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


def _prose_code_spans(path: Path) -> list[tuple[int, str]]:
    """Each inline code span outside a Markdown file's fences, as (line number, text)."""
    lines = LINE_BREAK.split(path.read_text(encoding="utf-8", errors="replace"))
    return [(index + 1, span) for index, span in prose_code_spans(lines)]


def interpreter_problems(root: Path) -> list[str]:
    """Report Python named by hand in a shipped skill's Markdown Bash or PowerShell, in a code span of its prose that
    runs it, or in a shell grant of its allowed-tools, where {{PYTHON}} belongs so that the command and the grant render
    as the platform names Python.

    A script under scripts/ runs from the checkout in its suite, unrendered, so it names the interpreter itself.
    """
    problems: list[str] = []
    for path in sorted(
        file_presence.matching(root / "skills", "**/*.md"), key=lambda found: found.relative_to(root).as_posix()
    ):
        name = path.relative_to(root).as_posix()
        for context, start, text in _shell_units(path):
            for offset, line in enumerate(text.split("\n")):
                if HAND_WRITTEN_INTERPRETER.search(line):
                    problems.append(
                        f"{name}:{start + offset} names Python by hand in {SHELL_LABELS[context]}; write "
                        f"{{{{PYTHON}}}}; see {INTERPRETER_DOC}"
                    )
        problems += [
            f"{name}:{number} names Python by hand in the code span `{span}`; write {{{{PYTHON}}}}; "
            f"see {INTERPRETER_DOC}"
            for number, span in _prose_code_spans(path)
            if HAND_WRITTEN_SPAN_COMMAND.match(span)
        ]
        if path.name != "SKILL.md":
            continue
        try:
            grants = frontmatter.parse(path.read_text(encoding="utf-8")).value("allowed-tools")
        except frontmatter.FrontmatterError:
            continue
        problems += [
            f"{name} grants {grant}, which names Python by hand; write {{{{PYTHON}}}}; see {INTERPRETER_DOC}"
            for grant in (grants if isinstance(grants, list) else [])
            if grant.startswith(("Bash(", "PowerShell(")) and HAND_WRITTEN_INTERPRETER.search(grant)
        ]
    return problems


def run_bash_suite(target: str) -> None:
    """Start a Bash suite as Git Bash's own program, from the repository root, with no shell above it.

    Windows ends a Git Bash process on a CI runner now and then with 0x80000004, and a Bash above the suite's reads that
    end as exit 0 or reports only its low byte, 4, so the runner would see a pass or an unnamed exit. Started directly,
    the suite's own status reaches the runner, which names a termination as one.
    """
    run_process([find_git_bash(), target])


def shell_script_targets(root: Path) -> list[Path]:
    """Every Bash script under skills/, and under each repository skill's scripts/."""
    scripts = [
        path
        for path in file_presence.matching(root / "skills", "**/*")
        if file_presence.is_present(path, other_is_absent=True) and is_shell_script(path)
    ]
    for skill in repository_skill_directories(root):
        scripts += [
            path
            for path in file_presence.matching(skill / "scripts", "**/*")
            if file_presence.is_present(path, other_is_absent=True) and is_shell_script(path)
        ]
    return sorted(scripts, key=lambda path: path.relative_to(root).as_posix().casefold())


def static_shell_check() -> None:
    scripts = shell_script_targets(REPOSITORY_ROOT)
    if not scripts:
        raise AssertionError("No shell scripts were found to validate.")
    shellcheck = find_shellcheck()
    if shellcheck is None:
        raise AssertionError(f"ShellCheck was not found: {platform_support.install_hint('ShellCheck')}")
    # Each syntax check and ShellCheck start as their own programs, with no shell above them, for the reason
    # run_bash_suite gives: a Bash above a child that Windows ends reads that end as exit 0, so `set -e` went on and an
    # ended check passed. Started directly, each one's own status reaches the runner, which names a termination.
    bash = find_git_bash()
    for path in scripts:
        bash_syntax_job(bash, relative(path))
    shellcheck_job(shellcheck, "--severity=warning", *(relative(path) for path in scripts))


# PSScriptAnalyzer reads every .ps1 under these roots, and every PowerShell fence in Markdown outside tests/.
SCRIPT_ANALYZER_ROOTS = ("tools", "tests", "skills", ".claude/skills")
# Reads the targets from the JSON file PSSA_TARGETS names and prints one JSON array of Warning and Error findings.
# Given no settings, PSScriptAnalyzer reads a PSScriptAnalyzerSettings.psd1 beside a file it is given, or in the
# working folder for a fence's text, which could turn its rules off; -Settings @{} keeps every rule on.
SCRIPT_ANALYZER_RUN = (
    "$ErrorActionPreference = 'Stop'; "
    "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
    "Import-Module PSScriptAnalyzer -MinimumVersion $env:PSSA_FLOOR; "
    "$targets = @(Get-Content -LiteralPath $env:PSSA_TARGETS -Raw -Encoding utf8 | ConvertFrom-Json); "
    "$found = [Collections.Generic.List[object]]::new(); "
    "for ($index = 0; $index -lt $targets.Count; $index++) { "
    "$target = $targets[$index]; "
    "$source = if ($null -ne $target.path) { @{ Path = $target.path } } else { @{ ScriptDefinition = $target.text } }; "
    "foreach ($record in Invoke-ScriptAnalyzer @source -Settings @{} -Severity Warning, Error) { "
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
        if suffix == ".ps1" and any(posix_within(name, top, strict=True) for top in SCRIPT_ANALYZER_ROOTS):
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
    findings = powershell_json(
        SCRIPT_ANALYZER_RUN,
        [{"path": str(target.path) if target.path else None, "text": target.text} for target in targets],
        "PSSA_TARGETS",
        "PSScriptAnalyzer could not run",
        {"PSSA_FLOOR": tools.format_version(VALIDATION_FLOORS["PSScriptAnalyzer"])},
    )
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
    """Every non-empty Bash fence in Markdown outside MARKDOWN_SHELL_EXCLUDED_ROOTS, a fence left open included, which
    runs to the end of its file as _shell_units reads it."""
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


INT_PARSE_ALLOWED = {
    "markdown_shell_check": "ShellCheck writes each warning's line number",
}


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
        options = ("--format=gcc", "--severity=warning", *fragments)
        check = render.shellcheck_command(shellcheck, *options)
        result = render.run_shellcheck(shellcheck, *options)
    refuse_termination(check, result.returncode, result.output)
    if result.returncode == 0:
        return
    reported: list[str] = []
    for line in program_lines(result.output):
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
    def test_every_markdown_fence_is_closed(self) -> None:
        self.assertEqual([], unclosed_fence_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_shell_tokens_are_quoted_and_executed_by_a_fixture(self) -> None:
        self.assertEqual([], shell_token_problems(REPOSITORY_ROOT))

    def test_shipped_skills_name_python_through_the_interpreter_token(self) -> None:
        self.assertEqual([], interpreter_problems(REPOSITORY_ROOT))

    def test_the_interpreter_token_renders_only_python_or_python3(self) -> None:
        # Pinned against the names the port renders, not the deployer's own constant.
        self.assertEqual({"PYTHON": frozenset({"python", "python3"})}, config.INTERPRETER_VARIABLES)
        self.assertEqual("python" if platform_support.WINDOWS else "python3", platform_support.PYTHON)

    def test_repository_suppresses_no_shellcheck_or_psscriptanalyzer_rule(self) -> None:
        self.assertEqual([], shell_suppression_problems(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))
