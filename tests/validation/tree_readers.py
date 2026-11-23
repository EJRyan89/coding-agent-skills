"""Every reader of a commit's tree is named here, with the bytes it promises.

The code-review snapshot and deploy.py --at write a commit's tree to disk with different bytes on purpose: the snapshot
lists the tree with git ls-tree and reads each blob with git cat-file, so it holds the committed bytes, while --at runs
git archive, which writes each file as a checkout would. The 2026-11-22 row on readers of a commit's tree in
docs/design.md's Decisions records why both stay. This policy names every place in the repository that runs git archive,
git ls-tree, or git cat-file, by its module and its scope, with the bytes it promises, and fails on a reader the
catalogue does not name, on one whose git words no longer give the bytes its entry promises, and on an entry that names
no reader, so a change to one reader is made with the other in view.

What counts as a reader's git words, in every Python file outside the suites, the root conftest.py, and tests/fixtures/:
the items of each list or tuple literal, and the positional arguments of each call whose name says git, as
ejryan89-skill-core's runtime_support.names_git reads it (`_git(source, "archive", ...)`, `GitClient().stream([...])`),
in order from the first string literal, with each other value standing for an unread word, and each call of a method
named file_at, GitClient's reader of a file's committed bytes; a first word that names the git program itself, as in
`["git", "archive", ...]`, is passed over, and git_client.subcommand reads past git's own options to the subcommand. The
command string of each call_tables PROCESS_STARTS runner that runs it through a shell, and each Bash or PowerShell
script outside the suites, are read by ejryan89-skill-core's git_subcommands, through skill_layout's
script_git_subcommands for a script.

Stated limits: a subcommand that only a value names, as `verb` in `git.output([verb, commit])`, is not read, and a git
cat-file mode that only a value names is read as one that gives the committed bytes. The words of a call whose name says
no git, as `runner(arguments)`, are read only where a list or tuple literal holds them, and a command a runner runs
through a shell only where it is one string literal, never a formatted string. A shell's -c script inside a list, and a
command in a Markdown fence, are not read. Other git commands that reach a tree's objects, such as read-tree into an
index or show given <commit>:<path>, are not catalogued: no repository code reads a file's bytes through them.
"""

from __future__ import annotations

import ast
import unittest
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

import git_client
import runtime_support
from bash_syntax import BashSyntaxError
from call_tables import PROCESS_STARTS
from exit_tables import NOT_SCANNED
from git_subcommands import UNREAD_PART, bash_git_subcommands, powershell_program
from path_containment import within
from skill_layout import UnreadScript, script_git_subcommands
from skill_scripts import powershell_script_commands
from validation_support import (
    MODULE_SCOPE,
    REPOSITORY_ROOT,
    CallableResolver,
    call_argument,
    is_test_script,
    python_trees,
    repository_files,
    scoped_nodes,
)

POLICY = "tests/validation/tree_readers.py"
DECISION = "the 2026-11-22 row on readers of a commit's tree in docs/design.md's Decisions"

# The bytes a reader of a commit's tree promises the code that calls it.
COMMITTED = "committed"
CHECKOUT = "checkout"
ENTRIES = "entries"
PROMISES = {
    COMMITTED: "each blob as the object store holds it, with no attribute, configuration, or filter applied",
    CHECKOUT: "each file as a checkout of the commit writes it, with line endings, ident, and filter drivers applied",
    ENTRIES: "the tree's entries, their paths, modes, object names, kinds, or sizes, and never a file's bytes",
}
# The git subcommands that read a commit's tree.
READER_SUBCOMMANDS = frozenset({"archive", "ls-tree", "cat-file"})
# The git cat-file modes that answer with an object's kind, size, or presence rather than its bytes, and those that
# apply the attributes and filters a checkout applies.
CAT_FILE_ENTRIES = frozenset({"-e", "-s", "-t"})
CAT_FILE_CONVERTS = frozenset({"--filters", "--textconv"})
CAT_FILE = "cat-file"
GIT = "git"
# GitClient's method that reads a file's committed bytes at a commit, whose every call is a reader of them.
FILE_AT = "file_at"
# The scripts besides Python that the policy reads through git_subcommands.
SHELL_SCRIPTS = frozenset({".sh", ".bash", ".ps1"})

