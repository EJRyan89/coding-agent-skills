"""What a skill's scripts may do: the commands they run, how they read gh output, how they run git and gh, the
"Script results" contract, the console setup, and the function names CodeQL reads as secrets.
"""

from __future__ import annotations

import ast
import json
import re
import shlex
import unittest
from pathlib import Path

from duplication import SKILL_CORE, SKILL_CORE_SCRIPTS
from validation_support import REPOSITORY_ROOT, SHELL_FENCES, is_test_script

from deployer import render

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
PYTHON_COMMAND = re.compile(
    r'subprocess\.(?:run|Popen|call|check_call|check_output)\(\s*\[\s*"([A-Za-z][A-Za-z0-9._-]*)"'
    r'|which\(\s*"([A-Za-z][A-Za-z0-9._-]*)"\s*\)'
)
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


def _shell_fences(markdown: str) -> list[str]:
    lines = markdown.splitlines()
    return [
        "\n".join(lines[index] for index in fence.body(len(lines)))
        for fence in render.find_fences(lines)
        if fence.closer is not None and fence.language.casefold() in SHELL_FENCES
    ]


def _file_commands(path: Path, known_call: re.Pattern[str]) -> set[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.casefold()
    found: set[str] = set()
    if suffix == ".py":
        found.update(known_call.findall(text))
        found.update(name for match in PYTHON_COMMAND.finditer(text) for name in match.groups() if name)
    elif suffix in {".sh", ".bash"}:
        found.update(shell_commands(text))
    elif suffix == ".md":
        for block in _shell_fences(text):
            found.update(shell_commands(block))
    return found


def _skill_files(directory: Path) -> list[Path]:
    """A skill's files, without its tests."""
    return sorted(path for path in directory.rglob("*") if path.is_file() and not is_test_script(path))


IMPORTED_MODULE = re.compile(
    r"^[ \t]*(?:from[ \t]+([A-Za-z_]\w*)[ \t]+import\b|import[ \t]+([A-Za-z_]\w*(?:[ \t]*,[ \t]*[A-Za-z_]\w*)*))",
    re.MULTILINE,
)
NAMED_SCRIPT = re.compile(r"/([a-z0-9-]+)/scripts/([A-Za-z_]\w*)\.py\b")


def _referenced_scripts(path: Path) -> set[str]:
    """Module names a file imports, and script names it gives by a path through another skill's scripts/."""
    text = path.read_text(encoding="utf-8", errors="replace")
    names = {name for _, name in NAMED_SCRIPT.findall(text)}
    if path.suffix.casefold() == ".py":
        for match in IMPORTED_MODULE.finditer(text):
            names.update([match.group(1)] if match.group(1) else (part.strip() for part in match.group(2).split(",")))
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
    names = "|".join(re.escape(name) for name in sorted(known))
    known_call = re.compile(rf'\[\s*"({names})"')
    problems: list[str] = []
    for metadata in sorted((root / "deploy-meta").glob("*.json")):
        skill = metadata.stem
        document = json.loads(metadata.read_text(encoding="utf-8"))
        declared = set(document.get("tools", [])) | set(document.get("optional_tools", []))
        directories = [root / "skills" / skill, *sorted((root / "skills").glob(f"*/{skill}"))]
        own = [
            path for directory in directories if (directory / "SKILL.md").is_file() for path in _skill_files(directory)
        ]
        used = {command for path in own for command in _file_commands(path, known_call)}
        reached: dict[str, Path] = {}
        for path in _reached_dependency_scripts(root, skill, own):
            for command in _file_commands(path, known_call):
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
CONSOLE_DOC = '"Script results" in docs/adding-a-skill.md'


def console_setup_problems(root: Path) -> list[str]:
    """Report a skill entry point whose __main__ block does not start by calling skill-core's use_utf8_output(), and
    a script that reconfigures a stream's encoding itself.

    Skill output names paths and text the user wrote, which a Windows pipe's legacy code page cannot encode; one
    function sets it up, so the copies cannot drift apart again.
    """
    problems: list[str] = []
    for path in sorted((root / "skills").glob("**/scripts/*.py")):
        if is_test_script(path) or path.parent.parent.name == CONSOLE_CORE:
            continue
        name = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "reconfigure"
                and any(keyword.arg == "encoding" for keyword in node.keywords)
            ):
                problems.append(
                    f"{name}:{node.lineno} reconfigures a stream itself; call {CONSOLE_SETUP}() from "
                    f"{CONSOLE_CORE} instead; see {CONSOLE_DOC}"
                )
        for node in tree.body:
            if not (isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'"):
                continue
            first = node.body[0]
            if not (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Call)
                and isinstance(first.value.func, ast.Name)
                and first.value.func.id == CONSOLE_SETUP
            ):
                problems.append(
                    f"{name}:{node.lineno} does not call {CONSOLE_SETUP}() first in its __main__ block; see "
                    f"{CONSOLE_DOC}"
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
    files += (root / "skills").glob("*/scripts/**/*.py")
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
    for path in sorted((root / "skills").glob("*/scripts/*")):
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
SUBPROCESS_CALLS = {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
CLIENTS_DOC = '"Script results" in docs/adding-a-skill.md'


def _client_command(node: ast.AST) -> tuple[int, str] | None:
    """The line and the git or gh command a node starts: a list or tuple that begins with it, or a subprocess call
    given it as a string, such as `subprocess.run("git fetch", shell=True)`."""
    first: ast.AST | None = None
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        first = node.elts[0]
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in SUBPROCESS_CALLS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.args
    ):
        first = node.args[0]
    if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
        return None
    command = first.value.split(" ", 1)[0]
    return (first.lineno, command) if command in CLIENT_COMMANDS else None


def client_command_problems(root: Path) -> list[str]:
    """Report a Python skill script outside skill-core that runs git or gh itself instead of through skill-core.

    The clients read no stdin, turn every prompt off, bound each command with a timeout, and classify its failures;
    a command a script starts itself has none of that, so a sweep can wait forever on a credential prompt.
    """
    problems: list[str] = []
    for path in sorted((root / "skills").glob("*/scripts/**/*.py")):
        name = path.relative_to(root).as_posix()
        if is_test_script(path) or name.startswith(f"{SKILL_CORE_SCRIPTS}/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for line, command in sorted({found for node in ast.walk(tree) if (found := _client_command(node))}):
            problems.append(
                f"{name}:{line} runs {command} itself; run it through {SKILL_CORE}'s {CLIENT_COMMANDS[command]}, "
                f"which bounds it and turns prompts off; see {CLIENTS_DOC}"
            )
    return problems


class SkillScriptsPolicies(unittest.TestCase):
    def test_skill_scripts_follow_the_script_results_contract(self) -> None:
        # An agent that learned one script's results can read every other's (#28).
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

    def test_skill_entry_points_set_up_the_console_through_skill_core(self) -> None:
        self.assertEqual([], console_setup_problems(REPOSITORY_ROOT))
