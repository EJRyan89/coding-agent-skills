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
from deployer import render
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
# A regression suite is found by its name alone, matched against the file's stem or its whole name, ignoring case.
TEST_NAME_PATTERNS = ("test_*", "test-*", "*_test", "*-test", "*.test.*")
# Every suite runs as `python <file>`, so a Python suite without this entry point runs no tests and still exits 0.
PYTHON_ENTRY_POINT = 'if __name__ == "__main__":'
SKILL_GUIDE = "docs/adding-a-skill.md"
# Every Python file under these is checked with `ruff format --check` and `ruff check`; none is excluded.
FORMAT_ROOTS = ("deployer", "tools", "tests", "skills", "deploy.py")
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
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CLAUDE.md",
    "AGENTS.md",
    ".github/pull_request_template.md",
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
    used_anywhere: set[str] = set()
    for metadata in sorted((root / "deploy-meta").glob("*.json")):
        skill = metadata.stem
        required = set(json.loads(metadata.read_text(encoding="utf-8")).get("required_vars", []))
        required_anywhere |= required
        directories = [root / "skills" / skill, *sorted((root / "skills").glob(f"*/{skill}"))]
        used = set().union(*(_template_tokens(path) for path in directories if (path / "SKILL.md").is_file()))
        used_anywhere |= used
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
        tokens = set(TEMPLATE_TOKEN.findall(text))
        used_anywhere |= tokens
        for key in sorted(tokens - derived):
            problems.append(f"shared asset {asset} uses non-derived variable {key}")
    # A derived value no template uses still has to pass the allowlist, so it could refuse a deployment for nothing.
    for key in sorted(derived - used_anywhere):
        problems.append(f"derived variable {key} is not used by any skill or shared asset")
    return problems


SHELL_FENCES = {"bash", "sh", "shell"}
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
    run: a dependency's tools are not the skill's unless it imports or names the script that runs them.
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
        reached = {
            command
            for path in _reached_dependency_scripts(root, skill, own)
            for command in _file_commands(path, known_call)
        }
        for name in sorted((used & known) - declared):
            problems.append(f"skill {skill} runs {name} without declaring it in tools")
        for name in sorted(declared - used - reached):
            problems.append(f"skill {skill} declares tool {name} but never runs it")
        for name in sorted(used - known - standard - SHELL_BUILTINS):
            problems.append(f"skill {skill} runs {name}, which a standard install lacks; see {COMMANDS_DOC}")
    return problems


METADATA_DOC = '"Metadata" in docs/adding-a-skill.md'


def metadata_format_problems(root: Path) -> list[str]:
    """Report deploy-meta files not laid out as tools/new_skill.py writes them, so every file reads the same way."""
    from tools import new_skill

    problems: list[str] = []
    for metadata in sorted((root / "deploy-meta").glob("*.json")):
        text = metadata.read_bytes().decode("utf-8")
        if text != new_skill.metadata_text(json.loads(text)):
            problems.append(f"deploy-meta/{metadata.name} is not in the canonical metadata format; see {METADATA_DOC}")
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


GRANTS_DOC = '"Granting tools" in docs/adding-a-skill.md'


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
                problems.append(
                    f"{name}:{number} expands a shell variable, so it always prompts: {command}; see {GRANTS_DOC}"
                )
    return problems


INSTALL_PATH = re.compile(r"\{\{HOME\}\}/\.claude/skills/([A-Za-z0-9._-]+)")
SIBLING_PATH = re.compile(r"\$\{CLAUDE_SKILL_DIR\}/\.\./([A-Za-z0-9._-]+)")
# A concrete file a skill names through its directory; a pattern such as scripts/* in a grant names none.
SKILL_DIR_FILE = re.compile(r"\$\{CLAUDE_SKILL_DIR\}/([A-Za-z0-9._/-]+)(?![A-Za-z0-9._/*<-])")
BARE_SCRIPT_PATH = re.compile(r"""(?:^|[\s"'=])(?:\./|\.\./[A-Za-z0-9._-]+/)?scripts/""")
# In SKILL.md prose, a code span that names the skill's own scripts/ or references/ by a relative path. Claude Code
# resolves it against the working directory, and Codex and Copilot against the adapter, which has neither folder.
PROSE_CODE_SPAN = re.compile(r"`([^`\n]+)`")
BARE_OWN_PATH = re.compile(r"""(?:^|[\s"'=(])(?:\./|\.\./[A-Za-z0-9._-]+/)?(?:scripts|references)/""")
# analyze-skill-cost recommends a command under `scripts/` in the skill it audits, which is not a path of its own.
BARE_OWN_PATH_EXEMPT = frozenset({("skills/analyze-skill-cost/SKILL.md", "scripts/")})
SKILL_PATHS_DOC = '"Paths to a skill\'s own files" in docs/adding-a-skill.md'
# Git Bash takes $HOME from HOME, which need not be the profile folder the deployer installs into.
HOME_VARIABLE = re.compile(r"\$(?:HOME\b|\{HOME\}|env:HOME\b)", re.IGNORECASE)
AGENTS_DOC = '"Subagent definitions" in docs/adding-a-skill.md'


def skill_path_problems(root: Path) -> list[str]:
    """Report skills and agents that reach a skill's files other than through ${CLAUDE_SKILL_DIR}."""
    skills = {path.parent.name for path in (root / "skills").glob("**/SKILL.md")}
    documents: list[tuple[Path, set[str] | None, Path | None]] = [
        (path, None, None) for path in sorted((root / "agents").glob("*.md"))
    ]
    for metadata in sorted((root / "deploy-meta").glob("*.json")):
        skill = metadata.stem
        dependencies = set(json.loads(metadata.read_text(encoding="utf-8")).get("skill_deps", []))
        for directory in [root / "skills" / skill, *sorted((root / "skills").glob(f"*/{skill}"))]:
            if (directory / "SKILL.md").is_file():
                documents += [(path, dependencies, directory) for path in sorted(directory.rglob("*.md"))]
    problems: list[str] = []
    for path, dependencies, directory in documents:
        name = path.relative_to(root).as_posix()
        in_fence = in_shell_fence = False
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_shell_fence = not in_fence and stripped[3:].strip().casefold() in SHELL_FENCES
                in_fence = not in_fence
                continue
            if path.name == "SKILL.md" and directory is not None and not in_fence:
                for span in PROSE_CODE_SPAN.findall(line):
                    if BARE_OWN_PATH.search(span) and (name, span) not in BARE_OWN_PATH_EXEMPT:
                        problems.append(
                            f"{name}:{number} names `{span}` by a bare relative path; see {SKILL_PATHS_DOC}"
                        )
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
            for match in SKILL_DIR_FILE.finditer(line) if directory is not None else ():
                named = match.group(1).rstrip(".")  # a path may end a sentence
                if not (directory / named).exists():
                    problems.append(f"{name}:{number} names ${{CLAUDE_SKILL_DIR}}/{named}, which does not exist")
    return problems


# An option that names where a script writes, given a placeholder for the agent to fill in: "--output <file>",
# "--plans=<dir>". Placeholders for paths a command printed, such as "--run <run directory>", name no output option.
OUTPUT_PLACEHOLDER = re.compile(r"""(--(?:output(?:-[a-z]+)*|out(?:-dir)?|plans))(?:\s+|=)["']?<[^>]*>""")
WORKING_FILES_DOC = '"Working files" in docs/adding-a-skill.md'