# Each reader of a commit's tree, by its module's path from the repository root and its scope, as scoped_nodes names
# it, with the bytes it promises: one of PROMISES.
TREE_READERS: dict[str, dict[str, str]] = {
    ".claude/skills/audit-repository/scripts/audit_repository.py": {"tracked_files": ENTRIES},
    ".claude/skills/change-skill/scripts/change_skill.py": {"Tree.base": COMMITTED},
    # deploy.py --at: the tree a deployment from a checkout of the commit reads, and the paths whose attributes it
    # asks git archive about.
    "deployer/source_ref.py": {"export": CHECKOUT, "_tree_paths": ENTRIES},
    "skills/ejryan89-code-review-core/scripts/review_reviewers.py": {"repository_files": ENTRIES},
    # The code-review snapshot and the reviewer files read at a trusted commit: the bytes as committed.
    "skills/ejryan89-code-review-core/scripts/review_runtime.py": {
        "_tree_entries": ENTRIES,
        "_runner_blob_reader.read": COMMITTED,
        "git_blob_reader": COMMITTED,
        "git_blob_prefix": COMMITTED,
        "_commit_members": ENTRIES,
        "read_blob": COMMITTED,
        "_blob_binary_test": COMMITTED,
    },
    "skills/ejryan89-skill-core/scripts/git_client.py": {"GitClient.file_at": COMMITTED},
    "skills/review-document/scripts/review_document.py": {"committed_blob": ENTRIES, "committed_base": COMMITTED},
    "skills/review-insights/scripts/review_guidance.py": {"read_committed": COMMITTED},
    "tests/validation/silent_reverts.py": {"current_blobs": ENTRIES},
    "tests/validation/upgrade_notes.py": {"revision_snapshot": ENTRIES},
    "tools/mutation_gate.py": {"tree_files": ENTRIES},
    "tools/release_notes.py": {"_notes_at": ENTRIES},
}
# The places whose words spell a reader's subcommand but never reach git, by module and scope, with the reason.
NOT_READERS: dict[str, dict[str, str]] = {
    "tools/skill_evals_scenarios.py": {
        "SYNTHESIS_FOLDERS": "names the folders a synthesis scenario's fixture copies, archive and checkout, not git's "
        "words"
    },
}


def scanned_files(root: Path, files: Iterable[Path] | None = None) -> list[Path]:
    """The Python, Bash, and PowerShell files the policy reads, among the repository's files or the files given: every
    one outside the suites, the root conftest.py, and tests/fixtures/, in order of their path from the root."""
    listed = repository_files(root) if files is None else files
    return [
        path
        for path in sorted(listed, key=lambda each: each.relative_to(root).as_posix())
        if (path.suffix == ".py" or path.suffix in SHELL_SCRIPTS)
        and not is_test_script(path)
        and not within(path, root / NOT_SCANNED)
        and path.relative_to(root).as_posix() != "conftest.py"
    ]


def _words(values: Sequence[ast.expr]) -> list[str]:
    """The words a sequence of values gives git, from its first string literal on, each other value read as UNREAD_PART,
    with a first word that names the git program passed over."""
    words: list[str] = []
    for value in values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            words.append(value.value)
        elif words:
            words.append(UNREAD_PART)
    if words and powershell_program(words[0]) == GIT:
        words = words[1:]
    return words


def _reads(words: Sequence[str]) -> tuple[str, str] | None:
    """The tree-reading subcommand git words run and the bytes it gives, or None for words that run none."""
    index = git_client.subcommand_index(words)
    if index is None or words[index] not in READER_SUBCOMMANDS:
        return None
    subcommand, rest = words[index], words[index + 1 :]
    if subcommand == "archive" or CAT_FILE_CONVERTS & set(rest):
        return subcommand, CHECKOUT
    if subcommand == "ls-tree" or (rest[:1] and rest[0] in CAT_FILE_ENTRIES):
        return subcommand, ENTRIES
    return subcommand, COMMITTED


def _shell_command(call: ast.Call, resolver: CallableResolver) -> str | None:
    """The command string a call_tables runner runs through a shell, or None for any other call."""
    start = PROCESS_STARTS.get(resolver.name(call.func))
    if start is None:
        return None
    shell = next((keyword.value for keyword in call.keywords if keyword.arg == "shell"), None)
    shell_given = shell is not None and not (isinstance(shell, ast.Constant) and not shell.value)
    command = call_argument(call, *start.command)
    through_shell = start.through_shell or (start.popen_keywords and shell_given)
    if through_shell and isinstance(command, ast.Constant) and isinstance(command.value, str):
        return command.value
    return None


def python_tree_reads(tree: ast.Module) -> list[tuple[int, str, str, str]]:
    """Each read of a commit's tree a Python module's git words make, as (line, scope, subcommand, bytes), in the
    order a walk of the module meets them."""
    resolver = CallableResolver(tree)
    found: list[tuple[int, str, str, str]] = []
    for node, scope in scoped_nodes(tree.body):
        readings: list[tuple[str, str] | None] = []
        if isinstance(node, ast.List | ast.Tuple):
            readings.append(_reads(_words(node.elts)))
        elif isinstance(node, ast.Call):
            if runtime_support.names_git(node.func):
                readings.append(_reads(_words(node.args)))
            if isinstance(node.func, ast.Attribute) and node.func.attr == FILE_AT:
                readings.append((CAT_FILE, COMMITTED))
            command = _shell_command(node, resolver)
            if command is not None:
                readings += [
                    _reads([subcommand]) for subcommand in bash_git_subcommands(command, powershell_script_commands)
                ]
        found += [(getattr(node, "lineno", 0), scope, *reading) for reading in readings if reading is not None]
    return found


