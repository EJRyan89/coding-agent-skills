"""Which agent runtimes run a skill: the capabilities each runtime offers and the reader of a skill's declaration.

A skill's `deploy-meta/<name>.json` may declare `runtime_support`, one value per runtime in RUNTIMES: `"full"`, or
an object whose `level` is `partial` or `none`, whose `reason` says in one line what the user loses, and whose `needs`
names the capabilities from CAPABILITIES the runtime lacks for it. Repository validation derives each skill's needs
from its frontmatter, with those of each skill it invokes, and holds the declaration to them;
tools/skill_reference.py prints the matrix and tools/runtime_canary.py checks it against what each runtime ran. It
lives in ejryan89-skill-core because audit-skill's script reads a declaration and derives the needs the same way,
through frontmatter_needs, invoked_skills, and carried_needs, wherever the skill it audits comes from, and a skill's
script imports nothing from the deployer.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Collection
from dataclasses import dataclass, field

import git_client
import python_reader
from powershell_commands import UNREAD_WORD
from text_lines import text_lines

RUNTIMES = ("claude-code", "codex", "copilot-cli")
RUNTIME_TITLES = {"claude-code": "Claude Code", "codex": "Codex CLI", "copilot-cli": "Copilot CLI"}
CAPABILITIES = {
    "agent-delegation": "starting a subagent from the session",
    "workflow": "Claude Code's Workflow tool, which starts subagents from a script with a per-call effort",
    "fail-closed-agent-hooks": (
        "running a subagent under its agent definition's hooks, with a tool call blocked when a hook fails or times "
        "out, which the code-review reviewer's guard needs"
    ),
    "user-only-start": "starting a user-only skill in a headless session as well as an interactive one",
    "sandbox-network": (
        "network access for a skill's commands in the runtime's default sandbox, which gh and a git fetch need"
    ),
}
# The allowed-tools grant that shows a skill needs a capability; user-only-start follows from disable-model-invocation,
# and fail-closed-agent-hooks from an Agent grant in a skill that starts an agent whose definition declares hooks.
CAPABILITY_GRANTS = {"Agent": "agent-delegation", "Workflow": "workflow"}
# The tools a skill may declare that reach a service over the network, so a skill declaring one, required or
# optional, needs sandbox-network.
NETWORK_TOOLS = frozenset({"gh", "copilot"})
# The git subcommands that reach a remote. Every skill runs git, so git is never declared as a tool; a skill whose
# script runs one of these, a Bash or PowerShell script by git_subcommands or a Python script by
# python_git_subcommands, needs sandbox-network as a network tool's does.
NETWORK_GIT_SUBCOMMANDS = frozenset({"clone", "fetch", "ls-remote", "pull", "push"})
# A git or gh subcommand as a literal names one, which audit-skill's grant reader reads too. A reader records a git
# subcommand no literal names as the word that stands for it, such as Bash's `$sub` or PowerShell's UNREAD_WORD, which
# may be any subcommand, one of NETWORK_GIT_SUBCOMMANDS included, so a script that runs one needs sandbox-network too.
SUBCOMMAND_NAME = re.compile(r"[a-z][a-z0-9-]*")
# The GitClient methods that run git with the words their caller gives them, as their first argument, `arguments`. Its
# other methods name their own subcommand, so a literal one is given, such as the ref in `commit_of("HEAD")`, is no
# word of git's.
GIT_ARGUMENT_METHODS = frozenset({"names", "output", "quiet_output", "run", "stream"})
# What a skill loses where a runtime lacks the capability, until its author states it more precisely.
DEFAULT_REASONS = {
    "agent-delegation": "It starts subagents, which this runtime cannot.",
    "workflow": "It uses Claude Code's Workflow tool, which this runtime lacks.",
    "fail-closed-agent-hooks": (
        "This runtime lets a tool call through when a hook times out, so its subagent runs without its agent "
        "definition's guard and its prompt is its only boundary."
    ),
    "user-only-start": "A headless copilot -p session cannot start it; start it from an interactive session.",
    "sandbox-network": "Its network calls need network access, which this runtime's default sandbox turns off.",
}
# The section of a runtime's guide that says how to give a skill a capability the runtime withholds by default, as its
# document, its heading, and what it gives. Every reason for lacking that capability there carries guide_pointer's
# sentence rather than restating the settings, so a change to the guide leaves no copy behind; repository validation
# holds both the reasons and the heading.
CAPABILITY_GUIDES = {
    ("codex", "sandbox-network"): ("docs/codex-support.md", "Network access", "the ways to open the network"),
}
# Copilot CLI starts subagents, but its `copilot -p` neither expands /<skill> for a user-only skill nor lets the model
# start one (Copilot CLI 1.0.91; see docs/copilot-support.md). Workflow is Claude Code's alone. Codex and Copilot CLI
# run hooks, but Codex lets a tool call through when a hook fails or times out, and Copilot CLI when one times out,
# where Claude Code's agent definition blocks it, so a guard there holds only when it runs (the fail-closed decision in
# docs/design.md). Codex's default
# sandbox gives commands no network until a setting opens it ("Network access" in docs/codex-support.md).
RUNTIME_CAPABILITIES = {
    "claude-code": frozenset(
        {"agent-delegation", "workflow", "fail-closed-agent-hooks", "user-only-start", "sandbox-network"}
    ),
    "codex": frozenset({"agent-delegation", "user-only-start"}),
    "copilot-cli": frozenset({"agent-delegation", "sandbox-network"}),
}
FULL = "full"
PARTIAL = "partial"
NONE = "none"
SHAPE = (
    'runtime_support, where present, must name each of claude-code, codex, and copilot-cli once: either "full", '
    'or an object {"level": "partial" or "none", "needs": [capabilities], "reason": "<one line>"}, where a partial '
    "names at least one need. Known capabilities: " + ", ".join(CAPABILITIES) + '. See "Metadata" in '
    "docs/adding-a-skill.md."
)


class RuntimeSupportError(ValueError):
    """A runtime_support value that does not have the declared shape; the message says what is wrong."""


@dataclass(frozen=True)
class Support:
    level: str
    needs: tuple[str, ...] = field(default=())
    reason: str = ""


def _support(runtime: str, value: object) -> Support:
    if value == FULL:
        return Support(FULL)
    if not isinstance(value, dict) or set(value) - {"level", "needs", "reason"}:
        raise RuntimeSupportError(f'{runtime} must be "full" or an object with level, needs, and reason')
    level, needs, reason = value.get("level"), value.get("needs", []), value.get("reason")
    if level not in (PARTIAL, NONE):
        raise RuntimeSupportError(f"{runtime} has level {level!r}; an object's level is partial or none")
    if not isinstance(needs, list) or not all(isinstance(need, str) for need in needs):
        raise RuntimeSupportError(f"{runtime} needs must be a list of capability names")
    unknown = [need for need in needs if need not in CAPABILITIES]
    if unknown:
        raise RuntimeSupportError(f"{runtime} needs unknown capability '{unknown[0]}'")
    if len(set(needs)) != len(needs):
        raise RuntimeSupportError(f"{runtime} names a need twice")
    if level == PARTIAL and not needs:
        raise RuntimeSupportError(f"{runtime} is partial but names no need")
    if not isinstance(reason, str) or not reason.strip() or "\n" in reason or "\r" in reason:
        raise RuntimeSupportError(f"{runtime} is {level} without a one-line reason")
    return Support(level, tuple(needs), reason.strip())


def parse(value: object) -> dict[str, Support]:
    """Each runtime's support from a runtime_support value; raises RuntimeSupportError naming the first problem."""
    if not isinstance(value, dict):
        raise RuntimeSupportError("runtime_support must be an object keyed by runtime")
    unknown = sorted(set(value) - set(RUNTIMES))
    if unknown:
        raise RuntimeSupportError(f"runtime_support names unknown runtime '{unknown[0]}'")
    missing = [runtime for runtime in RUNTIMES if runtime not in value]
    if missing:
        raise RuntimeSupportError(f"runtime_support does not declare {missing[0]}")
    return {runtime: _support(runtime, value[runtime]) for runtime in RUNTIMES}


