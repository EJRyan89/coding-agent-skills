"""What a skill's scripts may do: the commands they run, how they read gh output, how they run git and gh, the
"Script results" contract, the console setup, and the function names CodeQL reads as secrets.
"""

from __future__ import annotations

import ast
import json
import re
import shlex
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

from duplication import SKILL_CORE, SKILL_CORE_SCRIPTS
from toolchain import find_powershell
from validation_support import (
    REPOSITORY_ROOT,
    import_aliases,
    is_test_script,
    module_imports,
    qualified_name,
    skill_script_directories,
)

from deployer import platform_support, render

SHELL_OPERATORS = ("&&", "||", ";;", "|&", "()", "(", ")", ";", "&", "|", "<", ">", "\n")
COMMAND_SEPARATORS = {";", "&", "&&", "|", "||", "|&", "(", "\n", "{"}
LEADING_KEYWORDS = {"if", "then", "else", "elif", "while", "until", "do", "time", "!", "}", ")", "fi", "done"}
# Keywords whose following words, up to the next separator, are not commands: "for name in list", "case word in".
HEADER_KEYWORDS = {"case", "for", "function", "select"}
SHELL_BUILTINS = {
    "alias",
    "break",
    "cd",
    "command",
    "continue",
    "declare",
    "dirs",
    "echo",
    "eval",
    "exec",
    "exit",
    "export",
    "false",
    "getopts",
    "hash",
    "let",
    "local",
    "mapfile",
    "popd",
    "printf",
    "pushd",
    "pwd",
    "read",
    "readarray",
    "readonly",
    "return",
    "set",
    "shift",
    "shopt",
    "source",
    "test",
    "trap",
    "true",
    "type",
    "ulimit",
    "umask",
    "unalias",
    "unset",
    "wait",
}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
COMMAND_NAME = re.compile(r"[A-Za-z][A-Za-z0-9._-]*")
COMMANDS_DOC = '"Commands skills may run" in docs/adding-a-skill.md'


def _split_operators(token: str) -> list[str]:
    """Split a run of shell punctuation, which shlex returns as one token, into its operators."""
    if not token or any(char not in ";&|()<>\n" for char in token):
        return [token]
    parts: list[str] = []
    while token:
        operator = next(op for op in SHELL_OPERATORS if token.startswith(op))
        parts.append(operator)
        token = token[len(operator) :]
    return parts


