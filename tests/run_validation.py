"""Run the complete repository validation suite.

Usage: python -B tests/run_validation.py [-k PATTERN ...] [-v]

It runs the static policy checks below, then every regression suite under tests/ and each skill's scripts/ in
one pool of worker processes, largest first. A large Python suite is split into shards, each run by
tests/run_shard.py in its own process, so no single suite sets the length of the run. -k selects the policy
checks whose name, and the suites whose path, matches a pattern. VALIDATION_JOBS sets the number of workers.

When every changed file is documentation, it runs the policy checks and only the suites that name a changed
file; anything else, or a change it cannot determine, runs everything. --full always runs everything.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from deployer import platform_support
from deployer import tools

SKILLS_ROOT = REPOSITORY_ROOT / "skills"
MAXIMUM_INLINE_EXECUTABLE_LINES = 5
COMMAND_TIMEOUT_SECONDS = 20 * 60
EXECUTABLE_FENCE_LANGUAGES = {
    "bash",
    "javascript",
    "js",
    "powershell",
    "ps1",
    "pwsh",
    "python",
    "sh",
    "shell",
    "ts",
    "typescript",
}
EXECUTABLE_SCRIPT_EXTENSIONS = {".bash", ".cjs", ".js", ".mjs", ".ps1", ".py", ".sh", ".ts"}
TEST_SCRIPT_EXTENSIONS = {".py", ".sh", ".ps1"}
TEMPLATE_TOKEN = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
# Every Claude Code tool that can edit a file or run git, each of which the hub guard's hook must see.
HUB_GUARD_TOOLS = {"Bash", "Edit", "MultiEdit", "NotebookEdit", "PowerShell", "Write"}
SHARD_RUNNER = REPOSITORY_ROOT / "tests" / "run_shard.py"
# A Python suite is split into one shard per this many tests, up to MAXIMUM_SHARDS. Smaller shards spread a long
# suite further, at the cost of one more process start and suite import each.
TESTS_PER_SHARD = 6
MAXIMUM_SHARDS = 8
TEST_DEFINITION = re.compile(r"^[ \t]+def test_\w+", re.MULTILINE)
# Documentation no regression suite executes. A suite that names one of these files still runs when it changes.
# Markdown under skills/, agents/, or .claude/ is skill and agent behavior, not documentation.
DOCUMENTATION_DIRECTORIES = ("docs/", ".github/ISSUE_TEMPLATE/")
DOCUMENTATION_FILES = {
    "README.md", "CONTRIBUTING.md", "SECURITY.md", "CLAUDE.md", "AGENTS.md", ".github/pull_request_template.md",
}
# Bash and PowerShell suites cannot be split, so they start before any shard.
UNSPLIT_SUITE_WEIGHT = 1_000


@dataclass(frozen=True)
class ExecutableFence:
    line_number: int
    language: str
    body_line_count: int


def find_executable_fences(lines: list[str]) -> list[ExecutableFence]:
    fences: list[ExecutableFence] = []
    inside = False
    executable = False
    language = ""
    start = 0
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not inside and line.startswith("```"):
            inside = True
            start = index
            info = line[3:].split()
            language = info[0] if info else ""
            executable = language.casefold() in EXECUTABLE_FENCE_LANGUAGES
            continue
        if inside and line == "```":
            if executable:
                fences.append(ExecutableFence(start + 1, language, index - start - 1))
            inside = False
            executable = False
            language = ""
    if inside and executable:
        raise AssertionError(f"Unclosed executable {language} fence at line {start + 1}.")
    return fences


def _template_tokens(directory: Path) -> set[str]:
    tokens: set[str] = set()
    for path in directory.rglob("*"):
        if path.is_file():
            tokens.update(TEMPLATE_TOKEN.findall(path.read_text(encoding="utf-8", errors="replace")))
    return tokens


def deploy_variable_problems(
    root: Path,
    configured: set[str],
    prompted: set[str],
    derived: set[str],
) -> list[str]:
    """Report configured variables and template tokens that are not used consistently."""
    problems: list[str] = []
    for key in sorted(configured - prompted):
        problems.append(f"configure never prompts for configured variable {key}")
    for key in sorted(prompted - configured):
        problems.append(f"configure prompts for unknown variable {key}")

    required_anywhere: set[str] = set()
    for metadata in sorted((root / "deploy-meta").glob("*.json")):
        skill = metadata.stem
        required = set(json.loads(metadata.read_text(encoding="utf-8")).get("required_vars", []))
        required_anywhere |= required
        directories = [root / "skills" / skill, *sorted((root / "skills").glob(f"*/{skill}"))]
        used = set().union(*(_template_tokens(path) for path in directories if (path / "SKILL.md").is_file()))
        for key in sorted(used - required):
            problems.append(f"skill {skill} uses {{{{{key}}}}} without declaring it in required_vars")
        for key in sorted(required - used):
            problems.append(f"skill {skill} declares required variable {key} but never uses it")
        for key in sorted(required - configured - derived):
            problems.append(f"skill {skill} requires unknown variable {key}")
    for key in sorted(configured - required_anywhere):
        problems.append(f"configured variable {key} is not required by any skill")

    shared = json.loads((root / "source.json").read_text(encoding="utf-8")).get("shared_assets", {})
    for asset in sorted(shared):
        path = root / "skills" / asset
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        for key in sorted(set(TEMPLATE_TOKEN.findall(text)) - derived):
            problems.append(f"shared asset {asset} uses non-derived variable {key}")
    return problems


SHELL_FENCES = {"bash", "sh", "shell"}
SHELL_OPERATORS = ("&&", "||", ";;", "|&", "()", "(", ")", ";", "&", "|", "<", ">", "\n")
COMMAND_SEPARATORS = {";", "&", "&&", "|", "||", "|&", "(", "\n", "{"}
LEADING_KEYWORDS = {"if", "then", "else", "elif", "while", "until", "do", "time", "!", "}", ")", "fi", "done"}
# Keywords whose following words, up to the next separator, are not commands: "for name in list", "case word in".
HEADER_KEYWORDS = {"case", "for", "function", "select"}
SHELL_BUILTINS = {
    "alias", "break", "cd", "command", "continue", "declare", "dirs", "echo", "eval", "exec", "exit", "export",
    "false", "getopts", "hash", "let", "local", "mapfile", "popd", "printf", "pushd", "pwd", "read", "readarray",
    "readonly", "return", "set", "shift", "shopt", "source", "test", "trap", "true", "type", "ulimit", "umask",
    "unalias", "unset", "wait",
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
        token = token[len(operator):]
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
    for token in tokens:
        if state == "case-word":
            state = "patterns" if token == "in" else state
        elif state == "patterns":
            state = "command" if token == ")" else "arguments" if token == "esac" else state
        elif token == ";;":
            state = "patterns"
        elif token == "esac":
            state = "arguments"
        elif state == "command":
            if token in COMMAND_SEPARATORS or token in LEADING_KEYWORDS or ASSIGNMENT.match(token):
                continue
            state = "case-word" if token == "case" else "arguments"
            if token not in HEADER_KEYWORDS and COMMAND_NAME.fullmatch(token) and token not in functions:
                found.add(token)
        elif token in COMMAND_SEPARATORS:
            state = "command"
    return found


def _shell_fences(markdown: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] | None = None
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if current is not None:
                blocks.append("\n".join(current))
                current = None
            elif stripped[3:].strip().casefold() in SHELL_FENCES:
                current = []
        elif current is not None:
            current.append(line)
    return blocks


def _commands_run(directory: Path, known: set[str]) -> set[str]:
    """Commands a skill's scripts and Bash examples run, ignoring its tests."""
    names = "|".join(re.escape(name) for name in sorted(known))
    known_call = re.compile(rf'\[\s*"({names})"')
    found: set[str] = set()
    for path in directory.rglob("*"):
        if not path.is_file() or is_test_script(path):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        suffix = path.suffix.casefold()
        if suffix == ".py":
            found.update(known_call.findall(text))
            found.update(name for match in PYTHON_COMMAND.finditer(text) for name in match.groups() if name)
        elif suffix in {".sh", ".bash"}:
            found.update(shell_commands(text))
        elif suffix == ".md":
            for block in _shell_fences(text):
                found.update(shell_commands(block))
    return found


def skill_command_problems(root: Path, known: set[str], standard: set[str]) -> list[str]:
    """Report commands a skill runs that are neither standard nor declared, and declared tools it never runs."""
    problems: list[str] = []
    for metadata in sorted((root / "deploy-meta").glob("*.json")):
        skill = metadata.stem
        declared = set(json.loads(metadata.read_text(encoding="utf-8")).get("tools", []))
        directories = [root / "skills" / skill, *sorted((root / "skills").glob(f"*/{skill}"))]
        used = set().union(*(_commands_run(path, known) for path in directories if (path / "SKILL.md").is_file()))
        for name in sorted((used & known) - declared):
            problems.append(f"skill {skill} runs {name} without declaring it in tools")
        for name in sorted(declared - used):
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
            problems.append(f"{name}:{number} filters gh output with {flag}; parse its JSON in Python instead; "
                            f"see {COMMANDS_DOC}")
    return problems


GRANTS_DOC = "\"Granting tools\" in docs/adding-a-skill.md"