def frontmatter_needs(
    allowed_tools: list[str],
    user_only: bool,
    starts_hooked_agent: bool = False,
    tools: frozenset[str] = frozenset(),
    git_subcommands: frozenset[str] = frozenset(),
) -> set[str]:
    """The capabilities a skill's frontmatter, metadata, and scripts show it needs: its allowed-tools grants,
    user-only start, for a skill that grants Agent and starts an agent whose definition declares hooks, those hooks,
    and, for a skill that declares a network tool among its tools, required or optional, or whose scripts run a git
    subcommand that may reach a remote, as reaches_remote reads it, network access."""
    needs = {CAPABILITY_GRANTS[grant] for grant in allowed_tools if grant in CAPABILITY_GRANTS}
    if starts_hooked_agent and "agent-delegation" in needs:
        needs.add("fail-closed-agent-hooks")
    if tools & NETWORK_TOOLS or any(reaches_remote(subcommand) for subcommand in git_subcommands):
        needs.add("sandbox-network")
    return needs | {"user-only-start"} if user_only else needs


def reaches_remote(subcommand: str) -> bool:
    """Whether a git subcommand a script runs may reach a remote: one of NETWORK_GIT_SUBCOMMANDS, or one no literal
    names, which may be any of them."""
    return subcommand in NETWORK_GIT_SUBCOMMANDS or not SUBCOMMAND_NAME.fullmatch(subcommand)


