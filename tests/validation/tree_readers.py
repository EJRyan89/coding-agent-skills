"""Every reader of a commit's tree is named here, with the bytes it promises.

The code-review snapshot and deploy.py --at write a commit's tree to disk with different bytes on purpose: the snapshot
lists the tree with git ls-tree and reads each blob with git cat-file, so it holds the committed bytes, while --at runs
git archive, which writes each file as a checkout would. The row on readers of a commit's tree in docs/design.md's
Decisions records why both stay. This policy names every place in the repository that runs git archive, ls-tree,
cat-file, show, or grep, by its module and its scope, with the bytes it promises, and fails on a reader the catalogue
does not name, on one whose git words no longer give the bytes its entry promises, and on an entry that names no reader,
so a change to one reader is made with the other in view. archive gives a checkout's bytes, and so does cat-file, show,
or grep given --filters or --textconv; ls-tree gives entries, and so does cat-file given -e, -s, -t, or --batch-check;
any other cat-file, show, or grep gives the committed bytes.

What counts as a reader's git words, in every Python file outside the suites, the root conftest.py, and tests/fixtures/:
the items of each list or tuple literal, and the positional arguments of each call whose name says git, as
ejryan89-skill-core's runtime_support.names_git reads it (`_git(source, "archive", ...)`, `GitClient().stream([...])`),
in order from the first string literal, with each other value standing for an unread word, and each call of a method
named file_at, GitClient's reader of a file's committed bytes; a first word that names the git program itself, as in
`["git", "archive", ...]`, is passed over, and git_client.subcommand reads past git's own options to the subcommand. The
command string of each call_tables PROCESS_STARTS runner that runs it through a shell, and each Bash or PowerShell
script outside the suites, are read by ejryan89-skill-core's git_subcommands, through skill_layout's
script_git_subcommands for a script.

Stated limits, each one a fixture in tests/validation/test_tree_readers.py shows:
- A subcommand that only a value names, as `verb` in `git.output([verb, commit])`, is not read.
- A cat-file mode that only a value names is read as one that gives the committed bytes.
- A call named for git whose words start with a string literal before the subcommand, as
  `git_output("/repo", "cat-file", ...)`, takes that string for the subcommand.
- The words of a call whose name says no git, as `runner(arguments)`, are read only where a list or tuple literal holds
  them, and a file_at reached through a name bound to the method, as `read = client.file_at`, is not read.
- A command given as one string to a runner without a shell, or split from one string by str.split or shlex.split, is
  not read, and a command a runner runs through a shell is read only where it is one string literal.
- A shell's -c script inside a list, and a command in a Markdown fence, are not read.
- A Bash or PowerShell script's git, and a command a runner runs through a shell, are read by their subcommand alone, so
  cat-file, show, or grep there given --filters, --textconv, -e, -s, -t, or --batch-check is read as giving the
  committed bytes.
- A git a shell function or array runs with the words it is given, as `g() { git -C x "$@"; }`,
  `cmd=(git archive HEAD); "${cmd[@]}"`, `"$GIT" archive`, or `& git @args`, is not read at its callers.
- A grep over the working tree, which no repository code runs, is read as one over a commit.
- A command that writes a commit's tree into a working tree, worktree add, checkout, switch, or restore, is not read: it
  writes what a checkout writes and hands its caller no bytes of its own, and a later read of that working tree reads
  files, not git.
- A list, a tuple, or a call's arguments whose words are exactly a NOT_READERS entry is passed over wherever it sits,
  so a literal `("archive", "checkout")` meant as `git archive checkout` would be excused anywhere.
- A commit's tree read through GitHub's API, as the tarball snapshot and the contents, blobs, and trees calls do, is not
  read.
"""

from __future__ import annotations

import ast
import unittest
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

import git_client
import runtime_support
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
DECISION = "the row on readers of a commit's tree in docs/design.md's Decisions"

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
READER_SUBCOMMANDS = frozenset({"archive", "ls-tree", "cat-file", "show", "grep"})
# The cat-file options that answer with an object's kind, size, or presence rather than its bytes, and the options of
# cat-file, show, and grep that apply the attributes and filters a checkout applies; an option counts alone or with a
# value after =, as --batch-check=%(objectsize) does.
CAT_FILE_ENTRIES = frozenset({"-e", "-s", "-t", "--batch-check"})
CONVERTING_OPTIONS = frozenset({"--filters", "--textconv"})
CAT_FILE = "cat-file"
GIT = "git"
# GitClient's method that reads a file's committed bytes at a commit, whose every call is a reader of them.
FILE_AT = "file_at"
# The scripts besides Python that the policy reads through git_subcommands.
SHELL_SCRIPTS = frozenset({".sh", ".bash", ".ps1"})