def skill_grant_problems(root: Path) -> list[str]:
    """Report shell grants that cover every command or one shell only, grants no step uses, and own-script commands
    left ungranted.

    The rules are the analyze-skill-cost inventory's, so the audit and this policy cannot disagree.
    """
    sys.path.insert(0, str(SKILLS_ROOT / "analyze-skill-cost" / "scripts"))
    import skill_inventory

    problems: list[str] = []
    for metadata in sorted((root / "deploy-meta").glob("*.json")):
        skill_md = root / "skills" / metadata.stem / "SKILL.md"
        if not skill_md.is_file():
            continue
        name = skill_md.relative_to(root).as_posix()
        for line in skill_inventory.tools(skill_md):
            kind, _, detail = line.partition(" ")
            if kind == "UNSCOPED_ALLOWED":
                problems.append(f"{name} grants {detail} for every command; see {GRANTS_DOC}")
            elif kind == "UNPAIRED_ALLOWED":
                problems.append(f"{name} grants {detail} in one shell only; see {GRANTS_DOC}")
            elif kind == "UNUSED_ALLOWED":
                problems.append(f"{name} grants {detail}, which no step uses; see {GRANTS_DOC}")
            elif kind == "MISSING_ALLOWED" and detail.split(" ")[0] == "Bash":
                problems.append(f"{name}:{detail.split(' ')[1]} runs a command without a shell grant; see {GRANTS_DOC}")
            elif kind == "UNGRANTED" and "${CLAUDE_SKILL_DIR}" in detail:
                tool, number, command = detail.split(" ", 2)
                problems.append(f"{name}:{number} no {tool} grant covers {command}; see {GRANTS_DOC}")
            elif kind == "EXPANDS":
                number, command = detail.split(" ", 1)
                problems.append(f"{name}:{number} expands a shell variable, so it always prompts: {command}; see {GRANTS_DOC}")
    return problems


INSTALL_PATH = re.compile(r"\{\{HOME\}\}/\.claude/skills/([A-Za-z0-9._-]+)")
SIBLING_PATH = re.compile(r"\$\{CLAUDE_SKILL_DIR\}/\.\./([A-Za-z0-9._-]+)")
BARE_SCRIPT_PATH = re.compile(r"""(?:^|[\s"'=])(?:\./|\.\./[A-Za-z0-9._-]+/)?scripts/""")
SKILL_PATHS_DOC = "\"Paths to a skill's own files\" in docs/adding-a-skill.md"
# Git Bash takes $HOME from HOME, which need not be the profile folder the deployer installs into.
HOME_VARIABLE = re.compile(r"\$(?:HOME\b|\{HOME\}|env:HOME\b)", re.IGNORECASE)
AGENTS_DOC = "\"Subagent definitions\" in docs/adding-a-skill.md"


def skill_path_problems(root: Path) -> list[str]:
    """Report skills and agents that reach a skill's files other than through ${CLAUDE_SKILL_DIR}."""
    skills = {path.parent.name for path in (root / "skills").glob("**/SKILL.md")}
    documents: list[tuple[Path, set[str] | None]] = [(path, None) for path in sorted((root / "agents").glob("*.md"))]
    for metadata in sorted((root / "deploy-meta").glob("*.json")):
        skill = metadata.stem
        dependencies = set(json.loads(metadata.read_text(encoding="utf-8")).get("skill_deps", []))
        for directory in [root / "skills" / skill, *sorted((root / "skills").glob(f"*/{skill}"))]:
            if (directory / "SKILL.md").is_file():
                documents += [(path, dependencies) for path in sorted(directory.rglob("*.md"))]
    problems: list[str] = []
    for path, dependencies in documents:
        name = path.relative_to(root).as_posix()
        in_shell_fence = False
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_shell_fence = not in_shell_fence and stripped[3:].strip().casefold() in SHELL_FENCES
                continue
            for match in INSTALL_PATH.finditer(line):
                if match.group(1) in skills:
                    problems.append(
                        f"{name}:{number} names skill {match.group(1)} by its install path; see {SKILL_PATHS_DOC}"
                    )
            if in_shell_fence and BARE_SCRIPT_PATH.search(line):
                problems.append(f"{name}:{number} runs a script by a bare relative path; see {SKILL_PATHS_DOC}")
            if dependencies is None and HOME_VARIABLE.search(line):
                problems.append(f"{name}:{number} finds a file through $HOME; see {AGENTS_DOC}")
            for match in SIBLING_PATH.finditer(line):
                if dependencies is not None and match.group(1) not in dependencies:
                    problems.append(f"{name}:{number} reaches ../{match.group(1)} without declaring it in skill_deps")
    return problems


# Defense in depth only: the private-name scan in docs/releasing.md runs before every release. YourName is the
# placeholder user documentation may show; the drive-sync folder name is split so this file does not match itself.
PRIVATE_REFERENCE = re.compile(
    "|".join((
        r"C:[/\\]Users[/\\](?!YourName(?:[/\\]|$))",
        "One" + r"Drive - [^/\\\r\n]+",
        r"github\.com[/\\][A-Za-z0-9_.-]+-internal(?:[/\\]|$)",
    )),
    re.IGNORECASE,
)


def repository_files(root: Path) -> list[Path]:
    """Tracked files plus untracked ones Git does not ignore, which is what a commit could include."""
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root, capture_output=True, check=True,
    ).stdout.decode("utf-8")
    return [root / name for name in listed.split("\0") if name and (root / name).is_file()]


def private_references(root: Path, files: list[Path]) -> list[str]:
    """Report each line naming a real user profile, a synced drive folder, or an internal GitHub organization."""
    found: list[str] = []
    for path in sorted(files):
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for number, line in enumerate(lines, start=1):
            if PRIVATE_REFERENCE.search(line):
                found.append(f"{path.relative_to(root).as_posix()}:{number}: {line.strip()}")
    return found


def hub_guard_matcher_problems(settings: dict[str, object]) -> list[str]:
    """Report each tool that can edit a file or run git whose calls the hub guard's PreToolUse hook would not see."""
    hooks = settings.get("hooks")
    groups = hooks.get("PreToolUse", []) if isinstance(hooks, dict) else []
    guarded = [
        group for group in groups
        if any(re.search(r"tools/worktrees\.py\"? guard$", hook.get("command", "")) for hook in group.get("hooks", []))
    ]
    if not guarded:
        return ["no PreToolUse hook runs tools/worktrees.py guard"]
    matched: set[str] = set()
    for group in guarded:
        matcher = group.get("matcher", "")
        if matcher in {"", "*"}:
            return []
        matched.update(matcher.split("|"))
    return [f"the hub guard's hook does not match the {tool} tool" for tool in sorted(HUB_GUARD_TOOLS - matched)]


def repository_skill_problems(root: Path) -> list[str]:
    """Report each repository skill under .claude/skills without a matching .agents/skills shim, and each stray shim.

    Codex and Copilot CLI read .agents/skills, so a shim must carry the skill's own frontmatter, which is all they
    see before choosing it, and point to the skill as the authoritative workflow.
    """
    def frontmatter(path: Path) -> str:
        parts = path.read_text(encoding="utf-8").replace("\r\n", "\n").split("---\n", 2)
        return parts[1] if len(parts) == 3 and parts[0] == "" else ""

    skills = {path.parent.name: path for path in (root / ".claude" / "skills").glob("*/SKILL.md")}
    shims = {path.parent.name: path for path in (root / ".agents" / "skills").glob("*/SKILL.md")}
    found = [f".agents/skills/{name}/SKILL.md has no .claude/skills/{name}/SKILL.md" for name in sorted(set(shims) - set(skills))]
    for name in sorted(skills):
        shim = shims.get(name)
        if shim is None:
            found.append(f".claude/skills/{name}/SKILL.md has no .agents/skills/{name}/SKILL.md shim")
            continue
        if not frontmatter(skills[name]) or frontmatter(shim) != frontmatter(skills[name]):
            found.append(f".agents/skills/{name}/SKILL.md frontmatter differs from .claude/skills/{name}/SKILL.md")
        if f"`../../../.claude/skills/{name}/SKILL.md`" not in shim.read_text(encoding="utf-8"):
            found.append(f".agents/skills/{name}/SKILL.md does not point to ../../../.claude/skills/{name}/SKILL.md")
    return found


def fixture_source_problems(root: Path) -> list[str]:
    """Report a fixture source under tests/fixtures that could ship, collide with what ships, or break the contract.

    A fixture source, such as the runtime canary's, is deployed only into a throwaway home. It must keep its own
    source ID and skill names, the shipped source must never name it, and its skills follow the path contract.
    """
    shipped = json.loads((root / "source.json").read_text(encoding="utf-8"))
    names = {path.stem for path in (root / "deploy-meta").glob("*.json")} | set(shipped.get("bundles", {}))
    found: list[str] = []
    for source_file in sorted((root / "tests" / "fixtures").glob("*/source.json")):
        fixture = source_file.parent
        label = fixture.relative_to(root).as_posix()
        if json.loads(source_file.read_text(encoding="utf-8")).get("id") == shipped.get("id"):
            found.append(f"{label}/source.json reuses the shipped source ID")
        for metadata in sorted((fixture / "deploy-meta").glob("*.json")):
            if metadata.stem in names:
                found.append(f"{label} skill {metadata.stem} shares its name with a shipped skill or bundle")
        found += [f"{label}: {problem}" for problem in skill_path_problems(fixture)]
    shipped_files = [root / "source.json", *(path for folder in ("skills", "deploy-meta", "agents")
                                            for path in (root / folder).rglob("*") if path.is_file())]
    for path in shipped_files:
        if "tests/fixtures" in path.read_text(encoding="utf-8", errors="replace").replace("\\", "/"):
            found.append(f"{path.relative_to(root).as_posix()} names tests/fixtures, which never ships")
    return found