def names_git(function: ast.expr) -> bool:
    """Whether a called function's dotted name has a part with the word git in it, such as services.git.run, git_in,
    or GitClient(runner).output, read through attributes, and through a call only to the GitClient it constructs: a
    method called on what any other call gives back, as `git_output(...).split` and `x.output(...).decode` are, is that
    value's, never a git call's. GitHub is a word of its own, so a GitHubClient or a github_client call is not one."""
    parts: list[str] = []
    while True:
        if isinstance(function, ast.Attribute):
            parts.append(function.attr)
            function = function.value
        elif isinstance(function, ast.Call) and _callable_name(function.func) == git_client.GitClient.__name__:
            function = function.func
        else:
            break
    if isinstance(function, ast.Name):
        parts.append(function.id)
    return any(_says_git(part) for part in parts)


def _says_git(part: str) -> bool:
    """Whether one part of a dotted name holds the word git, its words split at underscores and at each capital that
    follows a lowercase letter, GitHub left out."""
    words = re.split(r"_|(?<=[a-z])(?=[A-Z])", re.sub("github", "", part, flags=re.I))
    return any(word.casefold() == "git" for word in words)


def _callable_name(function: ast.expr) -> str | None:
    """The name a call's function is known by: a plain name's, or the last part of a dotted one."""
    if isinstance(function, ast.Name):
        return function.id
    return function.attr if isinstance(function, ast.Attribute) else None


@dataclass(frozen=True)
class ScriptCallables:
    """What starts_git needs to know of the script a call is in: `imported`, each name an import binds, and `quiet`,
    the callables it names that start no git and the modules whose callables start none, as script_callables finds
    them."""

    imported: frozenset[str] = frozenset()
    quiet: frozenset[str] = frozenset()


def starts_git(function: ast.expr, script: ScriptCallables) -> bool:
    """Whether a call of `function` starts git, as a script spells one: a method in GIT_ARGUMENT_METHODS called on
    what names_git reads as a git client, such as services.git.run or GitClient(runner).output, or a callable named for
    git itself, such as git_in or store.git, that the script's `quiet` does not name. A method of an imported module is
    that module's, quiet only when the module is, as git_client is, so a script's own quiet `git` never stands for
    `worktrees.git`. Any other method of a git client, such as commit_of or rev_parse_path, names its own subcommand,
    so no word given it is git's."""
    name = _callable_name(function)
    if name is None:
        return False
    if isinstance(function, ast.Attribute) and not _says_git(name):
        return name in GIT_ARGUMENT_METHODS and names_git(function.value)
    module = _module_of(function, script)
    quiet = name in script.quiet if module is None else module in script.quiet
    return not quiet and names_git(function)