def shell_commands(script: str) -> set[str]:
    """Literal command names a Bash script runs, ignoring keywords, case patterns, and its own functions."""
    lexer = shlex.shlex(script.replace("\\\n", " "), posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    tokens = [part for token in lexer for part in _split_operators(token)]
    functions = {tokens[i] for i in range(len(tokens) - 1) if tokens[i + 1] == "()"}
    functions |= {tokens[i + 1] for i in range(len(tokens) - 1) if tokens[i] == "function"}
    found: set[str] = set()
    state = "command"
    for word in tokens:
        if state == "case-word":
            state = "patterns" if word == "in" else state
        elif state == "patterns":
            state = "command" if word == ")" else "arguments" if word == "esac" else state
        elif word == ";;":
            state = "patterns"
        elif word == "esac":
            state = "arguments"
        elif state == "command":
            if word in COMMAND_SEPARATORS or word in LEADING_KEYWORDS or ASSIGNMENT.match(word):
                continue
            state = "case-word" if word == "case" else "arguments"
            if word not in HEADER_KEYWORDS and COMMAND_NAME.fullmatch(word) and word not in functions:
                found.add(word)
        elif word in COMMAND_SEPARATORS:
            state = "command"
    return found


def _fences(markdown: str, context: str) -> list[str]:
    """The body of each closed fence that deployer/render.py renders in a context: shell for a Bash, sh, or shell
    fence, and powershell for a PowerShell, ps1, or pwsh fence."""
    lines = markdown.splitlines()
    return [
        "\n".join(lines[index] for index in fence.body(len(lines)))
        for fence in render.find_fences(lines)
        if fence.closer is not None and fence.context == context
    ]


# The calls that start a command, as the module's imports resolve them, and the position and keyword of the argument
# that names it: a list or tuple whose first item is the program, or a string whose first word is.
COMMAND_RUNNERS: dict[str, tuple[int, str]] = {
    **{f"subprocess.{name}": (0, "args") for name in ("run", "Popen", "call", "check_call", "check_output")},
    "subprocess.getoutput": (0, "cmd"),
    "subprocess.getstatusoutput": (0, "cmd"),
    "os.system": (0, "command"),
    "os.popen": (0, "cmd"),
    "asyncio.create_subprocess_exec": (0, "program"),
    "asyncio.create_subprocess_shell": (0, "cmd"),
    # skill-core's runner, which bounds a command with a time limit and gives it no stdin.
    "bounded_process.run_bounded": (0, "command"),
    "bounded_process.streaming": (0, "command"),
}
# skill-core's clients: each runs its one command for every call a script makes through it.
CLIENT_CLASSES = {"git_client.GitClient": "git", "github_client.GitHubClient": "gh"}
# shutil.which finds the program a script then runs. A script that takes it as an injected service calls it by its
# own name, such as services.which("dotnet"), so a call of any function named which counts too.
COMMAND_LOOKUP = "shutil.which"


def _is_lookup(call: ast.Call, aliases: dict[str, str]) -> bool:
    """Whether a call looks a program up: shutil.which under any name its imports give it, or a function named which."""
    function = call.func
    if isinstance(function, ast.Attribute) and function.attr == "which":
        return True
    return (isinstance(function, ast.Name) and function.id == "which") or (
        qualified_name(function, aliases) == COMMAND_LOOKUP
    )


def _program_name(command: str) -> str | None:
    """The program a command string starts, by its first word and without a .exe suffix, or None for a path or a word
    that names no program."""
    words = command.split(None, 1)
    name = words[0] if words else ""
    name = name[:-4] if name.casefold().endswith(".exe") else name
    return name if COMMAND_NAME.fullmatch(name) else None


def _name_bindings(tree: ast.Module) -> dict[str, list[ast.expr]]:
    """Every value the module assigns to each name, anywhere in it."""
    bindings: dict[str, list[ast.expr]] = defaultdict(list)
    for node in ast.walk(tree):
        targets: list[ast.expr]
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bindings[target.id].append(value)
    return bindings


class CommandReader:
    """The programs a Python module runs, each call resolved through the module's imports and each name it passes
    followed to every literal the module binds that name to, so an alias or a command list held in a variable is
    read as the command it is."""

    def __init__(self, tree: ast.Module) -> None:
        self.tree = tree
        self.aliases = import_aliases(tree)
        self.bindings = _name_bindings(tree)

    def program_literals(self, node: ast.expr | None, seen: frozenset[str] = frozenset()) -> list[ast.Constant]:
        """The string literals that may name the program a command expression runs: a string, the first item of a
        list or tuple, the left side of a +, what a which() call looks up, and every value a name is bound to."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node]
        if isinstance(node, (ast.List, ast.Tuple)):
            return self.program_literals(node.elts[0], seen) if node.elts else []
        if isinstance(node, ast.Starred):
            return self.program_literals(node.value, seen)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self.program_literals(node.left, seen)
        if isinstance(node, ast.Call) and _is_lookup(node, self.aliases) and node.args:
            return self.program_literals(node.args[0], seen)
        if isinstance(node, ast.Name) and node.id not in seen:
            values = self.bindings.get(node.id, [])
            return [found for value in values for found in self.program_literals(value, seen | {node.id})]
        return []

    def _named(self, node: ast.expr | None) -> list[tuple[int, str]]:
        return [
            (literal.lineno, name)
            for literal in self.program_literals(node)
            if isinstance(literal.value, str) and (name := _program_name(literal.value))
        ]

    def started(self, call: ast.Call) -> list[tuple[int, str]]:
        """The line and program of each command a COMMAND_RUNNERS call may start, and nothing for another call."""
        runner = COMMAND_RUNNERS.get(qualified_name(call.func, self.aliases))
        if runner is None:
            return []
        position, keyword = runner
        if len(call.args) > position:
            return self._named(call.args[position])
        return self._named(next((item.value for item in call.keywords if item.arg == keyword), None))

    def commands(self, known: set[str]) -> set[str]:
        """Every program the module runs through a runner or a skill-core client, or looks up with which, and every
        known tool a list or tuple begins with, since a module may hand that command to a function of its own."""
        found: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
                first = node.elts[0]
                if isinstance(first, ast.Constant) and first.value in known:
                    found.add(first.value)
            elif isinstance(node, ast.Call):
                client = CLIENT_CLASSES.get(qualified_name(node.func, self.aliases))
                found |= {client} if client else set()
                if _is_lookup(node, self.aliases) and node.args:
                    found |= {name for _, name in self._named(node.args[0])}
                found |= {name for _, name in self.started(node)}
        return found


# Prints one JSON array of {source, name} for each external command run by the PowerShell sources in the JSON file
# COMMAND_SOURCES names, as PowerShell's own parser reads them. A function a source defines is its own, and so is
# what PowerShell ships: its aliases and the commands of its core and of the modules under its home, such as
# Get-ChildItem and ForEach-Object. A module installed elsewhere, such as PSScriptAnalyzer, is not standard.
POWERSHELL_COMMANDS_RUN = (
    "$ErrorActionPreference = 'Stop'; "
    "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
    "$sources = @(Get-Content -LiteralPath $env:COMMAND_SOURCES -Raw -Encoding utf8 | ConvertFrom-Json); "
    "$shipped = @(Get-Module -ListAvailable | Where-Object { "
    "$_.ModuleBase.StartsWith($PSHOME, [StringComparison]::OrdinalIgnoreCase) } | ForEach-Object Name); "
    "$own = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase); "
    "foreach ($command in Get-Command -Module @($shipped + 'Microsoft.PowerShell.Core')) { "
    "[void]$own.Add($command.Name) }; "
    "foreach ($alias in Get-Alias) { [void]$own.Add($alias.Name) }; "
    "$found = [Collections.Generic.List[object]]::new(); "
    "for ($index = 0; $index -lt $sources.Count; $index++) { "
    "$tokens = $null; $errors = $null; "
    "$tree = [Management.Automation.Language.Parser]::ParseInput($sources[$index], [ref]$tokens, [ref]$errors); "
    "$functions = @($tree.FindAll({ param($node) "
    "$node -is [Management.Automation.Language.FunctionDefinitionAst] }, $true) | ForEach-Object Name); "
    "foreach ($command in $tree.FindAll({ param($node) "
    "$node -is [Management.Automation.Language.CommandAst] }, $true)) { "
    "$name = $command.GetCommandName(); "
    "if (-not $name -or $functions -contains $name -or $own.Contains($name)) { continue }; "
    "$found.Add([ordered]@{ source = $index; name = $name }) } }; "
    "[Console]::Out.WriteLine((ConvertTo-Json -InputObject $found.ToArray() -Compress -Depth 3))"
)


def powershell_commands(sources: list[str]) -> list[set[str]]:
    """The external programs each PowerShell source runs, read by PowerShell's own parser in one call for them all."""
    if not sources:
        return []
    with tempfile.TemporaryDirectory(prefix="powershell-commands-") as directory:
        request = Path(directory) / "sources.json"
        request.write_text(json.dumps(sources), encoding="utf-8")
        result = platform_support.run_tool(
            [find_powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", POWERSHELL_COMMANDS_RUN],
            {"COMMAND_SOURCES": str(request)},
        )
    lines = [line for line in result.output.splitlines() if line.strip()]
    try:
        found = json.loads(lines[-1]) if result.returncode == 0 and lines else None
    except json.JSONDecodeError:
        found = None
    if not isinstance(found, list):
        raise AssertionError(
            f"PowerShell could not read the commands (exit code {result.returncode}):\n{result.output}"
        )
    commands: list[set[str]] = [set() for _ in sources]
    for item in found:
        if name := _program_name(item["name"]):
            commands[item["source"]].add(name)
    return commands


def _powershell_sources(path: Path) -> list[str]:
    """The PowerShell a file holds: a .ps1 script, or Markdown's PowerShell fences."""
    suffix = path.suffix.casefold()
    if suffix == ".ps1":
        return [path.read_text(encoding="utf-8-sig", errors="replace")]
    if suffix == ".md":
        return _fences(path.read_text(encoding="utf-8", errors="replace"), "powershell")
    return []


def _file_commands(path: Path, known: set[str], powershell: dict[str, set[str]]) -> set[str]:
    """The programs a file runs, where powershell holds what each PowerShell source read for the run runs."""
    text = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.casefold()
    found = {command for source in _powershell_sources(path) for command in powershell[source]}
    if suffix == ".py":
        found |= CommandReader(ast.parse(text)).commands(known)
    elif suffix in {".sh", ".bash"}:
        found |= shell_commands(text)
    elif suffix == ".md":
        found |= {command for block in _fences(text, "shell") for command in shell_commands(block)}
    return found


def _skill_files(directory: Path) -> list[Path]:
    """A skill's files, without its tests."""
    return sorted(path for path in directory.rglob("*") if path.is_file() and not is_test_script(path))


NAMED_SCRIPT = re.compile(r"/([a-z0-9-]+)/scripts/([A-Za-z_]\w*)\.py\b")


def _referenced_scripts(path: Path) -> set[str]:
    """Module names a file imports, and script names it gives by a path through another skill's scripts/."""
    text = path.read_text(encoding="utf-8", errors="replace")
    names = {name for _, name in NAMED_SCRIPT.findall(text)}
    if path.suffix.casefold() == ".py":
        names |= {found.module.split(".")[0] for found in module_imports(ast.parse(text))}
    return names


def _skill_dependencies(root: Path, skill: str) -> list[str]:
    """The skill_deps closure of a skill, from deploy-meta."""
    found: list[str] = []
    pending = [skill]
    while pending:
        metadata = root / "deploy-meta" / f"{pending.pop()}.json"
        if not metadata.is_file():
            continue
        for dependency in json.loads(metadata.read_text(encoding="utf-8")).get("skill_deps", []):
            if dependency not in found and dependency != skill:
                found.append(dependency)
                pending.append(dependency)
    return found


def _reached_dependency_scripts(root: Path, skill: str, own: list[Path]) -> list[Path]:
    """The dependency scripts a skill runs: those its own files import or name, and what those import in turn."""
    scripts: dict[str, Path] = {}
    for dependency in _skill_dependencies(root, skill):
        for path in sorted((root / "skills" / dependency / "scripts").glob("*.py")):
            if not is_test_script(path):
                scripts.setdefault(path.stem, path)
    own_modules = {path.stem for path in own if path.suffix.casefold() == ".py"}
    pending = sorted({name for path in own for name in _referenced_scripts(path)} - own_modules)
    reached: set[str] = set()
    while pending:
        name = pending.pop()
        if name in reached or name not in scripts:
            continue
        reached.add(name)
        pending.extend(_referenced_scripts(scripts[name]))
    return [scripts[name] for name in sorted(reached)]


def skill_command_problems(root: Path, known: set[str], standard: set[str]) -> list[str]:
    """Report commands a skill runs that are neither standard nor declared, and declared tools it never runs.

    A skill runs what its own scripts and Bash examples run, and what the scripts of its skill_deps that it reaches
    run: a dependency's tools are not the skill's unless it imports or names the script that runs them, and then the
    skill declares them too, so a skill that reaches skill-core's GitHub client declares gh.
    """
    skills: list[tuple[str, set[str], list[Path], list[Path]]] = []
    for metadata in sorted((root / "deploy-meta").glob("*.json")):
        document = json.loads(metadata.read_text(encoding="utf-8"))
        declared = set(document.get("tools", [])) | set(document.get("optional_tools", []))
        directories = [root / "skills" / metadata.stem, *sorted((root / "skills").glob(f"*/{metadata.stem}"))]
        own = [
            path for directory in directories if (directory / "SKILL.md").is_file() for path in _skill_files(directory)
        ]
        skills.append((metadata.stem, declared, own, _reached_dependency_scripts(root, metadata.stem, own)))
    # PowerShell reads every source in one start, which costs far more than reading one.
    sources = sorted(
        {source for *_, own, deps in skills for path in own + deps for source in _powershell_sources(path)}
    )
    powershell = dict(zip(sources, powershell_commands(sources), strict=True))
    problems: list[str] = []
    for skill, declared, own, dependency_scripts in skills:
        used = {command for path in own for command in _file_commands(path, known, powershell)}
        reached: dict[str, Path] = {}
        for path in dependency_scripts:
            for command in _file_commands(path, known, powershell):
                reached.setdefault(command, path)
        for name in sorted((used & known) - declared):
            problems.append(f"skill {skill} runs {name} without declaring it in tools")
        for name in sorted((set(reached) & known) - used - declared):
            through = reached[name].relative_to(root).as_posix()
            problems.append(f"skill {skill} runs {name} through {through} without declaring it in tools")
        for name in sorted(declared - used - set(reached)):
            problems.append(f"skill {skill} declares tool {name} but never runs it")
        for name in sorted(used - known - standard - SHELL_BUILTINS):
            problems.append(f"skill {skill} runs {name}, which a standard install lacks; see {COMMANDS_DOC}")
    return problems


GH_FILTERS = {"--jq", "--template"}
GH_SHELL_CALL = re.compile(r"(?:^|[\s;|&(])gh\s")
GH_SHELL_FILTER = re.compile(r"\s(--jq|--template|-q)(?=[\s=]|$)")


def gh_filter_problems(root: Path) -> list[str]:
    """Report skill scripts that filter gh output with --jq, -q, or --template instead of parsing its JSON.

    Python parsing has one shape check that fails closed and can be tested offline from a fixture; a jq expression
    is a second language that only gh itself can run.
    """
    problems: list[str] = []
    for path in sorted((root / "skills").glob("**/scripts/*")):
        if not path.is_file() or is_test_script(path):
            continue
        name = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        found: list[tuple[int, str]] = []
        if path.suffix.casefold() == ".py":
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.Constant) and node.value in GH_FILTERS:
                    found.append((node.lineno, node.value))
                elif isinstance(node, (ast.List, ast.Tuple)):
                    values = {item.value for item in node.elts if isinstance(item, ast.Constant)}
                    # gh's -q applies a jq filter to --json output; git's -q never comes with --json.
                    if {"-q", "--json"} <= values:
                        found.append((node.lineno, "-q"))
        elif path.suffix.casefold() in {".sh", ".bash", ".ps1"}:
            for number, line in enumerate(text.splitlines(), start=1):
                match = GH_SHELL_FILTER.search(line) if GH_SHELL_CALL.search(line) else None
                if match:
                    found.append((number, match.group(1)))
        for number, flag in sorted(set(found)):
            problems.append(
                f"{name}:{number} filters gh output with {flag}; parse its JSON in Python instead; see {COMMANDS_DOC}"
            )
    return problems


CONSOLE_CORE = "skill-core"
CONSOLE_SETUP = "use_utf8_output"
CONSOLE_FUNCTION = f"console.{CONSOLE_SETUP}"
CONSOLE_DOC = '"Script results" in docs/adding-a-skill.md'


def console_entry_points(root: Path) -> list[Path]:
    """The Python that can run as a program: every shipped and repository skill's scripts outside skill-core, deploy.py,
    the deployer, and tools/, regression suites aside."""
    files = [
        path
        for scripts in skill_script_directories(root)
        if scripts.parent.name != CONSOLE_CORE
        for path in scripts.glob("*.py")
    ]
    files += [root / "deploy.py", *(root / "deployer").rglob("*.py"), *(root / "tools").rglob("*.py")]
    return sorted(
        (path for path in files if path.is_file() and not is_test_script(path)),
        key=lambda path: path.relative_to(root).as_posix(),
    )


# The standard streams use_utf8_output configures, under every name sys gives them.
STANDARD_STREAMS = frozenset(
    f"sys.{name}" for stream in ("stdin", "stdout", "stderr") for name in (stream, f"__{stream}__")
)
# Calls that build a text stream over a binary stream or a file descriptor, choosing its encoding, and the codecs
# calls that return a class that does.
STREAM_WRAPPERS = frozenset({"io.TextIOWrapper", "open", "builtins.open", "io.open", "os.fdopen"})
CODEC_WRAPPERS = frozenset({"codecs.getwriter", "codecs.getreader"})


def _is_reconfigure(node: ast.expr) -> bool:
    """Whether an expression is a stream's reconfigure method: stream.reconfigure, or getattr(stream, "reconfigure")."""
    if isinstance(node, ast.Attribute):
        return node.attr == "reconfigure"
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) > 1
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "reconfigure"
    )