def is_executable_script(path: Path) -> bool:
    return path.suffix.casefold() in EXECUTABLE_SCRIPT_EXTENSIONS


def is_shell_script(path: Path) -> bool:
    return path.suffix.casefold() in {".sh", ".bash"}


def is_test_script(path: Path) -> bool:
    if path.suffix.casefold() not in TEST_SCRIPT_EXTENSIONS:
        return False
    stem = path.stem.casefold()
    return (
        stem.startswith(("test_", "test-"))
        or stem.endswith(("_test", "-test"))
        or ".test." in path.name.casefold()
    )


def relative(path: Path) -> str:
    return path.relative_to(REPOSITORY_ROOT).as_posix()


def to_git_bash_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    if len(normalized) >= 2 and normalized[1] == ":":
        return f"/{normalized[0].lower()}{normalized[2:]}"
    return normalized


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def find_git_bash() -> str:
    if sys.platform == "win32":
        found = platform_support.find_bash()
        if found:
            return found
        raise AssertionError(
            "Git Bash was not found. Install Git for Windows or set GIT_BASH; "
            "Git Bash is required for repository validation."
        )
    configured = os.environ.get("GIT_BASH")
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("bash")
    if found:
        return found
    raise AssertionError("bash was not found; it is required for repository validation.")


def find_shellcheck() -> str | None:
    found = shutil.which("shellcheck")
    if found:
        return found
    winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "shellcheck.exe"
    return str(winget) if winget.is_file() else None


def find_powershell() -> str:
    found = shutil.which("pwsh")
    if found:
        return found
    raise AssertionError(
        "PowerShell 7 (pwsh) was not found in PATH. It is required for repository validation."
    )


PREREQUISITES: tuple[tuple[str, str, Callable[[], str | None]], ...] = (
    ("Git Bash", "Git Bash", find_git_bash),
    ("ShellCheck", "ShellCheck", find_shellcheck),
    ("PowerShell 7 (pwsh)", "PowerShell", find_powershell),
)


def missing_prerequisites(
    prerequisites: tuple[tuple[str, str, Callable[[], str | None]], ...] = PREREQUISITES,
) -> list[str]:
    """Describe every required tool that cannot be found, with its install hint."""
    missing: list[str] = []
    for label, hint_key, finder in prerequisites:
        try:
            found = finder()
        except AssertionError:
            found = None
        if not found:
            missing.append(f"  - {label}: {platform_support.install_hint(hint_key)}")
    return missing


# The oldest release of each validation tool the suite is known to work with. docs/dependency-updates.md owns these,
# and CI installs Python and ShellCheck at their floors. Git Bash has no floor: the shell scripts need no Bash 4.
DEPENDENCY_DOC = "docs/dependency-updates.md"
VALIDATION_FLOORS: dict[str, tuple[int, ...]] = {
    "Python": tools.MINIMUM_PYTHON,
    "ShellCheck": (0, 9, 0),
    "PowerShell 7 (pwsh)": (7, 0),
}


def read_tool_version(path: str) -> tuple[int, ...] | None:
    result = platform_support.run_tool([path, "--version"])
    return tools.parse_version(result.output) if result.returncode == 0 else None


def tool_versions(
    prerequisites: tuple[tuple[str, str, Callable[[], str | None]], ...] = PREREQUISITES,
    read_version: Callable[[str], tuple[int, ...] | None] = read_tool_version,
) -> dict[str, tuple[int, ...] | None]:
    """The version of each required tool that is found, or None where it cannot be read."""
    versions: dict[str, tuple[int, ...] | None] = {}
    for label, _, finder in prerequisites:
        try:
            found = finder()
        except AssertionError:
            found = None
        if found:
            versions[label] = read_version(found)
    return versions


def outdated_prerequisites(
    versions: dict[str, tuple[int, ...] | None], python: tuple[int, ...]
) -> list[str]:
    """Describe every found tool older than its floor, or whose version cannot be read, with its install hint."""
    hints = {label: hint_key for label, hint_key, _ in PREREQUISITES}
    found = {"Python": python, **versions}
    outdated: list[str] = []
    for label, floor in VALIDATION_FLOORS.items():
        if label not in found:
            continue
        version = found[label]
        hint = f": {platform_support.install_hint(hints[label])}" if label in hints else ""
        if version is None:
            outdated.append(
                f"  - {label}: its version could not be read; the floor is {tools.format_version(floor)} in "
                f"{DEPENDENCY_DOC}{hint}"
            )
        elif version < floor:
            outdated.append(
                f"  - {label} {tools.format_version(version)} is older than the floor "
                f"{tools.format_version(floor)} in {DEPENDENCY_DOC}{hint}"
            )
    return outdated


def report_prerequisite_problems(versions: dict[str, tuple[int, ...] | None]) -> bool:
    missing = missing_prerequisites()
    outdated = outdated_prerequisites(versions, tools.python_version())
    if missing:
        print("Repository validation cannot run; required tools were not found:", file=sys.stderr)
        for line in missing:
            print(line, file=sys.stderr)
    if outdated:
        print("Repository validation cannot run; required tools are older than their floors:", file=sys.stderr)
        for line in outdated:
            print(line, file=sys.stderr)
    if missing or outdated:
        for line in platform_support.INSTALL_HELP:
            print(line, file=sys.stderr)
    return bool(missing or outdated)


def step_summary(
    python: tuple[int, ...],
    versions: dict[str, tuple[int, ...] | None],
    mode: str,
    policies: int,
    jobs: int,
    seconds: float,
    failed: list[str],
) -> str:
    """The Markdown GitHub Actions shows on the run's summary page."""
    found = [f"Python {tools.format_version(python)}"] + [
        f"{label} {tools.format_version(version) if version else 'unknown'}" for label, version in versions.items()
    ]
    result = "**validation FAILED**" if failed else "**validation passed**"
    lines = [
        "## Repository validation",
        "",
        f"- {', '.join(found)}",
        f"- {mode}",
        f"- {policies} policy checks and {jobs} suite jobs in {seconds:.0f}s: {result}",
        *(f"- Failed: `{label}`" for label in failed),
    ]
    return "\n".join(lines) + "\n"


def append_step_summary(environment: Mapping[str, str], text: str) -> None:
    path = environment.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as summary:
            summary.write(text)


def run_process(arguments: list[str], environment: dict[str, str] | None = None) -> None:
    merged = dict(os.environ)
    if environment:
        merged.update(environment)
    # Redirect to real files: MSYS tools can reject inherited anonymous pipes with a
    # spurious "failed to set file descriptor text/binary mode" error.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            completed = subprocess.run(
                arguments,
                cwd=REPOSITORY_ROOT,
                env=merged,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                timeout=COMMAND_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(
                f"Command timed out after {COMMAND_TIMEOUT_SECONDS}s: {arguments[0]}"
            ) from exc
        if completed.returncode != 0:
            stdout.seek(0)
            stderr.seek(0)
            output = stdout.read().decode("utf-8", "replace")
            error = stderr.read().decode("utf-8", "replace")
            raise AssertionError(
                f"Command failed with exit code {completed.returncode}: "
                f"{' '.join(arguments)}\n{output}\n{error}"
            )


def run_git_bash(command: str) -> None:
    environment: dict[str, str] = {}
    prefix = ""
    shellcheck = find_shellcheck()
    if shellcheck is not None:
        environment["SHELLCHECK_BIN_DIR"] = str(Path(shellcheck).parent)
        prefix = (
            'if [ -n "${SHELLCHECK_BIN_DIR:-}" ]; then '
            'export PATH="$(cygpath -u "$SHELLCHECK_BIN_DIR" 2>/dev/null || '
            'printf %s "$SHELLCHECK_BIN_DIR"):$PATH"; fi; '
        )
    root = shell_quote(to_git_bash_path(str(REPOSITORY_ROOT)))
    run_process(
        [find_git_bash(), "-l", "-c", f"{prefix}set -euo pipefail; cd {root}; {command}"],
        environment,
    )