def _module_of(function: ast.expr, script: ScriptCallables) -> str | None:
    """The name an import binds that `function` is a method of, such as a module's, or None for a method of anything
    else or a plain name."""
    if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
        return function.value.id if function.value.id in script.imported else None
    return None


def script_callables(tree: ast.Module) -> ScriptCallables:
    """The names a script's imports bind, and the callables it names that start no git, for starts_git.

    git_client's own module and every name a script imports from it are quiet, since its callables start git only as
    a GitClient's methods. Of the functions, methods, and classes the script defines, one starts git when a definition
    of its name holds a call that does: a method in GIT_ARGUMENT_METHODS on any receiver, as a wrapper's
    `self.client.output(...)` is, a callable named for git the script does not define, or a module's, as starts_git
    reads one, or another of the script's own that starts git. Every other one is quiet, a helper that only words
    git's failure, as `git_failure` does, and two that only call each other, as a reader's recursion does, among
    them. Beyond what it reads: a callable the script imports from any module but git_client is read by its name
    alone, so a definition whose only git runs through such an import, under a name with no git in it, is read as
    quiet.
    """
    imported: set[str] = set()
    quiet: set[str] = set()
    calls: dict[str, list[ast.Call]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = {alias.asname or alias.name.partition(".")[0] for alias in node.names}
            imported |= names
            if isinstance(node, ast.ImportFrom) and node.module == git_client.__name__:
                quiet |= names
            elif isinstance(node, ast.Import):
                quiet.update(alias.asname or alias.name for alias in node.names if alias.name == git_client.__name__)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            calls.setdefault(node.name, []).extend(call for call in ast.walk(node) if isinstance(call, ast.Call))
    known = ScriptCallables(frozenset(imported), frozenset(quiet))

    def own(call: ast.Call) -> str | None:
        name = _callable_name(call.func)
        return name if name in calls and _module_of(call.func, known) is None else None

    def direct(call: ast.Call) -> bool:
        return _callable_name(call.func) in GIT_ARGUMENT_METHODS or (own(call) is None and starts_git(call.func, known))

    starting: set[str] = set()
    while more := {
        name
        for name, made in calls.items()
        if name not in starting and any(direct(call) or own(call) in starting for call in made)
    }:
        starting |= more
    return ScriptCallables(known.imported, frozenset(quiet | (calls.keys() - starting)))


def python_git_subcommands(source: str) -> set[str]:
    """The subcommand each git call in a Python script names. A skill's Python runs git through git_client's GitClient,
    often behind a function of its own or a sibling module's, so the call is read by its name, not its import.

    A git call is one `starts_git` reads as one: a GitClient method that takes git's words, such as run or output,
    called on what names_git reads as a client, or a callable named for git, such as git_in, that is not one of the
    script's own that script_callables finds starts no git, such as a git_failure that only words an error, nor one of
    git_client's own, such as GitError. A method called on what a git call gives back, as `git_output(...).split`, is
    none. Its words are the string literals among its positional arguments and among the items of a list or tuple
    literal there, in order, and git_client.subcommand reads past git's own options to the subcommand. Any other value
    stands as a placeholder, powershell_commands' UNREAD_WORD, wherever it is an item of a list or tuple literal, among
    the other arguments after the first literal, and wherever it is starred, since a starred value carries git's words,
    as `*fetch` in `git_in(checkout, git, *fetch)` does. So a subcommand no literal names, as in
    `git.output(["-C", directory, verb])`, is recorded as that word, which may be any subcommand and which
    reaches_remote reads as one that may reach a remote. A value that is not starred among the arguments before the
    first literal is the function's own, such as its client and its folder in `git(services, root, "fetch")`, and
    stands for no word.

    Beyond what it reads: a call that holds neither a literal word, a list or tuple literal, nor a starred value, such
    as a wrapper's `services.git.run(arguments)`, records nothing, so a wrapper's subcommands are read only at its
    callers' calls that name git; a value that is not starred before the first literal among the arguments, as `verb`
    in `git_in(checkout, runner, verb, "origin")`, is taken as the function's own; and what a variable holds, such as
    the subcommand in `sizing = ("cat-file", "-s", blob)`, is not followed. A starred value of git's own options, as
    `*SHOW_UNTRACKED` before `"worktree"`, is read as the subcommand. A callable named for git that runs no git,
    imported from a module other than git_client and given a list or tuple literal or a starred value, records a git
    call all the same, since script_callables reads only the script's own definitions. A script that does not parse, or
    nests past the parser's limits, raises python_reader's PythonRefused.
    """
    tree = python_reader.parse(source)
    script = script_callables(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not starts_git(node.func, script):
            continue
        words: list[str] = []
        for argument in node.args:
            listed = argument.elts if isinstance(argument, (ast.List, ast.Tuple)) else None
            for item in [argument] if listed is None else listed:
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    words.append(item.value)
                elif listed is not None or words or isinstance(item, ast.Starred):
                    words.append(UNREAD_WORD)
        subcommand = git_client.subcommand(words)
        if subcommand is not None:
            found.add(subcommand)
    return found


def invoked_skills(text: str, names: Collection[str]) -> set[str]:
    """The skills a SKILL.md's text invokes: each of `names` that is the first word of a code span on a line that
    says "invoke", such as `review-prs` in "invoke `review-prs --re-review`". A line ends where CommonMark ends one,
    at CR, LF, or CRLF, so a line separator before the span keeps it on the line that says "invoke".
    tests/ai-config/test_cross_skill_contracts.py holds that no invoked skill is user-only."""
    span = re.compile(r"`([^`]+)`")
    return {
        words[0]
        for line in text_lines(text)
        if "invoke" in line
        for found in span.findall(line)
        if (words := found.split()) and words[0] in names
    }


def carried_needs(
    name: str,
    own_needs: Callable[[str], set[str]],
    invokes: Callable[[str], set[str]],
    seen: frozenset[str] = frozenset(),
) -> dict[str, set[str]]:
    """What each skill `name` invokes carries over to it: the invoked skill's own needs and what its own invoked skills
    carry, followed once each, without user-only-start, since starting an invoked skill is the invoker's step. A
    skill's needs are its own_needs and every set this returns for it; repository validation and audit-skill's script
    both derive them through it, each reading a skill's own needs and what it invokes from where the skill sits."""
    carried: dict[str, set[str]] = {}
    for other in sorted(invokes(name) - seen - {name}):
        needs = set(own_needs(other))
        for more in carried_needs(other, own_needs, invokes, seen | {name}).values():
            needs |= more
        carried[other] = needs - {"user-only-start"}
    return carried


def lacking(runtime: str, needs: set[str]) -> tuple[str, ...]:
    """The capabilities among `needs` that `runtime` does not offer, in catalogue order."""
    return tuple(capability for capability in CAPABILITIES if capability in needs - RUNTIME_CAPABILITIES[runtime])


def guide_pointer(runtime: str, capability: str) -> str | None:
    """The sentence a reason for lacking `capability` on `runtime` carries, or None when no guide says how to give
    it."""
    guide = CAPABILITY_GUIDES.get((runtime, capability))
    return None if guide is None else f'See "{guide[1]}" in {guide[0]} for {guide[2]}.'


def declaration(needs: set[str]) -> dict[str, object]:
    """The runtime_support value that follows from `needs`, with the default reason for each partial and the guide
    pointer for each capability a guide says how to give."""
    value: dict[str, object] = {}
    for runtime in RUNTIMES:
        missing = lacking(runtime, needs)
        sentences = [DEFAULT_REASONS[need] for need in missing]
        sentences += [pointer for need in missing if (pointer := guide_pointer(runtime, need))]
        value[runtime] = {"level": PARTIAL, "needs": list(missing), "reason": " ".join(sentences)} if missing else FULL
    return value