def output_placeholder_problems(root: Path) -> list[str]:
    """Report command fences in shipped Markdown that leave the agent to choose where a script writes.

    Given a placeholder and a skill directory it already knows, an agent writes beside SKILL.md, and the deployer
    then sees the installed skill as modified and stops updating it.
    """
    problems: list[str] = []
    for path in sorted((root / "skills").rglob("*.md"), key=lambda path: path.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        in_command_fence = False
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_command_fence = (
                    not in_command_fence and stripped[3:].strip().casefold() in EXECUTABLE_FENCE_LANGUAGES
                )
                continue
            if in_command_fence:
                for match in OUTPUT_PLACEHOLDER.finditer(line):
                    problems.append(
                        f"{name}:{number} leaves {match.group(1)} to the agent; let the script choose "
                        f"and print the path; see {WORKING_FILES_DOC}"
                    )
    return problems


# Defense in depth only: the private-name scan in docs/releasing.md runs before every release. YourName is the
# placeholder user documentation may show; the drive-sync folder name is split so this file does not match itself.
PRIVATE_REFERENCE = re.compile(
    "|".join(
        (
            r"C:[/\\]Users[/\\](?!YourName(?:[/\\]|$))",
            "One" + r"Drive - [^/\\\r\n]+",
            r"github\.com[/\\][A-Za-z0-9_.-]+-internal(?:[/\\]|$)",
        )
    ),
    re.IGNORECASE,
)


def repository_files(root: Path) -> list[Path]:
    """Tracked files plus untracked ones Git does not ignore, which is what a commit could include."""
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
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
        group
        for group in groups
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
    found = [
        f".agents/skills/{name}/SKILL.md has no .claude/skills/{name}/SKILL.md"
        for name in sorted(set(shims) - set(skills))
    ]
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
    shipped_files = [
        root / "source.json",
        *(
            path
            for folder in ("skills", "deploy-meta", "agents")
            for path in (root / folder).rglob("*")
            if path.is_file()
        ),
    ]
    for path in shipped_files:
        if "tests/fixtures" in path.read_text(encoding="utf-8", errors="replace").replace("\\", "/"):
            found.append(f"{path.relative_to(root).as_posix()} names tests/fixtures, which never ships")
    return found


# What only deployer/platform_support.py may name, so that supporting another operating system changes one module.
PLATFORM_TOKENS: dict[str, tuple[str, ...]] = {
    "qualified": ("sys.platform", "os.name", "os.startfile"),
    "attribute": ("chmod", "fchmod", "lchmod", "st_file_attributes"),
    "keyword": ("creationflags",),
    "module": ("ctypes", "winreg", "msvcrt", "_winapi"),
    "variable": ("LOCALAPPDATA", "USERPROFILE", "ProgramFiles", "APPDATA"),
    "command": ("cygpath",),
}
# A module sanctions a use beside the code it excuses, as a module-level PLATFORM_ALLOWED = {token: reason}. An
# allowance the module no longer needs is reported.
PLATFORM_ALLOWANCE = "PLATFORM_ALLOWED"
# The module that defines PLATFORM_TOKENS, which names every token on purpose.
PLATFORM_POLICY_MODULE = "tests/run_validation.py"


def _platform_scanned_files(root: Path) -> list[Path]:
    """The code that runs on a user's machine or runs validation, other than platform_support itself."""
    files = [root / "deploy.py", root / "tests" / "run_validation.py", root / "tests" / "run_shard.py"]
    files += [*(root / "deployer").rglob("*.py"), *(root / "tools").rglob("*.py")]
    return [
        path for path in files if path.is_file() and path.relative_to(root).as_posix() != "deployer/platform_support.py"
    ]


def _platform_names(tree: ast.Module, skipped_tables: set[str]) -> list[tuple[int, str]]:
    """The platform tokens a module's code names, as (line, token), ignoring comments, docstrings, and test cases."""
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    command = re.compile(r"\b(?:" + "|".join(map(re.escape, PLATFORM_TOKENS["command"])) + r")\b")
    found: list[tuple[int, str]] = []

    def module_token(line: int, module: str) -> bool:
        top = module.split(".")[0]
        if top in PLATFORM_TOKENS["module"]:
            found.append((line, top))
            return True
        return False

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.ClassDef) and any(
            (base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", None)) == "TestCase"
            for base in node.bases
        ):
            return  # A test names what it tests, and its fixtures name tokens on purpose.
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id in skipped_tables for target in targets):
                return
        if isinstance(node, ast.Attribute):
            qualified = f"{node.value.id}.{node.attr}" if isinstance(node.value, ast.Name) else ""
            if qualified in PLATFORM_TOKENS["qualified"]:
                found.append((node.lineno, qualified))
            elif node.attr in PLATFORM_TOKENS["attribute"]:
                found.append((node.lineno, node.attr))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                module_token(node.lineno, alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if not module_token(node.lineno, module):
                for alias in node.names:
                    qualified = f"{module}.{alias.name}"
                    if qualified in PLATFORM_TOKENS["qualified"]:
                        found.append((node.lineno, qualified))
                    elif alias.name in PLATFORM_TOKENS["attribute"]:
                        found.append((node.lineno, alias.name))
        elif isinstance(node, ast.keyword) and node.arg in PLATFORM_TOKENS["keyword"]:
            found.append((node.lineno, node.arg))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            # Equality catches an environment lookup or entry, never prose that mentions the variable.
            if node.value in PLATFORM_TOKENS["variable"]:
                found.append((node.lineno, node.value))
            found.extend((node.lineno, name) for name in sorted(set(command.findall(node.value))))
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


def _platform_allowance(tree: ast.Module, name: str = PLATFORM_ALLOWANCE) -> dict[str, str] | None:
    """A module's allowance, such as PLATFORM_ALLOWED, or None when it does not map each token to its reason."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            try:
                allowance = ast.literal_eval(node.value)
            except ValueError:
                return None
            valid = isinstance(allowance, dict) and all(
                isinstance(token, str) and isinstance(reason, str) and reason.strip()
                for token, reason in allowance.items()
            )
            return allowance if valid else None
    return {}


def platform_code_problems(root: Path) -> list[str]:
    """Report operating-system-specific code outside deployer/platform_support.py, and allowances no longer needed.

    CLAUDE.md keeps every operating-system-specific behavior in that one module, so a port changes only it.
    """
    found: set[tuple[str, int, str]] = set()
    problems: list[str] = []
    for path in sorted(_platform_scanned_files(root), key=lambda path: path.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        allowed = _platform_allowance(tree)
        if allowed is None:
            problems.append(f"{name}: {PLATFORM_ALLOWANCE} must map each token to the reason it is allowed")
            allowed = {}
        tables = {PLATFORM_ALLOWANCE, "PLATFORM_TOKENS"} if name == PLATFORM_POLICY_MODULE else {PLATFORM_ALLOWANCE}
        names = _platform_names(tree, tables)
        found |= {(name, line, token) for line, token in names if token not in allowed}
        problems += [
            f"{name}: {PLATFORM_ALLOWANCE} allows {token}, which it no longer names"
            for token in sorted(set(allowed) - {token for _, token in names})
        ]
    return [
        f"{name}:{line} names {token}; move it behind deployer/platform_support.py"
        for name, line, token in sorted(found)
    ] + problems


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
            valid = isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value.strip()
            return value.value if valid else None
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
    for function in entry_points:
        for node in ast.walk(function):
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


# Calls that change the filesystem. CLAUDE.md routes every one the deployer makes through deployer/fsops.py, whose
# functions tests replace to inject failures. A method named here is flagged on any object, since the policy cannot
# tell a Path from another receiver; the names are chosen so that none is a common method of anything else.
FILESYSTEM_WRITES: dict[str, frozenset[str]] = {
    "method": frozenset(
        {"write_bytes", "write_text", "touch", "mkdir", "unlink", "rmdir", "rename", "symlink_to", "hardlink_to"}
    ),
    "qualified": frozenset(
        {
            "os.remove",
            "os.unlink",
            "os.rename",
            "os.replace",
            "os.mkdir",
            "os.makedirs",
            "os.rmdir",
            "os.removedirs",
            "os.fdopen",
            "os.link",
            "os.symlink",
            "shutil.rmtree",
            "shutil.move",
            "shutil.copy",
            "shutil.copy2",
            "shutil.copyfile",
            "shutil.copytree",
            "tempfile.mkstemp",
            "tempfile.mkdtemp",
            "tempfile.TemporaryDirectory",
            "tempfile.NamedTemporaryFile",
        }
    ),
}
# A module sanctions a write beside the code it excuses, as a module-level FSOPS_ALLOWED = {token: reason}.
FSOPS_ALLOWANCE = "FSOPS_ALLOWED"
WRITE_MODE_CHARACTERS = "wax+"


def _writes_scanned_files(root: Path) -> list[Path]:
    """The deployer's code, other than fsops itself."""
    files = [root / "deploy.py", *(root / "deployer").rglob("*.py")]
    return [path for path in files if path.is_file() and path.relative_to(root).as_posix() != "deployer/fsops.py"]


def _opens_for_writing(call: ast.Call, mode_position: int) -> bool:
    """Whether open(), or a Path's open(), is given a mode that writes."""
    mode = (
        call.args[mode_position]
        if len(call.args) > mode_position
        else next((keyword.value for keyword in call.keywords if keyword.arg == "mode"), None)
    )
    return (
        isinstance(mode, ast.Constant)
        and isinstance(mode.value, str)
        and any(character in mode.value for character in WRITE_MODE_CHARACTERS)
    )


def _filesystem_writes(tree: ast.Module) -> list[tuple[int, str]]:
    """The filesystem writes a module's code makes, as (line, token), ignoring test cases."""
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.ClassDef) and any(
            (base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", None)) == "TestCase"
            for base in node.bases
        ):
            return
        if isinstance(node, ast.ImportFrom):
            found.extend(
                (node.lineno, f"{node.module}.{alias.name}")
                for alias in node.names
                if f"{node.module}.{alias.name}" in FILESYSTEM_WRITES["qualified"]
            )
        elif isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Name) and function.id == "open" and _opens_for_writing(node, 1):
                found.append((node.lineno, "open"))
            elif isinstance(function, ast.Attribute):
                qualified = f"{function.value.id}.{function.attr}" if isinstance(function.value, ast.Name) else ""
                if qualified in FILESYSTEM_WRITES["qualified"]:
                    found.append((node.lineno, qualified))
                elif function.attr in FILESYSTEM_WRITES["method"]:
                    found.append((node.lineno, function.attr))
                elif function.attr == "open" and _opens_for_writing(node, 0):
                    found.append((node.lineno, "open"))
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