# Each reader of a commit's tree, by its module's path from the repository root and its scope, as scoped_nodes names
# it, with the bytes it promises: one of PROMISES.
TREE_READERS: dict[str, dict[str, str]] = {
    ".claude/skills/audit-repository/scripts/audit_repository.py": {
        "tracked_files": ENTRIES,
        "search_construct": COMMITTED,
    },
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
        "search_source": COMMITTED,
    },
    "skills/ejryan89-skill-core/scripts/git_client.py": {"GitClient.file_at": COMMITTED},
    "skills/review-document/scripts/review_document.py": {"committed_blob": ENTRIES, "committed_base": COMMITTED},
    "skills/review-insights/scripts/review_guidance.py": {"read_committed": COMMITTED},
    "tests/validation/silent_reverts.py": {"current_blobs": ENTRIES},
    "tests/validation/upgrade_notes.py": {"revision_snapshot": COMMITTED},
    "tools/mutation_gate.py": {"tree_files": ENTRIES},
    "tools/release_notes.py": {"_notes_at": COMMITTED},
}
# The words that spell a reader's subcommand but never reach git, with the reason: a list, a tuple, or a call's
# positional arguments whose items are all string literals, exactly these words in this order, is passed over wherever
# it sits.
NOT_READERS: dict[tuple[str, ...], str] = {
    ("archive", "checkout"): "the folders a synthesis scenario's fixture copies, which a skill evaluation names",
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
    subcommand = words[index]
    options = {word.partition("=")[0] for word in words[index + 1 :]}
    if subcommand == "archive" or CONVERTING_OPTIONS & options:
        return subcommand, CHECKOUT
    if subcommand == "ls-tree" or (subcommand == CAT_FILE and CAT_FILE_ENTRIES & options):
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


def _excused(
    values: Sequence[ast.expr], not_readers: Mapping[tuple[str, ...], str], used: set[tuple[str, ...]]
) -> bool:
    """Whether a sequence of values is exactly the words of a NOT_READERS entry, every one a string literal, the same
    in number and order; an entry it matches is added to `used`."""
    words = tuple(value.value for value in values if isinstance(value, ast.Constant) and isinstance(value.value, str))
    if len(words) != len(values) or words not in not_readers:
        return False
    used.add(words)
    return True


def python_tree_reads(
    tree: ast.Module,
    not_readers: Mapping[tuple[str, ...], str] = NOT_READERS,
    used: set[tuple[str, ...]] | None = None,
) -> list[tuple[int, str, str, str]]:
    """Each read of a commit's tree a Python module's git words make, as (line, scope, subcommand, bytes), in the
    order a walk of the module meets them, past each sequence that is exactly the words of a `not_readers` entry, which
    is added to `used`."""
    resolver = CallableResolver(tree)
    excusing: set[tuple[str, ...]] = set() if used is None else used
    found: list[tuple[int, str, str, str]] = []
    for node, scope in scoped_nodes(tree.body):
        readings: list[tuple[str, str] | None] = []
        if isinstance(node, ast.List | ast.Tuple):
            if not _excused(node.elts, not_readers, excusing):
                readings.append(_reads(_words(node.elts)))
        elif isinstance(node, ast.Call):
            if runtime_support.names_git(node.func) and not _excused(node.args, not_readers, excusing):
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
    not_readers: Mapping[tuple[str, ...], str] = NOT_READERS,
) -> list[str]:
    """Report each reader of a commit's tree that `readers` does not name, each one whose git words give other bytes
    than its entry promises, each scope that gives both a checkout's and the committed bytes, each entry of `readers`
    that names no reader, each entry of `not_readers` that no sequence spells or that gives no reason, and each promise
    PROMISES does not name."""
    problems: list[str] = []
    found: dict[tuple[str, str], list[tuple[int, str, str]]] = defaultdict(list)
    paths = scanned_files(root, files)
    shell_scripts = [path for path in paths if path.suffix in SHELL_SCRIPTS]
    for path in shell_scripts:
        name = path.relative_to(root).as_posix()
        try:
            script_reads = _script_reads(path)
        except UnreadScript as error:
            problems.append(f"{name}: cannot read the git it runs: {error.reason}")
            continue
        for line, scope, subcommand, given in script_reads:
            found[(name, scope)].append((line, subcommand, given))
    python = [path for path in paths if path.suffix == ".py"]
    used: set[tuple[str, ...]] = set()
    for _, name, tree in python_trees(python, root, problems):
        for line, scope, subcommand, given in python_tree_reads(tree, not_readers, used):
            found[(name, scope)].append((line, subcommand, given))
    for (name, scope), reads in sorted(found.items(), key=lambda item: (item[0][0], item[1][0][0], item[0][1])):
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
    problems += [
        f"{POLICY}: TREE_READERS names {name} {scope}, which reads no commit's tree; remove the entry"
        for name, scopes in sorted(readers.items())
        for scope in sorted(scopes)
        if (name, scope) not in found
    ]
    problems += [
        f"{POLICY}: NOT_READERS names the words {words!r}, which no sequence spells; remove the entry"
        for words in sorted(not_readers)
        if words not in used
    ]
    problems += [
        f"{POLICY}: NOT_READERS gives the words {words!r} no reason; state why they never reach git"
        for words, reason in sorted(not_readers.items())
        if not reason.strip()
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