def _reconfigure_names(tree: ast.Module) -> set[str]:
    """The names a module binds to a stream's reconfigure method anywhere in it, and reconfigure itself."""
    names = {"reconfigure"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_reconfigure(node.value):
            names |= {target.id for target in node.targets if isinstance(target, ast.Name)}
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value and _is_reconfigure(node.value):
            names |= {node.target.id} if isinstance(node.target, ast.Name) else set()
    return names


def _names_a_standard_stream(node: ast.AST, aliases: dict[str, str]) -> bool:
    """Whether an expression reaches a standard stream, such as sys.stdout.buffer or sys.stderr.fileno()."""
    return any(qualified_name(part, aliases) in STANDARD_STREAMS for part in ast.walk(node))


def _changes_a_stream(node: ast.AST, aliases: dict[str, str], reconfigures: set[str]) -> bool:
    """Whether a node sets a stream's encoding: a reconfigure call that may pass one, directly or through a name bound
    to the method; an assignment or setattr that replaces a standard stream; or a text wrapper built over one."""
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return any(
            isinstance(target, ast.Attribute) and qualified_name(target, aliases) in STANDARD_STREAMS
            for target in targets
        )
    if not isinstance(node, ast.Call):
        return False
    function = node.func
    if _is_reconfigure(function) or (isinstance(function, ast.Name) and function.id in reconfigures):
        return any(keyword.arg in {"encoding", None} for keyword in node.keywords)
    if qualified_name(function, aliases) in {"setattr", "builtins.setattr"} and len(node.args) > 1:
        stream = node.args[1]
        return (
            qualified_name(node.args[0], aliases) == "sys"
            and isinstance(stream, ast.Constant)
            and isinstance(stream.value, str)
            and f"sys.{stream.value}" in STANDARD_STREAMS
        )
    wrapper = qualified_name(function, aliases) in STREAM_WRAPPERS or (
        isinstance(function, ast.Call) and qualified_name(function.func, aliases) in CODEC_WRAPPERS
    )
    arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
    return wrapper and any(_names_a_standard_stream(argument, aliases) for argument in arguments)


def _stream_change_lines(tree: ast.Module, aliases: dict[str, str]) -> list[int]:
    """The lines on which a module sets a standard stream's encoding itself."""
    reconfigures = _reconfigure_names(tree)
    return sorted(
        {
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, (ast.stmt, ast.expr)) and _changes_a_stream(node, aliases, reconfigures)
        }
    )