def filesystem_write_problems(root: Path) -> list[str]:
    """Report filesystem writes in the deployer outside deployer/fsops.py, and allowances no longer needed."""
    found: set[tuple[str, int, str]] = set()
    problems: list[str] = []
    for path in sorted(_writes_scanned_files(root), key=lambda path: path.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        allowed = _platform_allowance(tree, FSOPS_ALLOWANCE)
        if allowed is None:
            problems.append(f"{name}: {FSOPS_ALLOWANCE} must map each token to the reason it is allowed")
            allowed = {}
        writes = _filesystem_writes(tree)
        found |= {(name, line, token) for line, token in writes if token not in allowed}
        problems += [
            f"{name}: {FSOPS_ALLOWANCE} allows {token}, which it no longer names"
            for token in sorted(set(allowed) - {token for _, token in writes})
        ]
    return [
        f"{name}:{line} writes with {token}; route it through deployer/fsops.py" for name, line, token in sorted(found)
    ] + problems


SHELL_LABELS = {"shell": "Bash", "powershell": "PowerShell"}
SHELL_ESCAPES = {"shell": "\\", "powershell": "`"}
# A fixture executes a token in a context when it calls run_tool and one of these finders.
SHELL_RUNNERS = {"shell": ("find_bash(",), "powershell": ("find_pwsh(", "find_powershell(")}
SHELL_WORD_BREAKS = " \t\n;|&()"


def _shell_units(path: Path) -> list[tuple[str, int, str]]:
    """A shipped file's Bash and PowerShell as (context, first line, text), classified as the deployer renders it."""
    context = render.SUFFIX_CONTEXT.get(path.suffix.casefold())
    text = path.read_text(encoding="utf-8", errors="replace")
    if context in SHELL_LABELS:
        return [(context, 1, text)]
    if context != "markdown":
        return []
    units: list[tuple[str, int, str]] = []
    fence: tuple[str, int, list[str]] | None = None
    for number, line in enumerate(text.split("\n"), start=1):
        trimmed = line.strip()
        if fence is None and trimmed.startswith("```"):
            words = trimmed[3:].split()
            fence = (render.FENCE_CONTEXT.get(words[0].casefold(), "text") if words else "text", number + 1, [])
        elif fence is not None and trimmed == "```":
            if fence[0] in SHELL_LABELS:
                units.append((fence[0], fence[1], "\n".join(fence[2])))
            fence = None
        elif fence is not None:
            fence[2].append(line)
    return units


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


def _executed_shell_tokens(root: Path) -> set[tuple[str, str]]:
    """The (context, token) pairs a tests/deployer test with a spaced value renders and then executes."""
    executed: set[tuple[str, str]] = set()
    for path in sorted((root / "tests" / "deployer").glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_") or "with_spaces" not in node.name:
                continue
            source = ast.get_source_segment(text, node) or ""
            if "run_tool(" not in source:
                continue
            for context, runners in SHELL_RUNNERS.items():
                if any(runner in source for runner in runners):
                    executed |= {(context, name) for name in TEMPLATE_TOKEN.findall(source)}
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


def _markdown_section(text: str, heading: str) -> str | None:
    lines = text.split("\n")
    if heading not in lines:
        return None
    start = lines.index(heading) + 1
    end = next((index for index in range(start, len(lines)) if lines[index].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


def suite_discovery_documentation_problems(root: Path) -> list[str]:
    """Report a rule that decides whether a regression suite runs and that "Validation" in the skill guide omits."""
    section = _markdown_section((root / SKILL_GUIDE).read_text(encoding="utf-8"), "## Validation")
    if section is None:
        return [f'{SKILL_GUIDE} has no "Validation" section']
    rules = [*TEST_NAME_PATTERNS, *sorted(TEST_SCRIPT_EXTENSIONS), PYTHON_ENTRY_POINT]
    return [f'{SKILL_GUIDE} "Validation" does not name `{rule}`' for rule in rules if f"`{rule}`" not in section]


def is_executable_script(path: Path) -> bool:
    return path.suffix.casefold() in EXECUTABLE_SCRIPT_EXTENSIONS


def is_shell_script(path: Path) -> bool:
    return path.suffix.casefold() in {".sh", ".bash"}


def is_test_script(path: Path) -> bool:
    if path.suffix.casefold() not in TEST_SCRIPT_EXTENSIONS:
        return False
    names = (path.stem.casefold(), path.name.casefold())
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in TEST_NAME_PATTERNS for name in names)


def relative(path: Path) -> str:
    return path.relative_to(REPOSITORY_ROOT).as_posix()


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def find_git_bash() -> str:
    found = platform_support.find_bash()
    if found:
        return found
    raise AssertionError(
        "Git Bash was not found. Install Git for Windows or set GIT_BASH; "
        "Git Bash is required for repository validation."
    )


def find_shellcheck() -> str | None:
    return platform_support.find_executable("shellcheck")


def find_powershell() -> str:
    found = shutil.which("pwsh")
    if found:
        return found
    raise AssertionError("PowerShell 7 (pwsh) was not found in PATH. It is required for repository validation.")


def find_ruff() -> str | None:
    """ruff from this interpreter, where requirements-dev.txt installs it even when its scripts are not on PATH."""
    try:
        from ruff.__main__ import find_ruff_bin

        return find_ruff_bin()
    except (ImportError, FileNotFoundError):
        return platform_support.find_executable("ruff")


PREREQUISITES: tuple[tuple[str, str, Callable[[], str | None]], ...] = (
    ("Git Bash", "Git Bash", find_git_bash),
    ("ShellCheck", "ShellCheck", find_shellcheck),
    ("PowerShell 7 (pwsh)", "PowerShell", find_powershell),
    ("ruff", "ruff", find_ruff),
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
# and CI installs Python, ShellCheck, and ruff at their floors. Git Bash has no floor: the shell scripts need no Bash 4.
# ruff's floor is its pin in requirements-dev.txt, because a newer minor release can change the formatting style.
DEPENDENCY_DOC = "docs/dependency-updates.md"
VALIDATION_FLOORS: dict[str, tuple[int, ...]] = {
    "Python": tools.MINIMUM_PYTHON,
    "ShellCheck": (0, 9, 0),
    "PowerShell 7 (pwsh)": (7, 0),
    "ruff": (0, 16, 10),
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


def outdated_prerequisites(versions: dict[str, tuple[int, ...] | None], python: tuple[int, ...]) -> list[str]:
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


def run_process(arguments: list[str], environment: dict[str, str] | None = None, cwd: Path = REPOSITORY_ROOT) -> None:
    merged = dict(os.environ)
    if environment:
        merged.update(environment)
    # Redirect to real files: MSYS tools can reject inherited anonymous pipes with a
    # spurious "failed to set file descriptor text/binary mode" error.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            completed = subprocess.run(
                arguments,
                cwd=cwd,
                env=merged,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                timeout=COMMAND_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(f"Command timed out after {COMMAND_TIMEOUT_SECONDS}s: {arguments[0]}") from exc
        if completed.returncode != 0:
            stdout.seek(0)
            stderr.seek(0)
            output = stdout.read().decode("utf-8", "replace")
            error = stderr.read().decode("utf-8", "replace")
            raise AssertionError(
                f"Command failed with exit code {completed.returncode}: {' '.join(arguments)}\n{output}\n{error}"
            )


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


def run_test_script(path: Path) -> None:
    target = relative(path)
    suffix = path.suffix.casefold()
    if suffix == ".py":
        run_process([sys.executable, "-B", target])
    elif suffix == ".sh":
        run_git_bash(f"bash {shell_quote(target)}")
    elif suffix == ".ps1":
        run_process([find_powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", target])
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
    found = (
        path for root in roots if root.is_dir() for path in root.rglob("*") if path.is_file() and is_test_script(path)
    )
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
            jobs.append(
                Job(
                    f"{label} [shard {index + 1}/{count}]",
                    label,
                    tests / count,
                    lambda s=suite, i=index, n=count: run_shard(s, i, n),
                )
            )
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


def ruff_format_check(root: Path, targets: list[str]) -> None:
    """Fail, naming each file, when ruff format would change any Python file under the targets in root."""
    ruff = find_ruff()
    if ruff is None:
        raise AssertionError(f"ruff was not found: {platform_support.install_hint('ruff')}")
    # Concise output names one file per line instead of printing each diff; no cache is written into the tree.
    try:
        run_process([ruff, "format", "--check", "--output-format", "concise", "--no-cache", *targets], cwd=root)
    except AssertionError as exc:
        raise AssertionError(
            f"{exc}\nRun `python -m ruff format` on the files named above, using requirements-dev.txt's ruff."
        ) from exc


def ruff_lint_check(root: Path, targets: list[str]) -> None:
    """Fail, naming each finding, when ruff check finds a violation of root's rule set under the targets."""
    ruff = find_ruff()
    if ruff is None:
        raise AssertionError(f"ruff was not found: {platform_support.install_hint('ruff')}")
    try:
        run_process([ruff, "check", "--output-format", "concise", "--no-cache", *targets], cwd=root)
    except AssertionError as exc:
        raise AssertionError(
            f"{exc}\nFix the findings named above; `python -m ruff check --fix` applies the ones ruff marks safe."
        ) from exc


def static_format_check() -> None:
    ruff_format_check(REPOSITORY_ROOT, list(FORMAT_ROOTS))


def static_lint_check() -> None:
    ruff_lint_check(REPOSITORY_ROOT, list(FORMAT_ROOTS))


def all_jobs() -> list[Job]:
    shell = "static shell checks (bash -n and ShellCheck on skill scripts)"
    python_format = "static format check (ruff format --check)"
    python_lint = "static lint check (ruff check)"
    return [
        Job(shell, shell, UNSPLIT_SUITE_WEIGHT, static_shell_check),
        Job(python_format, python_format, UNSPLIT_SUITE_WEIGHT, static_format_check),
        Job(python_lint, python_lint, UNSPLIT_SUITE_WEIGHT, static_lint_check),
        *suite_jobs(regression_suites()),
    ]


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
            "skills/update-coding-agent-skills/scripts/test_update.sh",
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
                completed = subprocess.run(
                    [sys.executable, "-B", str(SHARD_RUNNER), str(suite), str(index), "3"],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
            entries = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                sorted(f"test_{number}" for number in range(7)), sorted(e for e in entries if e.startswith("test_"))
            )
            self.assertEqual(3, entries.count("module from the suite directory"))
            self.assertEqual(3, entries.count("class"))
            self.assertNotIn("ran as __main__", entries)

            suite.write_text(
                suite.read_text(encoding="utf-8").replace("record('test_3')", "self.fail('boom')"), encoding="utf-8"
            )
            failing = subprocess.run(
                [sys.executable, "-B", str(SHARD_RUNNER), str(suite), "0", "3"], capture_output=True, text=True
            )
            self.assertEqual(1, failing.returncode)
            self.assertIn("boom", failing.stderr)
            refused = subprocess.run(
                [sys.executable, "-B", str(SHARD_RUNNER), str(suite), "3", "3"], capture_output=True, text=True
            )
            self.assertEqual(2, refused.returncode)

    def test_documentation_paths_are_classified(self) -> None:
        for path in (
            "docs/skills.md",
            "docs/new/guide.md",
            "README.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "CLAUDE.md",
            "AGENTS.md",
            ".github/ISSUE_TEMPLATE/bug.md",
            ".github/pull_request_template.md",
        ):
            with self.subTest(path=path):
                self.assertTrue(is_documentation(path))
        for path in (
            "skills/repo-cleanup/SKILL.md",
            "skills/README.md",
            "agents/code-review-reviewer.md",
            ".claude/skills/change-skill/SKILL.md",
            ".agents/skills/change-skill/SKILL.md",
            ".github/workflows/validate.yml",
            "tests/run_validation.py",
            "deployer/source.py",
            "source.json",
            "docs",
            "LICENSE",
            "readme.md",
        ):
            with self.subTest(path=path):
                self.assertFalse(is_documentation(path))

    def test_only_a_change_that_is_all_documentation_skips_the_suites(self) -> None:
        self.assertEqual(
            (True, "all 2 changed files are documentation"), documentation_only(["README.md", "docs/skills.md"])
        )
        for paths, reason in (
            (None, "could not be determined"),
            ([], "no changed files"),
            (
                ["docs/skills.md", "skills/repo-cleanup/SKILL.md"],
                "1 changed files are not documentation, such as skills/",
            ),
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
            self.assertEqual(
                [by_path, by_name], suites_naming(["docs/skills.md", "README.md"], [by_path, by_name, unrelated])
            )
            self.assertEqual([], suites_naming(["docs/other.md"], [by_path, by_name, unrelated]))

    def test_changed_files_include_commits_uncommitted_untracked_and_both_sides_of_a_rename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository with spaces"
            root.mkdir()
            environment = {
                **os.environ,
                "GIT_CONFIG_GLOBAL": str(Path(temporary) / "empty"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
            }
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
                self.assertEqual(
                    ["docs/edited.md", "docs/new.md", "docs/tool.md", "skills/a/tool.py"], changed_paths(root, "main")
                )
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
        suites = [path for path in regression_suites() if path.suffix.casefold() == ".py"]
        self.assertTrue(suites)
        missing = [relative(path) for path in suites if PYTHON_ENTRY_POINT not in path.read_text(encoding="utf-8")]
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
            write(
                ".agents/skills/good/SKILL.md", "---\nname: good\ndescription: Good.\n---\n\n" + pointer.format("good")
            )
            write(".claude/skills/missing/SKILL.md", "---\nname: missing\ndescription: Missing.\n---\n")
            write(".claude/skills/drifted/SKILL.md", "---\nname: drifted\ndescription: New.\n---\n")
            write(
                ".agents/skills/drifted/SKILL.md",
                "---\nname: drifted\ndescription: Old.\n---\n\n" + pointer.format("drifted"),
            )
            write(".claude/skills/astray/SKILL.md", "---\nname: astray\ndescription: Astray.\n---\n")
            write(
                ".agents/skills/astray/SKILL.md",
                "---\nname: astray\ndescription: Astray.\n---\n\n" + pointer.format("other"),
            )
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
                    '"Paths to a skill\'s own files" in docs/adding-a-skill.md',
                    "tests/fixtures/same/source.json reuses the shipped source ID",
                    "skills/alpha/notes.md names tests/fixtures, which never ships",
                ],
                fixture_source_problems(root),
            )

    def test_os_specific_code_stays_in_platform_support(self) -> None:
        # The macOS and Linux port then changes one module: deployer/platform_support.py.
        self.assertEqual([], platform_code_problems(REPOSITORY_ROOT))

    def test_skill_scripts_follow_the_script_results_contract(self) -> None:
        # An agent that learned one script's results can read every other's (#28).
        self.assertEqual([], script_contract_problems(REPOSITORY_ROOT))

    def test_script_contract_policy_detects_each_breach_and_honors_a_stated_exemption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "skills" / "alpha" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "run.py").write_text(
                "import json, sys\n"
                "def main(parser, value):\n"
                "    try:\n"
                "        value()\n"
                "    except OSError as exc:\n"
                "        parser.error(str(exc))\n"
                "    print(f'FAILED {value}', file=sys.stderr)\n"
                "    print(json.dumps(value))\n"
                "    if value:\n"
                "        return 3\n"
                "    parser.error('usage before any work is fine')\n"
                "    print('FAILED on stdout is fine')\n"
                "    return 2\n"
                "if __name__ == '__main__':\n"
                "    sys.exit(4)\n",
                encoding="utf-8",
            )
            (scripts / "hook.py").write_text(
                "import json\nEXIT_CONTRACT_EXEMPT = 'A hook protocol answers in JSON'\nprint(json.dumps({}))\n",
                encoding="utf-8",
            )
            (scripts / "vague.py").write_text("EXIT_CONTRACT_EXEMPT = ' '\n", encoding="utf-8")
            (scripts / "test_run.py").write_text("import sys\nsys.exit(5)\n", encoding="utf-8")
            (scripts / "tool.sh").write_text(
                'set -e\n[ -n "$1" ] || exit 3\nexit 0  # exit 6 in a comment\n', encoding="utf-8"
            )
            (scripts / "test_tool.sh").write_text("exit 7\n", encoding="utf-8")
            doc = '"Script results" in docs/adding-a-skill.md'
            self.assertEqual(
                [
                    f"skills/alpha/scripts/run.py:6 reports a failure through parser.error; print FAILED <reason> and "
                    f"exit 1; see {doc}",
                    f"skills/alpha/scripts/run.py:7 prints FAILED on stderr; print it on stdout; see {doc}",
                    f"skills/alpha/scripts/run.py:8 prints JSON; print one fact per line; see {doc}",
                    f"skills/alpha/scripts/run.py:10 exits 3; scripts exit only 0, 1, or 2; see {doc}",
                    f"skills/alpha/scripts/run.py:15 exits 4; scripts exit only 0, 1, or 2; see {doc}",
                    f"skills/alpha/scripts/tool.sh:2 exits 3; scripts exit only 0, 1, or 2; see {doc}",
                    "skills/alpha/scripts/vague.py: EXIT_CONTRACT_EXEMPT must be a non-empty string saying why",
                ],
                script_contract_problems(root),
            )

    def test_deployer_filesystem_writes_go_through_fsops(self) -> None:
        # Tests inject failures by replacing deployer/fsops.py's functions, which reach only writes made through it.
        self.assertEqual([], filesystem_write_problems(REPOSITORY_ROOT))

    def test_filesystem_write_policy_detects_each_write_and_a_stale_allowance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def write(relative_path: str, text: str) -> None:
                (root / relative_path).parent.mkdir(parents=True, exist_ok=True)
                (root / relative_path).write_text(text, encoding="utf-8")

            write("deploy.py", "from pathlib import Path\nPath('x').write_text('y')\n")
            write("deployer/fsops.py", "import os\nos.mkdir('x')\n")
            write(
                "deployer/writes.py",
                '"""Docstrings may say os.remove or shutil.rmtree."""\n'
                "import os\n"
                "import shutil\n"
                "import tempfile\n"
                "from os import replace\n"
                "\n"
                "\n"
                "def write(path, text):\n"
                "    # Comments may say path.unlink().\n"
                "    path.touch()\n"
                "    os.makedirs(path)\n"
                "    shutil.rmtree(path)\n"
                "    tempfile.mkstemp()\n"
                "    replace(path, path)\n"
                '    open(path, "w").close()\n'
                '    open(path, mode="ab").close()\n'
                '    path.open("x").close()\n'
                "    open(path).read()\n"
                '    open(path, "rb").read()\n'
                '    path.open(encoding="utf-8").read()\n'
                '    text.replace("a", "b")\n'
                "    os.path.exists(path)\n"
                "    return path.mkdir(parents=True)\n",
            )
            write(
                "deployer/allowed.py",
                'FSOPS_ALLOWED = {"tempfile.mkdtemp": "a throwaway working directory outside every managed root"}\n'
                "import tempfile\n"
                "WORK = tempfile.mkdtemp()\n",
            )
            write("deployer/gone.py", 'FSOPS_ALLOWED = {"shutil.rmtree": "the module no longer calls it"}\n')
            write("deployer/unexplained.py", 'FSOPS_ALLOWED = {"touch": ""}\nPATH.touch()\n')
            write(
                "deployer/tested.py",
                "import unittest\n\n\nclass Fixtures(unittest.TestCase):\n    def test_x(self):\n"
                '        PATH.write_bytes(b"")\n',
            )
            write("tools/elsewhere.py", "from pathlib import Path\nPath('x').write_text('y')\n")
            route = "; route it through deployer/fsops.py"
            self.assertEqual(
                [
                    f"deploy.py:2 writes with write_text{route}",
                    f"deployer/unexplained.py:2 writes with touch{route}",
                    f"deployer/writes.py:5 writes with os.replace{route}",
                    f"deployer/writes.py:10 writes with touch{route}",
                    f"deployer/writes.py:11 writes with os.makedirs{route}",
                    f"deployer/writes.py:12 writes with shutil.rmtree{route}",
                    f"deployer/writes.py:13 writes with tempfile.mkstemp{route}",
                    f"deployer/writes.py:15 writes with open{route}",
                    f"deployer/writes.py:16 writes with open{route}",
                    f"deployer/writes.py:17 writes with open{route}",
                    f"deployer/writes.py:23 writes with mkdir{route}",
                    "deployer/gone.py: FSOPS_ALLOWED allows shutil.rmtree, which it no longer names",
                    "deployer/unexplained.py: FSOPS_ALLOWED must map each token to the reason it is allowed",
                ],
                filesystem_write_problems(root),
            )

    def test_no_shipped_function_is_named_so_codeql_reads_its_result_as_a_secret(self) -> None:
        self.assertEqual([], secret_named_function_problems(REPOSITORY_ROOT))

    def test_secret_named_function_policy_flags_trusted_but_not_untrusted_or_tests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "skills" / "example" / "scripts"
            scripts.mkdir(parents=True)
            (root / "deployer").mkdir()
            (scripts / "runtime.py").write_text(
                "def resolve_trusted_commit():\n    pass\n\n\n"
                "class Reader:\n    def _trusted_files(self):\n        pass\n\n\n"
                "def is_trusted():\n    pass\n\n\ndef untrusted_text():\n    pass\n",
                encoding="utf-8",
            )
            (scripts / "test_runtime.py").write_text("def test_trusted_commit():\n    pass\n", encoding="utf-8")
            (root / "deployer" / "check.py").write_text("def trusted_paths():\n    pass\n", encoding="utf-8")
            self.assertEqual(
                [
                    "deployer/check.py:1: function trusted_paths",
                    "skills/example/scripts/runtime.py:1: function resolve_trusted_commit",
                    "skills/example/scripts/runtime.py:6: function _trusted_files",
                ],
                [problem.split(" is named", 1)[0] for problem in secret_named_function_problems(root)],
            )

    def test_platform_code_policy_detects_each_token_and_a_stale_allowance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def write(relative_path: str, text: str) -> None:
                (root / relative_path).parent.mkdir(parents=True, exist_ok=True)
                (root / relative_path).write_text(text, encoding="utf-8")

            write("deploy.py", "import sys\n\nif sys.platform == 'win32':\n    pass\n")
            write(
                "deployer/files.py",
                '"""Docstrings may say os.chmod, USERPROFILE, or cygpath."""\n'
                "import os\n"
                "from os import name\n"
                "import ctypes.wintypes\n"
                "from msvcrt import getch\n"
                "\n"
                "\n"
                "def private(path, environment):\n"
                '    """So may a function\'s: LOCALAPPDATA."""\n'
                "    # And comments: sys.platform, os.chmod, cygpath.\n"
                "    path.chmod(0o600)\n"
                '    return environment.get("LOCALAPPDATA"), {"ProgramFiles": os.name}\n',
            )
            write("deployer/platform_support.py", "import sys\nPLATFORM = sys.platform\n")
            write(
                "tools/shell.py",
                "import subprocess\n"
                'SNIPPET = "path=$(cygpath -m \\"$0\\")"\n'
                'OTHER = "cygpathology is not a command"\n'
                'subprocess.run(["x"], creationflags=0, check=False)\n',
            )
            write(
                "tools/allowed.py",
                'PLATFORM_ALLOWED = {"cygpath": "it falls back when cygpath is absent"}\n'
                'SNIPPET = "command -v cygpath && cygpath -m x"\n'
                'HOME = "USERPROFILE"\n',
            )
            write("tools/gone.py", 'PLATFORM_ALLOWED = {"cygpath": "the module no longer names it"}\n')
            write("tools/unexplained.py", 'PLATFORM_ALLOWED = {"cygpath": " "}\nSNIPPET = "cygpath -m x"\n')
            write(
                "tests/run_validation.py",
                'PLATFORM_TOKENS = {"variable": ("USERPROFILE",), "command": ("cygpath",)}\n'
                "import unittest\n"
                "\n"
                "\n"
                "class Fixtures(unittest.TestCase):\n"
                "    def test_names_tokens(self):\n"
                '        self.assertEqual("USERPROFILE", "USERPROFILE")\n'
                "\n"
                "\n"
                'HOME = "USERPROFILE"\n',
            )
            write("tests/deployer/test_platform.py", "import sys\nPLATFORM = sys.platform\n")
            write("skills/alpha/scripts/tool.py", "import sys\nPLATFORM = sys.platform\n")
            move = "; move it behind deployer/platform_support.py"
            self.assertEqual(
                [
                    f"deploy.py:3 names sys.platform{move}",
                    f"deployer/files.py:3 names os.name{move}",
                    f"deployer/files.py:4 names ctypes{move}",
                    f"deployer/files.py:5 names msvcrt{move}",
                    f"deployer/files.py:11 names chmod{move}",
                    f"deployer/files.py:12 names LOCALAPPDATA{move}",
                    f"deployer/files.py:12 names ProgramFiles{move}",
                    f"deployer/files.py:12 names os.name{move}",
                    f"tests/run_validation.py:10 names USERPROFILE{move}",
                    f"tools/allowed.py:3 names USERPROFILE{move}",
                    f"tools/shell.py:2 names cygpath{move}",
                    f"tools/shell.py:4 names creationflags{move}",
                    f"tools/unexplained.py:2 names cygpath{move}",
                    "tools/gone.py: PLATFORM_ALLOWED allows cygpath, which it no longer names",
                    "tools/unexplained.py: PLATFORM_ALLOWED must map each token to the reason it is allowed",
                ],
                platform_code_problems(root),
            )

    def test_shell_tokens_are_quoted_and_executed_by_a_fixture(self) -> None:
        self.assertEqual([], shell_token_problems(REPOSITORY_ROOT))

    def test_shell_token_policy_detects_unquoted_and_unexecuted_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def write(relative_path: str, text: str) -> None:
                (root / relative_path).parent.mkdir(parents=True, exist_ok=True)
                (root / relative_path).write_text(text, encoding="utf-8")

            write(
                "skills/alpha/SKILL.md",
                "Prose may say {{BARE}}.\n"
                "\n"
                "```bash\n"
                'python -B run.py "{{QUOTED}}" \'{{SINGLE}}\' "a \\" {{STILL_QUOTED}}"\n'
                "cd {{UNQUOTED}}/x  # but a comment may say {{IN_COMMENT}}\n"
                "```\n"
                "\n"
                "```text\n"
                "{{NOT_SHELL}}\n"
                "```\n"
                "\n"
                "```pwsh\n"
                'Set-Location "{{QUOTED}}"; Write-Output `{{ESCAPED}}\n'
                "```\n",
            )
            write("skills/alpha/scripts/run.sh", '#!/usr/bin/env bash\necho "{{QUOTED}}"\necho {{SCRIPT}}\n')
            write("skills/alpha/scripts/run.ps1", "Write-Output '{{PS_ONLY}}'\n")
            write("skills/alpha/scripts/tool.py", "ROOT = {{PYTHON}}\n")
            write(
                "tests/deployer/test_rendering.py",
                "class Fixtures:\n"
                "    def test_quoted_executes_with_spaces(self):\n"
                '        self.make_skill("alpha", "```bash\\nprintf \\"{{QUOTED}}\\"\\n```")\n'
                "        platform_support.run_tool([platform_support.find_bash(), 'run.sh'])\n"
                "\n"
                "    def test_single_executes_with_spaces(self):\n"
                '        self.make_skill("alpha", "{{SINGLE}} {{STILL_QUOTED}} {{UNQUOTED}} {{SCRIPT}}")\n'
                "        platform_support.run_tool([platform_support.find_bash(), 'run.sh'])\n"
                "\n"
                "    def test_powershell_executes(self):\n"
                '        self.make_skill("alpha", "{{PS_ONLY}}")\n'
                "        platform_support.run_tool([platform_support.find_pwsh(path), 'run.ps1'])\n"
                "\n"
                "    def test_quoted_renders_in_powershell_with_spaces(self):\n"
                '        self.make_skill("alpha", "{{QUOTED}}")\n'
                "        platform_support.find_pwsh(path)\n",
            )
            self.assertEqual(
                [
                    "skills/alpha/SKILL.md:5 has {{UNQUOTED}} outside quotes in Bash",
                    "skills/alpha/SKILL.md:13 has {{ESCAPED}} outside quotes in PowerShell",
                    "skills/alpha/scripts/run.sh:3 has {{SCRIPT}} outside quotes in Bash",
                    "skills/alpha/SKILL.md:13 carries {{ESCAPED}} in PowerShell, but no tests/deployer test named "
                    "*with_spaces* renders and executes it there",
                    "skills/alpha/SKILL.md:13 carries {{QUOTED}} in PowerShell, but no tests/deployer test named "
                    "*with_spaces* renders and executes it there",
                    "skills/alpha/scripts/run.ps1:1 carries {{PS_ONLY}} in PowerShell, but no tests/deployer test "
                    "named *with_spaces* renders and executes it there",
                ],
                shell_token_problems(root),
            )

    def test_suite_discovery_rules_are_documented(self) -> None:
        self.assertEqual([], suite_discovery_documentation_problems(REPOSITORY_ROOT))

    def test_suite_discovery_documentation_policy_detects_each_missing_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "docs").mkdir()
            guide = root / "docs" / "adding-a-skill.md"
            guide.write_text(
                '# Adding a skill\n\n## Files\n\n`test-*` `*.test.*` `.sh` `if __name__ == "__main__":`\n\n'
                "## Validation\n\nName it `test_*`, `*_test`, or `*-test` and use `.py` or `.ps1`.\n\n## Later\n",
                encoding="utf-8",
            )
            missing = 'docs/adding-a-skill.md "Validation" does not name'
            self.assertEqual(
                [
                    f"{missing} `test-*`",
                    f"{missing} `*.test.*`",
                    f"{missing} `.sh`",
                    f'{missing} `if __name__ == "__main__":`',
                ],
                suite_discovery_documentation_problems(root),
            )
            guide.write_text("# Adding a skill\n", encoding="utf-8")
            self.assertEqual(
                ['docs/adding-a-skill.md has no "Validation" section'], suite_discovery_documentation_problems(root)
            )

    def test_suite_names_follow_the_documented_patterns(self) -> None:
        self.assertEqual(("test_*", "test-*", "*_test", "*-test", "*.test.*"), TEST_NAME_PATTERNS)
        for name in (
            "test_a.py",
            "test-a.sh",
            "a_test.ps1",
            "a-test.py",
            "a.test.py",
            "a.test.b.sh",
            "TEST_A.PY",
            "Test-A.Ps1",
            "test_.py",
        ):
            with self.subTest(name=name):
                self.assertTrue(is_test_script(Path(name)))
        for name in (
            "a.py",
            "testa.py",
            "atest.py",
            "test_a.txt",
            "a_tests.py",
            "contest.py",
            "a.tests.py",
            "test_a.js",
        ):
            with self.subTest(name=name):
                self.assertFalse(is_test_script(Path(name)))

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
                "ungranted": (
                    json.dumps([f"Bash({own}*)", f"PowerShell({own}*)"]),
                    f'python -B "${{CLAUDE_SKILL_DIR}}/../core/scripts/y.py"\ngit status',
                ),
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
            skills = {"alpha": {"skill_deps": ["core"]}, "beta": {}, "core": {}, "analyze-skill-cost": {}}
            for skill, metadata in skills.items():
                (root / "deploy-meta" / f"{skill}.json").write_text(json.dumps(metadata), encoding="utf-8")
                (root / "skills" / skill / "scripts").mkdir(parents=True)
                (root / "skills" / skill / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
            (root / "skills" / "alpha" / "scripts" / "run.py").write_text("", encoding="utf-8")
            (root / "skills" / "core" / "scripts" / "lib.py").write_text("", encoding="utf-8")
            (root / "skills" / "alpha" / "references").mkdir()
            (root / "skills" / "alpha" / "references" / "checks.md").write_text(
                "See ${CLAUDE_SKILL_DIR}/references/checks.md and ${CLAUDE_SKILL_DIR}/references/<target>.md.\n"
                "A reference file may describe `scripts/run.py`, which Claude Code does not expand variables in.\n",
                encoding="utf-8",
            )
            (root / "skills" / "alpha" / "SKILL.md").write_text(
                "Searches `{{HOME}}/.claude/skills/<SKILL_NAME>/`.\n"
                "```bash\n"
                'python -B "${CLAUDE_SKILL_DIR}/scripts/run.py" --in "data/scripts/x"\n'
                'python -B "${CLAUDE_SKILL_DIR}/../core/scripts/lib.py"\n'
                "```\n"
                'Prose names `scripts/run.py`, grants `Bash(python -B "${CLAUDE_SKILL_DIR}/scripts/*)`, and points at '
                "${CLAUDE_SKILL_DIR}/references/checks.md.\n"
                "Give the user ${CLAUDE_SKILL_DIR}/references/gone.md and `${CLAUDE_SKILL_DIR}/../core/scripts/old.py`.\n"
                "Read `references/checks.md`, `./scripts/run.py`, `../core/scripts/lib.py`, and `references/`.\n"
                "Read `${CLAUDE_SKILL_DIR}/references/checks.md`; the target's `.github/scripts/x.py` and "
                "`data/references/y` are not ours.\n"
                "```text\n"
                "`scripts/run.py` in an example fence\n"
                "```\n",
                encoding="utf-8",
            )
            # analyze-skill-cost describes the scripts/ convention of the skills it audits, so that span is its own.
            (root / "skills" / "analyze-skill-cost" / "SKILL.md").write_text(
                "Recommend a tested command under `scripts/`, never `scripts/x.py`.\n", encoding="utf-8"
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
            doc = '"Paths to a skill\'s own files" in docs/adding-a-skill.md'
            agents_doc = '"Subagent definitions" in docs/adding-a-skill.md'
            self.assertEqual(
                [
                    f"agents/helper.md:1 names skill beta by its install path; see {doc}",
                    f"agents/helper.md:2 finds a file through $HOME; see {agents_doc}",
                    f"agents/helper.md:3 finds a file through $HOME; see {agents_doc}",
                    f"agents/helper.md:4 finds a file through $HOME; see {agents_doc}",
                    f"skills/alpha/SKILL.md:6 names `scripts/run.py` by a bare relative path; see {doc}",
                    "skills/alpha/SKILL.md:7 names ${CLAUDE_SKILL_DIR}/references/gone.md, which does not exist",
                    "skills/alpha/SKILL.md:7 names ${CLAUDE_SKILL_DIR}/../core/scripts/old.py, which does not exist",
                    f"skills/alpha/SKILL.md:8 names `references/checks.md` by a bare relative path; see {doc}",
                    f"skills/alpha/SKILL.md:8 names `./scripts/run.py` by a bare relative path; see {doc}",
                    f"skills/alpha/SKILL.md:8 names `../core/scripts/lib.py` by a bare relative path; see {doc}",
                    f"skills/alpha/SKILL.md:8 names `references/` by a bare relative path; see {doc}",
                    f"skills/analyze-skill-cost/SKILL.md:1 names `scripts/x.py` by a bare relative path; see {doc}",
                    f"skills/beta/SKILL.md:1 names skill core by its install path; see {doc}",
                    f"skills/beta/SKILL.md:3 runs a script by a bare relative path; see {doc}",
                    f"skills/beta/SKILL.md:4 runs a script by a bare relative path; see {doc}",
                    "skills/beta/SKILL.md:5 reaches ../core without declaring it in skill_deps",
                ],
                skill_path_problems(root),
            )

    def test_skills_never_leave_an_output_path_to_the_agent(self) -> None:
        self.assertEqual([], output_placeholder_problems(REPOSITORY_ROOT))

    def test_output_placeholder_policy_flags_output_options_only_in_command_fences(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "skills" / "alpha" / "references").mkdir(parents=True)
            (root / "skills" / "alpha" / "SKILL.md").write_text(
                'Prose may say --output "<file>".\n'
                "```bash\n"
                'python -B "${CLAUDE_SKILL_DIR}/scripts/a.py" enumerate --output "<batch file>"\n'
                'python -B "${CLAUDE_SKILL_DIR}/scripts/a.py" sweep --plans <plan directory> --output=<x>\n'
                'python -B "${CLAUDE_SKILL_DIR}/scripts/a.py" check --run "<run directory>" --input "<input file>"\n'
                'python -B "${CLAUDE_SKILL_DIR}/scripts/a.py" install --spec "<spec file>" --plan "<plan file>"\n'
                'python -B "${CLAUDE_SKILL_DIR}/scripts/a.py" collect --output "$TMP/x.json"\n'
                "```\n"
                '```text\n--output "<file>"\n```\n',
                encoding="utf-8",
            )
            (root / "skills" / "alpha" / "references" / "notes.md").write_text(
                "```powershell\npython -B x.py --output-dir '<dir>' --out <file>\n```\n", encoding="utf-8"
            )
            see = f"let the script choose and print the path; see {WORKING_FILES_DOC}"
            self.assertEqual(
                [
                    f"skills/alpha/SKILL.md:3 leaves --output to the agent; {see}",
                    f"skills/alpha/SKILL.md:4 leaves --plans to the agent; {see}",
                    f"skills/alpha/SKILL.md:4 leaves --output to the agent; {see}",
                    f"skills/alpha/references/notes.md:2 leaves --output-dir to the agent; {see}",
                    f"skills/alpha/references/notes.md:2 leaves --out to the agent; {see}",
                ],
                output_placeholder_problems(root),
            )

    def test_shell_command_extraction_ignores_keywords_patterns_and_functions(self) -> None:
        script = (
            "set -euo pipefail\n"
            'BASE=$(git rev-parse HEAD | tr -d "\\r") && echo "$BASE" >/dev/null\n'
            'case "$1" in\n'
            '  main|release/*) grep -q "x|jq" file ;;\n'
            '  *) helper "rg" ;;\n'
            "esac\n"
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
            doc = '"Commands skills may run" in docs/adding-a-skill.md'
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
            (root / "skills" / "alpha" / "scripts" / "run.py").write_text(
                'run(["gh", "pr", "list"])\n', encoding="utf-8"
            )
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

    def test_skill_command_policy_counts_the_dependency_scripts_a_skill_reaches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "deploy-meta").mkdir()
            for skill, metadata in {
                "core": {"tools": ["copilot", "gh"], "selectable": False},
                "named": {"tools": ["copilot", "gh"], "skill_deps": ["core"]},
                "imports": {"optional_tools": ["gh"], "skill_deps": ["core"]},
                "unused": {"optional_tools": ["dotnet-format"], "skill_deps": ["core"]},
                "inherits": {"skill_deps": ["core"]},
            }.items():
                (root / "deploy-meta" / f"{skill}.json").write_text(json.dumps(metadata), encoding="utf-8")
                (root / "skills" / skill / "scripts").mkdir(parents=True)
                (root / "skills" / skill / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
            core = root / "skills" / "core" / "scripts"
            (core / "pipeline.py").write_text("from github import Client\nimport store\n", encoding="utf-8")
            (core / "github.py").write_text('run(["gh", "api"])\n', encoding="utf-8")
            (core / "store.py").write_text("import json\n", encoding="utf-8")
            (core / "hosts.py").write_text('shutil.which("copilot")\n', encoding="utf-8")
            (core / "test_hosts.py").write_text("import pipeline\n", encoding="utf-8")
            # A skill reaches pipeline.py by naming its path, and github.py through pipeline.py's import, but never
            # hosts.py, so copilot is declared without being run.
            (root / "skills" / "named" / "SKILL.md").write_text(
                '```bash\npython -B "${CLAUDE_SKILL_DIR}/../core/scripts/pipeline.py" run\n```\n', encoding="utf-8"
            )
            (root / "skills" / "imports" / "scripts" / "run.py").write_text(
                "sys.path.insert(0, str(CORE))\nfrom store import load\nimport github\n", encoding="utf-8"
            )
            (root / "skills" / "unused" / "scripts" / "run.py").write_text("import store\n", encoding="utf-8")
            (root / "skills" / "inherits" / "scripts" / "run.py").write_text("import store\n", encoding="utf-8")
            self.assertEqual(
                [
                    "skill named declares tool copilot but never runs it",
                    "skill unused declares tool dotnet-format but never runs it",
                ],
                skill_command_problems(root, {"copilot", "dotnet-format", "gh"}, {"git", "python"}),
            )

    def test_deploy_metadata_is_in_the_canonical_format(self) -> None:
        self.assertEqual([], metadata_format_problems(REPOSITORY_ROOT))

    def test_metadata_format_policy_detects_other_indentation_and_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "deploy-meta").mkdir()
            files = {
                "canonical": '{\n    "required_vars": [],\n    "shared_deps": ["runtime-compatibility.md"]\n}\n',
                "two-space": '{\n  "required_vars": [],\n  "shared_deps": ["runtime-compatibility.md"]\n}\n',
                "expanded": '{\n    "required_vars": [],\n    "shared_deps": [\n        "runtime-compatibility.md"\n'
                "    ]\n}\n",
                "no-newline": '{\n    "required_vars": []\n}',
            }
            for name, text in files.items():
                (root / "deploy-meta" / f"{name}.json").write_text(text, encoding="utf-8", newline="")
            doc = '"Metadata" in docs/adding-a-skill.md'
            self.assertEqual(
                [
                    f"deploy-meta/expanded.json is not in the canonical metadata format; see {doc}",
                    f"deploy-meta/no-newline.json is not in the canonical metadata format; see {doc}",
                    f"deploy-meta/two-space.json is not in the canonical metadata format; see {doc}",
                ],
                metadata_format_problems(root),
            )

    def test_deploy_variable_policy_detects_unused_and_undeclared_variables(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "deploy-meta").mkdir()
            (root / "deploy-meta" / "alpha.json").write_text(
                json.dumps({"required_vars": ["REPOS_ROOT", "HOME"]}), encoding="utf-8"
            )
            (root / "skills" / "alpha").mkdir(parents=True)
            (root / "skills" / "alpha" / "SKILL.md").write_text("{{REPOS_ROOT}} {{SOURCE_ROOT}}\n", encoding="utf-8")
            (root / "skills" / "shared.md").write_text("{{REPOS_ROOT}} {{HOME}}\n", encoding="utf-8")
            (root / "source.json").write_text(json.dumps({"shared_assets": {"shared.md": "owner"}}), encoding="utf-8")
            self.assertEqual(
                [
                    "configure never prompts for configured variable SPARE",
                    "configure prompts for unknown variable EXTRA",
                    "skill alpha uses {{SOURCE_ROOT}} without declaring it in required_vars",
                    "skill alpha declares required variable HOME but never uses it",
                    "configured variable SPARE is not required by any skill",
                    "shared asset shared.md uses non-derived variable REPOS_ROOT",
                    "derived variable UNUSED_DERIVED is not used by any skill or shared asset",
                ],
                deploy_variable_problems(
                    root, {"REPOS_ROOT", "SPARE"}, {"REPOS_ROOT", "EXTRA"}, {"HOME", "SOURCE_ROOT", "UNUSED_DERIVED"}
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

    def test_missing_ruff_is_reported_with_the_install_command(self) -> None:
        self.assertIn(("ruff", "ruff", find_ruff), PREREQUISITES)
        # Neither the interpreter's ruff package nor one on PATH: no import or command error, only None.
        with (
            mock.patch.dict(sys.modules, {"ruff": None, "ruff.__main__": None}),
            mock.patch.object(platform_support, "find_executable", return_value=None),
        ):
            self.assertIsNone(find_ruff())
        self.assertEqual(
            ["  - ruff: python -m pip install -r requirements-dev.txt"],
            missing_prerequisites((("ruff", "ruff", lambda: None),)),
        )

    def test_format_check_names_an_unformatted_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text("[tool.ruff]\nline-length = 120\n", encoding="utf-8")
            (root / "clean.py").write_text('x = {"a": 1}\n', encoding="utf-8")
            ruff_format_check(root, ["clean.py"])
            (root / "module.py").write_text("x = {  'a':1 }\n", encoding="utf-8")
            with self.assertRaises(AssertionError) as raised:
                ruff_format_check(root, ["clean.py", "module.py"])
            message = str(raised.exception)
            self.assertRegex(message, r"(?m)^module\.py:\d+:\d+: unformatted: File would be reformatted$")
            self.assertNotRegex(message, r"(?m)^clean\.py:")
            self.assertEqual([], sorted(path.name for path in root.iterdir() if path.name.startswith(".")))
            self.assertIn("python -m ruff format", message)

    def test_format_check_covers_every_python_root(self) -> None:
        self.assertEqual(("deployer", "tools", "tests", "skills", "deploy.py"), FORMAT_ROOTS)
        self.assertIn("static format check (ruff format --check)", [job.name for job in all_jobs()])

    def test_lint_check_names_an_unused_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["F"]\n', encoding="utf-8")
            (root / "clean.py").write_text("import os\n\nprint(os.sep)\n", encoding="utf-8")
            ruff_lint_check(root, ["clean.py"])
            (root / "module.py").write_text("import os\n", encoding="utf-8")
            with self.assertRaises(AssertionError) as raised:
                ruff_lint_check(root, ["clean.py", "module.py"])
            message = str(raised.exception)
            self.assertRegex(message, r"(?m)^module\.py:1:8: F401 ")
            self.assertNotRegex(message, r"(?m)^clean\.py:")
            self.assertEqual([], sorted(path.name for path in root.iterdir() if path.name.startswith(".")))
            self.assertIn("python -m ruff check --fix", message)

    def test_lint_check_covers_every_python_root(self) -> None:
        jobs = {job.name: job for job in all_jobs()}
        self.assertIn("static lint check (ruff check)", jobs)
        with mock.patch(f"{__name__}.ruff_lint_check") as lint:
            jobs["static lint check (ruff check)"].run()
        lint.assert_called_once_with(REPOSITORY_ROOT, ["deployer", "tools", "tests", "skills", "deploy.py"])

    def test_missing_ruff_fails_the_lint_check_with_the_install_command(self) -> None:
        with mock.patch(f"{__name__}.find_ruff", return_value=None), self.assertRaises(AssertionError) as raised:
            ruff_lint_check(REPOSITORY_ROOT, ["deploy.py"])
        self.assertIn("ruff was not found: python -m pip install -r requirements-dev.txt", str(raised.exception))

    def test_validation_floors_are_the_documented_versions(self) -> None:
        self.assertEqual(
            {"Python": (3, 11), "ShellCheck": (0, 9, 0), "PowerShell 7 (pwsh)": (7, 0), "ruff": (0, 16, 10)},
            VALIDATION_FLOORS,
        )
        document = (REPOSITORY_ROOT / DEPENDENCY_DOC).read_text(encoding="utf-8")
        for row in (
            "| Python | 3.11 |",
            "| ShellCheck | 0.9.0 |",
            "| PowerShell 7 (`pwsh`) | 7.0 |",
            "| ruff | 0.16.10 |",
        ):
            with self.subTest(row=row):
                self.assertIn(row, document)

    def test_ci_exercises_each_floor(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
        self.assertIn("choco install shellcheck --version 0.9.0 ", workflow)
        self.assertIn("python-version: ['3.11', '3.x']", workflow)
        # Both matrix entries install the pinned development dependencies, so ruff runs at its floor.
        self.assertIn("python -m pip install -r requirements-dev.txt", workflow)
        requirements = (REPOSITORY_ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(["ruff==0.16.10", "mypy==2.4.0"], [line for line in requirements if "==" in line])
        dependabot = (REPOSITORY_ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
        self.assertRegex(
            dependabot, r"(?m)^  - package-ecosystem: pip\n    directory: /\n    schedule:\n      interval: weekly$"
        )
        # The aggregate job keeps the single required status check context that branch protection names.
        self.assertRegex(workflow, r"(?m)^  validate:\n(?:    .*\n)*?    needs: suite$")

    def test_deployable_workflow_is_a_manual_pinned_check_without_secrets(self) -> None:
        workflows = REPOSITORY_ROOT / ".github/workflows"
        workflow = (workflows / "deployable.yml").read_text(encoding="utf-8")
        reviewed = (workflows / "validate.yml").read_text(encoding="utf-8")
        # Dispatch is its only trigger, so it can never be a required status check or run on a pull request.
        triggers = re.search(r"(?ms)^on:\n(.*?)^permissions:", workflow).group(1)
        self.assertEqual(["workflow_dispatch"], re.findall(r"(?m)^  ([A-Za-z_]+):", triggers))
        self.assertRegex(workflow, r"(?m)^permissions:\n  contents: read\n")
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("GITHUB_TOKEN", workflow)
        pins = re.findall(r"(?m)^\s*-?\s*uses:\s*(\S+)@(\S+)(.*)$", workflow)
        self.assertTrue(pins)
        for action, reference, rest in pins:
            with self.subTest(action=action):
                self.assertRegex(reference, r"^[0-9a-f]{40}$")
                self.assertRegex(rest, r"^\s+# v\d+(?:\.\d+)*$")
        # An action both workflows use is pinned to the commit Dependabot reviews in validate.yml.
        shared = {action for action, _, _ in pins} & set(re.findall(r"uses:\s*(\S+)@", reviewed))
        self.assertTrue(shared)
        for action in shared:
            with self.subTest(shared=action):
                self.assertEqual(
                    re.search(rf"{re.escape(action)}@(\S+ +# v\S+)", reviewed).group(1),
                    re.search(rf"{re.escape(action)}@(\S+ +# v\S+)", workflow).group(1),
                )

    def test_deployable_workflow_installs_the_runtime_versions_the_readme_lists_for_the_fresh_runner(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/deployable.yml").read_text(encoding="utf-8")
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        for runtime, key in (
            ("Claude Code", "claude-version"),
            ("Codex CLI", "codex-version"),
            ("GitHub Copilot CLI", "copilot-version"),
        ):
            with self.subTest(runtime=runtime):
                # The third column: the maintainer's machines come first and may be ahead of the runner.
                tested = re.search(rf"(?m)^\| {runtime} \| \S+ \| (\d+(?:\.\d+)+) \|", readme).group(1)
                default = re.search(
                    rf"(?m)^      {key}:\n(?:        .*\n)*?        default: '([^']+)'", workflow
                ).group(1)
                self.assertEqual(tested, default)

    def test_prerequisite_check_reports_every_tool_older_than_its_floor(self) -> None:
        self.assertEqual(
            [
                "  - Python 3.10.12 is older than the floor 3.11 in docs/dependency-updates.md",
                "  - ShellCheck 0.8.0 is older than the floor 0.9.0 in docs/dependency-updates.md: "
                "winget install --id koalaman.shellcheck",
                "  - PowerShell 7 (pwsh): its version could not be read; the floor is 7.0 in "
                "docs/dependency-updates.md: winget install --id Microsoft.PowerShell",
                "  - ruff 0.16.9 is older than the floor 0.16.10 in docs/dependency-updates.md: "
                "python -m pip install -r requirements-dev.txt",
            ],
            outdated_prerequisites(
                {"Git Bash": None, "ShellCheck": (0, 8, 0), "PowerShell 7 (pwsh)": None, "ruff": (0, 16, 9)},
                (3, 10, 12),
            ),
        )
        self.assertEqual(
            [],
            outdated_prerequisites(
                {"Git Bash": None, "ShellCheck": (0, 9, 0), "PowerShell 7 (pwsh)": (7, 5, 3), "ruff": (0, 16, 10)},
                (3, 11, 0),
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
    parser.add_argument(
        "-k",
        dest="patterns",
        action="append",
        default=[],
        metavar="PATTERN",
        help="run only the policy checks whose name, and the suites whose path, match; repeatable",
    )
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
            mode = (
                f"Documentation only: {reason} since {base}. Running the policy checks and the "
                f"{len(suites)} suites that name a changed file; pass --full to run every suite."
            )
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