def _script_reads(path: Path) -> list[tuple[int, str, str, str]]:
    """Each read of a commit's tree a Bash or PowerShell script makes, with the script itself as its scope."""
    readings = [_reads([subcommand]) for subcommand in sorted(script_git_subcommands(path))]
    return [(1, MODULE_SCOPE, *reading) for reading in readings if reading is not None]


def _promise(reads: Iterable[tuple[str, str]]) -> str | None:
    """The bytes a scope's reads promise: a checkout's when any gives them, else the committed bytes when any gives
    those, else the entries; None when it gives both a checkout's and the committed bytes."""
    given = {given for _, given in reads}
    if {CHECKOUT, COMMITTED} <= given:
        return None
    return CHECKOUT if CHECKOUT in given else COMMITTED if COMMITTED in given else ENTRIES


def tree_reader_problems(
    root: Path,
    files: Iterable[Path] | None = None,
    readers: Mapping[str, Mapping[str, str]] = TREE_READERS,
    not_readers: Mapping[str, Mapping[str, str]] = NOT_READERS,
) -> list[str]:
    """Report each reader of a commit's tree that `readers` does not name, each one whose git words give other bytes
    than its entry promises, each scope that gives both a checkout's and the committed bytes, and each entry of
    `readers` or `not_readers` that names no reader, and each promise PROMISES does not name."""
    problems: list[str] = []
    found: dict[tuple[str, str], list[tuple[int, str, str]]] = defaultdict(list)
    paths = scanned_files(root, files)
    shell_scripts = [path for path in paths if path.suffix in SHELL_SCRIPTS]
    for path in shell_scripts:
        name = path.relative_to(root).as_posix()
        try:
            script_reads = _script_reads(path)
        except (UnreadScript, BashSyntaxError) as error:
            problems.append(f"{name}: cannot read the git it runs ({error})")
            continue
        for line, scope, subcommand, given in script_reads:
            found[(name, scope)].append((line, subcommand, given))
    python = [path for path in paths if path.suffix == ".py"]
    for _, name, tree in python_trees(python, root, problems):
        for line, scope, subcommand, given in python_tree_reads(tree):
            found[(name, scope)].append((line, subcommand, given))
    for (name, scope), reads in sorted(found.items(), key=lambda item: (item[0][0], item[1][0][0], item[0][1])):
        if scope in not_readers.get(name, {}):
            continue
        line, subcommand, _ = reads[0]
        listed = readers.get(name, {}).get(scope)
        promised = _promise((subcommand, given) for _, subcommand, given in reads)
        if promised is None:
            problems.append(
                f"{name}:{line} {scope} reads both a checkout's bytes and the committed bytes of a commit's tree; "
                f"split it into one reader for each, each named in {POLICY}'s TREE_READERS; see {DECISION}"
            )
        elif listed is None:
            problems.append(
                f"{name}:{line} {scope} reads a commit's tree with git {subcommand}, which TREE_READERS in {POLICY} "
                f"does not name; name it there with the bytes it promises ({', '.join(sorted(PROMISES))}), or in "
                f"NOT_READERS if the words never reach git; see {DECISION}"
            )
        elif listed != promised:
            problems.append(
                f"{name}:{line} {scope} reads {promised} bytes of a commit's tree, but TREE_READERS in {POLICY} "
                f"promises {listed}; keep the bytes its callers rely on, or change its entry with every caller in "
                f"view; see {DECISION}"
            )
    for catalogue, label in ((readers, "TREE_READERS"), (not_readers, "NOT_READERS")):
        problems += [
            f"{POLICY}: {label} names {name} {scope}, which reads no commit's tree; remove the entry"
            for name, scopes in sorted(catalogue.items())
            for scope in sorted(scopes)
            if (name, scope) not in found
        ]
    problems += [
        f"{POLICY}: TREE_READERS promises {promise!r} for {name} {scope}, which PROMISES does not name"
        for name, scopes in sorted(readers.items())
        for scope, promise in sorted(scopes.items())
        if promise not in PROMISES
    ]
    return problems


class TreeReaderPolicies(unittest.TestCase):
    def test_every_reader_of_a_commit_s_tree_is_named_with_the_bytes_it_promises(self) -> None:
        # The code-review snapshot promises the committed bytes and deploy.py --at a checkout's, on purpose; a reader
        # added or changed is checked against the others here.
        self.assertEqual([], tree_reader_problems(REPOSITORY_ROOT))