def _sets_up_the_console(statement: ast.stmt, aliases: dict[str, str]) -> bool:
    """Whether a statement calls skill-core's use_utf8_output, resolved through the module's imports."""
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and qualified_name(statement.value.func, aliases) == CONSOLE_FUNCTION
    )


def console_setup_problems(root: Path) -> list[str]:
    """Report an entry point whose __main__ block does not start by calling skill-core's use_utf8_output(), and a
    module that reconfigures a stream's encoding itself.

    Skill scripts, deploy.py, the deployer, and tools/ print paths and text the user wrote, which a Windows pipe's
    legacy code page cannot encode; one function sets it up, so the copies cannot drift apart again.
    """
    problems: list[str] = []
    for path in console_entry_points(root):
        name = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = import_aliases(tree)
        problems += [
            f"{name}:{line} reconfigures a stream itself; call {CONSOLE_SETUP}() from {CONSOLE_CORE} instead; "
            f"see {CONSOLE_DOC}"
            for line in _stream_change_lines(tree, aliases)
        ]
        for node in tree.body:
            if not (isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'"):
                continue
            if not _sets_up_the_console(node.body[0], aliases):
                problems.append(
                    f"{name}:{node.lineno} does not call {CONSOLE_CORE}'s {CONSOLE_SETUP}() first in its __main__ "
                    f"block; see {CONSOLE_DOC}"
                )
    return problems


RESULTS_DOC = '"Script results" in docs/adding-a-skill.md'
CONTRACT_EXEMPTION = "EXIT_CONTRACT_EXEMPT"
CONTRACT_EXIT_CODES = {0, 1, 2}
SHELL_EXIT = re.compile(r"(?:^|[\s;&|(])exit\s+(\d+)\b")


def _contract_exemption(tree: ast.Module) -> str | None:
    """A module's EXIT_CONTRACT_EXEMPT reason, "" when it has none, or None when it is not a non-empty string."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == CONTRACT_EXEMPTION for target in node.targets
        ):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value.strip():
                return value.value
            return None
    return ""


def _mentions_failed(node: ast.expr) -> bool:
    parts = node.values if isinstance(node, ast.JoinedStr) else [node]
    return any(
        isinstance(part, ast.Constant) and isinstance(part.value, str) and "FAILED" in part.value for part in parts
    )


def _contract_breaches(tree: ast.Module) -> list[tuple[int, str]]:
    """Where a script reports in a way "Script results" rules out, with what it does instead."""
    breaches: list[tuple[int, str]] = []
    handlers = [node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)]
    in_handler = {id(inner) for handler in handlers for statement in handler.body for inner in ast.walk(statement)}
    entry_points = [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in {"main", "_main"}
    ]
    for entry_point in entry_points:
        for node in ast.walk(entry_point):
            if (
                isinstance(node, ast.Return)
                and isinstance(node.value, ast.Constant)
                and type(node.value.value) is int
                and node.value.value not in CONTRACT_EXIT_CODES
            ):
                breaches.append((node.lineno, f"exits {node.value.value}; scripts exit only 0, 1, or 2"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if (
            isinstance(function, ast.Attribute)
            and function.attr == "error"
            and id(node) in in_handler
            and isinstance(function.value, ast.Name)
            and "parser" in function.value.id
        ):
            breaches.append((node.lineno, "reports a failure through parser.error; print FAILED <reason> and exit 1"))
        exits = (
            isinstance(function, ast.Attribute)
            and function.attr == "exit"
            and isinstance(function.value, ast.Name)
            and function.value.id == "sys"
        )
        if (exits or (isinstance(function, ast.Name) and function.id == "SystemExit")) and node.args:
            code = node.args[0]
            if isinstance(code, ast.Constant) and type(code.value) is int and code.value not in CONTRACT_EXIT_CODES:
                breaches.append((node.lineno, f"exits {code.value}; scripts exit only 0, 1, or 2"))
        if isinstance(function, ast.Name) and function.id == "print" and node.args:
            stderr = any(
                keyword.arg == "file" and ast.unparse(keyword.value) == "sys.stderr" for keyword in node.keywords
            )
            if stderr and _mentions_failed(node.args[0]):
                breaches.append((node.lineno, "prints FAILED on stderr; print it on stdout"))
            first = node.args[0]
            if (
                not stderr
                and isinstance(first, ast.Call)
                and isinstance(first.func, ast.Attribute)
                and first.func.attr == "dumps"
                and ast.unparse(first.func.value) == "json"
            ):
                breaches.append((node.lineno, "prints JSON; print one fact per line"))
    return sorted(breaches)


# CodeQL's sensitive-data heuristic treats a call to a function whose name says "secret" or "trusted" (but not
# "untrusted" or "is_trusted") as returning a secret, and raises clear-text logging on wherever the result is printed.
# A commit SHA from a function named that way was flagged twice, so shipped code names such functions otherwise.
CODEQL_SECRET_NAME = re.compile(r"secret|(?<!un)(?<!un_)(?<!is)(?<!is_)trusted", re.IGNORECASE)


def secret_named_function_problems(root: Path) -> list[str]:
    """Report each function in shipped or deployer code whose name CodeQL reads as returning a secret."""
    files = [root / "deploy.py", *(root / "deployer").rglob("*.py"), *(root / "tools").rglob("*.py")]
    files += [path for scripts in skill_script_directories(root) for path in scripts.rglob("*.py")]
    problems: list[str] = []
    for path in sorted(
        (path for path in files if path.is_file() and not is_test_script(path)), key=lambda p: p.as_posix()
    ):
        name = path.relative_to(root).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and CODEQL_SECRET_NAME.search(node.name):
                problems.append(
                    f"{name}:{node.lineno}: function {node.name} is named so CodeQL treats its result as a secret "
                    "and flags printing it; name it for what it returns"
                )
    return problems


def script_contract_problems(root: Path) -> list[str]:
    """Report skill scripts that break "Script results": a failure through parser.error or on stderr, JSON output,
    or an exit code other than 0, 1, and 2. A module that must speak another protocol says why in
    EXIT_CONTRACT_EXEMPT."""
    problems: list[str] = []
    for path in sorted(path for scripts in skill_script_directories(root) for path in scripts.glob("*")):
        if not path.is_file() or is_test_script(path):
            continue
        name = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".py":
            tree = ast.parse(text)
            exemption = _contract_exemption(tree)
            if exemption is None:
                problems.append(f"{name}: {CONTRACT_EXEMPTION} must be a non-empty string saying why")
            elif not exemption:
                problems += [f"{name}:{line} {breach}; see {RESULTS_DOC}" for line, breach in _contract_breaches(tree)]
        elif path.suffix in {".sh", ".bash"}:
            for number, line in enumerate(text.splitlines(), 1):
                for code in SHELL_EXIT.findall(line.split("#", 1)[0]):
                    if int(code) not in CONTRACT_EXIT_CODES:
                        problems.append(
                            f"{name}:{number} exits {code}; scripts exit only 0, 1, or 2; see {RESULTS_DOC}"
                        )
    return problems


# The command each shared client runs, and the client a skill script runs it through.
CLIENT_COMMANDS = {"git": "git_client.py's GitClient", "gh": "github_client.py's GitHubClient"}
CLIENTS_DOC = '"Script results" in docs/adding-a-skill.md'


def _client_commands(node: ast.AST, reader: CommandReader) -> list[tuple[int, str]]:
    """The line and the git or gh command a node starts: a list or tuple that begins with it, or a call that runs a
    command given it, such as `subprocess.run("git fetch", shell=True)`, directly or through a name bound to it."""
    found: list[tuple[int, str]] = []
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        first = node.elts[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            found = [(first.lineno, first.value.split(" ", 1)[0])]
    elif isinstance(node, ast.Call):
        found = reader.started(node)
    return [(line, command) for line, command in found if command in CLIENT_COMMANDS]


def client_command_problems(root: Path) -> list[str]:
    """Report a Python skill script outside skill-core that runs git or gh itself instead of through skill-core.

    The clients read no stdin, turn every prompt off, bound each command with a timeout, and classify its failures;
    a command a script starts itself has none of that, so a sweep can wait forever on a credential prompt.
    """
    problems: list[str] = []
    for path in sorted(path for scripts in skill_script_directories(root) for path in scripts.rglob("*.py")):
        name = path.relative_to(root).as_posix()
        if is_test_script(path) or name.startswith(f"{SKILL_CORE_SCRIPTS}/"):
            continue
        reader = CommandReader(ast.parse(path.read_text(encoding="utf-8", errors="replace")))
        commands = {found for node in ast.walk(reader.tree) for found in _client_commands(node, reader)}
        for line, command in sorted(commands):
            problems.append(
                f"{name}:{line} runs {command} itself; run it through {SKILL_CORE}'s {CLIENT_COMMANDS[command]}, "
                f"which bounds it and turns prompts off; see {CLIENTS_DOC}"
            )
    return problems


class SkillScriptsPolicies(unittest.TestCase):
    def test_skill_scripts_follow_the_script_results_contract(self) -> None:
        # An agent that learned one script's results can read every other's.
        self.assertEqual([], script_contract_problems(REPOSITORY_ROOT))

    def test_no_shipped_function_is_named_so_codeql_reads_its_result_as_a_secret(self) -> None:
        self.assertEqual([], secret_named_function_problems(REPOSITORY_ROOT))

    def test_skills_run_only_standard_or_declared_commands(self) -> None:
        from deployer import tools

        self.assertEqual(
            [], skill_command_problems(REPOSITORY_ROOT, set(tools.SKILL_TOOLS), set(tools.STANDARD_COMMANDS))
        )

    def test_skill_scripts_parse_gh_json_instead_of_filtering_it(self) -> None:
        self.assertEqual([], gh_filter_problems(REPOSITORY_ROOT))

    def test_skill_scripts_run_git_and_gh_through_skill_core(self) -> None:
        self.assertEqual([], client_command_problems(REPOSITORY_ROOT))

    def test_entry_points_set_up_the_console_through_skill_core(self) -> None:
        self.assertEqual([], console_setup_problems(REPOSITORY_ROOT))