def run_test_script(path: Path) -> None:
    target = relative(path)
    suffix = path.suffix.casefold()
    if suffix == ".py":
        run_process([sys.executable, "-B", target])
    elif suffix == ".sh":
        run_git_bash(f"bash {shell_quote(target)}")
    elif suffix == ".ps1":
        run_process(
            [find_powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", target]
        )
    else:
        raise AssertionError(f"Unsupported skill test type '{path.suffix}': {target}")


def worker_count() -> int:
    configured = os.environ.get("VALIDATION_JOBS")
    if configured:
        return max(1, int(configured))
    return max(1, min(16, os.cpu_count() or 1))


@dataclass(frozen=True)
class Job:
    label: str
    name: str  # what -k matches: the suite's path, without the shard
    weight: float  # a rough cost: the pool starts the heaviest jobs first
    run: Callable[[], None]


def regression_suites() -> list[Path]:
    """Every regression suite: the test scripts under tests/ and under each skill's scripts/."""
    roots = [REPOSITORY_ROOT / "tests", *(skill / "scripts" for skill in skill_directories())]
    found = (path for root in roots if root.is_dir() for path in root.rglob("*") if path.is_file() and is_test_script(path))
    return sorted(found, key=lambda path: relative(path).casefold())


def shard_count(suite: Path) -> int:
    if suite.suffix.casefold() != ".py":
        return 1
    tests = len(TEST_DEFINITION.findall(suite.read_text(encoding="utf-8")))
    return max(1, min(MAXIMUM_SHARDS, tests // TESTS_PER_SHARD))


def run_shard(suite: Path, index: int, count: int) -> None:
    run_process([sys.executable, "-B", relative(SHARD_RUNNER), relative(suite), str(index), str(count)])


def suite_jobs(suites: list[Path]) -> list[Job]:
    jobs = []
    for suite in suites:
        label = relative(suite)
        count = shard_count(suite)
        if suite.suffix.casefold() != ".py":
            jobs.append(Job(label, label, UNSPLIT_SUITE_WEIGHT, lambda s=suite: run_test_script(s)))
            continue
        tests = len(TEST_DEFINITION.findall(suite.read_text(encoding="utf-8")))
        if count == 1:
            jobs.append(Job(label, label, tests, lambda s=suite: run_test_script(s)))
            continue
        for index in range(count):
            jobs.append(Job(f"{label} [shard {index + 1}/{count}]", label, tests / count,
                            lambda s=suite, i=index, n=count: run_shard(s, i, n)))
    return jobs


def static_shell_check() -> None:
    scripts = sorted(
        (path for path in SKILLS_ROOT.rglob("*") if path.is_file() and is_shell_script(path)),
        key=lambda p: relative(p).casefold(),
    )
    if not scripts:
        raise AssertionError("No shell scripts were found to validate.")
    checks = " && ".join(f"bash -n {shell_quote(relative(path))}" for path in scripts)
    arguments = " ".join(shell_quote(relative(path)) for path in scripts)
    run_git_bash(f"{checks} && shellcheck --severity=warning {arguments}")


def all_jobs() -> list[Job]:
    shell = "static shell checks (bash -n and ShellCheck on skill scripts)"
    return [Job(shell, shell, UNSPLIT_SUITE_WEIGHT, static_shell_check),
            *suite_jobs(regression_suites())]


def run_jobs(jobs: list[Job], verbose: bool, workers: int) -> list[tuple[Job, AssertionError]]:
    """Run every job in one pool, heaviest first, and return the failures in label order."""
    failures: list[tuple[Job, AssertionError]] = []
    lock = threading.Lock()

    def attempt(job: Job) -> None:
        started = time.perf_counter()
        try:
            job.run()
            error = None
        except AssertionError as exc:
            error = exc
        with lock:
            if error is not None:
                failures.append((job, error))
            if verbose or error is not None:
                print(f"{'FAIL' if error else 'ok'} {time.perf_counter() - started:6.1f}s {job.label}", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(attempt, sorted(jobs, key=lambda job: -job.weight)))
    return sorted(failures, key=lambda failure: failure[0].label)


def is_documentation(path: str) -> bool:
    return path in DOCUMENTATION_FILES or path.startswith(DOCUMENTATION_DIRECTORIES)


def changed_paths(root: Path, base: str) -> list[str] | None:
    """Files changed since the merge base with `base`, plus uncommitted and untracked ones; None when Git cannot tell.

    Renames count as a deletion and an addition, so moving a file out of skills/ is still a change to skills/.
    """
    paths: set[str] = set()
    for command in (
        ["diff", "--name-only", "--no-renames", f"{base}...HEAD"],
        ["diff", "--name-only", "--no-renames", "HEAD"],
        ["ls-files", "--others", "--exclude-standard"],
    ):
        result = subprocess.run(["git", "-C", str(root), *command], capture_output=True, text=True, encoding="utf-8")
        if result.returncode != 0:
            return None
        paths.update(line for line in result.stdout.splitlines() if line)
    return sorted(paths)


def documentation_only(paths: list[str] | None) -> tuple[bool, str]:
    """Whether every changed file is documentation, and why. Anything uncertain is not."""
    if paths is None:
        return False, "the changed files could not be determined"
    if not paths:
        return False, "no changed files were found"
    others = [path for path in paths if not is_documentation(path)]
    if others:
        return False, f"{len(others)} changed files are not documentation, such as {others[0]}"
    return True, f"all {len(paths)} changed files are documentation"


def suites_naming(paths: list[str], suites: list[Path]) -> list[Path]:
    """The suites whose source names a changed file, by path or by file name."""
    names = {name for path in paths for name in (path, path.rsplit("/", 1)[-1])}
    return [suite for suite in suites if any(name in suite.read_text(encoding="utf-8") for name in names)]


def name_patterns(patterns: list[str]) -> list[str]:
    """unittest's -k rule: a pattern without a wildcard matches as a substring."""
    return [pattern if any(character in pattern for character in "*?[") else f"*{pattern}*" for pattern in patterns]


def skill_directories() -> list[Path]:
    return sorted((path for path in SKILLS_ROOT.iterdir() if path.is_dir()), key=lambda p: p.name.casefold())


class RepositoryValidation(unittest.TestCase):
    def test_tracked_claude_settings_hold_hooks_only(self) -> None:
        # Tracked settings reach every developer's sessions, so they may add hooks but never decide what a
        # developer allows; permissions and every other setting stay in user or local settings.
        settings = json.loads((REPOSITORY_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertLessEqual(set(settings), {"$schema", "hooks"})
        self.assertEqual([], [path for path in repository_files(REPOSITORY_ROOT) if path.name == "settings.local.json"])

    def test_hub_guard_hook_sees_every_tool_that_edits_files_or_runs_git(self) -> None:
        # A tool missing from the matcher is never shown to the guard, so the hub is open through it.
        settings = json.loads((REPOSITORY_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual([], hub_guard_matcher_problems(settings))

    def test_hub_guard_matcher_policy_detects_a_missing_tool_or_hook(self) -> None:
        command = 'python -B "$CLAUDE_PROJECT_DIR/tools/worktrees.py" guard'

        def settings(matcher: str, hook_command: str = command) -> dict[str, object]:
            group = {"matcher": matcher, "hooks": [{"type": "command", "command": hook_command}]}
            return {"hooks": {"PreToolUse": [group]}}

        self.assertEqual([], hub_guard_matcher_problems(settings("Edit|Write|MultiEdit|NotebookEdit|Bash|PowerShell")))
        self.assertEqual([], hub_guard_matcher_problems(settings("*")))
        self.assertEqual(
            ["the hub guard's hook does not match the PowerShell tool"],
            hub_guard_matcher_problems(settings("Edit|Write|MultiEdit|NotebookEdit|Bash")),
        )
        self.assertEqual(
            ["the hub guard's hook does not match the Bash tool", "the hub guard's hook does not match the Write tool"],
            hub_guard_matcher_problems(settings("Edit|MultiEdit|NotebookEdit|PowerShell")),
        )
        for broken in (settings("Bash|PowerShell", "python -B tools/other.py guard"), {}, {"hooks": {}}):
            with self.subTest(settings=broken):
                self.assertEqual(
                    ["no PreToolUse hook runs tools/worktrees.py guard"], hub_guard_matcher_problems(broken)
                )

    def test_skill_scripts_use_standard_layout(self) -> None:
        for skill in skill_directories():
            for path in skill.rglob("*"):
                if not path.is_file() or not is_executable_script(path):
                    continue
                inside = path.relative_to(skill).as_posix()
                self.assertTrue(
                    inside.startswith("scripts/"),
                    f"Skill '{skill.name}' contains executable file '{inside}' outside "
                    "scripts/. Move executable artifacts to skills/<name>/scripts/.",
                )

    def test_skill_markdown_does_not_embed_programs(self) -> None:
        for markdown in sorted(SKILLS_ROOT.rglob("*.md")):
            lines = markdown.read_text(encoding="utf-8").splitlines()
            for fence in find_executable_fences(lines):
                self.assertLessEqual(
                    fence.body_line_count,
                    MAXIMUM_INLINE_EXECUTABLE_LINES,
                    f"{relative(markdown)}:{fence.line_number} contains a {fence.language} "
                    f"fence with {fence.body_line_count} lines. Markdown may contain only "
                    f"short command examples of at most {MAXIMUM_INLINE_EXECUTABLE_LINES} "
                    "lines; move executable logic to the skill's scripts/ directory.",
                )

    def test_skills_leave_shared_runtime_guidance_to_their_adapters(self) -> None:
        # Claude runs a skill directly; only the generated ~/.agents adapter tells other runtimes to read shared
        # Markdown guidance first, so a skill that points to it spends a turn on every Claude run.
        shared = json.loads((REPOSITORY_ROOT / "source.json").read_text(encoding="utf-8"))["shared_assets"]
        guidance = sorted(name for name in shared if name.endswith(".md"))
        self.assertIn("runtime-compatibility.md", guidance)
        for skill in sorted(SKILLS_ROOT.glob("*/SKILL.md")):
            text = skill.read_text(encoding="utf-8")
            for name in guidance:
                self.assertNotIn(name, text, f"{relative(skill)} points to {name}; the runtime adapter does that")

    def test_embedded_script_policy_recognizes_long_executable_fences(self) -> None:
        short_example = ["```bash", "echo one", "echo two", "```"]
        embedded_program = ["```python", *[f"line_{n}()" for n in range(6)], "```"]
        self.assertEqual(2, find_executable_fences(short_example)[0].body_line_count)
        self.assertGreater(
            find_executable_fences(embedded_program)[0].body_line_count,
            MAXIMUM_INLINE_EXECUTABLE_LINES,
        )
        self.assertEqual([], find_executable_fences(["```text", *["x"] * 9, "```"]))
        with self.assertRaisesRegex(AssertionError, "Unclosed executable"):
            find_executable_fences(["```sh", "echo open"])

    def test_scripted_skills_have_regression_suites(self) -> None:
        for skill in skill_directories():
            scripts = skill / "scripts"
            if not scripts.is_dir():
                continue
            files = sorted(path for path in scripts.rglob("*") if path.is_file())
            if not any(is_executable_script(path) and not is_test_script(path) for path in files):
                continue
            with self.subTest(skill=skill.name):
                self.assertTrue(
                    any(is_test_script(path) for path in files),
                    f"Skill '{skill.name}' contains scripts but has no executable test "
                    "suite. Add a test_*, test-*, *_test, *-test, or *.test.* Python, "
                    "Bash, or PowerShell script under the skill's scripts/ directory.",
                )

    def test_every_regression_suite_is_found(self) -> None:
        suites = {relative(path) for path in regression_suites()}
        for expected in (
            "tests/deployer/test_frontmatter.py",
            "tests/tools/test_worktrees.py",
            "tests/ai-config/test_cross_skill_contracts.py",
            "skills/repo-cleanup/scripts/test_remove_worktree.sh",
            "skills/code-review-core/scripts/test_review_pipeline.py",
        ):
            self.assertIn(expected, suites)
        self.assertNotIn("tests/deployer/harness.py", suites)
        self.assertNotIn("tests/run_shard.py", suites)

    def test_shards_run_each_test_once_with_its_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "suite with spaces"
            root.mkdir()
            log = root / "log.txt"
            (root / "helper.py").write_text("VALUE = 'from the suite directory'\n", encoding="utf-8")
            tests = "".join(f"    def test_{number}(self):\n        record('test_{number}')\n" for number in range(7))
            suite = root / "test_fixture.py"
            suite.write_text(
                "import unittest\nfrom pathlib import Path\nimport helper\n"
                f"LOG = Path({str(log)!r})\n"
                "def record(entry):\n    with LOG.open('a', encoding='utf-8') as handle:\n        handle.write(entry + '\\n')\n"
                "def setUpModule():\n    record('module ' + helper.VALUE)\n"
                "class Fixture(unittest.TestCase):\n"
                "    @classmethod\n    def setUpClass(cls):\n        record('class')\n"
                f"{tests}"
                "if __name__ == '__main__':\n    record('ran as __main__')\n    unittest.main()\n",
                encoding="utf-8",
            )
            for index in range(3):
                completed = subprocess.run([sys.executable, "-B", str(SHARD_RUNNER), str(suite), str(index), "3"],
                                           capture_output=True, text=True)
                self.assertEqual(0, completed.returncode, completed.stderr)
            entries = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(sorted(f"test_{number}" for number in range(7)), sorted(e for e in entries if e.startswith("test_")))
            self.assertEqual(3, entries.count("module from the suite directory"))
            self.assertEqual(3, entries.count("class"))
            self.assertNotIn("ran as __main__", entries)

            suite.write_text(suite.read_text(encoding="utf-8").replace("record('test_3')", "self.fail('boom')"),
                             encoding="utf-8")
            failing = subprocess.run([sys.executable, "-B", str(SHARD_RUNNER), str(suite), "0", "3"],
                                     capture_output=True, text=True)
            self.assertEqual(1, failing.returncode)
            self.assertIn("boom", failing.stderr)
            refused = subprocess.run([sys.executable, "-B", str(SHARD_RUNNER), str(suite), "3", "3"],
                                     capture_output=True, text=True)
            self.assertEqual(2, refused.returncode)

    def test_documentation_paths_are_classified(self) -> None:
        for path in ("docs/skills.md", "docs/new/guide.md", "README.md", "CONTRIBUTING.md", "SECURITY.md", "CLAUDE.md",
                     "AGENTS.md", ".github/ISSUE_TEMPLATE/bug.md", ".github/pull_request_template.md"):
            with self.subTest(path=path):
                self.assertTrue(is_documentation(path))
        for path in ("skills/repo-cleanup/SKILL.md", "skills/README.md", "agents/code-review-reviewer.md",
                     ".claude/skills/change-skill/SKILL.md", ".agents/skills/change-skill/SKILL.md",
                     ".github/workflows/validate.yml", "tests/run_validation.py", "deployer/source.py",
                     "source.json", "docs", "LICENSE", "readme.md"):
            with self.subTest(path=path):
                self.assertFalse(is_documentation(path))

    def test_only_a_change_that_is_all_documentation_skips_the_suites(self) -> None:
        self.assertEqual((True, "all 2 changed files are documentation"),
                         documentation_only(["README.md", "docs/skills.md"]))
        for paths, reason in (
            (None, "could not be determined"),
            ([], "no changed files"),
            (["docs/skills.md", "skills/repo-cleanup/SKILL.md"], "1 changed files are not documentation, such as skills/"),
        ):
            with self.subTest(paths=paths):
                only, why = documentation_only(paths)
                self.assertFalse(only)
                self.assertIn(reason, why)

    def test_suites_that_name_a_changed_document_still_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            by_path, by_name, unrelated = root / "test_path.py", root / "test_name.py", root / "test_other.py"
            by_path.write_text("SOURCE = 'docs/skills.md'\n", encoding="utf-8")
            by_name.write_text("open('README.md')\n", encoding="utf-8")
            unrelated.write_text("pass\n", encoding="utf-8")
            self.assertEqual([by_path, by_name], suites_naming(["docs/skills.md", "README.md"], [by_path, by_name, unrelated]))
            self.assertEqual([], suites_naming(["docs/other.md"], [by_path, by_name, unrelated]))

    def test_changed_files_include_commits_uncommitted_untracked_and_both_sides_of_a_rename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository with spaces"
            root.mkdir()
            environment = {**os.environ, "GIT_CONFIG_GLOBAL": str(Path(temporary) / "empty"), "GIT_CONFIG_NOSYSTEM": "1",
                           "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
            (Path(temporary) / "empty").write_text("", encoding="utf-8")

            def git(*arguments: str) -> None:
                subprocess.run(["git", "-C", str(root), *arguments], env=environment, check=True, capture_output=True)

            for name in ("skills/a/tool.py", "docs/kept.md", "docs/edited.md"):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(f"{name}\n", encoding="utf-8")
            git("init", "-q", "-b", "main")
            git("add", ".")
            git("commit", "-q", "-m", "base")
            git("switch", "-q", "-c", "work")
            git("mv", "skills/a/tool.py", "docs/tool.md")
            git("commit", "-q", "-m", "move")
            (root / "docs" / "edited.md").write_text("changed\n", encoding="utf-8")
            (root / "docs" / "new.md").write_text("new\n", encoding="utf-8")
            with mock.patch.dict(os.environ, environment):
                self.assertEqual(["docs/edited.md", "docs/new.md", "docs/tool.md", "skills/a/tool.py"],
                                 changed_paths(root, "main"))
                self.assertIsNone(changed_paths(root, "no-such-base"))
                self.assertIsNone(changed_paths(Path(temporary), "main"))

    def test_large_python_suites_are_sharded_and_others_run_whole(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, tests in (("test_small.py", 3), ("test_large.py", 40), ("test_huge.py", 200)):
                body = "".join(f"    def test_{number}(self): pass\n" for number in range(tests))
                (root / name).write_text(f"import unittest\nclass T(unittest.TestCase):\n{body}", encoding="utf-8")
            (root / "test_script.ps1").write_text("exit 0\n", encoding="utf-8")
            self.assertEqual(1, shard_count(root / "test_small.py"))
            self.assertEqual(40 // TESTS_PER_SHARD, shard_count(root / "test_large.py"))
            self.assertEqual(MAXIMUM_SHARDS, shard_count(root / "test_huge.py"))
            self.assertEqual(1, shard_count(root / "test_script.ps1"))
            self.assertEqual(["*deployer*", "test_*.py"], name_patterns(["deployer", "test_*.py"]))

    def test_deploy_variables_are_declared_consistently_and_used(self) -> None:
        from deployer import config

        self.assertEqual(
            [],
            deploy_variable_problems(
                REPOSITORY_ROOT,
                set(config.CONFIGURED_VARIABLES),
                set(config.PROMPTS),
                set(config.DERIVED_VARIABLES),
            ),
        )

    def test_skill_reference_matches_the_skills(self) -> None:
        # docs/skills.md is generated from each skill's frontmatter and metadata, so a skill change that does not
        # rerun tools/skill_reference.py --write leaves the reference describing a skill that no longer exists.
        from tools import skill_reference

        self.assertEqual([], skill_reference.problems(REPOSITORY_ROOT))

    def test_repository_has_no_private_machine_or_organization_references(self) -> None:
        self.assertEqual([], private_references(REPOSITORY_ROOT, repository_files(REPOSITORY_ROOT)))

    def test_private_reference_scan_detects_each_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Each sample is split so this source file does not match the scan it tests.
            user = "C:\\" + "Users\\someone\\GitHub"
            forward = "c:/" + "users/Someone/tools"
            synced = "One" + "Drive - Contoso"
            internal = "github.com/contoso" + "-internal/tools"
            samples = {
                "allowed.md": "C:/Users/YourName/GitHub and C:\\Users\\YourName\\scoop\n",
                "user.md": f"Install to {user}\n",
                "forward.md": f"see {forward}\n",
                "synced.md": f"C:/Data/{synced}/notes\n",
                "internal.md": f"https://{internal}\n",
                "public.md": "https://github.com/contoso/tools-internal-docs\n",
            }
            for name, content in samples.items():
                (root / name).write_text(content, encoding="utf-8")
            self.assertEqual(
                [
                    f"forward.md:1: see {forward}",
                    f"internal.md:1: https://{internal}",
                    f"synced.md:1: C:/Data/{synced}/notes",
                    f"user.md:1: Install to {user}",
                ],
                private_references(root, [root / name for name in samples]),
            )

    def test_analyze_skill_cost_carries_the_shared_frontmatter_reader_unchanged(self) -> None:
        # A deployed skill cannot import the deployer, so analyze-skill-cost ships its own copy of the reader.
        shared = (REPOSITORY_ROOT / "deployer" / "frontmatter.py").read_bytes()
        copy = (SKILLS_ROOT / "analyze-skill-cost" / "scripts" / "frontmatter.py").read_bytes()
        self.assertEqual(shared, copy, "Copy deployer/frontmatter.py to skills/analyze-skill-cost/scripts/ unchanged.")

    def test_python_suites_run_their_tests_when_executed(self) -> None:
        # Every suite runs as `python <file>`, so one without a __main__ entry point defines its tests, runs none,
        # and still exits 0.
        suites = [
            *(REPOSITORY_ROOT / "tests").rglob("test_*.py"),
            *(path for path in SKILLS_ROOT.rglob("*.py") if is_test_script(path)),
        ]
        self.assertTrue(suites)
        missing = [
            relative(path) for path in sorted(suites)
            if 'if __name__ == "__main__":' not in path.read_text(encoding="utf-8")
        ]
        self.assertEqual([], missing)

    def test_repository_skills_have_matching_shims(self) -> None:
        self.assertEqual([], repository_skill_problems(REPOSITORY_ROOT))

    def test_repository_skill_shim_check_detects_each_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def write(relative: str, text: str) -> None:
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_text(text, encoding="utf-8")

            pointer = "Read and follow `../../../.claude/skills/{}/SKILL.md`.\n"
            write(".claude/skills/good/SKILL.md", "---\nname: good\ndescription: Good.\n---\n\nBody.\n")
            write(".agents/skills/good/SKILL.md", "---\nname: good\ndescription: Good.\n---\n\n" + pointer.format("good"))
            write(".claude/skills/missing/SKILL.md", "---\nname: missing\ndescription: Missing.\n---\n")
            write(".claude/skills/drifted/SKILL.md", "---\nname: drifted\ndescription: New.\n---\n")
            write(".agents/skills/drifted/SKILL.md", "---\nname: drifted\ndescription: Old.\n---\n\n" + pointer.format("drifted"))
            write(".claude/skills/astray/SKILL.md", "---\nname: astray\ndescription: Astray.\n---\n")
            write(".agents/skills/astray/SKILL.md", "---\nname: astray\ndescription: Astray.\n---\n\n" + pointer.format("other"))
            write(".agents/skills/stray/SKILL.md", "---\nname: stray\ndescription: Stray.\n---\n")
            self.assertEqual(
                [
                    ".agents/skills/stray/SKILL.md has no .claude/skills/stray/SKILL.md",
                    ".agents/skills/astray/SKILL.md does not point to ../../../.claude/skills/astray/SKILL.md",
                    ".agents/skills/drifted/SKILL.md frontmatter differs from .claude/skills/drifted/SKILL.md",
                    ".claude/skills/missing/SKILL.md has no .agents/skills/missing/SKILL.md shim",
                ],
                repository_skill_problems(root),
            )

    def test_fixture_sources_never_ship_or_collide_with_what_ships(self) -> None:
        self.assertTrue(list((REPOSITORY_ROOT / "tests" / "fixtures").glob("*/source.json")))
        self.assertEqual([], fixture_source_problems(REPOSITORY_ROOT))

    def test_fixture_source_policy_detects_each_problem(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def write(relative_path: str, text: str) -> None:
                (root / relative_path).parent.mkdir(parents=True, exist_ok=True)
                (root / relative_path).write_text(text, encoding="utf-8")

            write("source.json", json.dumps({"id": "owner/shipped", "bundles": {"suite": {"members": ["alpha"]}}}))
            write("deploy-meta/alpha.json", "{}")
            write("skills/alpha/SKILL.md", "# alpha\n")
            write("skills/alpha/notes.md", "See tests/fixtures/probe for a sample.\n")
            write("tests/fixtures/same/source.json", json.dumps({"id": "owner/shipped"}))
            write("tests/fixtures/clash/source.json", json.dumps({"id": "test/clash"}))
            write("tests/fixtures/clash/deploy-meta/alpha.json", "{}")
            write("tests/fixtures/clash/deploy-meta/suite.json", "{}")
            write("tests/fixtures/clash/skills/alpha/SKILL.md", "# alpha\n")
            write("tests/fixtures/clash/skills/suite/SKILL.md", "```bash\npython -B scripts/run.py\n```\n")
            self.assertEqual(
                [
                    "tests/fixtures/clash skill alpha shares its name with a shipped skill or bundle",
                    "tests/fixtures/clash skill suite shares its name with a shipped skill or bundle",
                    "tests/fixtures/clash: skills/suite/SKILL.md:2 runs a script by a bare relative path; see "
                    "\"Paths to a skill's own files\" in docs/adding-a-skill.md",
                    "tests/fixtures/same/source.json reuses the shipped source ID",
                    "skills/alpha/notes.md names tests/fixtures, which never ships",
                ],
                fixture_source_problems(root),
            )

    def test_skills_run_only_standard_or_declared_commands(self) -> None:
        from deployer import tools

        self.assertEqual(
            [], skill_command_problems(REPOSITORY_ROOT, set(tools.SKILL_TOOLS), set(tools.STANDARD_COMMANDS))
        )

    def test_skills_grant_only_scoped_twin_shell_patterns_that_cover_their_own_scripts(self) -> None:
        # allowed-tools pre-approves; a bare Bash would pre-approve every command in the turn that starts the skill.
        self.assertEqual([], skill_grant_problems(REPOSITORY_ROOT))

    def test_skill_grant_policy_detects_unscoped_unpaired_unused_and_ungranted_grants(self) -> None:
        own = 'python -B "${CLAUDE_SKILL_DIR}/scripts/'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "deploy-meta").mkdir()
            skills = {
                "bare": ('["Bash"]', f'{own}x.py"'),
                "unpaired": (json.dumps([f"Bash({own}*)"]), f'{own}x.py"'),
                "ungranted": (json.dumps([f"Bash({own}*)", f"PowerShell({own}*)"]),
                              f'python -B "${{CLAUDE_SKILL_DIR}}/../core/scripts/y.py"\ngit status'),
                "none": ('["Read"]', f'{own}x.py"'),
                "unused": (json.dumps([f"Bash({own}*)", f"PowerShell({own}*)", "Glob"]), f'{own}x.py"'),
                "expands": (json.dumps([f"Bash({own}*)", f"PowerShell({own}*)"]), f'{own}x.py" --cwd "$PWD"'),
                "good": (json.dumps([f"Bash({own}*)", f"PowerShell({own}*)"]), f'{own}x.py" --plan "<plan file>"'),
            }
            for skill, (allowed, command) in skills.items():
                (root / "deploy-meta" / f"{skill}.json").write_text("{}", encoding="utf-8")
                directory = root / "skills" / skill
                directory.mkdir(parents=True)
                (directory / "SKILL.md").write_text(
                    f"---\nname: {skill}\nallowed-tools: {allowed}\n---\n\n```bash\n{command}\n```\n", encoding="utf-8"
                )
            (root / "skills" / "asset").mkdir()
            self.assertEqual(
                [
                    f"skills/bare/SKILL.md grants Bash for every command; see {GRANTS_DOC}",
                    f'skills/expands/SKILL.md:7 expands a shell variable, so it always prompts: {own}x.py" --cwd "$PWD"; '
                    f"see {GRANTS_DOC}",
                    f"skills/none/SKILL.md grants Read, which no step uses; see {GRANTS_DOC}",
                    f"skills/none/SKILL.md:6 runs a command without a shell grant; see {GRANTS_DOC}",
                    f"skills/ungranted/SKILL.md:7 no Bash grant covers "
                    f'python -B "${{CLAUDE_SKILL_DIR}}/../core/scripts/y.py"; see {GRANTS_DOC}',
                    f"skills/ungranted/SKILL.md:7 no PowerShell grant covers "
                    f'python -B "${{CLAUDE_SKILL_DIR}}/../core/scripts/y.py"; see {GRANTS_DOC}',
                    f"skills/unpaired/SKILL.md grants Bash({own}*) in one shell only; see {GRANTS_DOC}",
                    f"skills/unused/SKILL.md grants Glob, which no step uses; see {GRANTS_DOC}",
                ],
                skill_grant_problems(root),
            )

    def test_skills_reach_their_own_and_sibling_files_through_the_skill_directory(self) -> None:
        self.assertEqual([], skill_path_problems(REPOSITORY_ROOT))

    def test_skill_path_policy_detects_install_paths_bare_scripts_and_undeclared_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "deploy-meta").mkdir()
            (root / "agents").mkdir()
            for skill, metadata in {"alpha": {"skill_deps": ["core"]}, "beta": {}, "core": {}}.items():
                (root / "deploy-meta" / f"{skill}.json").write_text(json.dumps(metadata), encoding="utf-8")
                (root / "skills" / skill).mkdir(parents=True)
                (root / "skills" / skill / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
            (root / "skills" / "alpha" / "SKILL.md").write_text(
                "Searches `{{HOME}}/.claude/skills/<SKILL_NAME>/`.\n"
                "```bash\n"
                'python -B "${CLAUDE_SKILL_DIR}/scripts/run.py" --in "data/scripts/x"\n'
                'python -B "${CLAUDE_SKILL_DIR}/../core/scripts/lib.py"\n'
                "```\n"
                "Prose may name `scripts/run.py`.\n",
                encoding="utf-8",
            )
            (root / "skills" / "beta" / "SKILL.md").write_text(
                "Read `{{HOME}}/.claude/skills/core/notes.md`.\n"
                "```bash\n"
                "python -B scripts/run.py\n"
                'TOOL="../core/scripts/lib.py"\n'
                'python -B "${CLAUDE_SKILL_DIR}/../core/scripts/lib.py"\n'
                "```\n",
                encoding="utf-8",
            )
            (root / "agents" / "helper.md").write_text(
                "Run `{{HOME}}/.claude/skills/beta/x.py`.\n"
                "          command: 'python -B \"$HOME/x.py\"'\n"
                "          command: 'python -B \"${HOME}/x.py\"'\n"
                "          command: 'python -B \"$env:HOME/x.py\"'\n"
                "          command: python -I -B -c \"import os; os.path.expanduser('~/x.py')\"\n"
                "Mentions $HOMEPAGE and $HOME_DIR.\n",
                encoding="utf-8",
            )
            doc = "\"Paths to a skill's own files\" in docs/adding-a-skill.md"
            agents_doc = "\"Subagent definitions\" in docs/adding-a-skill.md"
            self.assertEqual(
                [
                    f"agents/helper.md:1 names skill beta by its install path; see {doc}",
                    f"agents/helper.md:2 finds a file through $HOME; see {agents_doc}",
                    f"agents/helper.md:3 finds a file through $HOME; see {agents_doc}",
                    f"agents/helper.md:4 finds a file through $HOME; see {agents_doc}",
                    f"skills/beta/SKILL.md:1 names skill core by its install path; see {doc}",
                    f"skills/beta/SKILL.md:3 runs a script by a bare relative path; see {doc}",
                    f"skills/beta/SKILL.md:4 runs a script by a bare relative path; see {doc}",
                    "skills/beta/SKILL.md:5 reaches ../core without declaring it in skill_deps",
                ],
                skill_path_problems(root),
            )

    def test_shell_command_extraction_ignores_keywords_patterns_and_functions(self) -> None:
        script = (
            'set -euo pipefail\n'
            'BASE=$(git rev-parse HEAD | tr -d "\\r") && echo "$BASE" >/dev/null\n'
            'case "$1" in\n'
            '  main|release/*) grep -q "x|jq" file ;;\n'
            '  *) helper "rg" ;;\n'
            'esac\n'
            'for name in alpha beta; do basename "$name"; done\n'
            'helper() { if [ -n "$1" ]; then sed -n 1p "$1"; fi; }\n'
            'while read -r line; do printf "%s\\n" "$line" | jq .; done < list\n'
            '"$SCRIPT" --flag; ./local.sh\n'
        )
        self.assertEqual(
            {"basename", "echo", "git", "grep", "jq", "printf", "read", "sed", "set", "tr"}, shell_commands(script)
        )

    def test_skill_scripts_parse_gh_json_instead_of_filtering_it(self) -> None:
        self.assertEqual([], gh_filter_problems(REPOSITORY_ROOT))

    def test_gh_filter_policy_detects_jq_template_and_json_query_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "skills" / "alpha" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "tool.py").write_text(
                'run(["gh", "api", "repos/o/r/pulls", "--jq", ".[].number"])\n'
                'run(["pr", "view", "--json", "baseRefName", "-q", ".baseRefName"])\n'
                'run(["gh", "api", "x", "--template", "{{.}}"])\n'
                'run(["git", "fetch", "-q"])\n'
                'run(["gh", "pr", "view", "--json", "baseRefName"])\n',
                encoding="utf-8",
            )
            (scripts / "tool.sh").write_text(
                "gh pr view --json baseRefName -q .baseRefName\n"
                "BASE=$(gh api repos/o/r --jq=.default_branch)\n"
                "git fetch -q origin\n"
                "gh api repos/o/r --paginate --slurp\n",
                encoding="utf-8",
            )
            (scripts / "tool.ps1").write_text("gh api repos/o/r --template '{{.name}}'\n", encoding="utf-8")
            (scripts / "test_tool.py").write_text('self.assertNotIn("--jq", calls)\n', encoding="utf-8")
            doc = "\"Commands skills may run\" in docs/adding-a-skill.md"
            self.assertEqual(
                [
                    f"skills/alpha/scripts/tool.ps1:1 filters gh output with --template; parse its JSON in Python "
                    f"instead; see {doc}",
                    f"skills/alpha/scripts/tool.py:1 filters gh output with --jq; parse its JSON in Python instead; "
                    f"see {doc}",
                    f"skills/alpha/scripts/tool.py:2 filters gh output with -q; parse its JSON in Python instead; "
                    f"see {doc}",
                    f"skills/alpha/scripts/tool.py:3 filters gh output with --template; parse its JSON in Python "
                    f"instead; see {doc}",
                    f"skills/alpha/scripts/tool.sh:1 filters gh output with -q; parse its JSON in Python instead; "
                    f"see {doc}",
                    f"skills/alpha/scripts/tool.sh:2 filters gh output with --jq; parse its JSON in Python instead; "
                    f"see {doc}",
                ],
                gh_filter_problems(root),
            )

    def test_skill_command_policy_detects_undeclared_unused_and_nonstandard_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "deploy-meta").mkdir()
            for skill, metadata in {
                "alpha": {"tools": ["copilot"]},
                "beta": {},
                "gamma": {"tools": ["gh"]},
            }.items():
                (root / "deploy-meta" / f"{skill}.json").write_text(json.dumps(metadata), encoding="utf-8")
                (root / "skills" / skill / "scripts").mkdir(parents=True)
            (root / "skills" / "alpha" / "SKILL.md").write_text("Prose that mentions `gh` only.\n", encoding="utf-8")
            (root / "skills" / "alpha" / "scripts" / "run.py").write_text('run(["gh", "pr", "list"])\n', encoding="utf-8")
            (root / "skills" / "alpha" / "scripts" / "test_run.py").write_text('which("copilot")\n', encoding="utf-8")
            (root / "skills" / "beta" / "SKILL.md").write_text(
                "```bash\ngit status && dotnet-format --version\n```\n", encoding="utf-8"
            )
            (root / "skills" / "gamma" / "SKILL.md").write_text("Gamma\n", encoding="utf-8")
            (root / "skills" / "gamma" / "scripts" / "run.sh").write_text(
                "  gh pr view | jq .title\nrg -n TODO .\n", encoding="utf-8"
            )
            (root / "skills" / "gamma" / "scripts" / "fetch.py").write_text(
                'subprocess.run(["yq", "."])\nshutil.which("curl")\n', encoding="utf-8"
            )
            doc = '"Commands skills may run" in docs/adding-a-skill.md'
            self.assertEqual(
                [
                    "skill alpha runs gh without declaring it in tools",
                    "skill alpha declares tool copilot but never runs it",
                    "skill beta runs dotnet-format without declaring it in tools",
                    f"skill gamma runs jq, which a standard install lacks; see {doc}",
                    f"skill gamma runs rg, which a standard install lacks; see {doc}",
                    f"skill gamma runs yq, which a standard install lacks; see {doc}",
                ],
                skill_command_problems(root, {"copilot", "dotnet-format", "gh"}, {"curl", "git"}),
            )

    def test_deploy_variable_policy_detects_unused_and_undeclared_variables(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "deploy-meta").mkdir()
            (root / "deploy-meta" / "alpha.json").write_text(
                json.dumps({"required_vars": ["REPOS_ROOT", "HOME"]}), encoding="utf-8"
            )
            (root / "skills" / "alpha").mkdir(parents=True)
            (root / "skills" / "alpha" / "SKILL.md").write_text(
                "{{REPOS_ROOT}} {{HOME_URI}}\n", encoding="utf-8"
            )
            (root / "skills" / "shared.md").write_text("{{REPOS_ROOT}}\n", encoding="utf-8")
            (root / "source.json").write_text(
                json.dumps({"shared_assets": {"shared.md": "owner"}}), encoding="utf-8"
            )
            self.assertEqual(
                [
                    "configure never prompts for configured variable SPARE",
                    "configure prompts for unknown variable EXTRA",
                    "skill alpha uses {{HOME_URI}} without declaring it in required_vars",
                    "skill alpha declares required variable HOME but never uses it",
                    "configured variable SPARE is not required by any skill",
                    "shared asset shared.md uses non-derived variable REPOS_ROOT",
                ],
                deploy_variable_problems(
                    root, {"REPOS_ROOT", "SPARE"}, {"REPOS_ROOT", "EXTRA"}, {"HOME", "HOME_URI"}
                ),
            )

    def test_prerequisite_check_reports_every_missing_tool(self) -> None:
        def raises() -> str:
            raise AssertionError("not found")

        self.assertEqual(
            [
                "  - Git Bash: winget install --id Git.Git",
                "  - PowerShell 7 (pwsh): winget install --id Microsoft.PowerShell",
            ],
            missing_prerequisites(
                (
                    ("Git Bash", "Git Bash", raises),
                    ("ShellCheck", "ShellCheck", lambda: "C:/tools/shellcheck.exe"),
                    ("PowerShell 7 (pwsh)", "PowerShell", lambda: None),
                )
            ),
        )
        self.assertEqual([], missing_prerequisites((("ShellCheck", "ShellCheck", lambda: "shellcheck"),)))

    def test_validation_floors_are_the_documented_versions(self) -> None:
        self.assertEqual(
            {"Python": (3, 11), "ShellCheck": (0, 9, 0), "PowerShell 7 (pwsh)": (7, 0)}, VALIDATION_FLOORS
        )
        document = (REPOSITORY_ROOT / DEPENDENCY_DOC).read_text(encoding="utf-8")
        for row in ("| Python | 3.11 |", "| ShellCheck | 0.9.0 |", "| PowerShell 7 (`pwsh`) | 7.0 |"):
            with self.subTest(row=row):
                self.assertIn(row, document)

    def test_ci_exercises_each_floor(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
        self.assertIn("choco install shellcheck --version 0.9.0 ", workflow)
        self.assertIn("python-version: ['3.11', '3.x']", workflow)
        # The aggregate job keeps the single required status check context that branch protection names.
        self.assertRegex(workflow, r"(?m)^  validate:\n(?:    .*\n)*?    needs: suite$")

    def test_prerequisite_check_reports_every_tool_older_than_its_floor(self) -> None:
        self.assertEqual(
            [
                "  - Python 3.10.12 is older than the floor 3.11 in docs/dependency-updates.md",
                "  - ShellCheck 0.8.0 is older than the floor 0.9.0 in docs/dependency-updates.md: "
                "winget install --id koalaman.shellcheck",
                "  - PowerShell 7 (pwsh): its version could not be read; the floor is 7.0 in "
                "docs/dependency-updates.md: winget install --id Microsoft.PowerShell",
            ],
            outdated_prerequisites(
                {"Git Bash": None, "ShellCheck": (0, 8, 0), "PowerShell 7 (pwsh)": None}, (3, 10, 12)
            ),
        )
        self.assertEqual(
            [],
            outdated_prerequisites(
                {"Git Bash": None, "ShellCheck": (0, 9, 0), "PowerShell 7 (pwsh)": (7, 5, 3)}, (3, 11, 0)
            ),
        )
        # A missing tool is reported by missing_prerequisites, not again here.
        self.assertEqual([], outdated_prerequisites({}, (3, 14, 7)))

    def test_tool_versions_reads_each_found_tool(self) -> None:
        def raises() -> str:
            raise AssertionError("not found")

        read = {"C:/tools/shellcheck.exe": (0, 11, 0), "C:/tools/pwsh.exe": None}
        self.assertEqual(
            {"ShellCheck": (0, 11, 0), "PowerShell 7 (pwsh)": None},
            tool_versions(
                (
                    ("Git Bash", "Git Bash", raises),
                    ("ShellCheck", "ShellCheck", lambda: "C:/tools/shellcheck.exe"),
                    ("PowerShell 7 (pwsh)", "PowerShell", lambda: "C:/tools/pwsh.exe"),
                ),
                read.__getitem__,
            ),
        )

    def test_step_summary_reports_the_result_and_each_failure(self) -> None:
        text = step_summary(
            (3, 11, 9),
            {"Git Bash": (5, 2, 37), "ShellCheck": (0, 9, 0), "PowerShell 7 (pwsh)": None},
            "Full validation: no changed files were found.",
            140,
            52,
            61.4,
            ["policy test_one", "suite tests/x.py"],
        )
        self.assertEqual(
            "## Repository validation\n\n"
            "- Python 3.11.9, Git Bash 5.2.37, ShellCheck 0.9.0, PowerShell 7 (pwsh) unknown\n"
            "- Full validation: no changed files were found.\n"
            "- 140 policy checks and 52 suite jobs in 61s: **validation FAILED**\n"
            "- Failed: `policy test_one`\n"
            "- Failed: `suite tests/x.py`\n",
            text,
        )
        self.assertIn("**validation passed**", step_summary((3, 14, 7), {}, "Mode.", 1, 1, 1.0, []))

    def test_step_summary_is_appended_only_when_github_names_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "summary with spaces.md"
            path.write_text("earlier\n", encoding="utf-8")
            append_step_summary({"GITHUB_STEP_SUMMARY": str(path)}, "## Repository validation\n")
            self.assertEqual("earlier\n## Repository validation\n", path.read_text(encoding="utf-8"))
            append_step_summary({}, "ignored\n")
            self.assertEqual(["summary with spaces.md"], [child.name for child in Path(temporary).iterdir()])

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the repository's policy checks and regression suites.")
    parser.add_argument("-k", dest="patterns", action="append", default=[], metavar="PATTERN",
                        help="run only the policy checks whose name, and the suites whose path, match; repeatable")
    parser.add_argument("-v", "--verbose", action="store_true", help="list each policy check and suite as it finishes")
    parser.add_argument("--full", action="store_true", help="run every suite even when only documentation changed")
    arguments = parser.parse_args(argv)
    versions = tool_versions()
    if report_prerequisite_problems(versions):
        return 2
    started = time.perf_counter()
    patterns = name_patterns(arguments.patterns)
    loader = unittest.TestLoader()
    if patterns:
        loader.testNamePatterns = patterns
    policies = loader.loadTestsFromTestCase(RepositoryValidation)
    if patterns:
        jobs = [job for job in all_jobs() if any(fnmatch.fnmatchcase(job.name, pattern) for pattern in patterns)]
        mode = f"Selected by -k {' '.join(arguments.patterns)}."
    else:
        base = f"origin/{os.environ.get('GITHUB_BASE_REF') or 'main'}"
        paths = None if arguments.full else changed_paths(REPOSITORY_ROOT, base)
        only, reason = (False, "--full was given") if arguments.full else documentation_only(paths)
        if only:
            suites = suites_naming(paths, regression_suites())
            jobs = suite_jobs(suites)
            mode = (f"Documentation only: {reason} since {base}. Running the policy checks and the "
                    f"{len(suites)} suites that name a changed file; pass --full to run every suite.")
        else:
            jobs = all_jobs()
            mode = f"Full validation: {reason}."
        print(mode, flush=True)
    if not policies.countTestCases() and not jobs:
        print(f"No policy check or suite matches {' '.join(arguments.patterns)}.", file=sys.stderr)
        return 2

    policy_result = unittest.TextTestRunner(verbosity=2 if arguments.verbose else 1).run(policies)
    workers = worker_count()
    print(f"Running {len(jobs)} suite jobs on {workers} workers.", flush=True)
    failures = run_jobs(jobs, arguments.verbose, workers)
    for job, error in failures:
        print(f"\n{'=' * 70}\nFAILED {job.label}\n{'-' * 70}\n{error}", flush=True)
    passed = policy_result.wasSuccessful() and not failures
    seconds = time.perf_counter() - started
    print(
        f"\n{policy_result.testsRun} policy checks and {len(jobs)} suite jobs in {seconds:.0f}s: "
        + ("validation passed." if passed else f"validation FAILED ({len(failures)} suite jobs failed).")
    )
    failed = [f"policy {test.id().rsplit('.', 1)[-1]}" for test, _ in policy_result.failures + policy_result.errors]
    failed += [job.label for job, _ in failures]
    append_step_summary(
        os.environ,
        step_summary(tools.python_version(), versions, mode, policy_result.testsRun, len(jobs), seconds, failed),
    )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
